"""Funded-account knowledge: Topstep's rules for each account stage, and progress toward the goal.

Goals by stage:
  combine   reach the profit target without touching the Maximum Loss Limit, with the best day
            under 50% of total profit (consistency target)
  express   Express Funded Account (XFA): earn payouts. Standard path = winning days of $150+
            net; Consistency path = best day <= 40% of total profit over at least 3 trading days.
            Each payout restarts the count. Position size follows the Scaling Plan.
  practice  no goal - anything goes within the limits you set

What the bot does with this (all within your risk settings - it never trades bigger to chase a goal):
  * shows progress on the dashboard and in Telegram (/funded)
  * Combine: caps each day at 40% of the profit target (risk/manager.py) so one big day can't
    break the consistency target
  * XFA standard path: once today counts as a winning day, new trades are sized so a stop-out
    keeps it one (``funded.protect_winning_day``)
  * XFA consistency path: caps each day at ``consistency_limit`` x ``cycle_profit_goal``
  * preflight warns about settings that conflict with Topstep's prohibited-conduct rules

Rules change. RULES_CHECKED is when they were last compared with Topstep's help center; the
numbers in your TopstepX dashboard always win.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING

from topstep_bot.risk.topstep import COMBINE_CONSISTENCY_LIMIT, PlanSpec

if TYPE_CHECKING:
    from topstep_bot.config import FundedConfig

RULES_CHECKED = "2026-10-11"
SOURCES = {
    "xfa": "https://help.topstep.com/en/articles/8284215-express-funded-account-parameters",
    "scaling": "https://help.topstep.com/en/articles/8284223-what-is-the-scaling-plan",
    "prohibited": "https://help.topstep.com/en/articles/10305426-prohibited-trading-strategies-at-topstep",
    "hedging": "https://intercom.help/topstep-llc/en/articles/8284218",
}
# Largest payout per request (also limited to 50% of the balance), by plan and path. From Topstep's
# payout policy as summarised in 2026; check your dashboard.
PAYOUT_CAPS = {"standard": {"50K": 2_000, "100K": 3_000, "150K": 5_000},
               "consistency": {"50K": 3_000, "100K": 4_000, "150K": 6_000}}


@dataclass(frozen=True)
class Rule:
    stages: tuple[str, ...]  # combine / express / practice / all
    title: str
    text: str
    bot: str  # what the bot does about it
    source: str = ""


RULES: list[Rule] = [
    Rule(("all",), "Maximum Loss Limit",
         "Trails your highest end-of-day balance and never moves down; on a Combine it stops trailing at the starting "
         "balance. It is enforced in real time including open P&L - touching it closes the account.",
         "Tracks the floor, sizes every trade so a stop-out stays above it plus mll_buffer, and flattens if equity "
         "gets within half the buffer."),
    Rule(("all",), "Daily Loss Limit",
         "Optional on TopstepX accounts (none unless you added one).",
         "Applies your personal_daily_loss_limit always, and a Topstep DLL if you set account.topstep_daily_loss_limit."),
    Rule(("all",), "Flat by 15:10 CT",
         "All positions must be closed before 15:10 CT; Topstep starts liquidating at 15:08 CT.",
         "Flattens at session.flatten_at (15:00) and opens nothing after session.last_entry."),
    Rule(("combine",), "Profit target",
         "50K: $3,000, 100K: $6,000, 150K: $9,000, without breaking any rule.",
         "Shows progress; stops for the day at 40% of the target so the consistency target holds."),
    Rule(("combine",), "Consistency target",
         "Your best day should be under 50% of total profit when you pass; otherwise more profit is needed before "
         "the pass counts.",
         "Daily profit cap of 40% of the profit target (risk.daily_profit_target to change)."),
    Rule(("combine",), "Position limit",
         "5 / 10 / 15 minis for 50K / 100K / 150K (10 micros = 1 mini).",
         "Never sizes above it (risk.max_contracts can lower it further)."),
    Rule(("express",), "Scaling Plan",
         "Maximum position grows with your XFA balance (e.g. 50K: 2 minis below $1,500 profit, 3 from $1,500, 5 from "
         "$2,000). A higher limit applies from the next session; it never rises mid-session.",
         "Applies the tier for today's starting balance.", SOURCES["scaling"]),
    Rule(("express",), "Payout - Standard path",
         "Five winning days of at least $150 net profit (not necessarily in a row). Each payout restarts the count.",
         "Counts your winning days since funded.last_payout_date and, with protect_winning_day, sizes trades so a "
         "stop-out doesn't turn a winning day back into a non-winning one.", SOURCES["xfa"]),
    Rule(("express",), "Payout - Consistency path",
         "At least 3 trading days, and your largest winning day at most 40% of total net profit since the last payout.",
         "Tracks the ratio and caps each day at 40% of funded.cycle_profit_goal.", SOURCES["xfa"]),
    Rule(("express",), "Payout amount",
         "Up to 50% of the balance per request, capped by plan and path; 90/10 profit split.",
         "Shows an estimate of what you could request now.", SOURCES["xfa"]),
    Rule(("all",), "No hedging across accounts",
         "No opposite positions in the same or correlated markets across your accounts, and no coordinated trading "
         "with others.",
         "The hedge guard skips calls that would be opposite to another account's position (multi_account.hedge_guard).",
         SOURCES["hedging"]),
    Rule(("all",), "No full size into major news",
         "Purposefully trading your full maximum position size into a scheduled major news event is prohibited.",
         "Pauses new trades around high-impact releases and your instrument's key reports (news.enabled).",
         SOURCES["prohibited"]),
    Rule(("all",), "No simulator exploitation",
         "No high-frequency scalping, hundreds of rapid trades, tight brackets or auto-breakeven designed to capture "
         "unrealistic simulated fills, or orders outside the best bid/offer.",
         "Trades a few times a day on 1-minute-or-longer bars with ordinary stops, and prices every entry at or inside "
         "the best bid/offer. Keep risk.breakeven_at_r at 1R or more if you use it.", SOURCES["prohibited"]),
    Rule(("all",), "From your own computer",
         "Automated trading must run on your own PC - no VPS or VPN. API trading works on Combine, Express Funded and "
         "Practice accounts, not Live Funded.",
         "Runs locally; that part is up to you."),
]


def rules_for(stage: str | None = None) -> list[Rule]:
    return [r for r in RULES if stage is None or "all" in r.stages or stage in r.stages]


def rules_text(stage: str | None = None) -> str:
    lines = [f"Topstep rules the bot knows ({'all stages' if stage is None else stage}; checked {RULES_CHECKED}):"]
    for r in rules_for(stage):
        lines.append(f"• {r.title}: {r.text}\n  Bot: {r.bot}")
    lines.append("Rules change - your TopstepX dashboard and help.topstep.com always win.")
    return "\n".join(lines)


# ------------------------------------------------------------------------------ progress

def cycle_goal(cfg: FundedConfig, plan: PlanSpec) -> float:
    return cfg.cycle_profit_goal or plan.profit_target


def daily_cap(cfg: FundedConfig, plan: PlanSpec, stage: str) -> float | None:
    """The funded-goal daily profit cap for XFA accounts (the Combine's cap lives in the risk manager)."""
    if stage == "express" and cfg.payout_path == "consistency":
        return cfg.consistency_limit * cycle_goal(cfg, plan)
    return None


def protected_floor(cfg: FundedConfig, stage: str, day_pnl: float) -> float | None:
    """XFA standard path: once today is a winning day, the profit a new trade must not give back below."""
    if stage == "express" and cfg.payout_path == "standard" and cfg.protect_winning_day and day_pnl >= cfg.winning_day_usd:
        return cfg.winning_day_usd
    return None


def status(cfg: FundedConfig, plan: PlanSpec, stage: str, days: list[tuple[str, float, int]], today: date,
           today_pnl: float, balance: float, start_balance: float, max_minis: int, today_trades: int = 0) -> dict:
    """Progress toward this stage's goal.

    ``days``: finished trading days as (ISO day, net P&L, trades) from the trade journal; today's
    running P&L is added on top.
    """
    since = cfg.last_payout_date.isoformat() if cfg.last_payout_date and stage == "express" else ""
    hist = [(d, p, n) for d, p, n in days if d > since and d != today.isoformat()]
    all_days = [*hist, (today.isoformat(), today_pnl, today_trades)]
    pnls = [p for _, p, _ in all_days]
    total = sum(pnls)
    best = max(pnls, default=0.0)
    out: dict = {"stage": stage, "plan": plan.name, "rules_checked": RULES_CHECKED, "today_pnl": round(today_pnl, 2),
                 "since": since or None, "days": len(hist), "total": round(total, 2), "best_day": round(best, 2)}
    if stage == "combine":
        profit = balance - start_balance
        ratio = best / total if total > 0 else None
        needed = best / COMBINE_CONSISTENCY_LIMIT if best > 0 else 0.0
        out.update({
            "goal": "Pass the Combine", "target": plan.profit_target, "profit": round(profit, 2),
            "remaining": round(max(0.0, plan.profit_target - profit), 2),
            "progress": round(max(0.0, profit) / plan.profit_target, 3),
            "consistency": round(ratio, 3) if ratio is not None else None, "consistency_limit": COMBINE_CONSISTENCY_LIMIT,
            "consistency_ok": ratio is not None and ratio < COMBINE_CONSISTENCY_LIMIT,
            "needed_for_consistency": round(needed, 2),
        })
        lines = [f"Profit ${profit:,.0f} of ${plan.profit_target:,.0f} ({out['progress']:.0%})"]
        if ratio is not None:
            lines.append(f"Best day ${best:,.0f} = {ratio:.0%} of profit (must be under 50%)"
                         + ("" if out["consistency_ok"] else f" - you'd need ${needed:,.0f} total for it to count"))
        out["summary"] = lines
    elif stage == "express":
        payout_cap = PAYOUT_CAPS[cfg.payout_path].get(plan.name, 0)
        payout = max(0.0, min(0.5 * balance, payout_cap))
        out.update({"goal": f"Next payout ({cfg.payout_path} path)", "path": cfg.payout_path, "max_minis": max_minis,
                    "payout_estimate": round(payout, 2), "payout_cap": payout_cap})
        if cfg.payout_path == "standard":
            wins = [d for d, p, _ in all_days if p >= cfg.winning_day_usd]
            out.update({"winning_days": len(wins), "winning_days_needed": cfg.winning_days_needed,
                        "winning_day_usd": cfg.winning_day_usd, "today_is_winning": today_pnl >= cfg.winning_day_usd,
                        "eligible": len(wins) >= cfg.winning_days_needed})
            out["summary"] = [f"Winning days (${cfg.winning_day_usd:,.0f}+): {len(wins)} of {cfg.winning_days_needed}",
                              f"Today ${today_pnl:,.0f}" + (" - a winning day" if out["today_is_winning"] else
                                                            f" - ${cfg.winning_day_usd - today_pnl:,.0f} to a winning day")]
        else:
            traded = [d for d, _, n in all_days if n]
            ratio = best / total if total > 0 else None
            cap = daily_cap(cfg, plan, stage)
            out.update({"trading_days": len(traded), "trading_days_needed": cfg.consistency_days_needed,
                        "consistency": round(ratio, 3) if ratio is not None else None,
                        "consistency_limit": cfg.consistency_limit, "daily_cap": cap,
                        "eligible": len(traded) >= cfg.consistency_days_needed and ratio is not None
                        and ratio <= cfg.consistency_limit})
            out["summary"] = [f"Trading days: {len(traded)} of {cfg.consistency_days_needed}",
                              (f"Best day ${best:,.0f} = {ratio:.0%} of ${total:,.0f} profit (max {cfg.consistency_limit:.0%})"
                               if ratio is not None else "No profit yet this cycle"),
                              f"Daily cap ${cap:,.0f} ({cfg.consistency_limit:.0%} of your ${cycle_goal(cfg, plan):,.0f} goal)"]
        out["summary"].append(f"Payout you could request now: about ${payout:,.0f} (50% of balance, max ${payout_cap:,.0f}); "
                              f"position limit {max_minis} mini(s)")
        if out.get("eligible"):
            out["summary"].append("✅ Payout requirements look met - check your TopstepX dashboard.")
    else:
        out.update({"goal": "Practice", "summary": [f"Practice account: P&L ${total:,.0f} over {len(all_days)} day(s)"]})
    return out


def status_text(st: dict) -> str:
    lines = [f"🏁 {st['plan']} {st['stage']}: {st['goal']}"]
    lines += [f"• {s}" for s in st["summary"]]
    if st.get("since"):
        lines.append(f"(counting since your last payout on {st['since']})")
    lines.append(f"Rules checked {st['rules_checked']} - /rules for the full list.")
    return "\n".join(lines)
