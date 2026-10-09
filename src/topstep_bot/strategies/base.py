"""Strategy base class.

A strategy sees one closed bar at a time and may return a Signal:
  Signal("long" | "short", stop_price=..., target_price=..., reason=...)  to enter
  Signal("exit", reason=...)                                             to close
Position sizing, risk limits, order placement and session rules are handled by the bot,
so a strategy only decides *when* and *where* (stop/target) - never *how much*.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any, ClassVar

from topstep_bot.models import Bar, Contract, Signal


@dataclass
class StrategyContext:
    bar_close: datetime  # UTC time the bar closed
    local_close: datetime  # same instant in exchange time (Chicago)
    day: date  # trading day
    position: int  # signed contracts held (0 = flat)
    entry_price: float | None
    stop_price: float | None
    warmup: bool = False


@dataclass
class Setup:
    """A trade a strategy is building toward, for the dashboard's Setups panel.

    ``conditions`` are the entry rules in plain words with whether each is met right now (the
    live price stands in for the next bar's close). ``entry`` is the price the trigger needs (None
    when the trigger is a time or an event rather than a level); ``stop`` / ``target`` are what the
    signal would carry if it fired at ``entry`` (or the current price).
    """

    side: str  # long | short
    conditions: list[tuple[str, bool]] = field(default_factory=list)
    entry: float | None = None
    stop: float | None = None
    target: float | None = None
    note: str = ""  # e.g. "decides at the 10:30 checkpoint"
    strategy: str = ""  # filled in by the adaptive strategy for its sub-strategies

    @property
    def progress(self) -> float:
        return sum(1 for _, met in self.conditions if met) / len(self.conditions) if self.conditions else 0.0


def parse_hhmm(value: str | time) -> time:
    if isinstance(value, time):
        return value
    h, m = str(value).split(":")[:2]
    return time(int(h), int(m))


class Strategy(ABC):
    name: ClassVar[str]
    title: ClassVar[str]
    description: ClassVar[str]
    defaults: ClassVar[dict[str, Any]]

    def __init__(
        self,
        contract: Contract,
        timeframe_minutes: int,
        rth_open: time,
        rth_close: time,
        params: dict[str, Any] | None = None,
    ):
        params = dict(params or {})
        unknown = set(params) - set(self.defaults)
        if unknown:
            valid = ", ".join(sorted(self.defaults))
            raise ValueError(f"Unknown parameter(s) for '{self.name}': {', '.join(sorted(unknown))}. Valid: {valid}")
        self.p: dict[str, Any] = {**self.defaults, **params}
        self.contract = contract
        self.tf = timeframe_minutes
        self.rth_open = rth_open
        self.rth_close = rth_close
        self.setup()

    # ------------------------------------------------------------- overridables

    def setup(self) -> None:
        """Create indicators / validate parameters."""

    def on_new_day(self, day: date) -> None:
        """Called before the first bar of each trading day."""

    @abstractmethod
    def on_bar(self, bar: Bar, ctx: StrategyContext) -> Signal | None:
        """Process a closed bar; optionally return a Signal."""

    def trailing_stop(self, bar: Bar, ctx: StrategyContext) -> float | None:
        """Optionally return a new protective stop for the open position (only tightening is applied)."""
        return None

    def state(self) -> dict[str, Any]:
        """Key levels to show on the dashboard."""
        return {}

    def setups(self, price: float | None, now: datetime) -> list[Setup]:
        """Entries this strategy is building toward right now (``now`` in exchange time).

        Purely informational - the dashboard shows them so you can watch a trade form. Return []
        when nothing more can happen today (traded out, past the cutoff, outside its hours).
        """
        return []

    @property
    def warmup_days(self) -> int:
        """Trading days of history needed before the strategy's signals are meaningful."""
        return 2

    # ------------------------------------------------------------------ helpers

    def bar_open_local(self, ctx: StrategyContext) -> datetime:
        return ctx.local_close - timedelta(minutes=self.tf)

    def is_rth_bar(self, ctx: StrategyContext) -> bool:
        """True if the whole bar lies inside the regular trading hours."""
        start = self.bar_open_local(ctx)
        return start.date() == ctx.local_close.date() and self.rth_open <= start.time() and (
            ctx.local_close.time() <= self.rth_close
        )

    def in_rth(self, now: datetime) -> bool:
        return self.rth_open <= now.time() < self.rth_close

    def rth_minutes(self, now: datetime) -> int:
        """Minutes since today's regular-hours open at ``now`` (exchange time)."""
        return (now.hour * 60 + now.minute) - (self.rth_open.hour * 60 + self.rth_open.minute)

    def rth_time(self, minutes: int) -> str:
        """HH:MM of ``minutes`` after the regular-hours open."""
        total = self.rth_open.hour * 60 + self.rth_open.minute + minutes
        return f"{total // 60:02d}:{total % 60:02d}"

    def fmt(self, price: float | None) -> str:
        return "-" if price is None else f"{price:.{self.contract.price_decimals}f}"

    def minutes_since_open(self, ctx: StrategyContext) -> int:
        open_dt = ctx.local_close.replace(hour=self.rth_open.hour, minute=self.rth_open.minute, second=0, microsecond=0)
        return int((ctx.local_close - open_dt).total_seconds() // 60)

    def allows(self, side: str) -> bool:
        direction = self.p.get("direction", "both")
        return direction == "both" or direction == side

    def tick(self, n: float = 1) -> float:
        return n * self.contract.tick_size

    @classmethod
    def describe_params(cls) -> list[tuple[str, Any]]:
        return list(cls.defaults.items())
