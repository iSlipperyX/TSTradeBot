"""Topstep account rules (Trading Combine and Express Funded Account), as code.

Source: Topstep Help Center, checked October 2026 (Trading Combine Parameters, Maximum Loss
Limit, Consistency Target, Daily Loss Limit, Scaling Plan, TopstepX API Access). Topstep changes
its rules from time to time - always confirm against your TopstepX dashboard. docs/TOPSTEP_RULES.md
explains each rule and the guard that enforces it.

* Maximum Loss Limit (MLL): trails the highest END-OF-DAY balance, never moves down, and locks
  once it reaches the starting balance ($0 in an XFA). It is enforced in REAL TIME including
  unrealized P&L - touching it liquidates the account.
* Daily Loss Limit (DLL): optional, chosen at checkout ($1,000 / $2,000 / $3,000 for
  50K / 100K / 150K) and fixed afterwards. Hitting it flattens the account and blocks trading
  until 17:00 CT; it is not a rule violation.
* Consistency Target (Combine): the best single day must stay at or below 55% of the Profit
  Target. A bigger day raises the target to ``best day / 0.55``.
* Position caps: 5 / 10 / 15 minis (10 micros = 1 mini) for 50K / 100K / 150K. Express Funded
  Accounts follow a balance-based Scaling Plan. Some metals and energy products have their own,
  lower caps (see PRODUCT_LIMITS).
* All positions must be flat by 15:10 CT. The trading day runs 17:00 - 15:10 CT.
* News: no blackout window, but taking your MAXIMUM position size into a scheduled major
  economic release is a prohibited strategy.
* Automation: allowed in the Combine and XFA through the TopstepX (ProjectX) API, from your own
  computer only (no VPS / VPN / remote servers), no high-frequency trading. Live Funded Accounts
  may NOT trade through the API.
* XFA payouts: Standard path = 5 winning days of $150+; Consistency path = 3 trading days with the
  best day at or below 40% of net profit.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import time

COMBINE_CONSISTENCY_TARGET = 0.55  # best day / profit target
XFA_CONSISTENCY_LIMIT = 0.40  # best day / net profit (XFA "Consistency" payout path)
XFA_WINNING_DAY = 150.0
XFA_STANDARD_WINNING_DAYS = 5
XFA_CONSISTENCY_DAYS = 3
FLAT_BY = time(15, 10)  # Chicago time
MICROS_PER_MINI = 10

# Kept for older imports; the Combine rule is now measured against the profit target.
COMBINE_CONSISTENCY_LIMIT = COMBINE_CONSISTENCY_TARGET


@dataclass(frozen=True)
class PlanSpec:
    name: str
    account_size: float
    profit_target: float
    max_loss_limit: float
    max_minis: int
    daily_loss_limit: float  # the optional Topstep DLL chosen at checkout
    # Express Funded Account scaling plan: (minimum balance, max minis) tiers.
    xfa_scaling: tuple[tuple[float, int], ...]

    @property
    def legacy_daily_loss_limit(self) -> float:  # old name, kept for compatibility
        return self.daily_loss_limit

    @property
    def consistency_day_limit(self) -> float:
        """Largest single-day profit that does not raise the Combine profit target."""
        return COMBINE_CONSISTENCY_TARGET * self.profit_target


PLANS: dict[str, PlanSpec] = {
    "50K": PlanSpec("50K", 50_000, 3_000, 2_000, 5, 1_000, ((0, 2), (1_500, 3), (2_000, 5))),
    "100K": PlanSpec("100K", 100_000, 6_000, 3_000, 10, 2_000, ((0, 3), (1_500, 4), (2_000, 5), (3_000, 10))),
    "150K": PlanSpec(
        "150K", 150_000, 9_000, 4_500, 15, 3_000, ((0, 3), (1_500, 4), (2_000, 5), (3_000, 10), (4_500, 15))
    ),
}
_PLAN_INDEX = {"50K": 0, "100K": 1, "150K": 2}

# Product-specific caps in CONTRACTS of that product for 50K / 100K / 150K (Combine and XFA).
# Topstep calls these temporary and changes them with market conditions; 0 = not tradable.
PRODUCT_LIMITS: dict[str, tuple[int, int, int]] = {
    "GC": (3, 6, 9),
    "MGC": (30, 60, 90),
    "CL": (3, 6, 9),
    "MCL": (30, 60, 90),
    "SI": (0, 0, 0),
    "HG": (0, 0, 0),
    "PL": (0, 0, 0),
}


def starting_balance_for(plan: PlanSpec, stage: str) -> float:
    """Express Funded Accounts start at $0; Combines and practice accounts at the plan size."""
    return 0.0 if stage == "express" else plan.account_size


def max_minis_allowed(plan: PlanSpec, stage: str, start_of_day_balance: float) -> int:
    """Topstep's maximum position size in mini-contract units for today's session.

    In an XFA the limit follows the Scaling Plan and only changes between sessions, so it is
    computed from the balance at the start of the day.
    """
    if stage != "express":
        return plan.max_minis
    profit = start_of_day_balance - starting_balance_for(plan, stage)
    allowed = plan.xfa_scaling[0][1]
    for threshold, minis in plan.xfa_scaling:
        if profit >= threshold:
            allowed = minis
    return allowed


def product_limit(root: str, plan: PlanSpec) -> int | None:
    """Topstep's product-specific cap (in contracts of ``root``), or None when only the plan cap applies."""
    caps = PRODUCT_LIMITS.get(root.upper())
    return None if caps is None else caps[_PLAN_INDEX[plan.name]]


def max_contracts_allowed(plan: PlanSpec, stage: str, start_of_day_balance: float, root: str, is_micro: bool) -> int:
    """Topstep's maximum position in contracts of this instrument: plan/scaling cap and product cap."""
    minis = max_minis_allowed(plan, stage, start_of_day_balance)
    cap = minis * MICROS_PER_MINI if is_micro else minis
    product = product_limit(root, plan)
    return cap if product is None else min(cap, product)


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
    required_total: float  # total profit needed for the best day to be within the limit


def consistency(daily_pnls: list[float], limit: float = COMBINE_CONSISTENCY_TARGET) -> ConsistencyStatus:
    """Best day versus total profit: ``ok`` when the best day is at most ``limit`` of the total."""
    total = sum(daily_pnls)
    best = max(daily_pnls, default=0.0)
    ratio = best / total if total > 0 else None
    ok = total > 0 and best <= limit * total + 1e-9
    required = best / limit if best > 0 else 0.0
    return ConsistencyStatus(total, best, ratio, ok, required)


@dataclass
class CombineProgress:
    total_profit: float
    best_day: float
    profit_target: float  # after any consistency increase
    remaining: float
    passed: bool
    target_raised: bool

    def describe(self) -> str:
        if self.passed:
            return f"profit target reached (${self.total_profit:,.0f} of ${self.profit_target:,.0f})"
        text = f"${self.total_profit:,.0f} of ${self.profit_target:,.0f}, ${self.remaining:,.0f} to go"
        if self.target_raised:
            text += f" (target raised by the ${self.best_day:,.0f} best day)"
        return text


def combine_progress(plan: PlanSpec, total_profit: float, best_day: float) -> CombineProgress:
    """Where a Combine stands. The target is ``max(profit target, best day / 0.55)``."""
    target = max(plan.profit_target, best_day / COMBINE_CONSISTENCY_TARGET if best_day > 0 else 0.0)
    remaining = max(0.0, target - total_profit)
    return CombineProgress(total_profit, best_day, target, remaining, total_profit >= target - 1e-9, target > plan.profit_target)


@dataclass
class PayoutProgress:
    path: str
    eligible: bool
    detail: str


def xfa_payout_progress(daily_pnls: list[float], path: str = "standard") -> PayoutProgress:
    """Progress toward an Express Funded Account payout (days since the last payout)."""
    if path == "consistency":
        traded = len(daily_pnls)
        status = consistency(daily_pnls, XFA_CONSISTENCY_LIMIT)
        eligible = traded >= XFA_CONSISTENCY_DAYS and status.ok
        ratio = f"{status.ratio:.0%}" if status.ratio is not None else "n/a"
        return PayoutProgress(path, eligible, f"{traded}/{XFA_CONSISTENCY_DAYS} trading days, best day {ratio} of net profit "
                              f"(must be {XFA_CONSISTENCY_LIMIT:.0%} or less)")
    wins = sum(1 for p in daily_pnls if p >= XFA_WINNING_DAY)
    return PayoutProgress(path, wins >= XFA_STANDARD_WINNING_DAYS,
                          f"{wins}/{XFA_STANDARD_WINNING_DAYS} winning days of ${XFA_WINNING_DAY:,.0f}+")
