"""The controller: dashboard + Telegram + supervision, in its own process.

    you --> dashboard (http://127.0.0.1:8765) / Telegram --> controller --(local API)--> trading bot(s)
    your phone (Telegram) --> phone link (phone_access.py, only while switched on) --> controller

Each trading bot runs as a separate child process - one per account when config.yaml lists
several accounts. If one crashes, hangs or is stopped, the controller and the other accounts keep
running, so the dashboard and Telegram stay online: you can see what happened (exit reason,
logs) and start, restart or stop any account's bot, or switch it between paper and live.

Every command targets one account by name, or "all" of them (pause/resume/flatten/stop/...).
With a single account (no 'accounts' list) it is simply called "main" and nothing needs a name.

Supervision rules, per account (see service.py for the exit codes):
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
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from importlib import resources
from pathlib import Path
from typing import Any

import httpx

from topstep_bot.config import BotConfig, Secrets
from topstep_bot.logging_setup import spawn, tail
from topstep_bot.phone_access import PhoneAccess
from topstep_bot.phone_access import describe as describe_phone
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
SLOW_ACTIONS = {"train": 600, "learn": 3600}  # training replays weeks of history; learning backfills and replays up to a year
STATE_FILE = "controller.json"
MAIN = "main"  # the account name when config.yaml has no 'accounts' list
ALL = "all"
ALL_ALLOWED = {"pause", "resume", "flatten", "set_setting", "reset_settings"}  # actions that may target every account


def source(req: Request) -> str:
    """Who asked, for the activity log."""
    return "phone (Telegram)" if req.remote else "dashboard"


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


def account_list(cfg: BotConfig) -> list[str]:
    """The accounts the controller runs: the enabled 'accounts', or just 'main'."""
    return (cfg.account_names() or [MAIN]) if cfg.multi else [MAIN]


def saved_modes(cfg: BotConfig) -> dict[str, str]:
    state = load_state(cfg)
    modes = dict(state.get("modes") or {})
    if "mode" in state and MAIN not in modes:  # written by v1.1
        modes[MAIN] = state["mode"]
    return modes


def resolve_modes(cfg: BotConfig, cli_mode: str | None = None, only: list[str] | None = None) -> dict[str, str]:
    """Mode per account: --mode (for ``only``, else all) > last chosen on the dashboard > the account's
    'mode' in config.yaml > the shared 'mode'."""
    saved = saved_modes(cfg)
    out = {}
    for name in account_list(cfg):
        if cli_mode and (only is None or name in only):
            out[name] = cli_mode
            continue
        own = cfg.profile(name).mode if cfg.multi and name != MAIN else None
        out[name] = saved.get(name) or own or cfg.mode
    return out


def resolve_mode(cfg: BotConfig, cli_mode: str | None) -> str:
    """Single-account shortcut kept for older callers."""
    return resolve_modes(cfg, cli_mode)[account_list(cfg)[0]]


def remember_modes(cfg: BotConfig, modes: dict[str, str]) -> None:
    state = load_state(cfg)
    state["modes"] = {**(state.get("modes") or {}), **modes}
    state.pop("mode", None)
    save_state(cfg, state)


# ------------------------------------------------------------------------------ bot process

class BotProcess:
    """Starts, watches and stops one account's trading bot child process."""

    def __init__(
        self,
        cfg: BotConfig,
        secrets: Secrets,
        *,
        mode: str,
        config_path: str | None,
        account: str = MAIN,
        worker_command: list[str] | None = None,
        events: deque | None = None,
        poll_seconds: float = 5.0,
    ):
        self.cfg = cfg
        self.secrets = secrets
        self.mode = mode
        self.account = account
        self.multi = cfg.multi
        self.config_path = config_path
        self.worker_command = worker_command
        self.poll_seconds = poll_seconds
        self.events: deque[dict] = events if events is not None else deque(maxlen=100)
        data = Path(cfg.data_dir) / "accounts" / account if self.multi else Path(cfg.data_dir)  # = its account_dir
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

    @property
    def who(self) -> str:
        """How this bot is named in messages: 'Bot' or 'Bot c1'."""
        return f"Bot {self.account}" if self.multi else "Bot"

    def _event(self, level: str, message: str, alert: bool = False) -> None:
        getattr(log, level if level in ("info", "warning", "error") else "info")(message)
        self.events.appendleft({"ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "level": level, "message": message,
                                "account": self.account})
        if alert:
            asyncio.get_running_loop().run_in_executor(None, notify, self.secrets, message)

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.returncode is None

    def command(self, quiet: bool) -> list[str]:
        extra = ["--account", self.account] if self.multi else []
        if self.worker_command is not None:  # tests: a stand-in worker
            return [*self.worker_command, "--mode", self.mode, *extra]
        cmd = [sys.executable, "-m", "topstep_bot"]
        if self.config_path:
            cmd += ["-c", str(self.config_path)]
        return cmd + ["run", "--yes", "--mode", self.mode, *extra]

    def info(self) -> dict:
        uptime = int(time.time() - self.started_at) if self.started_at and self.running else None
        return {
            "account": self.account,
            "state": self.state,
            "mode": self.mode,
            "pid": self.proc.pid if self.running else None,
            "uptime_s": uptime,
            "uptime": f"{uptime // 3600}h {uptime % 3600 // 60}m" if uptime is not None else "-",
            "restarts_last_hour": len([t for t in self.restarts if time.time() - t < 3600]),
            "last_exit": self.last_exit,
        }

    # ---------------------------------------------------------------- lifecycle

    async def start(self, source: str = "controller", quiet: bool = False) -> str:
        async with self._lock:
            if self.running:
                return f"{self.who if self.multi else 'The bot'} is already {self.state}."
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
            self._event("info", f"{self.who} starting in {self.mode.upper()} mode (pid {self.proc.pid}) - requested by {source}")
            self._watcher = spawn(self._watch(self.proc), name=f"bot-watch-{self.account}")
            return f"Starting {self.who.lower() if self.multi else 'the bot'} in {self.mode.upper()} mode..."

    async def stop(self, source: str, intent: str = "stop") -> str:
        """Graceful stop: ask the bot to flatten and exit; force it only if it doesn't."""
        if not self.running:
            self.want_running = intent == "restart"
            return f"{self.who} is not running." if self.multi else "The bot is not running."
        self._intent = intent
        self.want_running = intent == "restart"
        self.state = "restarting" if intent == "restart" else "stopping"
        self._event("warning", f"{self.who} {'restart' if intent == 'restart' else 'stop'} requested by {source}")
        with contextlib.suppress(RuntimeError, ValueError):  # not reachable yet (starting/hung): forced below
            await self.action("stop", {"source": source})
        proc = self.proc
        try:
            await asyncio.wait_for(proc.wait(), STOP_TIMEOUT)
        except TimeoutError:
            self._event("error", f"{self.who} did not stop in time - forcing it. Its protective stops stay at TopstepX.",
                        alert=True)
            proc.kill()
            await proc.wait()
        if self._watcher:
            await asyncio.wait([self._watcher], timeout=5)
        return f"{self.who} stopped." if intent == "stop" else f"{self.who} restarted."

    async def restart(self, source: str) -> str:
        if not self.running:
            return await self.start(source)
        await self.stop(source, intent="restart")
        return await self.start(source) if not self.running else f"{self.who} restarted."

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
            self._event("info", f"{self.who} stopped ({reason})")
        elif intent == "restart":
            self.state = "restarting"
        elif intent == "hang":
            await self._restart_after_failure("stopped responding (killed)", ran)
        elif code == RESTART_EXIT_CODE:
            self._event("info", f"{self.who}: daily maintenance restart" if self.multi else "Daily maintenance restart")
            self.state = "restarting"
            await self.start("daily maintenance", quiet=True)
        elif code == 0:
            self.want_running = False
            self.state = "stopped"
            self._event("warning", f"{self.who} stopped: {reason}", alert=True)
        elif code == CONFIG_ERROR_EXIT_CODE and ran < 60:
            self.want_running = False
            self.state = "failed"
            self._event("error", f"{self.who} could not start: {reason}", alert=True)
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
            self._event("error", f"{self.who} {what}", alert=True)
            return
        if len(self.restarts) >= self.cfg.service.max_restarts_per_hour:
            self.state = "failed"
            self.want_running = False
            self._event("error", f"{self.who} {what}. Too many restarts in the last hour - giving up. Check the Logs tab.",
                        alert=True)
            return
        wait = BACKOFF[min(max(self.failures - 1, 0), len(BACKOFF) - 1)]
        self.state = "crashed"
        self._event("error", f"{self.who} {what}; restarting in {wait}s", alert=True)
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
                self._event("info", f"{self.who} is running ({self.mode.upper()})")
            if self.state not in ("running", "starting") or time.time() - (self.started_at or 0) < timeout:
                continue
            try:
                age = time.time() - self.heartbeat.stat().st_mtime
            except FileNotFoundError:
                age = time.time() - (self.started_at or time.time())
            if age > timeout:
                self._event("error", f"{self.who} has not responded for {age:.0f}s - restarting it", alert=True)
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
            raise RuntimeError(f"{self.who if self.multi else 'The bot'} is not running - start it first.")
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


def summarize(snapshot: dict | None) -> dict | None:
    """The few numbers the all-accounts cards show."""
    if not snapshot:
        return None
    r = snapshot.get("risk") or {}
    return {k: snapshot.get(k) for k in ("account", "mode", "plan", "contract", "strategy", "timeframe", "balance",
                                         "open_pnl", "position", "halted", "profit_target", "total_profit", "connected")} | {
        "day_pnl": r.get("day_pnl"), "paused": r.get("paused"), "locked": r.get("locked"), "mll_room": r.get("mll_room"),
        "trades_today": r.get("trades_today"), "max_trades": r.get("max_trades"),
    }


# ------------------------------------------------------------------------------ Telegram adapter

class ProxyActions:
    """What Telegram can do, implemented through the controller (works even when bots are down).

    Account-level methods take ``account``: an account name, "all" (where it makes sense), or None,
    which means "the only account" and is refused when there are several.
    """

    def __init__(self, ctl: Controller):
        self.ctl = ctl

    def account_names(self) -> list[str]:
        """Empty with a single account, so Telegram never asks 'which account?'."""
        return list(self.ctl.bots) if self.ctl.multi else []

    def _one(self, account: str | None) -> BotProcess:
        if account == ALL:
            raise ValueError("Pick one account for this, e.g. " + ", ".join(self.ctl.bots))
        return self.ctl.pick(account)[0]

    def _tag(self, bot: BotProcess, text: str) -> str:
        return f"[{bot.account}] {text}" if self.ctl.multi else text

    async def _each(self, account: str | None, fn: Callable[[BotProcess], Awaitable[str]]) -> str:
        """Run ``fn`` on each targeted bot; one line per account."""
        bots = self.ctl.pick(account)
        if len(bots) == 1:
            return await fn(bots[0])
        lines = []
        for bot in bots:
            try:
                lines.append(self._tag(bot, await fn(bot)))
            except (RuntimeError, ValueError) as exc:
                lines.append(self._tag(bot, f"❌ {exc}"))
        return "\n".join(lines)

    async def _text(self, name: str, account: str | None) -> str:
        bot = self._one(account)
        if not bot.running:
            return self.ctl.offline_text(bot)
        try:
            return self._tag(bot, (await bot.action(name))["text"])
        except (RuntimeError, ValueError) as exc:
            return self._tag(bot, str(exc))

    async def _msg(self, name: str, source: str, account: str | None, **payload: Any) -> str:
        async def one(bot: BotProcess) -> str:
            return (await bot.action(name, {"source": source, **payload})).get("message") or "Done."
        return await self._each(account, one)

    async def pause(self, source: str, account: str | None = None) -> str:
        return await self._msg("pause", source, account)

    async def resume(self, source: str, account: str | None = None) -> str:
        return await self._msg("resume", source, account)

    async def flatten(self, source: str, account: str | None = None) -> str:
        return await self._msg("flatten", source, account)

    async def stop(self, source: str, account: str | None = None) -> str:
        return await self._each(account, lambda b: b.stop(source))

    async def start_bot(self, source: str, account: str | None = None) -> str:
        return await self._each(account, lambda b: self.ctl.start_one(b, source))

    async def restart_bot(self, source: str, account: str | None = None) -> str:
        return await self._each(account, lambda b: self.ctl.restart_one(b, source))

    async def status_text(self, account: str | None = None) -> str:
        if self.ctl.multi and account in (None, ALL):
            return await self.ctl.accounts_text()
        bot = self._one(account)
        if not bot.running or bot.state != "running":
            return self.ctl.offline_text(bot)
        return await self._text("status_text", account) + f"\nBot process: {bot.state}, uptime {bot.info()['uptime']}"

    async def ideas_text(self, account: str | None = None) -> str:
        return await self._text("ideas_text", account)

    async def trades_text(self, account: str | None = None) -> str:
        return await self._text("trades_text", account)

    async def log_text(self, account: str | None = None) -> str:
        return await self._text("log_text", account)

    async def settings_text(self, account: str | None = None) -> str:
        return await self._text("settings_text", account)

    async def knowledge_text(self, account: str | None = None) -> str:
        return await self._text("knowledge_text", account)

    async def brief_text(self, account: str | None = None) -> str:
        return await self._text("brief_text", account)

    async def next_text(self, account: str | None = None) -> str:
        return await self._text("next_text", account)

    async def market_text(self, account: str | None = None) -> str:
        return await self._text("market_text", account)

    async def funded_text(self, account: str | None = None) -> str:
        return await self._text("funded_text", account)

    async def rules_text(self, account: str | None = None) -> str:
        """The rules for an account, straight from config.yaml - works with every bot down."""
        from topstep_bot.risk.summary import rules_text

        if self.ctl.multi and account in (None, ALL):
            return "\n\n".join(self._tag(self.ctl.bots[n], rules_text(self.ctl.account_cfg(n))) for n in self.ctl.bots)
        bot = self._one(account)
        return self._tag(bot, rules_text(self.ctl.account_cfg(bot.account)))

    async def research_text(self, account: str | None = None) -> str:
        if self.ctl.research is None:
            return "Research is turned off (research.enabled: false)."
        return self.ctl.research.text()

    async def train(self, source: str, account: str | None = None) -> str:
        bot = self._one(account)
        return self._tag(bot, (await bot.action("train", {"source": source})).get("message") or "Done.")

    async def learn(self, source: str, account: str | None = None) -> str:
        bot = self._one(account)
        return self._tag(bot, (await bot.action("learn", {"source": source})).get("message") or "Done.")

    def dashboard_info(self) -> dict:
        info = self.ctl.phone.status()
        return {**info, "text": describe_phone(info)}

    async def phone_on(self, source: str) -> str:
        return await self.ctl.phone.enable(source)

    async def phone_off(self, source: str) -> str:
        return await self.ctl.phone.disable(source)

    async def check_update(self, source: str) -> dict:
        return await self.ctl.updates.telegram_check(source)

    async def install_update(self, source: str, when: str = "now") -> str:
        return await self.ctl.updates.install(source, when)

    async def preview_setting(self, key: str, value: Any, account: str | None = None) -> dict:
        running = [b for b in self.ctl.pick(account) if b.running]
        if not running:
            raise RuntimeError("No targeted bot is running - start it first.")
        return await running[0].action("preview_setting", {"key": key, "value": value})

    async def change_setting(self, key: str, value: Any, source: str, account: str | None = None) -> str:
        return await self._msg("set_setting", source, account, key=key, value=value)

    async def reset_settings(self, source: str, account: str | None = None) -> str:
        return await self._msg("reset_settings", source, account)

    async def take_idea(self, rec_id: str, source: str, size: int | None = None, account: str | None = None) -> str:
        bot = self._one(account)
        return self._tag(bot, (await bot.action("take_idea", {"source": source, "id": rec_id, "size": size}))
                         .get("message") or "Done.")

    async def open_ideas(self, account: str | None = None) -> list[dict]:
        bot = self._one(account)
        if not bot.running:
            return []
        try:
            return (await bot.action("open_ideas"))["items"]
        except (RuntimeError, ValueError):
            return []

    async def find_idea(self, rec_id: str, account: str | None = None) -> dict | None:
        try:
            return (await self._one(account).action("find_idea", {"id": rec_id}))["item"]
        except (RuntimeError, ValueError):
            return None


# ------------------------------------------------------------------------------ controller

class Controller:
    telegram_api = "https://api.telegram.org"

    def __init__(self, cfg: BotConfig, secrets: Secrets, *, mode: str | None = None, modes: dict[str, str] | None = None,
                 config_path: str | None, worker_command: list[str] | None = None, port: int | None = None,
                 poll_seconds: float = 5.0, config_error: str | None = None):
        self.cfg = cfg
        self.secrets = secrets
        self.config_path = config_path
        self.events: deque[dict] = deque(maxlen=200)
        self._page_errors: deque[tuple[float, str]] = deque()  # (when, message) reported by open dashboards
        modes = modes or {}
        self.worker_command = worker_command
        self.poll_seconds = poll_seconds
        self.bots: dict[str, BotProcess] = {
            name: BotProcess(cfg, secrets, mode=modes.get(name) or mode or cfg.mode, config_path=config_path,
                             account=name, worker_command=worker_command, events=self.events, poll_seconds=poll_seconds)
            for name in account_list(cfg)
        }
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
                ("POST", "/api/bot/start", lambda r: self._lifecycle(r, "start")),
                ("POST", "/api/bot/stop", lambda r: self._lifecycle(r, "stop")),
                ("POST", "/api/bot/restart", lambda r: self._lifecycle(r, "restart")),
                ("POST", "/api/mode", self._mode),
                ("POST", "/api/action/*", self._proxy),
                ("POST", "/api/updates/check", lambda r: self.updates.check_text(source(r))),
                ("POST", "/api/updates/install", lambda r: self.updates.install(source(r), str(r.json().get("when", "now")))),
                ("POST", "/api/updates/cancel", lambda r: self.updates.cancel(source(r))),
                ("POST", "/api/setup/state", lambda r: self.setup.state()),
                ("POST", "/api/setup/login", lambda r: self.setup.login(r.json())),
                ("POST", "/api/setup/save", lambda r: self.setup.save(r.json())),
                ("POST", "/api/setup/telegram/find", lambda r: self.setup.telegram_find(r.json())),
                ("POST", "/api/setup/telegram/save", lambda r: self.setup.telegram_save(r.json())),
                ("POST", "/api/setup/telegram/test", lambda r: self.setup.telegram_test()),
                ("POST", "/api/setup/telegram/control", lambda r: self.setup.telegram_control(r.json())),
                ("POST", "/api/setup/telegram/remove", lambda r: self.setup.telegram_remove()),
                ("POST", "/api/setup/discord", lambda r: self.setup.discord_save(r.json())),
                ("POST", "/api/setup/github_token", lambda r: self.setup.github_token(r.json())),
                ("POST", "/api/setup/autostart", lambda r: self.setup.autostart(r.json())),
                ("POST", "/api/phone/on", lambda r: self.phone.enable(source(r))),
                ("POST", "/api/phone/off", lambda r: self.phone.disable(source(r), wait=not r.remote)),
                ("POST", "/api/page-error", self._page_error),
            ],
            token=self.token,
            name="dashboard",
        )
        self.instance = pysecrets.token_hex(4)  # changes on every start: an open dashboard reloads itself
        self.exit_code = 0
        self._exit = asyncio.Event()
        from topstep_bot.setup_service import SetupService
        from topstep_bot.update_service import UpdateService

        self.updates = UpdateService(self)
        self.setup = SetupService(self, config_error)
        self.research = self._make_research()
        self._research_task: asyncio.Task | None = None
        self.phone = PhoneAccess(self)  # after the dashboard's routes: the phone link serves some of them

    # ---------------------------------------------------------------- accounts

    @property
    def multi(self) -> bool:
        return self.cfg.multi

    @property
    def bot(self) -> BotProcess:
        """The first (with a single account: the only) bot."""
        return next(iter(self.bots.values()))

    def pick(self, account: str | None) -> list[BotProcess]:
        """Bots targeted by ``account``: a name, "all", or None (allowed only with a single account)."""
        name = (account or "").strip().lower()
        if name == ALL:
            return list(self.bots.values())
        if not name or (not self.multi and name == MAIN):
            if len(self.bots) == 1:
                return [self.bot]
            raise ValueError(f"Which account? Choose one of {', '.join(self.bots)} (or all)")
        if name not in self.bots:
            raise ValueError(f"No account named '{account}'. Accounts: {', '.join(self.bots)}")
        return [self.bots[name]]

    def account_cfg(self, name: str) -> BotConfig:
        """One account's effective settings (the shared settings with a single account)."""
        return self.cfg.for_account(name if self.multi else None)

    def _make_research(self):
        """The research collector (research.py) for every market the accounts trade, or None if turned off."""
        if not self.cfg.research.enabled:
            return None
        from topstep_bot.research import Research

        symbols = set()
        for name in self.bots:
            with contextlib.suppress(ValueError):
                symbols.add(self.account_cfg(name).instrument.symbol)
        return Research(self.cfg, Path(self.cfg.data_dir) / "research", sorted(symbols or {self.cfg.instrument.symbol}),
                        alert=lambda text: self.bot._event("warning", text, alert=True))

    # ---------------------------------------------------------------- dashboard routes

    def page(self, token: str, view: str = "pc") -> str:
        """The dashboard page for a viewer: on this PC, or on the phone through Telegram."""
        return self._page.replace("__TOKEN__", token).replace("__VIEW__", view)

    def _index(self, _: Request):
        return html_response(self.page(self.token))

    def _page_error(self, req: Request) -> str:
        """An open dashboard reports that its own script failed, so it shows in the logs (a phone has no console)."""
        body = req.json()
        message = " ".join(str(body.get("message", "")).split())[:300] or "unknown problem"
        now = time.time()
        while self._page_errors and now - self._page_errors[0][0] > 600:
            self._page_errors.popleft()
        if len(self._page_errors) >= 10 or any(m == message for _, m in self._page_errors):
            return "already noted"
        self._page_errors.append((now, message))
        where = "on your phone" if req.remote else "on this PC"
        self.bot._event("warning", f"The dashboard {where} hit a problem showing the page: {message}")
        log.info("That dashboard runs in: %s", " ".join(str(body.get("browser", "")).split())[:200] or "?")
        return "noted"

    async def _status(self, req: Request) -> dict:
        bots = list(self.bots.values())
        statuses = await asyncio.gather(*(b.status() if b.state in ("running", "starting") else _none() for b in bots))
        by_name = dict(zip(self.bots, statuses, strict=True))
        selected = req.query.get("account") or ""
        if selected not in self.bots:
            selected = self.bot.account
        data = by_name[selected]
        sel = self.bots[selected]
        return {
            "multi": self.multi,
            "selected": selected,
            "accounts": [{**b.info(), "summary": summarize((by_name[b.account] or {}).get("bot"))} for b in bots],
            "controller": {**sel.info(), "uptime": sel.info()["uptime"], "telegram": self.telegram is not None},
            "bot": data.get("bot") if data else None,
            "log": data.get("log") if data else None,
            "events": list(self.events)[:40],
            "instance": self.instance,
            "updates": self.updates.status(),
            "phone": self.phone.status(),
            "viewer": "phone" if req.remote else "pc",
            "setup": self.setup.summary(),
            "research": self.research.status() if self.research else None,
            # market / trading-day countdowns: computed here so they show even while the bot is stopped
            "clock": self._clock(selected),
        }

    def _clock(self, account: str | None = None) -> dict:
        from topstep_bot.instruments import get_spec
        from topstep_bot.strategies.base import parse_hhmm

        cfg = self.cfg
        if self.multi and account:
            with contextlib.suppress(ValueError):
                cfg = self.account_cfg(account)
        symbol = cfg.instrument.symbol
        try:
            spec = get_spec(symbol)
            rth = (parse_hhmm(spec.rth_open), parse_hhmm(spec.rth_close))
        except KeyError:  # a symbol outside the built-in table: index-futures hours
            rth = (parse_hhmm("08:30"), parse_hhmm("15:00"))
        return SessionSchedule(cfg.session).clock(datetime.now(timezone.utc), rth, symbol.upper())

    def _logs(self, req: Request) -> dict:
        base = Path(self.cfg.log_dir)
        account = req.query.get("account") or self.bot.account
        log_dir = base / "accounts" / account if self.multi and account in self.bots else base
        crashes = sorted(log_dir.glob("crash_*.txt"))
        return {
            "errors": "".join(tail(log_dir / "errors.log", 60)),
            "bot": "".join(tail(log_dir / "bot.log", 60)),
            "controller": "".join(tail(base / "controller.log", 40)),
            "crash": "".join(tail(crashes[-1], 40)) if crashes else "",
            "folder": str(log_dir),
        }

    async def _lifecycle(self, req: Request, what: str) -> str:
        account = req.json().get("account")
        proxy = ProxyActions(self)
        fn = {"start": proxy.start_bot, "stop": proxy.stop, "restart": proxy.restart_bot}[what]
        return await fn(source(req), account)

    async def _proxy(self, req: Request) -> dict | str:
        payload = req.json()
        if req.remote:
            payload["source"] = source(req)
        payload.setdefault("source", "dashboard")
        account = payload.pop("account", None)
        bots = self.pick(account)
        if len(bots) > 1:
            if req.param not in ALL_ALLOWED:
                raise ValueError("That can only be done for one account at a time")
            return await ProxyActions(self)._msg(req.param, payload.pop("source"), account, **payload)
        data = await bots[0].action(req.param, payload)
        data.pop("ok", None)
        return data

    async def start_one(self, bot: BotProcess, source: str) -> str:
        """Start one account's bot, unless setup isn't finished (then say what is missing)."""
        if not bot.running and (blocker := self.setup.blocker()):
            raise ValueError(blocker)
        return await bot.start(source)

    async def restart_one(self, bot: BotProcess, source: str) -> str:
        if not bot.running:
            return await self.start_one(bot, source)
        return await bot.restart(source)

    async def start_bot(self, source: str, account: str | None = None) -> str:
        return await ProxyActions(self).start_bot(source, account)

    async def restart_bot(self, source: str, account: str | None = None) -> str:
        return await ProxyActions(self).restart_bot(source, account)

    async def _mode(self, req: Request) -> str:
        body = req.json()
        if req.remote and str(body.get("mode", "")) == "live":
            raise ValueError("Switching to LIVE (real orders) can only be done on the PC. Paper mode can be chosen here.")
        return await self.set_mode(str(body.get("mode", "")), str(body.get("confirm", "")), source(req), body.get("account"))

    async def set_mode(self, mode: str, confirm: str, source: str, account: str | None = None) -> str:
        if mode not in ("paper", "live"):
            raise ValueError("mode must be paper or live")
        bots = [b for b in self.pick(account) if b.mode != mode]
        if not bots:
            return f"Already in {mode.upper()} mode."
        if mode == "live" and confirm.strip().upper() != "LIVE":
            raise ValueError("Type LIVE to confirm switching to live trading with real orders.")
        for bot in bots:
            status = await bot.status()
            snap = status.get("bot") if status else None
            if snap and (snap.get("position") or snap.get("trade")):
                raise ValueError(f"A trade is open{f' on {bot.account}' if self.multi else ''}. "
                                 "Close it first (Flatten), then switch modes.")
        lines = []
        for bot in bots:
            old, bot.mode = bot.mode, mode
            prefix = f"{bot.who}: mode" if self.multi else "Mode"
            bot._event("warning", f"{prefix} switched {old.upper()} -> {mode.upper()} by {source}", alert=True)
            if bot.running:
                await bot.restart(source)
                lines.append(f"{bot.who + ' switched' if self.multi else 'Switched'} to {mode.upper()} - "
                             f"the bot is restarting in {mode.upper()} mode.")
            else:
                lines.append(f"{prefix} set to {mode.upper()}. Press Start to run the bot.")
        remember_modes(self.cfg, {b.account: b.mode for b in self.bots.values()})
        return "\n".join(lines)

    # ---------------------------------------------------------------- text helpers

    def uptime_text(self) -> str:
        return self.bot.info()["uptime"]

    def offline_text(self, bot: BotProcess | None = None) -> str:
        b = bot or self.bot
        lines = [f"🤖 {b.who} is {b.state.upper()} ({b.mode.upper()} mode)."]
        if b.last_exit:
            lines.append(f"Last exit: {b.last_exit['reason']} (code {b.last_exit['code']}, {b.last_exit['at']})")
        target = f" {b.account}" if self.multi else ""
        if b.state in ("stopped", "crashed", "failed"):
            lines.append(f"Send /startbot{target} to start it, or check the dashboard's Logs tab.")
        elif b.state in ("starting", "restarting"):
            lines.append("It is starting up - try again in a few seconds.")
        return "\n".join(lines)

    async def accounts_text(self) -> str:
        """One line per account (Telegram /status and /accounts with several accounts)."""
        lines = ["Accounts:"]
        for bot in self.bots.values():
            status = await bot.status() if bot.state == "running" else None
            s = summarize(status.get("bot") if status else None)
            if s is None:
                lines.append(f"{bot.account}: {bot.state.upper()} ({bot.mode})")
                continue
            pos = "flat" if not s["position"] else f"{'long' if s['position'] > 0 else 'short'} {abs(s['position'])}"
            flag = " HALTED" if s["halted"] else " paused" if s["paused"] else " done for today" if s["locked"] else ""
            day = s["day_pnl"] or 0
            lines.append(f"{bot.account}: {'🔴 LIVE' if s['mode'] == 'live' else '🔵 paper'} {s['contract']} {s['strategy']} | "
                         f"today {'-' if day < 0 else ''}${abs(day):,.0f} | {pos}{flag}")
        lines.append("Details: /status <account>. Target any command: /pause c1, /pause all ...")
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

    async def stop_telegram(self) -> None:
        if self._telegram_task:  # stop polling before its HTTP client is closed below
            self._telegram_task.cancel()
            await asyncio.wait([self._telegram_task], timeout=5)
            self._telegram_task = None
        if self.telegram:
            tg, self.telegram = self.telegram, None
            await tg.close()

    async def restart_telegram(self) -> None:
        """Pick up a changed Telegram setup (Setup tab) without restarting the server."""
        await self.stop_telegram()
        await self.start_telegram()
        await self.phone.telegram_changed()

    async def start_bots(self, source: str = "controller start", only: set[str] | None = None) -> None:
        """Start every account's bot (or those in ``only``), a few seconds apart (spreads out their logins
        and history downloads)."""
        first = True
        for bot in list(self.bots.values()):
            if only is not None and bot.account not in only:
                continue
            if not first:
                await asyncio.sleep(self.cfg.multi_account.stagger_seconds)
            first = False
            try:
                await self.start_one(bot, source)
            except ValueError as exc:
                bot._event("warning", f"{bot.who if self.multi else 'The bot'} was not started: {exc}")

    async def run(self, start_bot: bool = True, open_browser: bool = True) -> None:
        try:
            await self.server.start()
        except OSError:
            raise RuntimeError(
                f"Port {self.cfg.dashboard.port} is busy - is the bot already running? Open {self.server.url}"
            ) from None
        log.info("Dashboard: %s", self.server.url)
        for bot in self.bots.values():
            spawn(bot.monitor(), name=f"bot-monitor-{bot.account}")
        await self.start_telegram()
        await self.phone.start()
        if start_bot:
            # After an update restart, only the accounts whose bots were running come back.
            plan = load_state(self.cfg).get("restart") or {}
            only = set(plan["bots"]) if self.multi and isinstance(plan.get("bots"), list) else None
            await self.start_bots(only=only)
        await self.updates.start()
        if self.research is not None:
            self._research_task = spawn(self.research.run(), name="research")
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
        if self._research_task:
            self._research_task.cancel()
            await asyncio.wait([self._research_task], timeout=5)
        await asyncio.gather(*(b.stop("controller shutdown") for b in self.bots.values() if b.running))
        if self._telegram_task:  # stop polling before its HTTP client is closed below
            self._telegram_task.cancel()
            await asyncio.wait([self._telegram_task], timeout=5)
        await self.phone.close()  # also puts Telegram's menu button back
        if self.telegram:
            await self.telegram.send("⏹ The Topstep Bot controller was closed on the PC. Start it there to resume.")
            await self.telegram.close()
        await self.server.stop()
        for bot in self.bots.values():
            await bot.close()


async def _none() -> None:
    return None


def run_controller(cfg: BotConfig, secrets: Secrets, *, config_path: str | None, modes: dict[str, str] | None = None,
                   mode: str | None = None, start_bot: bool = True, open_browser: bool = True,
                   config_error: str | None = None) -> int:
    from rich.console import Console
    from rich.panel import Panel

    from topstep_bot.keepawake import console_stays_responsive, keep_awake
    from topstep_bot.logging_setup import log_startup, setup_logging

    console = Console()
    setup_logging(cfg.log_dir, "INFO", console=console, file_prefix="controller", shared_files=False,
                  retention_days=cfg.log_retention_days, secrets=secrets.secret_values())
    modes = modes or resolve_modes(cfg, mode)
    if not cfg.multi:
        cfg.mode = modes.get(MAIN, cfg.mode)
    listing = ", ".join(f"{name} {m}" for name, m in modes.items())
    log_startup(cfg, f"controller ({listing})")
    remember_modes(cfg, modes)
    ctl = Controller(cfg, secrets, modes=modes, config_path=config_path, config_error=config_error)
    console.print(Panel.fit(
        f"[bold]Topstep Bot is running.[/] Dashboard: [bold cyan]{ctl.server.url}[/]\n"
        "Set up, start and stop the bot there - it opens in your browser by itself.\n"
        + (f"Accounts: {listing}\n" if ctl.multi else "")
        + "[dim]Keep this window open (minimise it if you like). Closing it, or Ctrl+C, stops the bot and the dashboard.[/]",
        border_style="cyan"))
    if config_error:
        console.print(f"[yellow]config.yaml has a problem - fix it on the dashboard's Setup tab:[/]\n{config_error}")
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
