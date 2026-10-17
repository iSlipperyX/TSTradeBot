"""One account's shared risk across every market it trades (the "account book").

Topstep's limits belong to the account, not to a market: the Maximum Loss Limit, the daily loss
limits, the Consistency Target and the maximum position size all count every open position
together. When the bot trades several markets in one account, each market (a "lane") has its
own TradingCore and RiskManager, and all of them share one AccountBook:

* The day's state (trades, wins, losing streak, cooldown, lock, realized P&L, best point of the
  day, pause and ramp-up) is kept once, here, so a loss in one market counts in all of them.
* Every lane registers a live view of its exposure (``Exposure``): open P&L, what it can still
  lose if its stop fills late (stop + slippage + jump allowance + fees), what it can still make
  at its target, and its size in mini-contract units.
* Before a lane sizes a trade, every hard limit is reduced by what the *other* lanes can still
  lose; the consistency-line room is reduced by what they can still make; and Topstep's position
  cap is reduced by the mini units they already hold (10 micros = 1 mini).

With one lane (the usual single-market bot) the other lanes' exposure is always zero, so every
check is exactly what it was before.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime

MICROS_PER_MINI = 10


@dataclass(frozen=True)
class Exposure:
    """What one lane has on right now (all in account dollars)."""

    open_pnl: float = 0.0
    at_risk: float = 0.0  # further loss from the current price if every stop fills late (>= 0)
    upside: float = 0.0  # further gain from the current price if every target fills (>= 0)
    minis: float = 0.0  # position (or pending entry) size in mini-contract units
    contracts: int = 0
    symbol: str = ""

    def __add__(self, other: Exposure) -> Exposure:
        return Exposure(self.open_pnl + other.open_pnl, self.at_risk + other.at_risk, self.upside + other.upside,
                        self.minis + other.minis, self.contracts + other.contracts, "")


NONE = Exposure()


def mini_units(contracts: int, is_micro: bool) -> float:
    """A position's size in Topstep's mini-contract units."""
    return abs(contracts) / MICROS_PER_MINI if is_micro else float(abs(contracts))


class AccountBook:
    """The day's state and the open exposure of one account, shared by all of its lanes."""

    def __init__(self, *, shared: bool = False):
        # ``shared``: a desk runs several lanes on this book. Lanes then don't reset the day when they
        # start one (the desk starts it once for the whole account).
        self.shared = shared
        self.day: date | None = None
        self.day_start_balance = 0.0
        self.trades_today = 0
        self.wins_today = 0
        self.consecutive_losses = 0
        self.last_loss_at: datetime | None = None
        self.lock_reason: str | None = None
        self.paused = False
        self.risk_scale = 1.0  # < 1 during the live ramp-up period
        self.best_prior_day = 0.0  # best finished day of the Combine so far (Consistency Target)
        self.realized_today = 0.0
        self.day_peak = 0.0  # best realized P&L today (the aggressive mode's lock-in)
        self.goal: float | None = None  # today's profit goal from the day plan (day_plan.py): trading stops once reached
        self.goal_text = ""  # where it came from, for the lock message ("your goal", "last goal", "suggested goal")
        self._lanes: dict[str, Callable[[], Exposure]] = {}

    # ------------------------------------------------------------------ lanes

    def register(self, key: str, exposure: Callable[[], Exposure]) -> None:
        self._lanes[key] = exposure

    def unregister(self, key: str) -> None:
        self._lanes.pop(key, None)

    @property
    def lanes(self) -> list[str]:
        return list(self._lanes)

    def exposure(self, key: str) -> Exposure:
        fn = self._lanes.get(key)
        return fn() if fn else NONE

    def others(self, key: str | None) -> Exposure:
        """Everything the account has on outside lane ``key``."""
        total = NONE
        for k, fn in self._lanes.items():
            if k != key:
                total = total + fn()
        return total

    def total(self) -> Exposure:
        return self.others(None)

    # -------------------------------------------------------------- day state

    def start_day(self, day: date, balance: float, realized_so_far: float = 0.0,
                  closed_today: list[tuple[datetime, float]] | None = None) -> None:
        """Begin a trading day for the whole account. ``closed_today``: (time, net P&L) of trades already
        closed today (after a restart), so counts, the losing streak and the cooldown carry on."""
        self.day = day
        self.day_start_balance = balance - realized_so_far
        self.trades_today = 0
        self.wins_today = 0
        self.consecutive_losses = 0
        self.last_loss_at = None
        self.lock_reason = None
        self.realized_today = 0.0
        self.day_peak = 0.0
        for ts, net_pnl in sorted(closed_today or []):
            self.record_trade(net_pnl, ts)
        if not closed_today:
            self.realized_today = realized_so_far
            self.day_peak = max(0.0, realized_so_far)

    def record_trade(self, net_pnl: float, ts: datetime) -> None:
        self.trades_today += 1
        self.realized_today += net_pnl
        self.day_peak = max(self.day_peak, self.realized_today)
        if net_pnl < 0:
            self.consecutive_losses += 1
            self.last_loss_at = ts
        else:
            self.wins_today += 1
            self.consecutive_losses = 0

    def lock(self, reason: str) -> str:
        if self.lock_reason is None:
            self.lock_reason = reason
        return reason
