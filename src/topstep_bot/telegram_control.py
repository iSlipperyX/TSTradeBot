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
  * /dashboard opens the dashboard inside Telegram once phone access is on (see phone_access.py);
    turning phone access on from here needs a confirmation tap.

Several accounts: name the account after the command (/pause c1, /status c2, /set c1 risk 150) or
use "all" (/pause all, /flatten all). Without a name the bot shows one button per account.
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
    ("dashboard", "Open the dashboard on your phone, inside Telegram"),
    ("accounts", "All your accounts at a glance"),
    ("pause", "Stop opening new trades"),
    ("resume", "Allow new trades again"),
    ("flatten", "Close everything and halt trading"),
    ("restart", "Restart the bot (flattens first)"),
    ("startbot", "Start the bot if it is stopped"),
    ("stop", "Stop the bot (Telegram and the dashboard stay online)"),
    ("ideas", "Recommended trades - tap Take to trade one"),
    ("ask", "Ask the bot anything, e.g. /ask why the last trade? (or just type)"),
    ("brief", "What the bot knows right now, in plain words"),
    ("next", "When the bot expects its next trade, and why"),
    ("market", "Market brief: key levels, today's reports, headlines"),
    ("funded", "Progress to your Combine pass or next payout"),
    ("research", "What the bot has gathered from the internet"),
    ("lab", "Simulation Lab: every market and strategy as fresh Combines, plus opportunities"),
    ("switch", "Trade another market for the rest of today, e.g. /switch MES (or /switch usual)"),
    ("coach", "AI coach: latest lessons, proposals and spending"),
    ("rules", "Topstep rules the bot knows and follows"),
    ("knowledge", "What the bot has learned: which strategy works when"),
    ("train", "Retrain the knowledge base on recent history now"),
    ("learn", "Feed the bot more history: backfill and replay all of it"),
    ("settings", "Show the settings you can change"),
    ("set", "Change a setting, e.g. /set risk 150"),
    ("reset", "Undo all setting changes made remotely"),
    ("trades", "Recent closed trades"),
    ("log", "Recent bot activity"),
    ("update", "Check GitHub for a newer version of the bot and install it"),
    ("help", "Show the commands"),
]
DANGEROUS = {"flatten", "stop", "restart", "startbot"}
CONTROL = {"pause", "resume", *DANGEROUS}  # may target one account or all
# One account at a time. /rules, /update and /dashboard aren't about one account's bot.
PER_ACCOUNT = {"ideas", "trades", "settings", "knowledge", "train", "log", "brief", "next", "learn", "market", "funded",
               "switch"}
CONFIRM_SECONDS = 60
MAX_TEXT = 4000

KEYBOARD = {
    "inline_keyboard": [
        [{"text": "📊 Status", "callback_data": "cmd:status"}, {"text": "⏸ Pause", "callback_data": "cmd:pause"},
         {"text": "▶️ Resume", "callback_data": "cmd:resume"}, {"text": "📱 Dashboard", "callback_data": "cmd:dashboard"}],
        [{"text": "📈 Market", "callback_data": "cmd:market"}, {"text": "🏁 Funded", "callback_data": "cmd:funded"},
         {"text": "📏 Rules", "callback_data": "cmd:rules"}],
        [{"text": "💡 Ideas", "callback_data": "cmd:ideas"}, {"text": "📜 Trades", "callback_data": "cmd:trades"},
         {"text": "🧠 Knowledge", "callback_data": "cmd:knowledge"}, {"text": "⚙️ Settings", "callback_data": "cmd:settings"}],
        [{"text": "💬 What I know", "callback_data": "cmd:brief"}, {"text": "⏳ Next trade", "callback_data": "cmd:next"}],
        [{"text": "🛑 Flatten & halt", "callback_data": "cmd:flatten"}, {"text": "🔄 Restart bot", "callback_data": "cmd:restart"},
         {"text": "▶️ Start bot", "callback_data": "cmd:startbot"}],
    ]
}


UPDATE_ACTIONS = ("update", "update_tonight")  # one confirmation message offers both


def _answers(asked: str, tapped: str) -> bool:
    """Does the tapped button answer the question that was asked?"""
    return tapped == asked or (asked == "update" and tapped in UPDATE_ACTIONS)


class TelegramError(Exception):
    def __init__(self, code: int | None, description: str):
        super().__init__(f"Telegram error {code}: {description}")
        self.code = code


def help_text(accounts: list[str] | None = None) -> str:
    lines = ["Topstep Bot commands:"] + [f"/{name} - {desc}" for name, desc in COMMANDS]
    lines.append("\nExamples: /set risk 150 - /set dailyloss 400 - /set strategy orb - /set news off")
    if accounts:
        first = accounts[0]
        lines.append(f"\nAccounts: {', '.join(accounts)}. Put the account after the command: /status {first}, "
                     f"/pause {first}, /pause all, /set {first} risk 150, /set all news off. "
                     "Without one you get a button per account.")
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

    async def send(self, text: str, keyboard: dict | None = None, silent: bool = False) -> int | None:
        params: dict[str, Any] = {"chat_id": self.chat_id, "text": text[:MAX_TEXT]}
        if keyboard:
            params["reply_markup"] = keyboard
        if silent:
            params["disable_notification"] = True
        try:
            msg = await self._call("sendMessage", **params)
            return msg.get("message_id") if isinstance(msg, dict) else None
        except (TelegramError, httpx.HTTPError) as exc:
            log.warning("Telegram send failed: %s", redact(exc))
            return None

    async def set_dashboard_button(self, url: str | None) -> None:
        """The button beside the message box: opens the dashboard while phone access is on, else the command list."""
        button = {"type": "web_app", "text": "Dashboard", "web_app": {"url": url}} if url else {"type": "commands"}
        params: dict[str, Any] = {"menu_button": button}
        with contextlib.suppress(ValueError):
            params["chat_id"] = int(self.chat_id)
        try:
            await self._call("setChatMenuButton", **params)
        except (TelegramError, httpx.HTTPError) as exc:
            log.debug("Telegram menu button not changed: %s", redact(exc))

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
            await self.send("🤖 Topstep Bot is online and listening for commands.\n\n" + help_text(self._accounts()), KEYBOARD)

    async def close(self) -> None:
        for task in list(self._background):
            task.cancel()
        await self.idle()
        await self._client.aclose()

    async def idle(self) -> None:
        """Wait until slow commands (like /train) have answered."""
        while self._background:
            await asyncio.wait(list(self._background))

    async def _answer(self, question: str, account: str | None = None) -> None:
        """The central intelligence's answer to a typed question (it may take a few seconds with AI answers)."""
        if account is None:
            accounts = self._accounts()
            first, _, rest = question.partition(" ")
            if accounts and first.lower() in accounts and rest:
                account, question = first.lower(), rest
        with contextlib.suppress(TelegramError, httpx.HTTPError):
            await self._call("sendChatAction", chat_id=self.chat_id, action="typing")
        try:
            await self.send(await self._do("ask", question, account=account))
        except (ValueError, RuntimeError) as exc:
            await self.send(f"❌ {exc}")
        except Exception:  # noqa: BLE001 - a question must never stop Telegram control
            log.exception("Telegram: answering a question failed")
            await self.send("❌ I couldn't answer that - see logs/errors.log.")

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
            if hasattr(self.actions, "ask"):  # a question for the central intelligence
                self._in_background(self._answer(text))
            else:
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
        elif data.startswith("pick:"):
            _, command, account = data.split(":", 2)
            await self.dispatch(command, self._who(user), [account])
        elif data.startswith(("take:", "takeh:")):
            parts = data.split(":")
            account = parts[1] if len(parts) == 3 else None
            await self._confirm_take(parts[-1], half=data.startswith("takeh:"), account=account)
        elif data.startswith("yes:"):
            action = data[4:]
            pending = self.pending.pop(message_id, None)
            if pending is None or not _answers(pending[0], action) or time.monotonic() - pending[1] > CONFIRM_SECONDS:
                await self._edit(message_id, "That confirmation expired - send the command again.")
                return
            payload = pending[2] if len(pending) > 2 else None
            if action in UPDATE_ACTIONS:
                await self._edit(message_id, "⏳ Installing the update - this can take a minute...")
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

    def _accounts(self) -> list[str]:
        """Account names when there are several (empty with one account)."""
        names = getattr(self.actions, "account_names", None)
        return list(names()) if names else []

    async def _do(self, name: str, *args: Any, account: str | None = None) -> Any:
        """Call an action (in-process BotActions, or the controller's async proxy)."""
        kwargs = {"account": account} if account is not None else {}
        result = getattr(self.actions, name)(*args, **kwargs)
        return await result if inspect.isawaitable(result) else result

    async def _execute(self, action: str, source: str, account: str | None = None) -> str:
        method = {"pause": "pause", "resume": "resume", "flatten": "flatten", "stop": "stop",
                  "restart": "restart_bot", "startbot": "start_bot"}[action]
        if not hasattr(self.actions, method):
            raise RuntimeError(f"/{action} is only available when the controller is running")
        return await self._do(method, source, account=account)

    async def _execute_confirmed(self, action: str, payload, source: str) -> str:
        if action in UPDATE_ACTIONS:
            return await self._do("install_update", source, "tonight" if action == "update_tonight" else "now")
        if action == "set":
            key, value, *rest = payload
            return await self._do("change_setting", key, value, source, account=rest[0] if rest else None)
        if action == "reset":
            return await self._do("reset_settings", source, account=payload)
        if action == "phone":
            return await self._do("phone_on", source)
        if action == "take":
            rec_id, size, *rest = payload
            return await self._do("take_idea", rec_id, source, size, account=rest[0] if rest else None)
        if action == "switch":
            symbol, account = payload
            return await self._do("switch_market", symbol, source, account=account)
        return await self._execute(action, source, payload)

    async def _ask(self, question: str, action: str, payload=None, yes: str | None = None) -> None:
        confirm = {"inline_keyboard": [[{"text": yes or f"Yes, {action}", "callback_data": f"yes:{action}"},
                                        {"text": "Cancel", "callback_data": "no"}]]}
        message_id = await self.send(question, confirm)
        if message_id is not None:
            self.pending[message_id] = (action, time.monotonic(), payload)

    async def _offer_update(self, source: str) -> None:
        if not hasattr(self.actions, "check_update"):
            raise RuntimeError("/update is only available when the controller is running")
        await self.send("🔎 Checking GitHub for updates...")
        offer = await self._do("check_update", source)
        if not offer["can_install"]:
            await self.send(offer["text"])
            return
        now = {"text": "Install now", "callback_data": "yes:update"}
        if offer["quiet"]:
            rows = [[now]]
            note = "The bot stops for about a minute while it updates. It only installs while no trade or order is open."
        else:
            rows = [[{"text": "After the close", "callback_data": "yes:update_tonight"}, now]]
            note = ("Trading hours are on. 'After the close' installs it once today's trading is done. "
                    "'Install now' only works while no trade or order is open, and stops the bot for about a minute.")
        rows.append([{"text": "Not now", "callback_data": "no"}])
        message_id = await self.send(f"{offer['text']}\n\n{note}", {"inline_keyboard": rows})
        if message_id is not None:
            self.pending[message_id] = ("update", time.monotonic(), None)

    async def _dashboard(self, source: str, args: list[str]) -> None:
        if not hasattr(self.actions, "dashboard_info"):
            raise RuntimeError("/dashboard is only available when the controller is running")
        if args and args[0].lower() in ("off", "stop"):
            await self.send(await self._do("phone_off", source))
            return
        info = await self._do("dashboard_info")
        if info["url"]:
            from topstep_bot.phone_access import dashboard_keyboard

            if await self.send("📊 Your dashboard, inside Telegram. Only your Telegram account can open it.\n"
                               "To turn the phone link off: /dashboard off", dashboard_keyboard(info["url"])) is None:
                await self.send("Telegram would not show the Open dashboard button. It only works in a private chat "
                                "with your bot, not in a group.")
        elif info["problem"]:
            await self.send(f"📱 {info['problem']}")
        elif info["enabled"]:
            await self.send(f"⏳ {info['text']}")
        else:
            await self._ask("📱 Turn on phone access? The dashboard opens here in Telegram while you are away from the PC, "
                            "and only your Telegram account can open it. The bot keeps running on the PC. "
                            "Turn it off any time with /dashboard off.", "phone", yes="Yes, turn it on")

    async def _pick_account(self, command: str, with_all: bool) -> None:
        """Several accounts and none named: one button per account (and 'All' where it makes sense)."""
        names = self._accounts() + (["all"] if with_all else [])
        rows = [[{"text": n if n != "all" else "All accounts", "callback_data": f"pick:{command}:{n}"}
                 for n in names[i:i + 3]] for i in range(0, len(names), 3)]
        await self.send(f"/{command} - which account?", {"inline_keyboard": rows})

    async def _confirm_take(self, rec_id: str, half: bool, account: str | None = None) -> None:
        idea = await self._do("find_idea", rec_id, account=account)
        if idea is None or not idea["size"]:
            await self.send("That idea can't be taken (not found, or no valid size).")
            return
        size = max(1, idea["size"] // 2) if half else idea["size"]
        target = f", target {idea['target']}" if idea["target"] is not None else ""
        where = f" on {account}" if account else ""
        await self._ask(
            f"Take {idea['id']} ({idea['title']}){where}: {idea['side']} {size} near {idea['entry']}, stop {idea['stop']}{target}?\n"
            f"It is re-priced at the current market and sized by your risk rules (never larger). "
            f"Idea: {idea['reason']}",
            "take", (rec_id, size, account), yes=f"Yes, take {size}",
        )

    async def _send_ideas(self, account: str | None = None) -> None:
        ideas = await self._do("open_ideas", account=account)
        at = f"{account}:" if account else ""
        rows = [[{"text": f"Take {i['id']} ({i['side']} {i['size']})", "callback_data": f"take:{at}{i['id']}"},
                 {"text": "½ size", "callback_data": f"takeh:{at}{i['id']}"}] for i in ideas]
        await self.send(await self._do("ideas_text", account=account), {"inline_keyboard": rows} if rows else KEYBOARD)

    QUESTIONS = {
        "flatten": "⚠️ Close any open position, cancel all orders and HALT trading until the bot is restarted?",
        "stop": "⚠️ Stop the bot? It flattens first. Telegram and the dashboard stay online, so you can start it "
                "again with /startbot. (To just stop new trades, use /pause.)",
        "restart": "🔄 Restart the bot? It flattens any open position first, then starts again.",
        "startbot": "▶️ Start the bot? It will trade automatically in its current mode.",
    }

    async def _slow(self, name: str, source: str, failed: str, account: str | None = None) -> None:
        """Answer a slow command (/train, /learn) once it finishes; runs in the background."""
        try:
            await self.send(await self._do(name, source, account=account))
        except (ValueError, RuntimeError) as exc:
            await self.send(f"❌ {exc}")
        except Exception:  # noqa: BLE001 - runs outside the polling loop's own error handling
            log.exception("Telegram /%s failed", name)
            await self.send(f"❌ {failed} - see the Logs tab.")

    async def dispatch(self, command: str, source: str, args: list[str] | None = None) -> None:
        args = list(args or [])
        try:
            await self._dispatch(command, source, args)
        except (ValueError, RuntimeError) as exc:
            await self.send(f"❌ {exc}")

    async def _dispatch(self, command: str, source: str, args: list[str]) -> None:
        accounts = self._accounts()
        target = None
        if accounts and args and args[0].lower() in (*accounts, "all"):
            target = args.pop(0).lower()
        if target == "all" and command in PER_ACCOUNT:
            await self.send(f"/{command} works on one account at a time - e.g. /{command} {accounts[0]}")
            return
        if accounts and target is None and (command in CONTROL or command in PER_ACCOUNT or command == "reset"):
            await self._pick_account(command, with_all=command not in PER_ACCOUNT)
            return
        where = "" if target is None else " on ALL accounts" if target == "all" else f" on {target}"

        if command in ("status", "start", "accounts"):
            await self.send(await self._do("status_text", account=target), KEYBOARD)
        elif command in ("pause", "resume"):
            await self.send(await self._execute(command, source, target), KEYBOARD)
        elif command in DANGEROUS:
            if not self.cfg.confirm_dangerous:
                await self.send(await self._execute(command, source, target))
                return
            question = self.QUESTIONS[command]
            if where:
                question = question.replace("?", f"{where}?", 1)
            await self._ask(question, command, target)
        elif command == "trades":
            await self.send(await self._do("trades_text", account=target))
        elif command == "ideas":
            await self._send_ideas(target)
        elif command == "settings":
            await self.send(await self._do("settings_text", account=target))
        elif command == "knowledge":
            await self.send(await self._do("knowledge_text", account=target))
        elif command == "train":
            await self.send("🧠 Training on recent history - this takes a few seconds...")
            self._in_background(self._slow("train", source, "Training failed", target))
        elif command in ("brief", "next", "market", "funded", "rules", "research", "lab", "coach"):
            await self.send(await self._do(f"{command}_text", account=target))
        elif command == "learn":
            await self.send("📚 Learning from the long-run memory: downloading missing history and replaying all of it. "
                            "This can take a minute; the bot keeps trading meanwhile...")
            self._in_background(self._slow("learn", source, "Learning failed", target))
        elif command == "set":
            if len(args) < 2 or (accounts and target is None):
                example = f"/set {accounts[0]} risk 150 or /set all risk 150" if accounts else "/set risk 150"
                await self.send(f"Usage: /set {'<account|all> ' if accounts else ''}<setting> <value>, e.g. {example}. "
                                "Send /settings for the list.")
                return
            value = " ".join(args[1:])
            p = await self._do("preview_setting", args[0], value, account=target)
            if not p["changed"] and target != "all":
                await self.send(f"{p['label']} is already {p['new']}.")
                return
            warn = "\n⚠️ This increases your risk." if p["riskier"] else ""
            change = f"{p['label']} -> {p['new']}" if target == "all" else f"{p['label']}: {p['old']} -> {p['new']}"
            await self._ask(f"Change {change}{where}?{warn}", "set", (p["key"], value, target), yes="Yes, change it")
        elif command == "switch":
            if not hasattr(self.actions, "switch_market"):
                raise RuntimeError("/switch is only available when the controller is running")
            if not args:
                await self.send("Usage: /switch <market>, e.g. /switch MES, or /switch usual to go back. The bot must be "
                                "flat; it restarts on that market for the rest of today (until 15:10 CT).")
                return
            symbol = args[0].upper()
            back = symbol in ("USUAL", "BACK")
            question = ("Go back to the usual market" if back else f"Trade {symbol} for the rest of today") + f"{where}? " \
                + "The bot restarts on it (about half a minute); it must have no open trade."
            await self._ask(question, "switch", ("" if back else symbol, target), yes="Yes, switch")
        elif command == "reset":
            await self._ask(f"Undo every setting changed from the dashboard/Telegram{where} and go back to config.yaml?",
                            "reset", target, yes="Yes, reset")
        elif command == "ask":
            if not args:
                await self.send("Ask me anything about the bot, e.g. /ask why did you take the last trade? "
                                "You can also just type the question.")
                return
            self._in_background(self._answer(" ".join(args), target))
        elif command == "log":
            await self.send(await self._do("log_text", account=target))
        elif command == "update":
            await self._offer_update(source)
        elif command == "dashboard":
            await self._dashboard(source, args)
        else:
            await self.send(help_text(accounts), KEYBOARD)
