"""Control the bot from Telegram.

The bot long-polls the Telegram Bot API from your PC (no webhook, no open ports), so every
order is still placed by the bot on your own computer, as Topstep requires.

Security:
  * Only messages from TELEGRAM_CHAT_ID are obeyed (optionally narrowed to specific user IDs
    with ``telegram.allowed_user_ids``); everything else is ignored.
  * Commands sent while the bot was offline are discarded at startup, so an old /flatten
    can't fire unexpectedly.
  * /flatten and /stop require tapping a confirmation button within 60 seconds.
  * Remote commands can pause, resume, flatten or stop - they cannot change risk limits.

Commands: /status /pause /resume /flatten /stop /trades /log /help
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx

from topstep_bot.config import TelegramConfig
from topstep_bot.control import BotActions
from topstep_bot.notify import redact

log = logging.getLogger(__name__)

COMMANDS = [
    ("status", "Account, position and risk status"),
    ("pause", "Stop opening new trades"),
    ("resume", "Allow new trades again"),
    ("flatten", "Close everything and halt trading"),
    ("stop", "Shut the bot down (flattens first)"),
    ("trades", "Recent closed trades"),
    ("log", "Recent bot activity"),
    ("help", "Show the commands"),
]
DANGEROUS = {"flatten", "stop"}
CONFIRM_SECONDS = 60
MAX_TEXT = 4000

KEYBOARD = {
    "inline_keyboard": [
        [{"text": "📊 Status", "callback_data": "cmd:status"}, {"text": "⏸ Pause", "callback_data": "cmd:pause"},
         {"text": "▶️ Resume", "callback_data": "cmd:resume"}],
        [{"text": "📜 Trades", "callback_data": "cmd:trades"}, {"text": "🛑 Flatten", "callback_data": "cmd:flatten"},
         {"text": "⏹ Stop bot", "callback_data": "cmd:stop"}],
    ]
}


class TelegramError(Exception):
    def __init__(self, code: int | None, description: str):
        super().__init__(f"Telegram error {code}: {description}")
        self.code = code


def help_text() -> str:
    lines = ["Topstep Bot commands:"] + [f"/{name} - {desc}" for name, desc in COMMANDS]
    lines.append("\n/flatten and /stop ask for confirmation. A stopped bot can only be restarted on your PC.")
    return "\n".join(lines)


class TelegramController:
    def __init__(
        self,
        token: str,
        chat_id: str | int,
        actions: BotActions,
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
        await self._client.aclose()

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
        command = text[1:].split()[0].split("@")[0].lower()
        await self.dispatch(command, self._who(user))

    async def _handle_callback(self, cq: dict) -> None:
        message = cq.get("message") or {}
        chat_id = message.get("chat", {}).get("id")
        user = cq.get("from", {})
        try:
            await self._call("answerCallbackQuery", callback_query_id=cq["id"])
        except (TelegramError, httpx.HTTPError):
            pass
        if not self._authorized(chat_id, user.get("id")):
            self._reject(chat_id, user.get("id"))
            return
        data = str(cq.get("data", ""))
        message_id = message.get("message_id")
        if data.startswith("cmd:"):
            await self.dispatch(data[4:], self._who(user))
        elif data.startswith("yes:"):
            action = data[4:]
            pending = self.pending.pop(message_id, None)
            if pending is None or pending[0] != action or time.monotonic() - pending[1] > CONFIRM_SECONDS:
                await self._edit(message_id, "That confirmation expired - send the command again.")
                return
            result = self._execute(action, self._who(user))
            await self._edit(message_id, f"✅ {result}")
        elif data == "no":
            self.pending.pop(message_id, None)
            await self._edit(message_id, "Cancelled - nothing was changed.")

    @staticmethod
    def _who(user: dict) -> str:
        name = user.get("username") or user.get("first_name") or user.get("id")
        return f"Telegram ({name})"

    def _execute(self, action: str, source: str) -> str:
        return {
            "pause": self.actions.pause,
            "resume": self.actions.resume,
            "flatten": self.actions.flatten,
            "stop": self.actions.stop,
        }[action](source)

    async def dispatch(self, command: str, source: str) -> None:
        if command in ("status", "start"):
            await self.send(self.actions.status_text(), KEYBOARD)
        elif command in ("pause", "resume"):
            await self.send(self._execute(command, source), KEYBOARD)
        elif command in DANGEROUS:
            if not self.cfg.confirm_dangerous:
                await self.send(self._execute(command, source))
                return
            question = (
                "⚠️ Close any open position, cancel all orders and HALT trading until the bot is restarted?"
                if command == "flatten"
                else "⚠️ Shut the bot down? It flattens first and can only be restarted from your PC."
            )
            confirm = {"inline_keyboard": [[{"text": f"Yes, {command}", "callback_data": f"yes:{command}"},
                                            {"text": "Cancel", "callback_data": "no"}]]}
            message_id = await self.send(question, confirm)
            if message_id is not None:
                self.pending[message_id] = (command, time.monotonic())
        elif command == "trades":
            await self.send(self.actions.trades_text())
        elif command == "log":
            await self.send(self.actions.log_text())
        else:
            await self.send(help_text(), KEYBOARD)
