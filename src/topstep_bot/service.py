"""Shared pieces of the bot <-> controller contract.

The controller (controller.py) starts the trading bot as a child process and talks to it through
environment variables, exit codes, a heartbeat file and an exit note:

  exit code 0   stopped on purpose (dashboard/Telegram Stop, Ctrl+C)  -> stays stopped
  exit code 75  daily maintenance restart requested by the bot         -> restarted quietly
  exit code 2   could not start (configuration/account problem)        -> stays stopped, alert
  anything else crashed                                                 -> restarted with backoff
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path

import httpx

from topstep_bot.config import Secrets
from topstep_bot.notify import redact

log = logging.getLogger("topstep_bot.service")

RESTART_EXIT_CODE = 75
CONFIG_ERROR_EXIT_CODE = 2
ENV_EXIT_FILE = "TOPSTEP_BOT_EXIT_FILE"


def write_exit_note(code: int, reason: str) -> None:
    """Tell the controller why the bot is exiting (shown on the dashboard and in Telegram)."""
    path = os.environ.get(ENV_EXIT_FILE)
    if not path:
        return
    try:
        Path(path).write_text(
            json.dumps({"code": code, "reason": reason, "at": datetime.now().isoformat(timespec="seconds")}),
            encoding="utf-8",
        )
    except OSError as exc:
        log.debug("could not write exit note: %s", exc)


def read_exit_note(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def notify(secrets: Secrets, text: str) -> None:
    """Best-effort alert (Telegram/Discord) from the controller."""
    text = f"[Topstep Bot] {text}"
    targets = []
    if secrets.telegram_bot_token and secrets.telegram_chat_id:
        targets.append((f"https://api.telegram.org/bot{secrets.telegram_bot_token}/sendMessage",
                        {"chat_id": secrets.telegram_chat_id, "text": text}))
    if secrets.discord_webhook_url:
        targets.append((secrets.discord_webhook_url, {"content": text[:1900]}))
    for url, body in targets:  # one failing channel must not silence the other
        try:
            httpx.post(url, json=body, timeout=10)
        except httpx.HTTPError as exc:
            log.warning("Alert failed: %s", redact(exc))
