"""Strategy base class.

A strategy sees one closed bar at a time and may return a Signal:
  Signal("long" | "short", stop_price=..., target_price=..., reason=...)  to enter
  Signal("exit", reason=...)                                             to close
Position sizing, risk limits, order placement and session rules are handled by the bot,
so a strategy only decides *when* and *where* (stop/target) - never *how much*.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
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
