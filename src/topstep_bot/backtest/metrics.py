"""Performance statistics and a Topstep Trading Combine pass/fail simulator."""

from __future__ import annotations

import math
import statistics
from collections import Counter
from dataclasses import dataclass

from topstep_bot.engine import DayRecord
from topstep_bot.execution import ManagedTrade
from topstep_bot.risk.topstep import PlanSpec, combine_progress


def compute_metrics(trades: list[ManagedTrade], days: list[DayRecord], starting_balance: float) -> dict:
    n = len(trades)
    pnls = [t.net_pnl for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    gross_win = sum(wins)
    gross_loss = -sum(losses)
    rs = [r for t in trades if (r := t.r_multiple()) is not None]

    equity = peak = starting_balance
    max_dd = 0.0
    streak = max_streak = 0
    for p in pnls:
        equity += p
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
        streak = streak + 1 if p <= 0 else 0
        max_streak = max(max_streak, streak)

    daily = [d.pnl for d in days]
    traded_days = [d for d in days if d.trades]
    sharpe = None
    if len(daily) > 1 and statistics.pstdev(daily) > 0:
        sharpe = statistics.mean(daily) / statistics.pstdev(daily) * math.sqrt(252)

    return {
        "net_pnl": sum(pnls),
        "trades": n,
        "win_rate": len(wins) / n if n else 0.0,
        "avg_win": gross_win / len(wins) if wins else 0.0,
        "avg_loss": -gross_loss / len(losses) if losses else 0.0,
        "profit_factor": gross_win / gross_loss if gross_loss > 0 else (math.inf if gross_win > 0 else 0.0),
        "expectancy": sum(pnls) / n if n else 0.0,
        "avg_r": statistics.mean(rs) if rs else 0.0,
        "max_drawdown": max_dd,
        "max_losing_streak": max_streak,
        "fees": sum(t.fees for t in trades),
        "days": len(days),
        "traded_days": len(traded_days),
        "green_days": sum(1 for d in traded_days if d.pnl > 0),
        "best_day": max(daily, default=0.0),
        "worst_day": min(daily, default=0.0),
        "sharpe": sharpe,
        "longs": sum(1 for t in trades if t.side.label == "LONG"),
        "shorts": sum(1 for t in trades if t.side.label == "SHORT"),
        "exit_reasons": dict(Counter(t.exit_reason.split(": ")[0] for t in trades).most_common()),
    }


@dataclass
class CombineOutcome:
    status: str  # passed | failed | incomplete
    start_index: int
    days_used: int
    profit: float
    detail: str


def simulate_combine(days: list[DayRecord], plan: PlanSpec, start_index: int = 0) -> CombineOutcome:
    """Replay daily results through Topstep's Combine rules starting on ``days[start_index]``.

    Uses each day's P&L and its worst intraday equity (the MLL is enforced in real time).
    """
    size = plan.account_size
    balance = size
    floor = size - plan.max_loss_limit
    traded: list[float] = []
    for n, d in enumerate(days[start_index:], start=1):
        worst = balance + (d.min_equity - d.start_balance)
        if worst <= floor:
            return CombineOutcome("failed", start_index, n, worst - size, f"MLL breached on {d.day}")
        balance += d.pnl
        if d.trades:
            traded.append(d.pnl)
        floor = max(floor, min(balance - plan.max_loss_limit, size))
        profit = balance - size
        if combine_progress(plan, profit, max(traded, default=0.0)).passed:
            return CombineOutcome("passed", start_index, n, profit, f"target reached on {d.day}")
    return CombineOutcome("incomplete", start_index, len(days) - start_index, balance - size, "data ran out")


def combine_statistics(days: list[DayRecord], plan: PlanSpec) -> dict:
    """Pass rate when starting the Combine on every possible day of the backtest."""
    outcomes = [simulate_combine(days, plan, i) for i in range(len(days))]
    passed = [o for o in outcomes if o.status == "passed"]
    failed = [o for o in outcomes if o.status == "failed"]
    decided = len(passed) + len(failed)
    return {
        "from_first_day": outcomes[0] if outcomes else None,
        "attempts": decided,
        "pass_rate": len(passed) / decided if decided else None,
        "median_days_to_pass": statistics.median(o.days_used for o in passed) if passed else None,
        "median_days_to_fail": statistics.median(o.days_used for o in failed) if failed else None,
    }
