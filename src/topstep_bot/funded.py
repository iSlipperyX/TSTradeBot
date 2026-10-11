"""Funded-account goals: progress toward passing the Combine or the next Express Funded payout.

The rules themselves live in risk/topstep.py (and are explained in docs/TOPSTEP_RULES.md and by
``topstep-bot rules``). This module adds what the bot does with them while it runs:

  * progress on the dashboard (Account goal panel) and in Telegram (/funded):
      combine   profit against the (consistency-adjusted) target
      express   payout progress since your last payout (``funded.last_payout_date``), on the path
                you chose at activation (``account.payout_path``), an estimate of what you could
                request now, and today's Scaling Plan limit
  * XFA standard path: once today counts as a winning day ($150+), new trades are sized so a
    stop-out keeps it one (``funded.protect_winning_day``)
  * XFA consistency path: each day is capped at 40% of ``funded.cycle_profit_goal``, so no single
    day can break the 40% rule once you reach the profit you're building toward

None of this ever makes the bot trade bigger or more often: the goal rules only make it more careful.
"""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING

from topstep_bot.risk.topstep import (
    XFA_CONSISTENCY_LIMIT,
    XFA_WINNING_DAY,
    PlanSpec,
    combine_progress,
    xfa_payout_progress,
)

if TYPE_CHECKING:
    from topstep_bot.config import FundedConfig

# Largest payout per request (also limited to 50% of the balance), by path and plan. From Topstep's
# payout policy as summarised in 2026 - the official page gives only the overall maximums
# ($5,000 standard, $6,000 consistency). Check your dashboard.
PAYOUT_CAPS = {"standard": {"50K": 2_000, "100K": 3_000, "150K": 5_000},
               "consistency": {"50K": 3_000, "100K": 4_000, "150K": 6_000}}


def cycle_goal(cfg: FundedConfig, plan: PlanSpec) -> float:
    return cfg.cycle_profit_goal or plan.profit_target


def daily_cap(cfg: FundedConfig, plan: PlanSpec, stage: str, path: str) -> float | None:
    """XFA consistency path: the most one day may make (the Combine's cap lives in the risk manager)."""
    if stage == "express" and path == "consistency":
        return XFA_CONSISTENCY_LIMIT * cycle_goal(cfg, plan)
    return None


def protected_floor(cfg: FundedConfig, stage: str, path: str, day_pnl: float) -> float | None:
    """XFA standard path: once today is a winning day, the profit a new trade must not give back below."""
    if stage == "express" and path == "standard" and cfg.protect_winning_day and day_pnl >= XFA_WINNING_DAY:
        return XFA_WINNING_DAY
    return None


def status(cfg: FundedConfig, path: str, plan: PlanSpec, stage: str, days: list[tuple[str, float, int]], today: date,
           today_pnl: float, balance: float, start_balance: float, max_minis: int, today_trades: int = 0) -> dict:
    """Progress toward this stage's goal.

    ``days``: finished trading days as (ISO day, net P&L, trades) from the trade journal; today's
    running P&L is added on top.
    """
    since = cfg.last_payout_date.isoformat() if cfg.last_payout_date and stage == "express" else ""
    hist = [(d, p, n) for d, p, n in days if d > since and d != today.isoformat()]
    all_days = [*hist, (today.isoformat(), today_pnl, today_trades)]
    out: dict = {"stage": stage, "plan": plan.name, "since": since or None, "today_pnl": round(today_pnl, 2)}
    if stage == "combine":
        best = max([p for _, p, _ in all_days], default=0.0)
        profit = balance - start_balance
        prog = combine_progress(plan, profit, best)
        out.update({"goal": "Pass the Combine", "target": round(prog.profit_target, 2), "profit": round(profit, 2),
                    "remaining": round(prog.remaining, 2), "progress": round(max(0.0, profit) / prog.profit_target, 3),
                    "best_day": round(best, 2), "target_raised": prog.target_raised, "eligible": prog.passed,
                    "summary": [f"Profit {prog.describe()}",
                                f"Best day ${best:,.0f} (keep it at or below ${plan.consistency_day_limit:,.0f}, "
                                "55% of the target, or the target goes up)"]})
    elif stage == "express":
        # Only days with trades count toward the consistency path; every day can be a winning day.
        pnls = [p for _, p, n in all_days if n or path == "standard"]
        prog = xfa_payout_progress(pnls, path)
        cap = PAYOUT_CAPS[path].get(plan.name, 0)
        payout = max(0.0, min(0.5 * balance, cap))
        winning = today_pnl >= XFA_WINNING_DAY
        out.update({"goal": f"Next payout ({path} path)", "path": path, "eligible": prog.eligible, "detail": prog.detail,
                    "payout_estimate": round(payout, 2), "payout_cap": cap, "max_minis": max_minis,
                    "today_is_winning": winning})
        if path == "standard":
            out["winning_days"] = sum(1 for p in pnls if p >= XFA_WINNING_DAY)
            out["winning_days_needed"] = 5
        else:
            out["trading_days"] = len(pnls)
            out["trading_days_needed"] = 3
            out["daily_cap"] = daily_cap(cfg, plan, stage, path)
        lines = [prog.detail[:1].upper() + prog.detail[1:]]
        if path == "standard":
            lines.append(f"Today ${today_pnl:,.0f}" + (" - a winning day" if winning else
                                                      f" - ${XFA_WINNING_DAY - today_pnl:,.0f} to a winning day"))
        else:
            lines.append(f"Daily cap ${out['daily_cap']:,.0f} (40% of your ${cycle_goal(cfg, plan):,.0f} goal)")
        lines.append(f"Payout you could request now: about ${payout:,.0f} (50% of balance, max ${cap:,.0f}); "
                     f"position limit {max_minis} mini(s) today")
        if prog.eligible:
            lines.append("✅ Payout requirements look met - check your TopstepX dashboard.")
        out["summary"] = lines
    else:
        total = sum(p for _, p, _ in all_days)
        out.update({"goal": "Practice", "eligible": False,
                    "summary": [f"Practice account: P&L ${total:,.0f} over {len(all_days)} day(s)"]})
    return out


def status_text(st: dict) -> str:
    lines = [f"🏁 {st['plan']} {st['stage']}: {st['goal']}"]
    lines += [f"• {s}" for s in st["summary"]]
    if st.get("since"):
        lines.append(f"(counting since your last payout on {st['since']})")
    lines.append("Send /rules for every Topstep rule and what the bot does about it.")
    return "\n".join(lines)
