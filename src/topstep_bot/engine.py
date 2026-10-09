"""The trading core: turns bars and prices into decisions.

The same TradingCore is driven by the backtester (historical bars) and by the live runner
(API bars + realtime prices), so a backtest exercises exactly the code that trades live.

Per closed bar:   strategy -> exit signals -> stop management -> risk checks -> new entries
Per price update: open-risk monitor (personal daily loss limit, MLL cushion) -> flatten if needed
Per clock tick:   trading-day rollover (end-of-day MLL update), session flatten deadline
"""

from __future__ import annotations

import logging
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import TYPE_CHECKING

from topstep_bot.broker.base import Broker
from topstep_bot.config import BotConfig
from topstep_bot.execution import ManagedTrade, OrderManager, TradeState
from topstep_bot.indicators import ATR
from topstep_bot.journal import Journal
from topstep_bot.knowledge import FIRST_TRADE, MANUAL, RegimeTracker, slot_for
from topstep_bot.manual import ManualTrading
from topstep_bot.market_context import MarketContext
from topstep_bot.models import Account, Bar, Contract, OrderSide, Signal
from topstep_bot.notify import Notifier
from topstep_bot.risk.manager import RiskManager
from topstep_bot.risk.topstep import LossLimitTracker
from topstep_bot.sessions import SessionSchedule
from topstep_bot.setups import SetupTracker
from topstep_bot.strategies.base import Strategy, StrategyContext

log = logging.getLogger(__name__)
event_log = logging.getLogger("topstep_bot.events")

if TYPE_CHECKING:
    from topstep_bot.first_trade import FirstTradePlanner
    from topstep_bot.knowledge import KnowledgeBase
    from topstep_bot.memory import LongRunMemory
    from topstep_bot.recommendations import RecommendationBook
    from topstep_bot.remote import RemoteControl

_LEVELS = {"debug": logging.DEBUG, "info": logging.INFO, "warning": logging.WARNING,
           "error": logging.ERROR, "critical": logging.CRITICAL}
UTC = timezone.utc

FLATTEN_RETRY = timedelta(seconds=5)


@dataclass
class TradePlan:
    """A signal turned into a concrete, risk-sized order."""

    side: OrderSide
    entry_ref: float
    stop: float
    target: float | None
    size: int
    limit: float | None
    planned_risk: float


@dataclass
class DayRecord:
    day: date
    start_balance: float
    end_balance: float
    min_equity: float
    trades: int

    @property
    def pnl(self) -> float:
        return self.end_balance - self.start_balance


class TradingCore:
    def __init__(
        self,
        *,
        cfg: BotConfig,
        contract: Contract,
        broker: Broker,
        strategy: Strategy,
        risk: RiskManager,
        orders: OrderManager,
        schedule: SessionSchedule,
        tracker: LossLimitTracker,
        clock: Callable[[], datetime],
        account_label: str,
        journal: Journal | None = None,
        notifier: Notifier | None = None,
    ):
        self.cfg = cfg
        self.contract = contract
        self.broker = broker
        self.strategy = strategy
        self.risk = risk
        self.orders = orders
        self.schedule = schedule
        self.tracker = tracker
        self.clock = clock
        self.account_label = account_label
        self.journal = journal
        self.notifier = notifier
        self.tf = timedelta(minutes=cfg.instrument.timeframe_minutes)

        self.balance = 0.0
        self.last_price: float | None = None
        self.last_bar: Bar | None = None
        self.current_day: date | None = None
        self.strategy_day: date | None = None
        self.closed_trades: list[ManagedTrade] = []
        self.daily_records: list[DayRecord] = []
        self.events: deque[dict] = deque(maxlen=200)
        self.halted: str | None = None
        self._day_min_equity = 0.0
        self._last_flatten: datetime | None = None
        self._skip_note: tuple[date | None, str] | None = None
        self.atr = ATR(14)
        self.regime = RegimeTracker()  # calm / volatile, for the knowledge base
        self.market = MarketContext()  # the market snapshot saved with every observation
        self.recommender: RecommendationBook | None = None  # trade ideas for the dashboard (+ what the bot learns from)
        self.knowledge: KnowledgeBase | None = None  # what has worked when; drives the adaptive strategy
        self.remote: RemoteControl | None = None  # settings/trades from the dashboard and Telegram
        self.manual = ManualTrading(self)  # trades you open yourself from the dashboard's trade ticket
        self.setups = SetupTracker(self)  # the trades every strategy is building toward (dashboard)
        self.first_trade: FirstTradePlanner | None = None  # an educated trade soon after starting (first_trade.py)
        self._insights: dict[bool, tuple[tuple, dict]] = {}  # cached "What the bot learned" reports (recent / long-run)
        self.memory: LongRunMemory | None = None  # every bar seen + what every strategy did on all of it (reports only)

        orders.on_trade_closed = self._on_trade_closed
        orders.on_event = self._on_order_event
        broker.on_account = self._on_account
        self._bind_strategy()

    def attach_knowledge(self, knowledge: KnowledgeBase | None) -> None:
        self.knowledge = knowledge
        self._bind_strategy()

    def _bind_strategy(self) -> None:
        """Give a strategy that can use them the knowledge base and the live regime."""
        bind = getattr(self.strategy, "bind_knowledge", None)
        if bind:
            bind(self.knowledge, lambda: self.regime.value)

    def slot(self, ts: datetime | None = None) -> str:
        """Time-of-day slot (open / midday / close / off) of ``ts`` or now."""
        return slot_for(self.schedule.local(ts or self.clock()).time())

    def market_snapshot(self, price: float | None = None, ts: datetime | None = None) -> dict[str, float]:
        """What the market looks like at ``price`` (default: the last price) and ``ts`` (default: now)."""
        if price is None:
            price = self.last_price if self.last_price is not None else (self.last_bar.close if self.last_bar else None)
        if price is None:
            return {}
        return self.market.snapshot(price, self.schedule.local(ts or self.clock()), self.regime.ratio,
                                    self.strategy.rth_open)

    def trade_facts(self, t: ManagedTrade) -> dict:
        """What a closed trade teaches beyond its result: price path, costs and fill quality.

        Keys match the knowledge base's Observation fields. ``cost_r`` is the fees in R: the
        slippage of a real fill is already in its prices (and in ``slip_in`` / ``slip_out``).
        """
        mfe, mae = t.excursion_r()
        slip_in, slip_out = t.slippage_ticks(self.contract.tick_size)
        bars = None
        if t.opened_at and t.closed_at:
            bars = max(0, round((t.closed_at - t.opened_at) / self.tf))
        cost_r = None
        if t.risk_points and t.filled_size:
            cost_r = round(t.fees / t.filled_size / self.contract.point_value / t.risk_points, 3)
        return {"ctx": dict(t.context) or None, "mfe_r": mfe, "mae_r": mae, "bars": bars, "cost_r": cost_r,
                "slip_in": slip_in, "slip_out": slip_out}

    # ------------------------------------------------------------------ events

    def event(self, level: str, message: str, kind: str | None = None, *, log_it: bool = True) -> None:
        if log_it:
            event_log.log(_LEVELS.get(level, logging.INFO), message, extra={"event": kind or "activity"})
        local = self.schedule.local(self.clock()).isoformat(timespec="seconds")
        self.events.appendleft({"ts": local, "level": level, "message": message})
        if self.journal:
            self.journal.log_event(level, message)
        if kind and self.notifier:
            self.notifier.notify(kind, message)

    def _on_order_event(self, level: str, message: str) -> None:
        kind = "error" if level in ("error", "critical") else None
        if message.startswith("ENTRY"):
            kind = "entry"
        self.event(level, message, kind, log_it=False)  # already logged by the order manager

    async def _on_account(self, acct: Account) -> None:
        self.balance = acct.balance

    # ------------------------------------------------------------- day handling

    async def begin_day(
        self, day: date, balance: float, realized: float = 0.0, closed_today: list[tuple[datetime, float]] | None = None
    ) -> None:
        self.current_day = day
        self.balance = balance
        self.risk.start_day(day, balance, realized, closed_today)
        self._day_min_equity = balance
        if self.strategy_day != day:
            self.strategy.on_new_day(day)
            self.strategy_day = day

    async def end_day(self) -> None:
        if self.current_day is None:
            return
        floor = self.tracker.end_of_day(self.balance)
        rec = DayRecord(
            self.current_day, self.risk.day_start_balance, self.balance, self._day_min_equity, self.risk.trades_today
        )
        self.daily_records.append(rec)
        if rec.trades:
            self.risk.best_prior_day = max(self.risk.best_prior_day, rec.pnl)
        if self.journal:
            self.journal.record_day(rec.day, self.account_label, rec.start_balance, rec.end_balance, rec.trades, floor)
            self.journal.set_state(f"mll_floor:{self.account_label}", floor)
            self.journal.set_state(f"last_eod:{self.account_label}", rec.day.isoformat())
        if rec.trades:
            self.event(
                "info",
                f"Day {rec.day} closed: {rec.trades} trade(s), P&L ${rec.pnl:,.2f}, balance ${rec.end_balance:,.2f}, "
                f"MLL floor ${floor:,.2f}",
                "daily_summary",
            )

    async def roll_day_if_needed(self, ts: datetime) -> None:
        day = self.schedule.trading_day(ts)
        if day == self.current_day:
            return
        await self.end_day()
        await self.begin_day(day, self.balance)

    # -------------------------------------------------------------------- bars

    def context(self, bar: Bar, warmup: bool = False) -> StrategyContext:
        close = bar.ts + self.tf
        t = self.orders.trade
        own = t is None or t.strategy == self.strategy.name
        return StrategyContext(
            bar_close=close,
            local_close=self.schedule.local(close),
            day=self.schedule.trading_day(bar.ts),
            position=self.orders.position if own else 0,
            entry_price=t.entry_price if t else None,
            stop_price=t.stop_price if t else None,
            warmup=warmup,
        )

    def observe_bar(self, bar: Bar) -> None:
        """Update the bot's own indicators (ATR, regime) with a closed bar."""
        self.last_bar = bar
        self.atr.update(bar.high, bar.low, bar.close)
        rth = self.schedule.is_rth(bar.ts, self.strategy.rth_open, self.strategy.rth_close)
        self.regime.update(bar.high, bar.low, bar.close, rth=rth)
        self.market.update(bar, self.schedule.trading_day(bar.ts), rth)
        t = self.orders.trade
        if t is not None and t.opened_at is not None and bar.ts >= t.opened_at:
            t.note_prices(bar.high, bar.low)

    def warmup_bar(self, bar: Bar) -> None:
        """Feed history to the strategy so indicators are ready; never trades."""
        day = self.schedule.trading_day(bar.ts)
        if day != self.strategy_day:
            self.strategy.on_new_day(day)
            self.strategy_day = day
        self.observe_bar(bar)
        self.strategy.on_bar(bar, self.context(bar, warmup=True))
        if self.recommender:
            self.recommender.warmup_bar(bar)
        if self.last_price is None:
            self.last_price = bar.close

    async def on_bar(self, bar: Bar) -> None:
        await self._process_bar(bar)
        if self.recommender:
            for action, tag, value in self.recommender.on_bar(bar):
                t = self.orders.trade
                if t is None or t.tag != tag:
                    continue
                if action == "exit":
                    await self.orders.exit(value)
                elif action == "stop":
                    await self.orders.update_stop(value)
        self.setups.on_bar_closed(bar.ts + self.tf, bar.close)
        if self.first_trade:
            await self.first_trade.on_bar_closed(bar.ts + self.tf)
        if self.remote:
            self.remote.on_flat()

    def owns_trade(self) -> bool:
        """True if the open trade (if any) belongs to the auto-traded strategy."""
        t = self.orders.trade
        return t is None or t.strategy == self.strategy.name

    def switch_strategy(self, name: str) -> None:
        """Change the auto-traded strategy (only while flat)."""
        if not self.orders.is_flat:
            raise RuntimeError("can only switch strategy while flat")
        if self.recommender:
            new = self.recommender.swap_active(name)
        else:
            from topstep_bot.strategies import create_strategy

            new = create_strategy(name, {}, self.contract, self.cfg.instrument.timeframe_minutes)
            if self.current_day:
                new.on_new_day(self.current_day)
        self.strategy = new
        self.strategy_day = self.current_day
        self.orders.strategy_name = new.name
        self.cfg.strategy.name, self.cfg.strategy.params = new.name, {}
        self._bind_strategy()

    async def _process_bar(self, bar: Bar) -> None:
        await self.roll_day_if_needed(bar.ts)
        t = self.orders.trade
        if t is not None and t.state == TradeState.PENDING and t.created_at <= bar.ts:
            await self.orders.cancel_unfilled_entry("price moved away from the signal")
        self.observe_bar(bar)
        ctx = self.context(bar)
        signal = self.strategy.on_bar(bar, ctx)
        if signal is not None and signal.action == "exit":
            if not self.orders.is_flat and self.owns_trade():
                await self.orders.exit(f"strategy exit: {signal.reason}")
            return
        await self._manage_open_trade(bar, ctx)
        if signal is not None and signal.side is not None:
            await self._handle_entry(signal, bar, ctx)

    async def _handle_entry(self, sig: Signal, bar: Bar, ctx: StrategyContext) -> None:
        side = sig.side
        if self.orders.trade is not None or self.orders.position != 0:
            held = self.orders.position
            if held and (held > 0) != (side == OrderSide.BUY) and self.owns_trade():
                await self.orders.exit(f"reversal signal: {sig.reason}")
            return
        now = ctx.bar_close
        reason = f"bot halted ({self.halted})" if self.halted else self.risk.entry_block_reason(now, self.balance)
        if reason:
            self._note_skip(f"Skipped {side.label} signal: {reason}")
            if self.recommender:
                ref = self.last_price if self.last_price is not None else bar.close
                plan = self.plan_entry(sig, ref)
                self.recommender.record_active(sig, ctx, ref, None if isinstance(plan, str) else plan, "skipped", reason)
            return
        entry_ref = self.last_price if self.last_price is not None else bar.close
        plan = self.plan_entry(sig, entry_ref)
        if isinstance(plan, str):
            self._note_skip(f"Skipped {side.label}: {plan}")
            if self.recommender:
                self.recommender.record_active(sig, ctx, entry_ref, None, "skipped", plan)
            return
        trade = await self.orders.enter(side, plan.size, plan.stop, plan.target, sig.reason, ref_price=entry_ref,
                                        limit_price=plan.limit, planned_risk=plan.planned_risk)
        if trade is not None:
            trade.context = self.market_snapshot(entry_ref, ctx.bar_close)
        if self.recommender:
            self.recommender.record_active(sig, ctx, entry_ref, plan, "taken" if trade else "skipped",
                                           "" if trade else "order could not be placed", tag=trade.tag if trade else None)

    def plan_entry(self, sig: Signal, entry_ref: float) -> TradePlan | str:
        """Turn a signal into a concrete, risk-sized trade - or the reason it can't be traded."""
        side = sig.side
        if side is None or sig.stop_price is None:
            return "signal has no protective stop"
        c = self.contract
        stop = c.round_price(sig.stop_price, "down" if side == OrderSide.BUY else "up")
        distance = (entry_ref - stop) * side.sign
        if distance <= 0:
            return f"stop {stop} is on the wrong side of price {entry_ref}"
        min_dist = c.price_offset(self.cfg.risk.min_stop_ticks)
        if self.cfg.risk.min_stop_atr and self.atr.value:
            min_dist = max(min_dist, c.round_price(self.cfg.risk.min_stop_atr * self.atr.value, "up"))
        if distance < min_dist:
            stop = c.round_price(entry_ref - side.sign * min_dist)
        elif c.ticks(distance) > self.cfg.risk.max_stop_ticks:
            return f"stop is {c.ticks(distance):.0f} ticks away (max {self.cfg.risk.max_stop_ticks})"
        max_slip = self.cfg.execution.max_entry_slippage_ticks
        worst_entry = entry_ref + side.sign * c.price_offset(max_slip or 0)
        # Risk holds even at the worst fill; near scheduled news the size is capped below Topstep's max.
        size = self.risk.position_size(worst_entry, stop, self.balance, now=self.clock())
        if size < 1:
            return "1 contract would risk more than the allowed budget"
        target = None
        if sig.target_price is not None:
            target = c.round_price(sig.target_price, "down" if side == OrderSide.BUY else "up")
            if (target - entry_ref) * side.sign <= 0:
                target = None
        limit = None
        if max_slip is not None:
            limit = c.round_price(worst_entry, "up" if side == OrderSide.BUY else "down")
        planned = size * self.risk.risk_per_contract(worst_entry, stop)
        return TradePlan(side, entry_ref, stop, target, size, limit, planned)

    def _note_skip(self, message: str) -> None:
        """Log a skipped signal once per day per message to avoid noise."""
        key = (self.current_day, message)
        if self._skip_note != key:
            self._skip_note = key
            self.event("info", message)

    async def _manage_open_trade(self, bar: Bar, ctx: StrategyContext) -> None:
        t = self.orders.trade
        if t is None or t.state != TradeState.OPEN or t.entry_price is None:
            return
        long = t.side == OrderSide.BUY
        sign = t.side.sign
        candidates: list[float] = []
        strat_stop = self.strategy.trailing_stop(bar, ctx) if t.strategy == self.strategy.name else None
        if strat_stop is not None:
            candidates.append(strat_stop)
        rcfg = self.cfg.risk
        if rcfg.breakeven_at_r and not t.breakeven_done and t.risk_points > 0:
            excursion = (bar.high - t.entry_price) if long else (t.entry_price - bar.low)
            if excursion >= rcfg.breakeven_at_r * t.risk_points:
                t.breakeven_done = True
                candidates.append(t.entry_price + sign * self.contract.price_offset(rcfg.breakeven_offset_ticks))
        if rcfg.trail_atr_multiple and self.atr.value:
            candidates.append(bar.close - sign * rcfg.trail_atr_multiple * self.atr.value)
        if not candidates:
            return
        best = max(candidates) if long else min(candidates)
        if (long and best >= bar.close) or (not long and best <= bar.close):
            if (long and best > t.stop_price) or (not long and best < t.stop_price):
                await self.orders.exit("price closed through the trailing stop")
            return
        await self.orders.update_stop(best)

    # ------------------------------------------------------------------ prices

    async def on_price(self, ts: datetime, price: float) -> None:
        self.last_price = price
        self.orders.last_price = price
        if self.orders.is_flat:
            return
        if self.orders.trade is not None:
            self.orders.trade.note_prices(price, price)
        open_pnl = self.orders.open_pnl(price)
        self._day_min_equity = min(self._day_min_equity, self.balance + open_pnl)
        reason = self.risk.check_open_risk(self.balance, open_pnl)
        if reason:
            await self._flatten(reason, ts, kind="risk")

    def observe_extremes(self, bar: Bar) -> float:
        """Backtests: worst intrabar equity for the open position (Topstep checks the MLL in real time)."""
        pos = self.orders.position
        if pos == 0:
            equity = self.balance
        else:
            worst = bar.low if pos > 0 else bar.high
            equity = self.balance + self.orders.open_pnl(worst)
        self._day_min_equity = min(self._day_min_equity, equity)
        return equity

    async def _flatten(self, reason: str, now: datetime, kind: str | None = None) -> None:
        if self._last_flatten and now - self._last_flatten < FLATTEN_RETRY:
            return
        self._last_flatten = now
        self.event("warning", f"FLATTEN: {reason}", kind)
        await self.orders.flatten_all(reason)

    # ------------------------------------------------------------------- clock

    async def on_clock(self, now: datetime) -> None:
        await self.roll_day_if_needed(now)
        guard = self.orders.guard
        if guard is not None and guard.tripped and not self.halted:
            await self.halt(guard.tripped)
        if self.schedule.must_be_flat(now) and not self.orders.is_flat:
            await self._flatten("session flatten time (Topstep requires flat by 15:10 CT)", now)
        elif (news_reason := self.risk.news_flatten_reason(now, self.orders.position)) is not None:
            await self._flatten(news_reason, now, kind="risk")
        elif self.cfg.news.flatten_before and self.schedule.news and not self.orders.is_flat:
            event = self.schedule.news.releasing_soon(now)
            if event:
                await self._flatten(f"closing ahead of news: {event.label}", now)
        if self.first_trade:
            await self.first_trade.on_clock(now)

    async def halt(self, reason: str) -> None:
        """Kill switch: flatten and stop opening trades until restarted."""
        self.halted = reason
        self.risk.paused = True
        await self._flatten(f"halted: {reason}", self.clock(), kind="risk")

    # ---------------------------------------------------------------- results

    async def _on_trade_closed(self, t: ManagedTrade) -> None:
        self.risk.record_trade(t.net_pnl, t.closed_at or self.clock())
        if self.recommender:
            self.recommender.trade_closed(t)
        if t.strategy == MANUAL:
            self.manual.trade_closed(t)
        elif t.strategy == FIRST_TRADE and self.first_trade:
            self.first_trade.trade_closed(t)
        self.closed_trades.append(t)
        if self.journal and self.current_day:
            self.journal.record_trade(t, self.current_day, self.account_label, self.contract.name, self.trade_facts(t))
        r = t.r_multiple()
        self.event(
            "info",
            f"Closed {t.side.label} {t.filled_size} {self.contract.name}: {t.exit_reason}, "
            f"net ${t.net_pnl:,.2f}" + (f" ({r:+.2f}R)" if r is not None else ""),
            "exit",
        )

    def knowledge_summary(self) -> dict | None:
        if self.knowledge is None:
            return None
        from topstep_bot.strategies import BASE_STRATEGIES, STRATEGIES

        names = [(n, STRATEGIES[n].title) for n in BASE_STRATEGIES]
        return self.knowledge.summary(names, today=self.schedule.trading_day(self.clock()), slot=self.slot(),
                                      regime=self.regime.value)

    def knowledge_text(self) -> str:
        if self.knowledge is None:
            return "The knowledge base is turned off (knowledge.enabled: false)."
        from topstep_bot.insights import report_text
        from topstep_bot.strategies import BASE_STRATEGIES, STRATEGIES

        names = [(n, STRATEGIES[n].title) for n in BASE_STRATEGIES]
        table = self.knowledge.text(names, today=self.schedule.trading_day(self.clock()), slot=self.slot(),
                                    regime=self.regime.value)
        report = self.insights()
        return table if not report or not report["coverage"]["total"] else table + "\n\n" + report_text(report, compact=True)

    def insights(self, longrun: bool = False) -> dict | None:
        """The "What the bot learned" report (insights.py), rebuilt only when its knowledge base changes.

        ``longrun``: the same report on the long-run memory (memory.py) instead of the recent knowledge base.
        """
        kb = (self.memory.knowledge if self.memory else None) if longrun else self.knowledge
        if kb is None:
            return None
        key = (id(kb), kb.updated, len(kb.obs))
        cached = self._insights.get(longrun)
        if cached is None or cached[0] != key:
            from topstep_bot.insights import build_report
            from topstep_bot.strategies import BASE_STRATEGIES, STRATEGIES

            names = [(n, STRATEGIES[n].title) for n in BASE_STRATEGIES]
            cached = self._insights[longrun] = (key, build_report(kb, names, slippage_ticks=self.cfg.risk.slippage_ticks))
        return cached[1]

    def forecast(self) -> dict | None:
        """When the next automatic trade is likely, and why (forecast.py). Never raises: it's informational."""
        from topstep_bot.forecast import forecast

        try:
            return forecast(self)
        except Exception:  # noqa: BLE001 - a forecast must never disturb trading or the dashboard
            log.exception("Next-trade forecast failed")
            return None

    def brief(self) -> dict:
        """What the bot knows, in plain sentences (briefing.py)."""
        from topstep_bot.briefing import build_brief

        return build_brief(self)

    def _trade_view(self, t: ManagedTrade) -> dict:
        """The open trade plus where it stands now (open P&L in dollars and R)."""
        d = t.to_dict()
        price = self.last_price
        if t.entry_price is not None and price is not None and t.filled_size:
            d["open_pnl"] = round(self.orders.open_pnl(price), 2)
            d["r_now"] = round((price - t.entry_price) * t.side.sign / t.risk_points, 2) if t.risk_points else None
        d["breakeven_ok"] = (t.entry_price is not None and t.stop_order_id is not None and price is not None
                             and (t.stop_price - t.entry_price) * t.side.sign < 0
                             and (price - t.entry_price) * t.side.sign >= self.contract.price_offset(2))
        return d

    def snapshot(self) -> dict:
        open_pnl = self.orders.open_pnl()
        equity = self.balance + open_pnl
        plan = self.risk.plan
        progress = self.risk.combine_progress(self.balance, open_pnl)
        start = self.tracker.starting_balance
        trade = self.orders.trade
        return {
            "time": self.schedule.local(self.clock()).strftime("%Y-%m-%d %H:%M:%S CT"),
            "mode": self.cfg.mode,
            "account": self.account_label,
            "plan": f"{plan.name} {self.risk.stage}",
            "contract": self.contract.name,
            "strategy": self.strategy.title,
            "timeframe": self.cfg.instrument.timeframe_minutes,
            "balance": round(self.balance, 2),
            "equity": round(equity, 2),
            "open_pnl": round(open_pnl, 2),
            "position": self.orders.position,
            "last_price": self.last_price,
            # Combine: the target after any Consistency Target increase.
            "profit_target": progress.profit_target if progress else None,
            "total_profit": round(self.balance - start, 2),
            "trade": self._trade_view(trade) if trade else None,
            "trades_today": [{**t.to_dict(), "time": self.schedule.local(t.closed_at).strftime("%H:%M")}
                             for t in reversed(self.closed_trades)
                             if t.closed_at and self.schedule.trading_day(t.closed_at) == self.current_day][:20],
            "contract_info": {"tick_size": self.contract.tick_size, "tick_value": self.contract.tick_value,
                              "point_value": self.contract.point_value, "decimals": self.contract.price_decimals,
                              "atr": round(self.atr.value, 4) if self.atr.value else None},
            "manual_block": self.manual.block_reason(),  # why the trade ticket can't place a trade now
            "setups": self.setups.view(),
            "last_trade": self.orders.last_trade.to_dict() if self.orders.last_trade else None,
            "risk": self.risk.snapshot(self.balance, open_pnl),
            "strategy_state": {
                k: (round(v, 2) if isinstance(v, float) else v) for k, v in self.strategy.state().items()
            },
            "last_bar": self.last_bar.ts.isoformat() if self.last_bar else None,
            "halted": self.halted,
            "connected": self.broker.connected,
            "events": list(self.events)[:50],
            "recommendations": self.recommender.snapshot() if self.recommender else None,
            "settings": self.remote.describe() if self.remote else None,
            "slot": self.slot(),
            "regime": self.regime.value,
            "knowledge": self.knowledge_summary(),
            "first_trade": self.first_trade.view() if self.first_trade else None,
            "forecast": self.forecast(),
            "memory": self.memory.status() if self.memory else None,
        }
