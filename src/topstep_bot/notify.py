"""Optional phone/desktop alerts via Discord webhook and/or Telegram bot.

Configure in .env:
    DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...
    TELEGRAM_BOT_TOKEN=123456:ABC...
    TELEGRAM_CHAT_ID=123456789
Alerts are sent from a background queue so a slow webhook never delays trading.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re

import httpx

from topstep_bot.config import NotificationsConfig, Secrets

log = logging.getLogger(__name__)

_SECRET_URL_PARTS = re.compile(r"(/bot)\d+:[\w-]+|(/api/webhooks/\d+/)[\w-]+")


def redact(text: object) -> str:
    """Hide Telegram bot tokens and Discord webhook secrets in error messages before they're logged
    (httpx puts the full request URL, which contains them, into its exception text)."""
    return _SECRET_URL_PARTS.sub(lambda m: (m.group(1) or m.group(2)) + "<secret>", str(text))


class Notifier:
    def __init__(self, cfg: NotificationsConfig, secrets: Secrets, prefix: str = "Topstep Bot"):
        self.discord = secrets.discord_webhook_url
        self.telegram = (
            (secrets.telegram_bot_token, secrets.telegram_chat_id)
            if secrets.telegram_bot_token and secrets.telegram_chat_id
            else None
        )
        self.enabled = cfg.enabled and bool(self.discord or self.telegram)
        self.kinds = set(cfg.events)
        self.prefix = prefix
        self._queue: asyncio.Queue[str] = asyncio.Queue(maxsize=200)
        self._task: asyncio.Task | None = None
        self._client: httpx.AsyncClient | None = None

    def start(self) -> None:
        if self.enabled and self._task is None:
            self._client = httpx.AsyncClient(timeout=10)
            from topstep_bot.logging_setup import spawn

            self._task = spawn(self._worker(), name="notifier")

    async def stop(self) -> None:
        if self._task is None:
            return
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._queue.join(), timeout=5)
        self._task.cancel()
        if self._client:
            await self._client.aclose()
        self._task = None

    def notify(self, kind: str, text: str) -> None:
        if not self.enabled or kind not in self.kinds:
            return
        try:
            self._queue.put_nowait(f"[{self.prefix}] {text}")
        except asyncio.QueueFull:
            log.warning("Notification queue full; dropping: %s", text)

    async def _worker(self) -> None:
        assert self._client is not None
        while True:
            text = await self._queue.get()
            targets = []
            if self.discord:
                targets.append(("Discord", self.discord, {"content": text[:1900]}))
            if self.telegram:
                token, chat_id = self.telegram
                targets.append(("Telegram", f"https://api.telegram.org/bot{token}/sendMessage",
                                {"chat_id": chat_id, "text": text[:4000]}))
            try:
                for name, url, body in targets:  # one failing channel must not silence the other
                    try:
                        await self._post(url, body)
                    except Exception as exc:  # noqa: BLE001 - alerts are best effort
                        log.warning("%s alert failed: %s", name, redact(exc))
            finally:
                self._queue.task_done()

    async def _post(self, url: str, body: dict) -> None:
        assert self._client is not None
        for _ in range(3):
            resp = await self._client.post(url, json=body)
            if resp.status_code == 429:
                retry = float(resp.headers.get("retry-after", "2"))
                await asyncio.sleep(min(retry, 10))
                continue
            resp.raise_for_status()
            return
