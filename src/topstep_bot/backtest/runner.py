"""Bar-by-bar backtester that drives the real TradingCore with a simulated broker."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from topstep_bot.bars import resample
from topstep_bot.broker.paper import PaperBroker
from topstep_bot.config import BotConfig
from topstep_bot.engine import DayRecord
from topstep_bot.execution import ManagedTrade
from topstep_bot.factory import build_core, fees_for, starting_balance
from topstep_bot.models import Bar, Contract
from topstep_bot.sessions import SessionSchedule

UTC = timezone.utc


@dataclass
class BacktestResult:
    cfg: BotConfig
    contract: Contract
    starting_balance: float
    final_balance: float
    trades: list[ManagedTrade]
    days: list[DayRecord]
    breaches: list[tuple[datetime, float, float]] = field(default_factory=list)
    events: list[dict] = field(default_factory=list)
    first_day: date | None = None
    last_day: date | None = None
    bars: int = 0


def _bar_seconds(bars: list[Bar]) -> int:
    gaps = sorted((b.ts - a.ts).total_seconds() for a, b in zip(bars, bars[1:50], strict=False) if b.ts > a.ts)
    return int(gaps[0]) if gaps else 60


def prepare_bars(bars: list[Bar], timeframe_minutes: int) -> list[Bar]:
    """Resample finer data to the strategy timeframe; reject data that is coarser than it."""
    if not bars:
        raise ValueError("No bars to backtest")
    seconds = _bar_seconds(bars)
    target = timeframe_minutes * 60
    if seconds > target:
        raise ValueError(f"Data is {seconds // 60}-minute bars but the strategy needs {timeframe_minutes}-minute bars")
    if seconds < target:
        return list(resample(bars, timeframe_minutes))
    return bars


async def run_backtest(
    cfg: BotConfig,
    bars: list[Bar],
    contract: Contract,
    *,
    warmup_days: int | None = None,
    start: date | None = None,
    end: date | None = None,
    progress: Callable[[float], None] | None = None,
    enforce_mll: bool = True,
) -> BacktestResult:
    """Replay ``bars`` through the real trading core.

    ``enforce_mll=False`` (used by training) switches off the trailing Maximum Loss Limit so one bad
    stretch doesn't end the simulated account and hide how the strategy did afterwards. Daily limits
    stay on; Combine pass rates are computed from the daily results with the real MLL.
    """
    bars = prepare_bars(bars, cfg.instrument.timeframe_minutes)
    schedule = SessionSchedule(cfg.session)
    trading_days = sorted({schedule.trading_day(b.ts) for b in bars})
    if warmup_days is None:  # warm up only as long as this strategy needs
        from topstep_bot.strategies import create_strategy

        warmup_days = create_strategy(cfg.strategy.name, cfg.strategy.params, contract, cfg.instrument.timeframe_minutes).warmup_days
    warmup = warmup_days
    if start is None:
        if len(trading_days) <= warmup:
            raise ValueError(f"Need more than {warmup} trading days of data (have {len(trading_days)}) for warmup")
        start = trading_days[warmup]

    now = [bars[0].ts]
    balance0 = starting_balance(cfg)
    broker = PaperBroker(
        contract, balance0, slippage_ticks=cfg.risk.slippage_ticks, fees_round_turn=fees_for(cfg, contract)
    )
    core = build_core(cfg, contract, broker, clock=lambda: now[0], account_label="backtest")
    core.balance = balance0
    if not enforce_mll:
        core.tracker.max_loss_limit = 1e12
        core.tracker.floor = -1e12
    if hasattr(core.strategy, "bind_knowledge"):
        # The adaptive strategy is backtested walk-forward: it starts knowing nothing and learns
        # from every strategy's outcomes as the data plays, exactly as it does live. (No peeking.)
        from topstep_bot.knowledge import KnowledgeBase
        from topstep_bot.recommendations import RecommendationBook

        core.recommender = RecommendationBook(core, quiet=True)
        core.attach_knowledge(KnowledgeBase.from_config(cfg, None))
    tf = timedelta(minutes=cfg.instrument.timeframe_minutes)
    breaches: list[tuple[datetime, float, float]] = []
    in_breach = False
    last_bar: Bar | None = None
    total = len(bars)

    for i, bar in enumerate(bars):
        day = schedule.trading_day(bar.ts)
        if end is not None and day > end:
            break
        now[0] = bar.ts
        if day < start:
            core.warmup_bar(bar)
            broker.last_price = bar.close
            continue
        last_bar = bar
        await core.roll_day_if_needed(bar.ts)
        await broker.on_bar(bar)
        worst_equity = core.observe_extremes(bar)
        below = worst_equity <= core.tracker.floor
        if below and not in_breach:
            breaches.append((bar.ts, worst_equity, core.tracker.floor))
            core.event("critical", f"MLL breached: equity ${worst_equity:,.2f} <= floor ${core.tracker.floor:,.2f}")
        in_breach = below
        close_time = bar.ts + tf
        now[0] = close_time
        await core.on_price(close_time, bar.close)
        await core.on_bar(bar)
        await core.on_clock(close_time)
        await broker.drain()
        if progress and i % 2000 == 0:
            progress(i / total)

    if last_bar is not None and not core.orders.is_flat:
        await core.orders.flatten_all("end of backtest")
        broker.live = True
        await broker.on_price(now[0], last_bar.close)
        await broker.drain()
    await core.end_day()
    if progress:
        progress(1.0)

    return BacktestResult(
        cfg=cfg,
        contract=contract,
        starting_balance=balance0,
        final_balance=broker.balance,
        trades=list(core.closed_trades),
        days=list(core.daily_records),
        breaches=breaches,
        events=list(core.events),
        first_day=start,
        last_day=core.daily_records[-1].day if core.daily_records else None,
        bars=total,
    )
