"""24/7 supervisor: keeps the bot running on your own computer.

`topstep-bot service` starts the bot as a child process and:
  * restarts it after a crash (with increasing waits, and a cap on restarts per hour),
  * restarts it if it hangs (its heartbeat file stops updating),
  * restarts it every day during the CME maintenance halt (the bot asks for this itself) so
    contract rolls, tokens and connections are always fresh,
  * keeps the computer awake,
  * sends a Telegram/Discord alert when something goes wrong,
  * stops for good when the bot is stopped on purpose (Ctrl+C, dashboard Stop, Telegram /stop).

Topstep requires automated trading to originate from your personal device - run this on your
own PC, not a VPS.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
from collections import deque
from logging.handlers import RotatingFileHandler
from pathlib import Path

import httpx

from topstep_bot.config import BotConfig, Secrets
from topstep_bot.keepawake import keep_awake

log = logging.getLogger("topstep_bot.service")

RESTART_EXIT_CODE = 75  # the bot asks for a quiet maintenance restart
CONFIG_ERROR_EXIT_CODE = 2
BACKOFF = (10, 30, 60, 120, 300)
HEALTHY_AFTER = 600  # seconds of uptime that reset the crash backoff


def notify(secrets: Secrets, text: str) -> None:
    """Best-effort alert from the supervisor (it has no event loop)."""
    text = f"[Topstep Bot service] {text}"
    try:
        if secrets.telegram_bot_token and secrets.telegram_chat_id:
            httpx.post(
                f"https://api.telegram.org/bot{secrets.telegram_bot_token}/sendMessage",
                json={"chat_id": secrets.telegram_chat_id, "text": text},
                timeout=10,
            )
        if secrets.discord_webhook_url:
            httpx.post(secrets.discord_webhook_url, json={"content": text[:1900]}, timeout=10)
    except httpx.HTTPError as exc:
        log.warning("Alert failed: %s", exc)


def _setup_logging(log_dir: Path = Path("logs")) -> None:
    log_dir.mkdir(exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-8s %(message)s")
    file_handler = RotatingFileHandler(log_dir / "service.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    file_handler.setFormatter(fmt)
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    log.handlers[:] = [file_handler, console]
    log.setLevel(logging.INFO)
    log.propagate = False


class Supervisor:
    def __init__(
        self,
        cfg: BotConfig,
        secrets: Secrets,
        *,
        config_path: str | None,
        mode: str | None,
        child_command: list[str] | None = None,
        poll_seconds: float = 5.0,
    ):
        self.cfg = cfg
        self.secrets = secrets
        self.config_path = config_path
        self.mode = mode
        self.child_command = child_command
        self.poll_seconds = poll_seconds
        self.heartbeat = Path(cfg.data_dir) / "heartbeat"
        self.restarts: deque[float] = deque()

    def command(self, first: bool) -> list[str]:
        if self.child_command is not None:
            return list(self.child_command)
        cmd = [sys.executable, "-m", "topstep_bot"]
        if self.config_path:
            cmd += ["-c", self.config_path]
        cmd += ["run", "--yes"]
        if self.mode:
            cmd += ["--mode", self.mode]
        if not first:
            cmd.append("--no-browser")  # don't open a new dashboard tab on every restart
        return cmd

    def run_child(self, first: bool, quiet: bool) -> tuple[int, float]:
        """Run the bot once. Returns (exit code, seconds it ran)."""
        self.heartbeat.parent.mkdir(parents=True, exist_ok=True)
        self.heartbeat.unlink(missing_ok=True)
        env = dict(os.environ)
        env.update(
            TOPSTEP_BOT_SUPERVISED="1",
            TOPSTEP_BOT_HEARTBEAT=str(self.heartbeat.resolve()),
            TOPSTEP_BOT_QUIET_START="1" if quiet else "0",
        )
        started = time.time()
        proc = subprocess.Popen(self.command(first), env=env)
        timeout = self.cfg.service.heartbeat_timeout_seconds
        try:
            while proc.poll() is None:
                time.sleep(self.poll_seconds)
                if time.time() - started < timeout:
                    continue
                try:
                    age = time.time() - self.heartbeat.stat().st_mtime
                except FileNotFoundError:
                    age = time.time() - started
                if age > timeout:
                    log.error("Bot has not responded for %.0fs - restarting it", age)
                    notify(self.secrets, f"Bot stopped responding for {age:.0f}s; restarting it. Its protective stops stay at TopstepX.")
                    proc.terminate()
                    try:
                        proc.wait(timeout=20)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait()
                    return -1, time.time() - started
        except KeyboardInterrupt:
            log.info("Ctrl+C - waiting for the bot to shut down cleanly...")
            try:
                proc.wait(timeout=60)
            except subprocess.TimeoutExpired:
                proc.kill()
            raise
        return proc.returncode, time.time() - started

    def _too_many_restarts(self) -> bool:
        now = time.time()
        while self.restarts and now - self.restarts[0] > 3600:
            self.restarts.popleft()
        return len(self.restarts) >= self.cfg.service.max_restarts_per_hour

    def loop(self, sleep=time.sleep) -> int:
        first, quiet, failures = True, False, 0
        while True:
            log.info("Starting bot%s", " (maintenance restart)" if quiet else "")
            code, ran = self.run_child(first, quiet)
            first = False
            if code == 0:
                log.info("Bot stopped on purpose - service exiting")
                return 0
            if code == RESTART_EXIT_CODE:
                log.info("Daily maintenance restart")
                quiet, failures = True, 0
                continue
            if code == CONFIG_ERROR_EXIT_CODE and ran < 60:
                log.error("Bot could not start because of a configuration problem - fix it and start the service again")
                notify(self.secrets, "Bot could not start (configuration problem). Check the bot window / logs on your PC.")
                return code
            quiet = False
            failures = 0 if ran > HEALTHY_AFTER else failures + 1
            self.restarts.append(time.time())
            if self._too_many_restarts():
                log.error("Too many restarts in the last hour - giving up")
                notify(self.secrets, "Bot keeps crashing; the service gave up. Check logs/bot.log on your PC.")
                return 1
            wait = BACKOFF[min(max(failures - 1, 0), len(BACKOFF) - 1)]
            log.warning("Bot exited with code %s after %.0fs; restarting in %ss", code, ran, wait)
            notify(self.secrets, f"Bot exited unexpectedly (code {code}); restarting in {wait}s.")
            sleep(wait)


def run_service(cfg: BotConfig, secrets: Secrets, *, config_path: str | None, mode: str | None) -> int:
    _setup_logging()
    log.info("Topstep Bot 24/7 service started (mode: %s). Stop with Ctrl+C or Telegram /stop.", mode or cfg.mode)
    with keep_awake(cfg.service.keep_awake):
        try:
            return Supervisor(cfg, secrets, config_path=config_path, mode=mode).loop()
        except KeyboardInterrupt:
            log.info("Service stopped")
            return 0
