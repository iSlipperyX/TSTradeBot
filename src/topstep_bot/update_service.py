"""Updates inside the controller: when to check, when it is safe to install, and the restart after.

  * Checks GitHub shortly after the controller starts and then every ``updates.check_every_hours``.
    A new version is announced once (dashboard banner, Telegram with a button, Discord).
  * Installs **only when you confirm** (dashboard, Telegram /update, or the menu on the PC), and
    only while no trade or order is open: the bot is paused, checked flat (in live mode at
    TopstepX too), then stopped before a single file changes. "After the close" waits for the
    first quiet moment outside the trading hours instead.
  * After installing, the controller exits with UPDATE_EXIT_CODE and the process that started it
    starts the new version (same mode; the bot only if it was running). If the new version fails
    to start, the previous one is restored and started again, and you get an alert.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import subprocess
import sys
import time
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from topstep_bot import __version__
from topstep_bot.logging_setup import spawn
from topstep_bot.service import notify
from topstep_bot.sessions import SessionSchedule
from topstep_bot.updater import UpdateError, UpdateInfo, Updater, cached_info, describe, load_state, save_state

if TYPE_CHECKING:
    from topstep_bot.config import BotConfig
    from topstep_bot.controller import Controller

log = logging.getLogger("topstep_bot.updates")

UPDATE_EXIT_CODE = 76  # controller -> the process that started it: "start me again, with the new code"
RELAUNCHED_ENV = "TOPSTEP_BOT_RELAUNCHED"
QUIET_BEFORE = timedelta(minutes=15)  # "trading hours" start this long before session.trade_start...
QUIET_AFTER = timedelta(minutes=10)  # ...and end this long after session.flatten_at
RESTART_DELAY = 3.0  # seconds between "installed" and the restart, so the reply reaches you first
STARTUP_GRACE = 120  # a new version that exits with an error this soon after starting is rolled back
UTC = timezone.utc
UNSUPPORTED = ("Updates need the bot's own folder (the one with start.bat). This copy was installed as a Python package - "
               "update it with pip instead.")


class UpdateService:
    def __init__(
        self,
        ctl: Controller,
        *,
        updater: Updater | None = None,
        broker_check: Callable[[], Awaitable[None]] | None = None,
        clock: Callable[[], datetime] | None = None,
        first_check_delay: float = 60.0,
        poll_seconds: float = 60.0,
    ):
        self.ctl = ctl
        self.cfg = ctl.cfg.updates
        self.updater = updater if updater is not None else Updater.for_config(ctl.cfg, token=ctl.secrets.github_token)
        self.state_dir = Path(ctl.cfg.data_dir)
        self.schedule = SessionSchedule(ctl.cfg.session)
        self.clock = clock or (lambda: datetime.now(UTC))
        self._broker_check = broker_check or self._check_broker_flat
        self.first_check_delay = first_check_delay
        self.poll_seconds = poll_seconds
        self.info: UpdateInfo | None = cached_info(self.state_dir)
        self.busy: str | None = None  # checking | installing | restarting
        self.message: str | None = None  # how the last install went
        self.scheduled: dict | None = load_state(self.state_dir).get("scheduled")
        self.next_check: datetime | None = None
        self._lock = asyncio.Lock()
        self._waiting_reason: str | None = None

    @property
    def supported(self) -> bool:
        return self.updater is not None

    # ---------------------------------------------------------------- state for the dashboard

    def status(self) -> dict:
        info = self.info
        return {
            "supported": self.supported,
            "auto_check": self.cfg.enabled,
            "every_hours": self.cfg.check_every_hours,
            "method": self.updater.method if self.updater else None,
            "repo": self.cfg.repo,
            "branch": self.cfg.branch,
            "version": __version__,
            "token": bool(self.updater and self.updater.token),
            "busy": self.busy,
            "message": self.message,
            "info": info.to_dict() if info else None,
            "headline": info.headline() if info else None,
            "scheduled": self.scheduled,
            "quiet": self.quiet(),
            "next_check": self.next_check.isoformat(timespec="minutes") if self.next_check else None,
        }

    def quiet(self, now: datetime | None = None) -> bool:
        """True outside the trading hours (with a margin), when restarting the bot costs nothing."""
        local = self.schedule.local(now or self.clock())
        day = local.date()
        if not self.schedule.is_trade_day(day):
            return True
        start = self.schedule.at(day, self.ctl.cfg.session.trade_start) - QUIET_BEFORE
        end = self.schedule.flatten_time(day) + QUIET_AFTER
        return not start <= local < end

    def _save(self, **changes: Any) -> None:
        state = load_state(self.state_dir)
        for key, value in changes.items():
            if value is None:
                state.pop(key, None)
            else:
                state[key] = value
        save_state(self.state_dir, state)

    def _require(self) -> Updater:
        if self.updater is None:
            raise ValueError(UNSUPPORTED)
        return self.updater

    # ---------------------------------------------------------------- check

    async def check(self, source: str = "dashboard") -> UpdateInfo:
        updater = self._require()
        previous, self.busy = self.busy, self.busy or "checking"
        try:
            info = await asyncio.to_thread(updater.check)
        finally:
            self.busy = previous
        self.info = info
        if info.error:
            log.warning("Update check (%s) failed: %s", source, info.error)
        return info

    async def check_text(self, source: str) -> str:
        info = await self.check(source)
        if info.error:
            raise ValueError(info.headline())
        return info.headline()

    async def telegram_check(self, source: str) -> dict:
        """For Telegram /update: what changed, and whether it can be installed now."""
        if not self.supported:
            return {"text": UNSUPPORTED, "can_install": False, "quiet": True}
        info = await self.check(source)
        text = describe(info)
        if self.scheduled and info.available:
            text += "\n\n⏳ Already scheduled to install after the close. Send /update again to install it now instead."
        return {"text": text, "can_install": info.can_install, "quiet": self.quiet()}

    # ---------------------------------------------------------------- install

    async def install(self, source: str, when: str = "now") -> str:
        updater = self._require()
        if self.busy in ("installing", "restarting"):
            raise ValueError("An update is already being installed.")
        async with self._lock:
            info = await self.check(source)
            if info.error:
                raise ValueError(f"Could not check GitHub: {info.error}")
            if not info.available:
                self._unschedule()
                return "Already up to date - nothing to install."
            if info.problem:
                raise ValueError(f"The update can't be installed yet: {info.problem}.")
            if when == "tonight" and not self.quiet():
                self.scheduled = {"sha": info.latest, "by": source, "at": datetime.now().isoformat(timespec="minutes")}
                self._save(scheduled=self.scheduled)
                self.ctl.bot._event("info", f"Update scheduled for after the close by {source}")
                return ("Scheduled: the update installs after today's trading, once the bot is flat. "
                        "Cancel it any time on the dashboard (Settings).")
            return await self._install_now(updater, info, source)

    async def cancel(self, source: str) -> str:
        if not self.scheduled:
            return "No update is scheduled."
        self._unschedule()
        self.ctl.bot._event("info", f"Scheduled update cancelled by {source}")
        return "The scheduled update was cancelled. Nothing was changed."

    def _unschedule(self) -> None:
        if self.scheduled:
            self.scheduled = None
            self._save(scheduled=None)

    async def _install_now(self, updater: Updater, info: UpdateInfo, source: str) -> str:
        bot = self.ctl.bot
        self.busy, self.message = "installing", None
        try:
            await self._ensure_flat(source)
            restart_bot = bot.running or bot.want_running
            mode = bot.mode
            label = f"{(info.current or 'unknown')[:7]} -> {info.latest[:7]}"
            bot._event("warning", f"Installing update {label} - requested by {source}")
            bot.want_running = False  # no automatic restart of the old version meanwhile
            if bot.running:
                await bot.stop(f"update ({source})")
            try:
                record = await asyncio.to_thread(updater.install, info, self.ctl.config_path)
            except Exception as exc:  # noqa: BLE001 - whatever went wrong, the old version must run again
                if not isinstance(exc, UpdateError):
                    log.exception("Update failed unexpectedly")
                self.message = f"Update failed: {exc}"
                bot._event("error", self.message, alert=True)
                if restart_bot:
                    await bot.start("update undone")
                raise ValueError(self.message) from None
        except BaseException:
            self.busy = None
            raise
        self._unschedule()
        self._save(restart={"mode": mode, "bot": restart_bot, "at": time.time(), "announce": updated_text(info, record["to"])})
        self.busy, self.message = "restarting", "Update installed - restarting with the new version."
        bot._event("info", f"Update installed ({label}); restarting the controller with the new version")
        asyncio.get_running_loop().call_later(RESTART_DELAY, lambda: spawn(self._restart(), name="update-restart"))
        parts = [*(["the bot"] if restart_bot else []), "the dashboard", *(["Telegram"] if self.ctl.telegram else [])]
        what = ", ".join(parts[:-1]) + " and " + parts[-1] if len(parts) > 1 else parts[0]
        verb = "restart" if len(parts) > 1 else "restarts"
        return f"Update installed. {what[0].upper()}{what[1:]} {verb} with the new version now - back in about 30 seconds."

    async def _ensure_flat(self, source: str) -> None:
        """Pause new entries, then make sure no trade or order is open. Raises ValueError (and resumes) if one is."""
        bot = self.ctl.bot
        paused = False
        try:
            if bot.running:
                status = await bot.status()
                snap = status.get("bot") if status else None
                if snap is None:
                    raise ValueError("The bot is starting or not answering, so it can't be confirmed that no trade is "
                                     "open. Try again in a minute.")
                if not (snap.get("risk") or {}).get("paused"):
                    with contextlib.suppress(RuntimeError, ValueError):
                        await bot.action("pause", {"source": f"update ({source})"})
                        paused = True
                    status = await bot.status()
                    snap = (status or {}).get("bot") or snap
                if snap.get("position") or snap.get("trade"):
                    raise ValueError("A trade is open. Updates only install while the bot is flat: close the trade "
                                     "(or let it finish) and try again, or choose 'After the close'.")
            await self._broker_check()
        except ValueError:
            if paused:
                with contextlib.suppress(RuntimeError, ValueError):
                    await bot.action("resume", {"source": f"update ({source}) - not installed"})
            raise

    async def _check_broker_flat(self) -> None:
        """Live mode: ask TopstepX itself, so an order placed outside the bot also blocks the update."""
        if self.ctl.bot.mode != "live" or not self.ctl.secrets.has_credentials:
            return
        from topstep_bot.api.rest import ProjectXClient
        from topstep_bot.live import select_account

        cfg, s = self.ctl.cfg, self.ctl.secrets
        try:
            async with ProjectXClient(s.username, s.api_key, cfg.api.base_url, cfg.api.timeout_seconds) as client:
                account = await select_account(client, cfg)
                positions = await client.search_open_positions(account.id)
                orders = await client.search_open_orders(account.id)
        except Exception as exc:  # noqa: BLE001 - any doubt means: don't update
            raise ValueError(f"Could not confirm with TopstepX that the account is flat ({exc}). Nothing was changed; "
                             "try again shortly.") from None
        if positions or orders:
            raise ValueError(f"TopstepX shows {len(positions)} open position(s) and {len(orders)} working order(s) on "
                             f"{account.name}. Updates only install while the account is flat.")

    async def _restart(self) -> None:
        """Hand over to the new version: say so on Telegram, then end the controller (UPDATE_EXIT_CODE)."""
        tg, self.ctl.telegram = self.ctl.telegram, None
        for task in asyncio.all_tasks():
            if task.get_name() == "telegram":  # stop polling before the client closes
                task.cancel()
        if tg is not None:
            await tg.send("🔄 Update installed - restarting with the new version. Back in about 30 seconds.")
            await tg.close()
        self.ctl.request_exit(UPDATE_EXIT_CODE)

    # ---------------------------------------------------------------- background

    async def start(self) -> None:
        """On controller start: announce a finished update, then check now and then on a schedule."""
        restart = load_state(self.state_dir).get("restart")
        if restart:
            self._save(restart=None)
            self.info, self.first_check_delay = None, min(self.first_check_delay, 5.0)  # confirm the new version soon
            text = restart.get("announce")
            if text:
                self.message = text.removeprefix("✅ ")
                self.ctl.bot._event("info", self.message)
                await self._tell(text)
        if self.supported:
            spawn(self.run(), name="updates")

    async def _tell(self, text: str, keyboard: dict | None = None) -> None:
        if self.ctl.telegram is not None:
            await self.ctl.telegram.send(text, keyboard)
        else:
            await asyncio.get_running_loop().run_in_executor(None, notify, self.ctl.secrets, text)

    async def run(self) -> None:
        await asyncio.sleep(self.first_check_delay)
        while True:
            try:
                if self.cfg.enabled and not self.busy and (self.next_check is None or self.clock() >= self.next_check):
                    self.next_check = self.clock() + timedelta(hours=self.cfg.check_every_hours)
                    info = await self.check("automatic check")
                    if info.available:
                        await self._announce(info)
                if self.scheduled and not self.busy and self.quiet():
                    await self._install_scheduled()
            except Exception:  # noqa: BLE001 - a failed round must not end the checks for good
                log.exception("Update check round failed")
            await asyncio.sleep(self.poll_seconds)

    async def _announce(self, info: UpdateInfo) -> None:
        state = load_state(self.state_dir)
        if state.get("notified") == info.latest or not self.cfg.notify:
            return
        self._save(notified=info.latest)
        self.ctl.bot._event("info", info.headline() + " See it on the Settings tab.")
        text = describe(info) + "\n\nNothing changes until you confirm. Tap below to see when it can install."
        await self._tell(text, {"inline_keyboard": [[{"text": "⬆️ Update options", "callback_data": "cmd:update"}]]})

    async def _install_scheduled(self) -> None:
        by = (self.scheduled or {}).get("by", "you")
        try:
            message = await self.install(f"{by}, scheduled for after the close")
        except ValueError as exc:
            reason = str(exc)
            if reason != self._waiting_reason:  # say why it waits once, not every minute
                self._waiting_reason = reason
                self.ctl.bot._event("warning", f"Scheduled update waits: {reason}")
            return
        self._waiting_reason = None
        log.info("Scheduled update: %s", message)


def updated_text(info: UpdateInfo, sha: str) -> str:
    """The message after a restart into a new version: what you now have."""
    titles = [c.title for c in info.changes[:3]]
    more = (info.change_count if info.exact else len(info.changes)) - len(titles)
    version = f" (version {info.new_version})" if info.new_version and info.new_version != info.version else ""
    text = f"✅ Topstep Bot updated to {sha[:7]}{version}"
    if titles:
        text += ": " + "; ".join(titles) + (f"; and {more} more" if more > 0 else "")
    return text + "."


# ------------------------------------------------------------------------------ restart after an update

def relaunch(cfg: BotConfig, config_path: str | None, code: int, secrets: Any = None, out: Callable[[str], None] = print) -> int:
    """Called when the controller ended with UPDATE_EXIT_CODE: start the new version.

    The first process stays as a small launcher and starts each new version as a child process (so the
    window, start.bat and the Windows sign-in shortcut keep working without changes). A child that
    is updated again just exits, and this loop starts the next version. If a new version fails to
    start, the previous one is restored and started instead."""
    if os.environ.get(RELAUNCHED_ENV) == "1":
        return code  # the launcher above us starts the new version
    data = Path(cfg.data_dir)
    while code == UPDATE_EXIT_CODE:
        plan = load_state(data).get("restart") or {}
        cmd = [sys.executable, "-m", "topstep_bot", *(["-c", str(config_path)] if config_path else []),
               "start", "--yes", "--no-browser", "--mode", plan.get("mode") or cfg.mode,
               *([] if plan.get("bot", True) else ["--no-bot"])]
        out("Restarting Topstep Bot with the new version...")
        started = time.monotonic()
        code = _run_child(cmd)
        if code not in (0, UPDATE_EXIT_CODE, 130) and time.monotonic() - started < STARTUP_GRACE:
            code = _undo_failed_update(cfg, data, code, secrets, out, plan)
    return code


def _run_child(cmd: list[str]) -> int:
    proc = subprocess.Popen(cmd, env={**os.environ, RELAUNCHED_ENV: "1"})
    while True:
        try:
            return proc.wait()
        except KeyboardInterrupt:  # Ctrl+C reaches the child too: wait for it to stop (it flattens first)
            continue


def _undo_failed_update(cfg: BotConfig, data: Path, code: int, secrets: Any, out: Callable[[str], None], plan: dict) -> int:
    updater = Updater.for_config(cfg)
    if updater is None or not updater.can_roll_back():
        return code
    try:
        rec = updater.rollback()
    except UpdateError as exc:
        out(f"The new version failed to start (exit code {code}) and could not be undone: {exc}")
        return code
    finally:
        updater.close()
    text = (f"⚠️ The new version failed to start (exit code {code}), so the previous version "
            f"({(rec.get('from') or 'from before the update')[:7]}) was restored. Details are in the logs folder.")
    out(text)
    log.error(text)
    state = load_state(data)
    state["restart"] = {**plan, "announce": text}
    save_state(data, state)
    if secrets is not None:
        with contextlib.suppress(Exception):
            notify(secrets, text)
    return UPDATE_EXIT_CODE  # start the restored version
