"""Topstep account rules (Trading Combine and Express Funded Account).

Sources: Topstep Help Center (Maximum Loss Limit, trading hours, scaling plan articles).
Rules change from time to time - always confirm against your TopstepX dashboard.

* Maximum Loss Limit (MLL): trails the highest END-OF-DAY balance, never moves down, and
  locks once it reaches the starting balance. It is enforced in REAL TIME including
  unrealized P&L - touching it liquidates the account.
* Daily Loss Limit: new/reset TopstepX accounts (since Aug 2024) have none by default.
* Position caps: 5 / 10 / 15 minis (micros count 1/10) for 50K / 100K / 150K.
  Express Funded Accounts follow a balance-based Scaling Plan instead.
* Combine consistency: the best day must be under 50% of total profit when passing.
* All positions must be flat by 15:10 CT; Topstep starts flattening at 15:08 CT.
"""

from __future__ import annotations

from dataclasses import dataclass

COMBINE_CONSISTENCY_LIMIT = 0.50


@dataclass(frozen=True)
class PlanSpec:
    name: str
    account_size: float
    profit_target: float
    max_loss_limit: float
    max_minis: int
    legacy_daily_loss_limit: float
    # Express Funded Account scaling plan: (minimum balance, max minis) tiers.
    xfa_scaling: tuple[tuple[float, int], ...]


PLANS: dict[str, PlanSpec] = {
    "50K": PlanSpec("50K", 50_000, 3_000, 2_000, 5, 1_000, ((0, 2), (1_500, 3), (2_000, 5))),
    "100K": PlanSpec("100K", 100_000, 6_000, 3_000, 10, 2_000, ((0, 3), (1_500, 4), (2_000, 5), (3_000, 10))),
    "150K": PlanSpec(
        "150K", 150_000, 9_000, 4_500, 15, 3_000, ((0, 3), (1_500, 4), (2_000, 5), (3_000, 10), (4_500, 15))
    ),
}


def starting_balance_for(plan: PlanSpec, stage: str) -> float:
    """Express Funded Accounts start at $0; Combines and practice accounts at the plan size."""
    return 0.0 if stage == "express" else plan.account_size


def max_minis_allowed(plan: PlanSpec, stage: str, start_of_day_balance: float) -> int:
    """Topstep's maximum position size in mini-contract units for today's session."""
    if stage != "express":
        return plan.max_minis
    profit = start_of_day_balance - starting_balance_for(plan, stage)
    allowed = plan.xfa_scaling[0][1]
    for threshold, minis in plan.xfa_scaling:
        if profit >= threshold:
            allowed = minis
    return allowed


class LossLimitTracker:
    """Tracks the trailing Maximum Loss Limit floor."""

    def __init__(self, starting_balance: float, max_loss_limit: float, floor: float | None = None):
        self.starting_balance = starting_balance
        self.max_loss_limit = max_loss_limit
        self.floor = floor if floor is not None else starting_balance - max_loss_limit

    def end_of_day(self, balance: float) -> float:
        """Apply an end-of-day balance; returns the (possibly raised) floor."""
        candidate = min(balance - self.max_loss_limit, self.starting_balance)
        self.floor = max(self.floor, candidate)
        return self.floor

    def room(self, equity: float) -> float:
        """Dollars between current equity (balance + open P&L) and the floor."""
        return equity - self.floor

    def breached(self, equity: float) -> bool:
        return equity <= self.floor


@dataclass
class ConsistencyStatus:
    total_profit: float
    best_day: float
    ratio: float | None  # best day / total profit
    ok: bool
    required_total: float  # total profit needed for the best day to be < 50%


def consistency(daily_pnls: list[float], limit: float = COMBINE_CONSISTENCY_LIMIT) -> ConsistencyStatus:
    total = sum(daily_pnls)
    best = max(daily_pnls, default=0.0)
    ratio = best / total if total > 0 else None
    ok = total > 0 and best < limit * total
    required = best / limit if best > 0 else 0.0
    return ConsistencyStatus(total, best, ratio, ok, required)
