"""The controller: dashboard + Telegram + supervision, in its own process.

    you --> dashboard (http://127.0.0.1:8765) / Telegram --> controller --(local API)--> trading bot(s)

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
from datetime import datetime
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
from topstep_bot.web import HttpServer, Request, html_response

log = logging.getLogger("topstep_bot.controller")

BACKOFF = (10, 30, 60, 120, 300)
HEALTHY_AFTER = 600  # seconds of uptime that reset the crash backoff
STOP_TIMEOUT = 45  # seconds to wait for a graceful stop (it flattens first)
STATE_FILE = "controller.json"
MAIN = "main"  # the account name when config.yaml has no 'accounts' list
ALL = "all"
ALL_ALLOWED = {"pause", "resume", "flatten", "set_setting", "reset_settings"}  # actions that may target every account


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
    return cfg.account_names() if cfg.multi else [MAIN]


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
        own = cfg.profile(name).mode if cfg.multi else None
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
        data = Path(cfg.data_dir) / "accounts" / account if cfg.multi else Path(cfg.data_dir)  # = its account_dir
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
        self._client = httpx.AsyncClient(timeout=8)
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
                return f"{self.who} is already {self.state}."
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
            return f"Starting {self.who.lower()} in {self.mode.upper()} mode..."

    async def stop(self, source: str, intent: str = "stop") -> str:
        """Graceful stop: ask the bot to flatten and exit; force it only if it doesn't."""
        if not self.running:
            self.want_running = intent == "restart"
            return f"{self.who} is not running."
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
            self._event("info", f"{self.who}: daily maintenance restart")
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
            raise RuntimeError(f"{self.who} is not running - start it first.")
        try:
            r = await self._client.post(f"http://127.0.0.1:{self.port}/action/{name}", json=payload or {},
                                        headers={"X-Token": self.token})
        except httpx.HTTPError:
            raise RuntimeError(f"{self.who} is not ready yet (still starting?)") from None
        data = r.json()
        if not data.get("ok", False):
            raise ValueError(data.get("message") or "the bot refused the request")
        return data

    async def close(self) -> None:
        await self._client.aclose()


def summarize(snapshot: dict | None) -> dict | None:
    """The few numbers the all-accounts table shows."""
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

    Every method takes ``account``: an account name, "all" (where it makes sense), or None, which
    means "the only account" and is refused when there are several.
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
        return await self._each(account, lambda b: b.start(source))

    async def restart_bot(self, source: str, account: str | None = None) -> str:
        return await self._each(account, lambda b: b.restart(source))

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

    async def funded_text(self, account: str | None = None) -> str:
        return await self._text("funded_text", account)

    async def rules_text(self, account: str | None = None) -> str:
        """Rules for that account's stage when its bot runs, else every stage (works with all bots down)."""
        from topstep_bot.funded import rules_text

        bots = [b for b in self.ctl.pick(account) if b.running] if account not in (None, ALL) or not self.ctl.multi else []
        if bots:
            with contextlib.suppress(RuntimeError, ValueError):
                return self._tag(bots[0], (await bots[0].action("rules_text"))["text"])
        return rules_text(None)

    async def train(self, source: str, account: str | None = None) -> str:
        bot = self._one(account)
        return self._tag(bot, (await bot.action("train", {"source": source})).get("message") or "Done.")

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
    def __init__(self, cfg: BotConfig, secrets: Secrets, *, mode: str | None = None, modes: dict[str, str] | None = None,
                 config_path: str | None, worker_command: list[str] | None = None, port: int | None = None,
                 poll_seconds: float = 5.0):
        self.cfg = cfg
        self.secrets = secrets
        self.config_path = config_path
        self.events: deque[dict] = deque(maxlen=200)
        modes = modes or {}
        self.bots: dict[str, BotProcess] = {
            name: BotProcess(cfg, secrets, mode=modes.get(name) or mode or cfg.mode, config_path=config_path,
                             account=name, worker_command=worker_command, events=self.events, poll_seconds=poll_seconds)
            for name in account_list(cfg)
        }
        self.started_at = time.time()
        self.token = pysecrets.token_urlsafe(24)
        self.telegram = None
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
            ],
            token=self.token,
            name="dashboard",
        )

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
        if not name:
            if len(self.bots) == 1:
                return [self.bot]
            raise ValueError(f"Which account? Choose one of {', '.join(self.bots)} (or all)")
        if name not in self.bots:
            raise ValueError(f"No account named '{account}'. Accounts: {', '.join(self.bots)}")
        return [self.bots[name]]

    # ---------------------------------------------------------------- dashboard routes

    def _index(self, _: Request):
        return html_response(self._page.replace("__TOKEN__", self.token))

    async def _status(self, req: Request) -> dict:
        bots = list(self.bots.values())
        statuses = await asyncio.gather(*(b.status() if b.state in ("running", "starting") else _none() for b in bots))
        by_name = dict(zip(self.bots, statuses, strict=True))
        selected = req.query.get("account") or ""
        if selected not in self.bots:
            selected = self.bot.account
        data = by_name[selected]
        return {
            "multi": self.multi,
            "selected": selected,
            "accounts": [{**b.info(), "summary": summarize((by_name[b.account] or {}).get("bot"))} for b in bots],
            "controller": {**self.bots[selected].info(), "telegram": self.telegram is not None},
            "bot": data.get("bot") if data else None,
            "log": data.get("log") if data else None,
            "events": list(self.events)[:40],
        }

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
        return await fn("dashboard", account)

    async def _proxy(self, req: Request) -> dict | str:
        payload = req.json()
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

    async def _mode(self, req: Request) -> str:
        body = req.json()
        return await self.set_mode(str(body.get("mode", "")), str(body.get("confirm", "")), "dashboard",
                                   body.get("account"))

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
            bot._event("warning", f"{bot.who}: mode switched {old.upper()} -> {mode.upper()} by {source}", alert=True)
            if bot.running:
                await bot.restart(source)
                lines.append(f"{bot.who} switched to {mode.upper()} - restarting in {mode.upper()} mode.")
            else:
                lines.append(f"{bot.who}: mode set to {mode.upper()}. Press Start to run it.")
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

        tg = TelegramController(s.telegram_bot_token, s.telegram_chat_id, ProxyActions(self), self.cfg.telegram)
        try:
            await tg.start(announce=True)
        except Exception as exc:  # noqa: BLE001 - the dashboard still works without Telegram
            log.warning("Telegram control could not start: %s", exc)
            await tg.close()
            return
        self.telegram = tg
        spawn(tg.run(), name="telegram")

    async def start_bots(self, source: str = "controller start") -> None:
        """Start every account's bot, a few seconds apart (spreads out their logins and history downloads)."""
        for i, bot in enumerate(self.bots.values()):
            if i:
                await asyncio.sleep(self.cfg.multi_account.stagger_seconds)
            await bot.start(source)

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
        if start_bot:
            spawn(self.start_bots(), name="start-bots")
        if open_browser and self.cfg.dashboard.open_browser:
            webbrowser.open(self.server.url)
        try:
            await asyncio.Event().wait()
        finally:
            await self.shutdown()

    async def shutdown(self) -> None:
        log.info("Controller shutting down")
        await asyncio.gather(*(b.stop("controller shutdown") for b in self.bots.values() if b.running))
        if self.telegram:
            await self.telegram.send("⏹ The Topstep Bot controller was closed on the PC. Start it there to resume.")
            await self.telegram.close()
        await self.server.stop()
        for bot in self.bots.values():
            await bot.close()


async def _none() -> None:
    return None


def run_controller(cfg: BotConfig, secrets: Secrets, *, config_path: str | None, modes: dict[str, str],
                   start_bot: bool = True, open_browser: bool = True) -> int:
    from rich.console import Console

    from topstep_bot.keepawake import keep_awake
    from topstep_bot.logging_setup import log_startup, setup_logging

    console = Console()
    setup_logging(cfg.log_dir, "INFO", console=console, file_prefix="controller", shared_files=False,
                  retention_days=cfg.log_retention_days,
                  secrets=secrets.secret_values())
    listing = ", ".join(f"{name} {mode}" for name, mode in modes.items())
    log_startup(cfg, f"controller ({listing})")
    remember_modes(cfg, modes)
    ctl = Controller(cfg, secrets, modes=modes, config_path=config_path)
    console.print(f"[bold]Dashboard:[/] {ctl.server.url}   (Ctrl+C here stops everything)")
    if ctl.multi:
        console.print(f"Accounts: {listing}")
    try:
        with keep_awake(cfg.service.keep_awake):
            asyncio.run(ctl.run(start_bot=start_bot, open_browser=open_browser))
    except RuntimeError as exc:
        console.print(f"[red]{exc}[/]")
        return 1
    except KeyboardInterrupt:
        console.print("Stopped.")
    return 0
