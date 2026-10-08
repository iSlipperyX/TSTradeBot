"""Live and paper trading runner.

Data flow:
  * Bars: the official bar history endpoint is polled right after each bar closes, so live
    bars are identical to the bars a backtest would use.
  * Prices: the realtime market hub streams quotes; they drive open-risk checks (and fills
    in paper mode). If the stream drops, prices fall back to REST polling.
  * Orders (live mode): the realtime user hub reports order/position/fill changes, and a
    periodic REST reconcile repairs anything missed.

Stop the bot with Ctrl+C, the dashboard's Stop button, Telegram /stop, or by creating a file
named KILL in the working directory (which also flattens and halts trading).

When started by the 24/7 service (topstep-bot service) the runner also writes a heartbeat
file, restarts itself once a day during the CME maintenance halt, and announces restarts
quietly. See service.py.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time as _time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from topstep_bot.api.realtime import MarketStream
from topstep_bot.api.rest import ProjectXClient, ProjectXError
from topstep_bot.bars import floor_time
from topstep_bot.broker.base import Broker
from topstep_bot.broker.paper import PaperBroker
from topstep_bot.config import BotConfig, Secrets
from topstep_bot.engine import TradingCore
from topstep_bot.factory import build_core, fees_for, starting_balance
from topstep_bot.journal import Journal
from topstep_bot.models import Account, BarUnit, Contract, Quote
from topstep_bot.news import NewsCalendar
from topstep_bot.notify import Notifier
from topstep_bot.sessions import session_open_for

log = logging.getLogger(__name__)
UTC = timezone.utc
KILL_FILE = Path("KILL")


class SetupError(Exception):
    """A configuration/account problem the user needs to fix (shown without a traceback)."""


@dataclass
class Controls:
    """Requests coming from the dashboard, Telegram, the 24/7 service or the bot itself."""

    stop: asyncio.Event = field(default_factory=asyncio.Event)
    flatten_requested: bool = False
    flatten_reason: str = "flatten requested from dashboard"
    restart_requested: bool = False  # quiet daily maintenance restart (exit code 75)
    failure: str | None = None  # the bot stopped itself after an internal error (exit code 1)


async def select_account(client: ProjectXClient, cfg: BotConfig) -> Account:
    accounts = await client.search_accounts(only_active=True)
    if not accounts:
        raise SetupError("No active accounts found on this TopstepX login.")
    if cfg.account.account_id is not None:
        match = [a for a in accounts if a.id == cfg.account.account_id]
    elif cfg.account.account_name:
        match = [a for a in accounts if a.name.lower() == cfg.account.account_name.lower()]
    else:
        match = [a for a in accounts if a.can_trade]
        if len(match) > 1:
            names = ", ".join(f"{a.name} (id {a.id})" for a in match)
            raise SetupError(f"Several accounts can trade: {names}. Set account.account_id in config.yaml (or run setup).")
    if not match:
        names = ", ".join(f"{a.name} (id {a.id})" for a in accounts)
        raise SetupError(f"Configured account not found. Available: {names}")
    return match[0]


async def resolve_contract(client: ProjectXClient, cfg: BotConfig) -> Contract:
    if cfg.instrument.contract_id:
        return await client.contract_by_id(cfg.instrument.contract_id)
    return await client.resolve_contract(cfg.instrument.symbol)


class LiveRunner:
    def __init__(self, cfg: BotConfig, secrets: Secrets, controls: Controls | None = None):
        if not secrets.has_credentials:
            raise SetupError("Missing TopstepX credentials. Run 'topstep-bot setup' (or set TOPSTEPX_USERNAME / TOPSTEPX_API_KEY in .env).")
        self.cfg = cfg
        self.secrets = secrets
        self.controls = controls or Controls()
        self.client = ProjectXClient(secrets.username, secrets.api_key, cfg.api.base_url, cfg.api.timeout_seconds)
        self.journal = Journal(cfg.journal_path)
        self.notifier = Notifier(cfg.notifications, secrets, prefix=f"Topstep Bot ({cfg.mode})")
        self.core: TradingCore | None = None
        self.broker: Broker | None = None
        self.market: MarketStream | None = None
        self.account: Account | None = None
        self.contract: Contract | None = None
        self._last_bar_ts: datetime | None = None
        self._last_price_at: datetime | None = None
        self._tasks: list[asyncio.Task] = []
        # Set by the 24/7 service (see service.py).
        self.supervised = os.environ.get("TOPSTEP_BOT_SUPERVISED") == "1"
        self.quiet_start = os.environ.get("TOPSTEP_BOT_QUIET_START") == "1"
        heartbeat = os.environ.get("TOPSTEP_BOT_HEARTBEAT")
        self.heartbeat_path = Path(heartbeat) if heartbeat else None
        self.started_at = datetime.now(UTC)
        self._checked_in: date | None = None

    @staticmethod
    def now() -> datetime:
        return datetime.now(UTC)

    # ------------------------------------------------------------------ setup

    async def prepare(self) -> TradingCore:
        cfg = self.cfg
        self.account = await select_account(self.client, cfg)
        if cfg.mode == "live" and not self.account.can_trade:
            raise SetupError(f"Account {self.account.name} is not allowed to trade right now (canTrade = false).")
        self.contract = await resolve_contract(self.client, cfg)
        log.info("Account %s (id %s), contract %s (%s)", self.account.name, self.account.id, self.contract.name, self.contract.id)

        if "DLL" in self.account.name.upper() and cfg.account.topstep_daily_loss_limit is None:
            from topstep_bot.risk.topstep import PLANS

            cfg.account.topstep_daily_loss_limit = PLANS[cfg.account.plan].legacy_daily_loss_limit
            log.info("Account name indicates a Topstep Daily Loss Limit; enforcing $%.0f", cfg.account.topstep_daily_loss_limit)
        label = f"{self.account.name}" if cfg.mode == "live" else f"PAPER-{self.account.name}"
        if cfg.mode == "live":
            from topstep_bot.broker.projectx import ProjectXBroker

            self.broker = ProjectXBroker(self.client, self.account.id, cfg.api.user_hub_url)
        else:
            paper_balance = self.journal.get_state("paper_balance", starting_balance(cfg))
            self.broker = PaperBroker(
                self.contract,
                paper_balance,
                slippage_ticks=cfg.risk.slippage_ticks,
                fees_round_turn=fees_for(cfg, self.contract),
                account_name=label,
                live=True,
            )
        floor = self.journal.get_state(f"mll_floor:{label}")
        core = build_core(
            cfg, self.contract, self.broker, clock=self.now, account_label=label,
            mll_floor=floor, journal=self.journal, notifier=self.notifier,
        )
        self.core = core
        acct = await self.broker.get_account()
        core.balance = acct.balance

        # Today's closed trades so far, so a mid-day restart keeps the day's P&L, trade count,
        # losing streak and cooldown. Live: from TopstepX (includes anything traded by hand).
        # Paper: the simulated broker starts empty each run, so use the journal.
        today = core.schedule.trading_day(self.now())
        if cfg.mode == "live":
            realized, closed_today = await self.broker.realized_pnl_since(session_open_for(today, core.schedule.tz))
        else:
            closed_today = self.journal.closed_trades(label, today)
            realized = sum(pnl for _, pnl in closed_today)
        if closed_today:
            log.info("Restoring today's %d closed trade(s), net $%.2f", len(closed_today), realized)
        last_eod = self.journal.get_state(f"last_eod:{label}")
        if last_eod is None or last_eod < today.isoformat():
            core.tracker.end_of_day(acct.balance - realized)  # yesterday's close balance

        await self._warmup()
        await core.begin_day(today, acct.balance, realized, closed_today)
        await self._setup_news()
        self._apply_ramp_up()
        return core

    async def _warmup(self) -> None:
        assert self.core and self.contract
        tf = self.cfg.instrument.timeframe_minutes
        end = self.now()
        start = end - timedelta(days=int(self.cfg.data.warmup_days * 1.6) + 3)
        bars = await self.client.retrieve_bars_range(
            self.contract.id, start, end, BarUnit.MINUTE, tf, live=self.cfg.data.live_market_data
        )
        closed = [b for b in bars if b.ts + timedelta(minutes=tf) <= end]
        for bar in closed:
            self.core.warmup_bar(bar)
        if closed:
            self._last_bar_ts = closed[-1].ts
        log.info("Warmed up on %d %d-minute bars", len(closed), tf)

    # -------------------------------------------------------------------- run

    async def run(self, on_ready: Callable[[TradingCore], Awaitable[None]] | None = None) -> None:
        core = await self.prepare()
        assert self.broker and self.contract
        self.notifier.start()
        await self.broker.start()
        if hasattr(self.broker, "stream"):
            self.broker.stream.hub.on_connected(self._safe_reconcile)

        self.market = MarketStream(self.cfg.api.market_hub_url, self.client.get_token, self.contract.id)
        self.market.on_quote = self._on_quote
        await self._safe_reconcile()

        self._tasks = [
            asyncio.create_task(self.market.hub.run(), name="market-hub"),
            asyncio.create_task(self._bar_loop(), name="bars"),
            asyncio.create_task(self._clock_loop(), name="clock"),
            asyncio.create_task(self._reconcile_loop(), name="reconcile"),
            asyncio.create_task(self._news_loop(), name="news"),
        ]
        for task in self._tasks:
            task.add_done_callback(self._loop_ended)
        core.event(
            "info",
            f"Bot started in {self.cfg.mode.upper()} mode: {core.strategy.title} on {self.contract.name} "
            f"({self.cfg.instrument.timeframe_minutes}m), account {core.account_label}",
            None if self.quiet_start else "start",
        )
        try:
            if on_ready:
                await on_ready(core)
            await self.controls.stop.wait()
        finally:
            await self.shutdown()

    async def shutdown(self) -> None:
        core = self.core
        if core and self.cfg.execution.flatten_on_shutdown and not core.orders.is_flat:
            core.event("warning", "Shutting down: flattening open position")
            try:
                await core.orders.flatten_all("bot shutdown")
                await asyncio.sleep(2)
            except Exception as exc:  # noqa: BLE001
                core.event("critical", f"Could not flatten on shutdown: {exc} - CHECK YOUR ACCOUNT")
        for task in self._tasks:
            task.cancel()
        if self.market:
            await self.market.hub.stop()
        if self.broker:
            await self.broker.stop()
        if core:
            self._save_paper_balance()
            if self.controls.restart_requested:
                core.event("info", "Restarting for daily maintenance")
            else:
                core.event("info", "Bot stopped", "stop")
        await self.notifier.stop()
        await self.client.close()
        self.journal.close()

    def _loop_ended(self, task: asyncio.Task) -> None:
        """A background loop must run until shutdown. If one dies, stop (and flatten) rather than keep
        running half-blind; the 24/7 service then restarts the bot."""
        if task.cancelled() or self.controls.stop.is_set():
            return
        exc = task.exception()
        reason = f"the {task.get_name()} loop " + (f"crashed ({exc!r})" if exc else "ended unexpectedly")
        log.critical("Internal error: %s", reason, exc_info=exc)
        if self.core:
            self.core.event("critical", f"Internal error: {reason}. Stopping the bot (it flattens first).", "error")
        self.controls.failure = reason
        self.controls.stop.set()

    # ------------------------------------------------------------------ loops

    async def _on_quote(self, q: Quote) -> None:
        price = q.last if q.last is not None else q.mid
        if price is None or self.core is None:
            return
        self._last_price_at = self.now()
        if isinstance(self.broker, PaperBroker):
            await self.broker.on_price(q.ts, price, q.bid, q.ask)
        await self.core.on_price(q.ts, price)

    async def _bar_loop(self) -> None:
        """Fetch each bar right after it closes."""
        assert self.core and self.contract
        tf = self.cfg.instrument.timeframe_minutes
        tf_s = tf * 60
        while True:
            now = self.now()
            next_close = floor_time(now, tf_s) + timedelta(seconds=tf_s)
            await asyncio.sleep((next_close - now).total_seconds() + 1.5)
            if not self.core.schedule.market_open(next_close - timedelta(seconds=tf_s)):
                continue  # weekend / daily halt: no bars to fetch
            for _ in range(6):
                try:
                    if await self._fetch_new_bars(tf):
                        break
                except ProjectXError as exc:
                    log.warning("Bar fetch failed: %s", exc)
                except Exception:  # noqa: BLE001
                    log.exception("Bar fetch error")
                await asyncio.sleep(4)

    async def _fetch_new_bars(self, tf: int) -> bool:
        assert self.core and self.contract
        now = self.now()
        start = self._last_bar_ts or now - timedelta(minutes=tf * 5)
        bars = await self.client.retrieve_bars(
            self.contract.id, start, now, BarUnit.MINUTE, tf, limit=100, live=self.cfg.data.live_market_data
        )
        new = [b for b in bars if (self._last_bar_ts is None or b.ts > self._last_bar_ts) and b.ts + timedelta(minutes=tf) <= now]
        for bar in new:
            self._last_bar_ts = bar.ts
            await self.core.on_bar(bar)
        return bool(new)

    async def _clock_loop(self) -> None:
        assert self.core
        core = self.core
        last_balance_check = self.now()
        while True:
            await asyncio.sleep(1)
            now = self.now()
            try:
                await core.on_clock(now)
                if self.controls.flatten_requested:
                    self.controls.flatten_requested = False
                    await core.halt(self.controls.flatten_reason)
                if KILL_FILE.exists() and not core.halted:
                    await core.halt("KILL file found")
                stale = self._last_price_at is None or now - self._last_price_at > timedelta(seconds=20)
                if stale and not core.orders.is_flat and now.second % 10 == 0:
                    await self._poll_price()
                if now - last_balance_check > timedelta(seconds=60):
                    last_balance_check = now
                    if self.cfg.mode == "live":
                        core.balance = (await self.broker.get_account()).balance
                    self._save_paper_balance()
                    self._apply_ramp_up()
                if now.second % 10 == 0:
                    self._beat()
                self._maybe_check_in(now)
                self._maybe_daily_restart(now)
            except Exception:  # noqa: BLE001 - keep the clock alive
                log.exception("Clock loop error")

    # ----------------------------------------------------------- 24/7 helpers

    async def _setup_news(self) -> None:
        if not self.cfg.news.enabled or self.core is None:
            return
        n = self.cfg.news
        calendar = NewsCalendar(n.url, self.cfg.data_path / "news_cache.json", n.impacts, n.currencies,
                                n.minutes_before, n.minutes_after)
        if not calendar.load_cache() or calendar.fetched_at is None or (
            self.now() - calendar.fetched_at > timedelta(hours=6)
        ):
            await calendar.refresh()
        self.core.schedule.news = calendar
        upcoming = calendar.upcoming(self.now(), hours=24)
        if upcoming:
            tz = self.core.schedule.tz
            listing = ", ".join(f"{e.label} {e.time.astimezone(tz):%a %H:%M} CT" for e in upcoming[:6])
            self.core.event("info", f"News blackouts in the next 24h: {listing}")

    async def _news_loop(self) -> None:
        while True:
            await asyncio.sleep(6 * 3600)
            if self.core and self.core.schedule.news:
                try:
                    await self.core.schedule.news.refresh()
                except Exception:  # noqa: BLE001 - keep the last calendar; trading goes on
                    log.exception("Economic calendar refresh failed")

    def _apply_ramp_up(self) -> None:
        """The first live trading days on an account run at reduced risk (replaces a paper-trading period).

        Only days on which the bot actually closed a trade count, so days spent idle (holidays, news,
        no signal) don't use up the ramp-up.
        """
        core = self.core
        rcfg = self.cfg.risk
        if core is None or self.cfg.mode != "live" or rcfg.ramp_up_days <= 0:
            return
        today = core.schedule.trading_day(self.now()).isoformat()
        completed = len([d for d in self.journal.trading_days(core.account_label) if d < today])
        scale = rcfg.ramp_up_risk_fraction if completed < rcfg.ramp_up_days else 1.0
        if scale != core.risk.risk_scale:
            core.risk.risk_scale = scale
            if scale < 1:
                core.event(
                    "info",
                    f"Ramp-up: live day {completed + 1} of {rcfg.ramp_up_days} on this account - risking "
                    f"${rcfg.risk_per_trade * scale:,.0f} per trade instead of ${rcfg.risk_per_trade:,.0f}",
                )
            else:
                core.event("info", "Ramp-up complete: trading at full configured risk")

    def _beat(self) -> None:
        """Tell the 24/7 service we're alive (it restarts the bot if this goes stale)."""
        if self.heartbeat_path:
            try:
                self.heartbeat_path.write_text(str(_time.time()), encoding="utf-8")
            except OSError as exc:
                log.debug("heartbeat write failed: %s", exc)

    def _save_paper_balance(self) -> None:
        if isinstance(self.broker, PaperBroker) and self.broker.position == 0:
            self.journal.set_state("paper_balance", self.broker.balance)

    def _maybe_check_in(self, now: datetime) -> None:
        """Once each weekday morning, send a short 'still running' message."""
        core = self.core
        check_in = self.cfg.service.check_in_time
        if core is None or check_in is None:
            return
        local = core.schedule.local(now)
        if local.weekday() >= 5 or local.time() < check_in or self._checked_in == local.date():
            return
        self._checked_in = local.date()
        if local.time() > (datetime.combine(local.date(), check_in) + timedelta(minutes=5)).time():
            return  # started later in the day: skip
        r = core.risk
        core.event(
            "info",
            f"Good morning: bot running in {self.cfg.mode.upper()} mode on {core.contract.name}. "
            f"Balance ${core.balance:,.2f}, MLL room ${core.tracker.room(core.balance):,.2f}"
            + (f". Today is a no-trade day." if not core.schedule.is_trade_day(core.schedule.trading_day(now)) else "")
            + (" PAUSED." if r.paused else ""),
            "daily_summary",
        )

    def _maybe_daily_restart(self, now: datetime) -> None:
        """Under the 24/7 service, restart once a day in the CME halt (picks up contract rolls)."""
        core = self.core
        restart_at = self.cfg.service.daily_restart_time
        if not self.supervised or restart_at is None or core is None or self.controls.stop.is_set():
            return
        local = core.schedule.local(now)
        due = datetime.combine(local.date(), restart_at, tzinfo=core.schedule.tz)
        if self.started_at < due <= now and core.orders.is_flat:
            self.controls.restart_requested = True
            self.controls.stop.set()

    async def _poll_price(self) -> None:
        """Fallback price source when the realtime stream is quiet or disconnected."""
        assert self.core and self.contract
        now = self.now()
        bars = await self.client.retrieve_bars(
            self.contract.id, now - timedelta(minutes=5), now, BarUnit.MINUTE, 1, limit=5,
            live=self.cfg.data.live_market_data, include_partial=True,
        )
        if bars:
            price = bars[-1].close
            if isinstance(self.broker, PaperBroker):
                await self.broker.on_price(now, price)
            await self.core.on_price(now, price)

    async def _reconcile_loop(self) -> None:
        while True:
            await asyncio.sleep(self.cfg.execution.reconcile_interval_seconds)
            await self._safe_reconcile()

    async def _safe_reconcile(self) -> None:
        if self.core is None:
            return
        try:
            await self.core.orders.reconcile()
        except Exception as exc:  # noqa: BLE001
            log.warning("Reconcile failed: %s", exc)
