"""The controller: dashboard + Telegram + supervision, in its own process.

    you --> dashboard (http://127.0.0.1:8765) / Telegram --> controller --(local API)--> trading bot

The trading bot runs as a separate child process. If it crashes, hangs or is stopped, the
controller keeps running, so the dashboard and Telegram stay online: you can see what happened
(exit reason, logs) and start, restart or stop the bot, or switch between paper and live.

Supervision rules (see service.py for the exit codes):
  * crash             -> restart after 10s, 30s, 60s, 120s, 300s... (gives up after
                         service.max_restarts_per_hour and alerts you)
  * hang              -> no heartbeat for service.heartbeat_timeout_seconds -> killed and restarted
  * daily maintenance -> the bot asks for it at 16:05 CT and is restarted quietly
  * deliberate stop   -> stays stopped until you start it again
  * config problem    -> stays stopped, with the reason shown
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import secrets as pysecrets
import socket
import sys
import time
import webbrowser
from collections import deque
from datetime import datetime, timezone
from importlib import resources
from pathlib import Path
from typing import Any

import httpx

from topstep_bot.config import BotConfig, Secrets
from topstep_bot.logging_setup import spawn, tail
from topstep_bot.service import (
    CONFIG_ERROR_EXIT_CODE,
    ENV_EXIT_FILE,
    RESTART_EXIT_CODE,
    notify,
    read_exit_note,
)
from topstep_bot.sessions import SessionSchedule
from topstep_bot.web import HttpServer, Request, html_response

log = logging.getLogger("topstep_bot.controller")

BACKOFF = (10, 30, 60, 120, 300)
HEALTHY_AFTER = 600  # seconds of uptime that reset the crash backoff
STOP_TIMEOUT = 45  # seconds to wait for a graceful stop (it flattens first)
ACTION_TIMEOUT = 8  # seconds to wait for the bot to answer a request
SLOW_ACTIONS = {"train": 600}  # training downloads and replays weeks of history
STATE_FILE = "controller.json"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def load_state(cfg: BotConfig) -> dict:
    try:
        return json.loads((Path(cfg.data_dir) / STATE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(cfg: BotConfig, state: dict) -> None:
    path = Path(cfg.data_dir) / STATE_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")


def resolve_mode(cfg: BotConfig, cli_mode: str | None) -> str:
    """--mode wins; otherwise the mode last chosen on the dashboard; otherwise config.yaml."""
    return cli_mode or load_state(cfg).get("mode") or cfg.mode


# ------------------------------------------------------------------------------ bot process

class BotProcess:
    """Starts, watches and stops the trading bot child process."""

    def __init__(
        self,
        cfg: BotConfig,
        secrets: Secrets,
        *,
        mode: str,
        config_path: str | None,
        worker_command: list[str] | None = None,
        events: deque | None = None,
        poll_seconds: float = 5.0,
    ):
        self.cfg = cfg
        self.secrets = secrets
        self.mode = mode
        self.config_path = config_path
        self.worker_command = worker_command
        self.poll_seconds = poll_seconds
        self.events: deque[dict] = events if events is not None else deque(maxlen=100)
        data = Path(cfg.data_dir)
        data.mkdir(parents=True, exist_ok=True)
        self.heartbeat = data / "heartbeat"
        self.exit_file = data / "bot_exit.json"
        self.state = "stopped"  # stopped | starting | running | stopping | restarting | crashed | failed
        self.proc: asyncio.subprocess.Process | None = None
        self.port = 0
        self.token = ""
        self.started_at: float | None = None
        self.last_exit: dict | None = None
        self.restarts: deque[float] = deque()
        self.failures = 0
        self.want_running = False
        self._intent: str | None = None  # why the controller itself is stopping the bot
        self._watcher: asyncio.Task | None = None
        self._client = httpx.AsyncClient(timeout=ACTION_TIMEOUT)
        self._lock = asyncio.Lock()

    # ---------------------------------------------------------------- helpers

    def _event(self, level: str, message: str, alert: bool = False) -> None:
        getattr(log, level if level in ("info", "warning", "error") else "info")(message)
        self.events.appendleft({"ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "level": level, "message": message})
        if alert:
            asyncio.get_running_loop().run_in_executor(None, notify, self.secrets, message)

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.returncode is None

    def command(self, quiet: bool) -> list[str]:
        if self.worker_command is not None:  # tests: a stand-in worker
            return [*self.worker_command, "--mode", self.mode]
        cmd = [sys.executable, "-m", "topstep_bot"]
        if self.config_path:
            cmd += ["-c", str(self.config_path)]
        return cmd + ["run", "--yes", "--mode", self.mode]

    def info(self) -> dict:
        uptime = int(time.time() - self.started_at) if self.started_at and self.running else None
        return {
            "state": self.state,
            "mode": self.mode,
            "pid": self.proc.pid if self.running else None,
            "uptime_s": uptime,
            "restarts_last_hour": len([t for t in self.restarts if time.time() - t < 3600]),
            "last_exit": self.last_exit,
        }

    # ---------------------------------------------------------------- lifecycle

    async def start(self, source: str = "controller", quiet: bool = False) -> str:
        async with self._lock:
            if self.running:
                return f"The bot is already {self.state}."
            self.want_running = True
            self._intent = None
            self.port, self.token = _free_port(), pysecrets.token_urlsafe(24)
            for f in (self.heartbeat, self.exit_file):
                f.unlink(missing_ok=True)
            env = dict(os.environ)
            env.update(
                TOPSTEP_BOT_SUPERVISED="1",
                TOPSTEP_BOT_HEARTBEAT=str(self.heartbeat.resolve()),
                TOPSTEP_BOT_QUIET_START="1" if quiet else "0",
                TOPSTEP_BOT_API_PORT=str(self.port),
                TOPSTEP_BOT_API_TOKEN=self.token,
                **{ENV_EXIT_FILE: str(self.exit_file.resolve())},
            )
            self.proc = await asyncio.create_subprocess_exec(*self.command(quiet), env=env)
            self.started_at = time.time()
            self.state = "starting"
            self._event("info", f"Bot starting in {self.mode.upper()} mode (pid {self.proc.pid}) - requested by {source}")
            self._watcher = spawn(self._watch(self.proc), name="bot-watch")
            return f"Starting the bot in {self.mode.upper()} mode..."

    async def stop(self, source: str, intent: str = "stop") -> str:
        """Graceful stop: ask the bot to flatten and exit; force it only if it doesn't."""
        if not self.running:
            self.want_running = intent == "restart"
            return "The bot is not running."
        self._intent = intent
        self.want_running = intent == "restart"
        self.state = "restarting" if intent == "restart" else "stopping"
        self._event("warning", f"Bot {'restart' if intent == 'restart' else 'stop'} requested by {source}")
        with contextlib.suppress(RuntimeError, ValueError):  # not reachable yet (starting/hung): forced below
            await self.action("stop", {"source": source})
        proc = self.proc
        try:
            await asyncio.wait_for(proc.wait(), STOP_TIMEOUT)
        except TimeoutError:
            self._event("error", "Bot did not stop in time - forcing it. Its protective stops stay at TopstepX.", alert=True)
            proc.kill()
            await proc.wait()
        if self._watcher:
            await asyncio.wait([self._watcher], timeout=5)
        return "Bot stopped." if intent == "stop" else "Bot restarted."

    async def restart(self, source: str) -> str:
        if not self.running:
            return await self.start(source)
        await self.stop(source, intent="restart")
        return await self.start(source) if not self.running else "Bot restarted."

    async def _watch(self, proc: asyncio.subprocess.Process) -> None:
        code = await proc.wait()
        ran = time.time() - (self.started_at or time.time())
        note = read_exit_note(self.exit_file) or {}
        reason = note.get("reason") or ("crashed" if code not in (0, RESTART_EXIT_CODE) else "exited")
        self.last_exit = {"code": code, "reason": reason, "at": datetime.now().isoformat(timespec="seconds")}
        if proc is not self.proc:
            return
        intent, self._intent = self._intent, None
        if intent == "stop":
            self.state = "stopped"
            self._event("info", f"Bot stopped ({reason})")
        elif intent == "restart":
            self.state = "restarting"
        elif intent == "hang":
            await self._restart_after_failure("stopped responding (killed)", ran)
        elif code == RESTART_EXIT_CODE:
            self._event("info", "Daily maintenance restart")
            self.state = "restarting"
            await self.start("daily maintenance", quiet=True)
        elif code == 0:
            self.want_running = False
            self.state = "stopped"
            self._event("warning", f"Bot stopped: {reason}", alert=True)
        elif code == CONFIG_ERROR_EXIT_CODE and ran < 60:
            self.want_running = False
            self.state = "failed"
            self._event("error", f"Bot could not start: {reason}", alert=True)
        else:
            await self._restart_after_failure(f"crashed (exit code {code}): {reason}", ran)

    async def _restart_after_failure(self, what: str, ran: float) -> None:
        self.failures = 0 if ran > HEALTHY_AFTER else self.failures + 1
        now = time.time()
        self.restarts.append(now)
        while self.restarts and now - self.restarts[0] > 3600:
            self.restarts.popleft()
        if not self.want_running:
            self.state = "crashed"
            self._event("error", f"Bot {what}", alert=True)
            return
        if len(self.restarts) >= self.cfg.service.max_restarts_per_hour:
            self.state = "failed"
            self.want_running = False
            self._event("error", f"Bot {what}. Too many restarts in the last hour - giving up. Check the Logs tab.",
                        alert=True)
            return
        wait = BACKOFF[min(max(self.failures - 1, 0), len(BACKOFF) - 1)]
        self.state = "crashed"
        self._event("error", f"Bot {what}; restarting in {wait}s", alert=True)
        await asyncio.sleep(wait)
        if self.want_running and not self.running:
            await self.start("auto-restart")

    async def monitor(self) -> None:
        """Mark the bot 'running' once its API answers; restart it if its heartbeat goes stale."""
        timeout = self.cfg.service.heartbeat_timeout_seconds
        while True:
            await asyncio.sleep(self.poll_seconds)
            if not self.running:
                continue
            if self.state == "starting" and await self.status() is not None:
                self.state = "running"
                self.failures = self.failures if time.time() - (self.started_at or 0) < HEALTHY_AFTER else 0
                self._event("info", f"Bot is running ({self.mode.upper()})")
            if self.state not in ("running", "starting") or time.time() - (self.started_at or 0) < timeout:
                continue
            try:
                age = time.time() - self.heartbeat.stat().st_mtime
            except FileNotFoundError:
                age = time.time() - (self.started_at or time.time())
            if age > timeout:
                self._event("error", f"Bot has not responded for {age:.0f}s - restarting it", alert=True)
                self._intent = "hang"
                self.proc.kill()

    # ---------------------------------------------------------------- talking to the bot

    async def status(self) -> dict | None:
        if not self.running or not self.port:
            return None
        try:
            r = await self._client.get(f"http://127.0.0.1:{self.port}/status", headers={"X-Token": self.token})
            return r.json() if r.status_code == 200 else None
        except (httpx.HTTPError, ValueError):
            return None

    async def action(self, name: str, payload: dict | None = None) -> dict:
        if not self.running:
            raise RuntimeError("The bot is not running - start it first.")
        try:
            r = await self._client.post(f"http://127.0.0.1:{self.port}/action/{name}", json=payload or {},
                                        headers={"X-Token": self.token}, timeout=SLOW_ACTIONS.get(name, ACTION_TIMEOUT))
        except httpx.TimeoutException:
            raise RuntimeError("The bot is busy and did not answer in time - it is still running; try again shortly.") from None
        except httpx.HTTPError:
            raise RuntimeError("The bot is not ready yet (still starting?)") from None
        data = r.json()
        if not data.get("ok", False):
            raise ValueError(data.get("message") or "the bot refused the request")
        return data

    async def close(self) -> None:
        await self._client.aclose()


# ------------------------------------------------------------------------------ Telegram adapter

class ProxyActions:
    """What Telegram can do, implemented through the controller (works even when the bot is down)."""

    def __init__(self, ctl: Controller):
        self.ctl = ctl
        self.bot = ctl.bot

    async def _text(self, name: str) -> str:
        if not self.bot.running:
            return self.ctl.offline_text()
        try:
            return (await self.bot.action(name))["text"]
        except (RuntimeError, ValueError) as exc:
            return str(exc)

    async def _msg(self, name: str, source: str, **payload: Any) -> str:
        return (await self.bot.action(name, {"source": source, **payload})).get("message") or "Done."

    async def pause(self, source: str) -> str:
        return await self._msg("pause", source)

    async def resume(self, source: str) -> str:
        return await self._msg("resume", source)

    async def flatten(self, source: str) -> str:
        return await self._msg("flatten", source)

    async def stop(self, source: str) -> str:
        return await self.bot.stop(source)

    async def start_bot(self, source: str) -> str:
        return await self.bot.start(source)

    async def restart_bot(self, source: str) -> str:
        return await self.bot.restart(source)

    async def status_text(self) -> str:
        if not self.bot.running or self.bot.state != "running":
            return self.ctl.offline_text()
        return await self._text("status_text") + f"\nBot process: {self.bot.state}, uptime {self.ctl.uptime_text()}"

    async def ideas_text(self) -> str:
        return await self._text("ideas_text")

    async def trades_text(self) -> str:
        return await self._text("trades_text")

    async def log_text(self) -> str:
        return await self._text("log_text")

    async def settings_text(self) -> str:
        return await self._text("settings_text")

    async def knowledge_text(self) -> str:
        return await self._text("knowledge_text")

    async def train(self, source: str) -> str:
        return await self._msg("train", source)

    async def check_update(self, source: str) -> dict:
        return await self.ctl.updates.telegram_check(source)

    async def install_update(self, source: str, when: str = "now") -> str:
        return await self.ctl.updates.install(source, when)

    async def preview_setting(self, key: str, value: Any) -> dict:
        return await self.bot.action("preview_setting", {"key": key, "value": value})

    async def change_setting(self, key: str, value: Any, source: str) -> str:
        return await self._msg("set_setting", source, key=key, value=value)

    async def reset_settings(self, source: str) -> str:
        return await self._msg("reset_settings", source)

    async def take_idea(self, rec_id: str, source: str, size: int | None = None) -> str:
        return await self._msg("take_idea", source, id=rec_id, size=size)

    async def open_ideas(self) -> list[dict]:
        if not self.bot.running:
            return []
        try:
            return (await self.bot.action("open_ideas"))["items"]
        except (RuntimeError, ValueError):
            return []

    async def find_idea(self, rec_id: str) -> dict | None:
        try:
            return (await self.bot.action("find_idea", {"id": rec_id}))["item"]
        except (RuntimeError, ValueError):
            return None


# ------------------------------------------------------------------------------ controller

class Controller:
    telegram_api = "https://api.telegram.org"

    def __init__(self, cfg: BotConfig, secrets: Secrets, *, mode: str, config_path: str | None,
                 worker_command: list[str] | None = None, port: int | None = None, poll_seconds: float = 5.0):
        self.cfg = cfg
        self.secrets = secrets
        self.config_path = config_path
        self.events: deque[dict] = deque(maxlen=100)
        self.bot = BotProcess(cfg, secrets, mode=mode, config_path=config_path, worker_command=worker_command,
                              events=self.events, poll_seconds=poll_seconds)
        self.started_at = time.time()
        self.token = pysecrets.token_urlsafe(24)
        self.telegram = None
        self._telegram_task: asyncio.Task | None = None
        self._page = resources.files("topstep_bot.dashboard").joinpath("index.html").read_text(encoding="utf-8")
        self.server = HttpServer(
            cfg.dashboard.host if cfg.dashboard.host in ("127.0.0.1", "localhost") else "127.0.0.1",
            cfg.dashboard.port if port is None else port,
            [
                ("GET", "/", self._index),
                ("GET", "/api/status", self._status),
                ("GET", "/api/logs", self._logs),
                ("POST", "/api/bot/start", lambda r: self.bot.start("dashboard")),
                ("POST", "/api/bot/stop", lambda r: self.bot.stop("dashboard")),
                ("POST", "/api/bot/restart", lambda r: self.bot.restart("dashboard")),
                ("POST", "/api/mode", self._mode),
                ("POST", "/api/action/*", self._proxy),
                ("POST", "/api/updates/check", lambda r: self.updates.check_text("dashboard")),
                ("POST", "/api/updates/install", lambda r: self.updates.install("dashboard", str(r.json().get("when", "now")))),
                ("POST", "/api/updates/cancel", lambda r: self.updates.cancel("dashboard")),
            ],
            token=self.token,
            name="dashboard",
        )
        self.instance = pysecrets.token_hex(4)  # changes on every start: an open dashboard reloads itself
        self.exit_code = 0
        self._exit = asyncio.Event()
        from topstep_bot.update_service import UpdateService

        self.updates = UpdateService(self)

    # ---------------------------------------------------------------- dashboard routes

    def _index(self, _: Request):
        return html_response(self._page.replace("__TOKEN__", self.token))

    async def _status(self, _: Request) -> dict:
        data = await self.bot.status() if self.bot.state in ("running", "starting") else None
        return {
            "controller": {**self.bot.info(), "uptime": self.uptime_text(), "telegram": self.telegram is not None},
            "bot": data.get("bot") if data else None,
            "log": data.get("log") if data else None,
            "events": list(self.events)[:30],
            "instance": self.instance,
            "updates": self.updates.status(),
            # market / trading-day countdowns: computed here so they show even while the bot is stopped
            "clock": self._clock(),
        }

    def _clock(self) -> dict:
        from topstep_bot.instruments import get_spec
        from topstep_bot.strategies.base import parse_hhmm

        symbol = self.cfg.instrument.symbol
        try:
            spec = get_spec(symbol)
            rth = (parse_hhmm(spec.rth_open), parse_hhmm(spec.rth_close))
        except KeyError:  # a symbol outside the built-in table: index-futures hours
            rth = (parse_hhmm("08:30"), parse_hhmm("15:00"))
        return SessionSchedule(self.cfg.session).clock(datetime.now(timezone.utc), rth, symbol.upper())

    def _logs(self, _: Request) -> dict:
        log_dir = Path(self.cfg.log_dir)
        crashes = sorted(log_dir.glob("crash_*.txt"))
        return {
            "errors": "".join(tail(log_dir / "errors.log", 60)),
            "bot": "".join(tail(log_dir / "bot.log", 60)),
            "controller": "".join(tail(log_dir / "controller.log", 40)),
            "crash": "".join(tail(crashes[-1], 40)) if crashes else "",
            "folder": str(log_dir),
        }

    async def _proxy(self, req: Request) -> dict:
        payload = req.json()
        payload.setdefault("source", "dashboard")
        data = await self.bot.action(req.param, payload)
        data.pop("ok", None)
        return data

    async def _mode(self, req: Request) -> str:
        body = req.json()
        return await self.set_mode(str(body.get("mode", "")), str(body.get("confirm", "")), "dashboard")

    async def set_mode(self, mode: str, confirm: str, source: str) -> str:
        if mode not in ("paper", "live"):
            raise ValueError("mode must be paper or live")
        if mode == self.bot.mode:
            return f"Already in {mode.upper()} mode."
        if mode == "live" and confirm.strip().upper() != "LIVE":
            raise ValueError("Type LIVE to confirm switching to live trading with real orders.")
        status = await self.bot.status()
        bot = status.get("bot") if status else None
        if bot and (bot.get("position") or bot.get("trade")):
            raise ValueError("A trade is open. Close it first (Flatten), then switch modes.")
        old, self.bot.mode = self.bot.mode, mode
        state = load_state(self.cfg)
        state["mode"] = mode
        save_state(self.cfg, state)
        self.bot._event("warning", f"Mode switched {old.upper()} -> {mode.upper()} by {source}", alert=True)
        if self.bot.running:
            await self.bot.restart(source)
            return f"Switched to {mode.upper()} - the bot is restarting in {mode.upper()} mode."
        return f"Mode set to {mode.upper()}. Press Start to run the bot."

    # ---------------------------------------------------------------- text helpers

    def uptime_text(self) -> str:
        if not self.bot.running or not self.bot.started_at:
            return "-"
        s = int(time.time() - self.bot.started_at)
        return f"{s // 3600}h {s % 3600 // 60}m"

    def offline_text(self) -> str:
        b = self.bot
        lines = [f"🤖 Bot is {b.state.upper()} ({b.mode.upper()} mode)."]
        if b.last_exit:
            lines.append(f"Last exit: {b.last_exit['reason']} (code {b.last_exit['code']}, {b.last_exit['at']})")
        if b.state in ("stopped", "crashed", "failed"):
            lines.append("Send /startbot to start it, or check the dashboard's Logs tab.")
        elif b.state in ("starting", "restarting"):
            lines.append("It is starting up - try again in a few seconds.")
        return "\n".join(lines)

    # ---------------------------------------------------------------- run

    async def start_telegram(self) -> None:
        s = self.secrets
        if not (self.cfg.telegram.control_enabled and s.telegram_bot_token and s.telegram_chat_id):
            return
        from topstep_bot.telegram_control import TelegramController

        tg = TelegramController(s.telegram_bot_token, s.telegram_chat_id, ProxyActions(self), self.cfg.telegram,
                                api_base=self.telegram_api)
        try:
            await tg.start(announce=True)
        except Exception as exc:  # noqa: BLE001 - the dashboard still works without Telegram
            log.warning("Telegram control could not start: %s", exc)
            await tg.close()
            return
        self.telegram = tg
        self._telegram_task = spawn(tg.run(), name="telegram")

    async def run(self, start_bot: bool = True, open_browser: bool = True) -> None:
        try:
            await self.server.start()
        except OSError:
            raise RuntimeError(
                f"Port {self.cfg.dashboard.port} is busy - is the bot already running? Open {self.server.url}"
            ) from None
        log.info("Dashboard: %s", self.server.url)
        spawn(self.bot.monitor(), name="bot-monitor")
        await self.start_telegram()
        if start_bot:
            await self.bot.start("controller start")
        await self.updates.start()
        if open_browser and self.cfg.dashboard.open_browser:
            webbrowser.open(self.server.url)
        try:
            await self._exit.wait()
        finally:
            await self.shutdown()

    def request_exit(self, code: int) -> None:
        """End run() (used to restart with a newly installed update)."""
        self.exit_code = code
        self._exit.set()

    async def shutdown(self) -> None:
        log.info("Controller shutting down")
        if self.bot.running:
            await self.bot.stop("controller shutdown")
        if self._telegram_task:  # stop polling before its HTTP client is closed below
            self._telegram_task.cancel()
            await asyncio.wait([self._telegram_task], timeout=5)
        if self.telegram:
            await self.telegram.send("⏹ The Topstep Bot controller was closed on the PC. Start it there to resume.")
            await self.telegram.close()
        await self.server.stop()
        await self.bot.close()


def run_controller(cfg: BotConfig, secrets: Secrets, *, config_path: str | None, mode: str,
                   start_bot: bool = True, open_browser: bool = True) -> int:
    from rich.console import Console

    from topstep_bot.keepawake import console_stays_responsive, keep_awake
    from topstep_bot.logging_setup import log_startup, setup_logging

    console = Console()
    setup_logging(cfg.log_dir, "INFO", console=console, file_prefix="controller", shared_files=False,
                  retention_days=cfg.log_retention_days,
                  secrets=[secrets.api_key, secrets.telegram_bot_token, secrets.discord_webhook_url,
                           secrets.github_token])
    cfg.mode = mode
    log_startup(cfg, f"controller ({mode})")
    state = load_state(cfg)
    state["mode"] = mode
    save_state(cfg, state)
    ctl = Controller(cfg, secrets, mode=mode, config_path=config_path)
    console.print(f"[bold]Dashboard:[/] {ctl.server.url}   (Ctrl+C here stops everything)")
    try:
        with keep_awake(cfg.service.keep_awake), console_stays_responsive():
            asyncio.run(ctl.run(start_bot=start_bot, open_browser=open_browser))
    except RuntimeError as exc:
        console.print(f"[red]{exc}[/]")
        return 1
    except KeyboardInterrupt:
        console.print("Stopped.")
    if ctl.exit_code:
        from topstep_bot.update_service import relaunch

        return relaunch(cfg, config_path, ctl.exit_code, secrets, out=console.print)
    return 0
