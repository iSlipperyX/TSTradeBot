"""Optional phone/desktop alerts via Discord webhook and/or Telegram bot.

Configure in .env:
    DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...
    TELEGRAM_BOT_TOKEN=123456:ABC...
    TELEGRAM_CHAT_ID=123456789
Alerts are sent from a background queue so a slow webhook never delays trading.
"""

from __future__ import annotations

import asyncio
import logging

import httpx

from topstep_bot.config import NotificationsConfig, Secrets

log = logging.getLogger(__name__)


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
            self._task = asyncio.create_task(self._worker(), name="notifier")

    async def stop(self) -> None:
        if self._task is None:
            return
        try:
            await asyncio.wait_for(self._queue.join(), timeout=5)
        except asyncio.TimeoutError:
            pass
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
            try:
                if self.discord:
                    await self._post(self.discord, {"content": text[:1900]})
                if self.telegram:
                    token, chat_id = self.telegram
                    await self._post(f"https://api.telegram.org/bot{token}/sendMessage", {"chat_id": chat_id, "text": text})
            except Exception as exc:  # noqa: BLE001 - alerts are best effort
                log.warning("Notification failed: %s", exc)
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
