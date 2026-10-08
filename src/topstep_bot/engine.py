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
from typing import TYPE_CHECKING
from datetime import date, datetime, timedelta, timezone

from topstep_bot.broker.base import Broker
from topstep_bot.config import BotConfig
from topstep_bot.execution import ManagedTrade, OrderManager, TradeState
from topstep_bot.indicators import ATR
from topstep_bot.journal import Journal
from topstep_bot.models import Account, Bar, Contract, OrderSide, Signal
from topstep_bot.notify import Notifier
from topstep_bot.risk.manager import RiskManager
from topstep_bot.risk.topstep import LossLimitTracker
from topstep_bot.sessions import SessionSchedule
from topstep_bot.strategies.base import Strategy, StrategyContext

log = logging.getLogger(__name__)
event_log = logging.getLogger("topstep_bot.events")

if TYPE_CHECKING:
    from topstep_bot.recommendations import RecommendationBook

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
        self.recommender: RecommendationBook | None = None  # live/paper only: trade ideas for the dashboard
        self.remote = None  # RemoteControl: settings/trades from the dashboard and Telegram

        orders.on_trade_closed = self._on_trade_closed
        orders.on_event = self._on_order_event
        broker.on_account = self._on_account

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

    async def begin_day(self, day: date, balance: float, realized: float = 0.0, trades: int = 0) -> None:
        self.current_day = day
        self.balance = balance
        self.risk.start_day(day, balance, realized, trades)
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

    def warmup_bar(self, bar: Bar) -> None:
        """Feed history to the strategy so indicators are ready; never trades."""
        day = self.schedule.trading_day(bar.ts)
        if day != self.strategy_day:
            self.strategy.on_new_day(day)
            self.strategy_day = day
        self.atr.update(bar.high, bar.low, bar.close)
        self.strategy.on_bar(bar, self.context(bar, warmup=True))
        if self.recommender:
            self.recommender.warmup_bar(bar)
        self.last_bar = bar
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

    async def _process_bar(self, bar: Bar) -> None:
        await self.roll_day_if_needed(bar.ts)
        t = self.orders.trade
        if t is not None and t.state == TradeState.PENDING and t.created_at <= bar.ts:
            await self.orders.cancel_unfilled_entry("price moved away from the signal")
        self.last_bar = bar
        self.atr.update(bar.high, bar.low, bar.close)
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
        if distance < min_dist:
            stop = c.round_price(entry_ref - side.sign * min_dist)
        elif c.ticks(distance) > self.cfg.risk.max_stop_ticks:
            return f"stop is {c.ticks(distance):.0f} ticks away (max {self.cfg.risk.max_stop_ticks})"
        max_slip = self.cfg.execution.max_entry_slippage_ticks
        worst_entry = entry_ref + side.sign * c.price_offset(max_slip or 0)
        size = self.risk.position_size(worst_entry, stop, self.balance)  # risk holds even at the worst fill
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
        if self.schedule.must_be_flat(now) and not self.orders.is_flat:
            await self._flatten("session flatten time (Topstep requires flat by 15:10 CT)", now)
        elif self.cfg.news.flatten_before and self.schedule.news and not self.orders.is_flat:
            event = self.schedule.news.releasing_soon(now)
            if event:
                await self._flatten(f"closing ahead of news: {event.label}", now)

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
        self.closed_trades.append(t)
        if self.journal and self.current_day:
            self.journal.record_trade(t, self.current_day, self.account_label, self.contract.name)
        r = t.r_multiple()
        self.event(
            "info",
            f"Closed {t.side.label} {t.filled_size} {self.contract.name}: {t.exit_reason}, "
            f"net ${t.net_pnl:,.2f}" + (f" ({r:+.2f}R)" if r is not None else ""),
            "exit",
        )

    def snapshot(self) -> dict:
        open_pnl = self.orders.open_pnl()
        equity = self.balance + open_pnl
        plan = self.risk.plan
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
            "profit_target": plan.profit_target if self.risk.stage == "combine" else None,
            "total_profit": round(self.balance - start, 2),
            "trade": trade.to_dict() if trade else None,
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
        }
