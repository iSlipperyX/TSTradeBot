"""Control the bot from Telegram.

Telegram runs in the controller process (controller.py), not inside the trading bot, so it keeps
working when the bot is stopped or has crashed - you can see why and start/restart it remotely.
It long-polls the Telegram Bot API from your PC (no webhook, no open ports), so every order is
still placed on your own computer, as Topstep requires.

Security:
  * Only messages from TELEGRAM_CHAT_ID are obeyed (optionally narrowed to specific user IDs
    with ``telegram.allowed_user_ids``); everything else is ignored.
  * Commands sent while the bot was offline are discarded at startup, so an old /flatten
    can't fire unexpectedly.
  * Anything that trades, stops or changes settings needs a confirmation tap within 60 seconds.
  * Settings stay within safe bounds; mode (paper/live), account and Topstep rules can't be changed here.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import time
from typing import Any

import httpx

from topstep_bot.config import TelegramConfig
from topstep_bot.notify import redact

log = logging.getLogger(__name__)

COMMANDS = [
    ("status", "Account, position and risk status"),
    ("pause", "Stop opening new trades"),
    ("resume", "Allow new trades again"),
    ("flatten", "Close everything and halt trading"),
    ("restart", "Restart the bot (flattens first)"),
    ("startbot", "Start the bot if it is stopped"),
    ("stop", "Stop the bot (Telegram and the dashboard stay online)"),
    ("ideas", "Recommended trades - tap Take to trade one"),
    ("knowledge", "What the bot has learned: which strategy works when"),
    ("train", "Retrain the knowledge base on recent history now"),
    ("settings", "Show the settings you can change"),
    ("set", "Change a setting, e.g. /set risk 150"),
    ("reset", "Undo all setting changes made remotely"),
    ("trades", "Recent closed trades"),
    ("log", "Recent bot activity"),
    ("help", "Show the commands"),
]
DANGEROUS = {"flatten", "stop", "restart", "startbot"}
CONFIRM_SECONDS = 60
MAX_TEXT = 4000

KEYBOARD = {
    "inline_keyboard": [
        [{"text": "📊 Status", "callback_data": "cmd:status"}, {"text": "⏸ Pause", "callback_data": "cmd:pause"},
         {"text": "▶️ Resume", "callback_data": "cmd:resume"}],
        [{"text": "💡 Ideas", "callback_data": "cmd:ideas"}, {"text": "📜 Trades", "callback_data": "cmd:trades"},
         {"text": "🧠 Knowledge", "callback_data": "cmd:knowledge"}, {"text": "⚙️ Settings", "callback_data": "cmd:settings"}],
        [{"text": "🛑 Flatten & halt", "callback_data": "cmd:flatten"}, {"text": "🔄 Restart bot", "callback_data": "cmd:restart"},
         {"text": "▶️ Start bot", "callback_data": "cmd:startbot"}],
    ]
}


class TelegramError(Exception):
    def __init__(self, code: int | None, description: str):
        super().__init__(f"Telegram error {code}: {description}")
        self.code = code


def help_text() -> str:
    lines = ["Topstep Bot commands:"] + [f"/{name} - {desc}" for name, desc in COMMANDS]
    lines.append("\nExamples: /set risk 150 - /set dailyloss 400 - /set strategy orb - /set news off")
    lines.append("Trading, stopping/starting and setting changes all ask for confirmation. "
                 "Telegram keeps working while the bot is stopped - use /startbot or /restart.")
    return "\n".join(lines)


class TelegramController:
    def __init__(
        self,
        token: str,
        chat_id: str | int,
        actions: Any,
        cfg: TelegramConfig,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        api_base: str = "https://api.telegram.org",
    ):
        self.chat_id = str(chat_id)
        self.actions = actions
        self.cfg = cfg
        self._client = httpx.AsyncClient(base_url=f"{api_base}/bot{token}/", timeout=40, transport=transport)
        self.offset: int | None = None
        self.pending: dict[int, tuple[str, float]] = {}  # confirmation message id -> (action, created)
        self._warned_chats: set[str] = set()
        self._background: set[asyncio.Task] = set()  # slow commands, run beside the polling loop

    # ---------------------------------------------------------------- API

    async def _call(self, method: str, **params: Any) -> Any:
        resp = await self._client.post(method, json=params)
        try:
            data = resp.json()
        except ValueError:
            raise TelegramError(resp.status_code, resp.text[:200]) from None
        if not data.get("ok"):
            raise TelegramError(data.get("error_code", resp.status_code), data.get("description", "unknown error"))
        return data.get("result")

    async def send(self, text: str, keyboard: dict | None = None) -> int | None:
        params: dict[str, Any] = {"chat_id": self.chat_id, "text": text[:MAX_TEXT]}
        if keyboard:
            params["reply_markup"] = keyboard
        try:
            msg = await self._call("sendMessage", **params)
            return msg.get("message_id") if isinstance(msg, dict) else None
        except (TelegramError, httpx.HTTPError) as exc:
            log.warning("Telegram send failed: %s", redact(exc))
            return None

    async def _edit(self, message_id: int, text: str) -> None:
        try:
            await self._call("editMessageText", chat_id=self.chat_id, message_id=message_id, text=text[:MAX_TEXT])
        except (TelegramError, httpx.HTTPError) as exc:
            log.debug("Telegram edit failed: %s", redact(exc))

    # ------------------------------------------------------------ lifecycle

    async def start(self, announce: bool = True) -> None:
        """Validate the token, register the command menu and discard any backlog of old commands."""
        await self._call("getMe")
        await self._call("setMyCommands", commands=[{"command": c, "description": d} for c, d in COMMANDS])
        backlog = await self._call("getUpdates", offset=-1, timeout=0)
        if backlog:
            self.offset = backlog[-1]["update_id"] + 1
            await self._call("getUpdates", offset=self.offset, timeout=0)  # confirm (drop) them
        if announce:
            await self.send("🤖 Topstep Bot is online and listening for commands.\n\n" + help_text(), KEYBOARD)

    async def close(self) -> None:
        for task in list(self._background):
            task.cancel()
        await self.idle()
        await self._client.aclose()

    async def idle(self) -> None:
        """Wait until slow commands (like /train) have answered."""
        while self._background:
            await asyncio.wait(list(self._background))

    def _in_background(self, coro) -> None:
        """Run a slow command without blocking the polling loop, so /flatten or /stop still work meanwhile."""
        task = asyncio.create_task(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def run(self) -> None:
        backoff = 1.0
        while True:
            try:
                params: dict[str, Any] = {"timeout": 25, "allowed_updates": ["message", "callback_query"]}
                if self.offset is not None:
                    params["offset"] = self.offset
                updates = await self._call("getUpdates", **params)
                backoff = 1.0
                for upd in updates or []:
                    self.offset = upd["update_id"] + 1
                    try:
                        await self.handle_update(upd)
                    except Exception:  # noqa: BLE001 - one bad update must not stop control
                        log.exception("Telegram update failed")
            except TelegramError as exc:
                if exc.code == 401:
                    log.error("Telegram control disabled: the bot token was rejected (check TELEGRAM_BOT_TOKEN)")
                    return
                if exc.code == 409:
                    log.warning("Telegram: another program (or a webhook) is using this bot token; retrying in 30s")
                    await asyncio.sleep(30)
                else:
                    log.warning("Telegram polling error: %s", exc)
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 60)
            except httpx.HTTPError as exc:
                log.debug("Telegram network error: %s", redact(exc))
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)
            except Exception:  # noqa: BLE001 - never let Telegram control die silently: keep polling
                log.exception("Telegram polling failed unexpectedly; retrying in %.0fs", backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    # --------------------------------------------------------------- updates

    def _authorized(self, chat_id: Any, user_id: Any) -> bool:
        if str(chat_id) != self.chat_id:
            return False
        allowed = self.cfg.allowed_user_ids
        return not allowed or (user_id is not None and int(user_id) in allowed)

    def _reject(self, chat_id: Any, user_id: Any) -> None:
        key = f"{chat_id}/{user_id}"
        if key not in self._warned_chats:
            self._warned_chats.add(key)
            log.warning("Ignored Telegram command from unauthorized chat %s / user %s", chat_id, user_id)

    async def handle_update(self, upd: dict) -> None:
        if "callback_query" in upd:
            await self._handle_callback(upd["callback_query"])
            return
        msg = upd.get("message")
        if not msg or not isinstance(msg.get("text"), str):
            return
        chat_id = msg.get("chat", {}).get("id")
        user = msg.get("from", {})
        if not self._authorized(chat_id, user.get("id")):
            self._reject(chat_id, user.get("id"))
            return
        text = msg["text"].strip()
        if not text.startswith("/"):
            await self.send("Send /help to see the commands.", KEYBOARD)
            return
        parts = text[1:].split()
        command = parts[0].split("@")[0].lower() if parts else "help"
        await self.dispatch(command, self._who(user), parts[1:])

    async def _handle_callback(self, cq: dict) -> None:
        message = cq.get("message") or {}
        chat_id = message.get("chat", {}).get("id")
        user = cq.get("from", {})
        with contextlib.suppress(TelegramError, httpx.HTTPError):
            await self._call("answerCallbackQuery", callback_query_id=cq["id"])
        if not self._authorized(chat_id, user.get("id")):
            self._reject(chat_id, user.get("id"))
            return
        data = str(cq.get("data", ""))
        message_id = message.get("message_id")
        if data.startswith("cmd:"):
            await self.dispatch(data[4:], self._who(user))
        elif data.startswith(("take:", "takeh:")):
            await self._confirm_take(data.split(":", 1)[1], half=data.startswith("takeh:"))
        elif data.startswith("yes:"):
            action = data[4:]
            pending = self.pending.pop(message_id, None)
            if pending is None or pending[0] != action or time.monotonic() - pending[1] > CONFIRM_SECONDS:
                await self._edit(message_id, "That confirmation expired - send the command again.")
                return
            payload = pending[2] if len(pending) > 2 else None
            try:
                result = await self._execute_confirmed(action, payload, self._who(user))
            except (ValueError, RuntimeError) as exc:
                await self._edit(message_id, f"❌ {exc}")
                return
            await self._edit(message_id, f"✅ {result}")
        elif data == "no":
            self.pending.pop(message_id, None)
            await self._edit(message_id, "Cancelled - nothing was changed.")

    @staticmethod
    def _who(user: dict) -> str:
        name = user.get("username") or user.get("first_name") or user.get("id")
        return f"Telegram ({name})"

    async def _do(self, name: str, *args: Any) -> Any:
        """Call an action (in-process BotActions, or the controller's async proxy)."""
        result = getattr(self.actions, name)(*args)
        return await result if inspect.isawaitable(result) else result

    async def _execute(self, action: str, source: str) -> str:
        method = {"pause": "pause", "resume": "resume", "flatten": "flatten", "stop": "stop",
                  "restart": "restart_bot", "startbot": "start_bot"}[action]
        if not hasattr(self.actions, method):
            raise RuntimeError(f"/{action} is only available when the controller is running")
        return await self._do(method, source)

    async def _execute_confirmed(self, action: str, payload, source: str) -> str:
        if action == "set":
            key, value = payload
            return await self._do("change_setting", key, value, source)
        if action == "reset":
            return await self._do("reset_settings", source)
        if action == "take":
            rec_id, size = payload
            return await self._do("take_idea", rec_id, source, size)
        return await self._execute(action, source)

    async def _ask(self, question: str, action: str, payload=None, yes: str | None = None) -> None:
        confirm = {"inline_keyboard": [[{"text": yes or f"Yes, {action}", "callback_data": f"yes:{action}"},
                                        {"text": "Cancel", "callback_data": "no"}]]}
        message_id = await self.send(question, confirm)
        if message_id is not None:
            self.pending[message_id] = (action, time.monotonic(), payload)

    async def _confirm_take(self, rec_id: str, half: bool) -> None:
        idea = await self._do("find_idea", rec_id)
        if idea is None or not idea["size"]:
            await self.send("That idea can't be taken (not found, or no valid size).")
            return
        size = max(1, idea["size"] // 2) if half else idea["size"]
        target = f", target {idea['target']}" if idea["target"] is not None else ""
        await self._ask(
            f"Take {idea['id']} ({idea['title']}): {idea['side']} {size} near {idea['entry']}, stop {idea['stop']}{target}?\n"
            f"It is re-priced at the current market and sized by your risk rules (never larger). "
            f"Idea: {idea['reason']}",
            "take", (rec_id, size), yes=f"Yes, take {size}",
        )

    async def _send_ideas(self) -> None:
        ideas = await self._do("open_ideas")
        rows = [[{"text": f"Take {i['id']} ({i['side']} {i['size']})", "callback_data": f"take:{i['id']}"},
                 {"text": "½ size", "callback_data": f"takeh:{i['id']}"}] for i in ideas]
        await self.send(await self._do("ideas_text"), {"inline_keyboard": rows} if rows else KEYBOARD)

    QUESTIONS = {
        "flatten": "⚠️ Close any open position, cancel all orders and HALT trading until the bot is restarted?",
        "stop": "⚠️ Stop the bot? It flattens first. Telegram and the dashboard stay online, so you can start it "
                "again with /startbot. (To just stop new trades, use /pause.)",
        "restart": "🔄 Restart the bot? It flattens any open position first, then starts again.",
        "startbot": "▶️ Start the bot? It will trade automatically in its current mode.",
    }

    async def _train(self, source: str) -> None:
        try:
            await self.send(await self._do("train", source))
        except (ValueError, RuntimeError) as exc:
            await self.send(f"❌ {exc}")
        except Exception:  # noqa: BLE001 - runs outside the polling loop's own error handling
            log.exception("Telegram /train failed")
            await self.send("❌ Training failed - see the Logs tab.")

    async def dispatch(self, command: str, source: str, args: list[str] | None = None) -> None:
        args = args or []
        try:
            await self._dispatch(command, source, args)
        except (ValueError, RuntimeError) as exc:
            await self.send(f"❌ {exc}")

    async def _dispatch(self, command: str, source: str, args: list[str]) -> None:
        if command in ("status", "start"):
            await self.send(await self._do("status_text"), KEYBOARD)
        elif command in ("pause", "resume"):
            await self.send(await self._execute(command, source), KEYBOARD)
        elif command in DANGEROUS:
            if not self.cfg.confirm_dangerous:
                await self.send(await self._execute(command, source))
                return
            await self._ask(self.QUESTIONS[command], command)
        elif command == "trades":
            await self.send(await self._do("trades_text"))
        elif command == "ideas":
            await self._send_ideas()
        elif command == "settings":
            await self.send(await self._do("settings_text"))
        elif command == "knowledge":
            await self.send(await self._do("knowledge_text"))
        elif command == "train":
            await self.send("🧠 Training on recent history - this takes a few seconds...")
            self._in_background(self._train(source))
        elif command == "set":
            if len(args) < 2:
                await self.send("Usage: /set <setting> <value>, e.g. /set risk 150. Send /settings for the list.")
                return
            p = await self._do("preview_setting", args[0], " ".join(args[1:]))
            if not p["changed"]:
                await self.send(f"{p['label']} is already {p['new']}.")
                return
            warn = "\n⚠️ This increases your risk." if p["riskier"] else ""
            await self._ask(f"Change {p['label']}: {p['old']} -> {p['new']}?{warn}", "set", (p["key"], " ".join(args[1:])),
                            yes="Yes, change it")
        elif command == "reset":
            await self._ask("Undo every setting changed from the dashboard/Telegram and go back to config.yaml?", "reset",
                            yes="Yes, reset")
        elif command == "log":
            await self.send(await self._do("log_text"))
        else:
            await self.send(help_text(), KEYBOARD)
