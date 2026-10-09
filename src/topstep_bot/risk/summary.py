"""Plain-language summary of Topstep's rules for this account and what the bot does about each
('topstep-bot rules')."""

from __future__ import annotations

from topstep_bot.config import BotConfig
from topstep_bot.instruments import SPECS, offline_contract
from topstep_bot.risk.manager import CONSISTENCY_GUARD_FRACTION, DEFAULT_COMBINE_DAILY_CAP
from topstep_bot.risk.topstep import FLAT_BY, PLANS, max_contracts_allowed, product_limit, starting_balance_for


def _money(v: float) -> str:
    return f"${v:,.0f}"


def rule_rows(cfg: BotConfig) -> list[tuple[str, str, str]]:
    """(rule, Topstep's limit for this account, what the bot does) rows."""
    plan = PLANS[cfg.account.plan]
    stage = cfg.account.stage
    risk = cfg.risk
    rows: list[tuple[str, str, str]] = []

    floor_start = starting_balance_for(plan, stage) - plan.max_loss_limit
    rows.append((
        "Maximum Loss Limit",
        f"{_money(plan.max_loss_limit)} below the highest end-of-day balance (starts at {_money(floor_start)}), "
        "checked live with open P&L",
        f"sizes every trade to stay {_money(risk.mll_buffer)} above it; closes trades {_money(risk.mll_buffer / 2)} above it",
    ))

    dll = cfg.account.topstep_daily_loss_limit
    rows.append((
        "Daily Loss Limit",
        f"{_money(dll)} (you added it at checkout)" if dll else
        f"none set (optional {_money(plan.daily_loss_limit)} if you added it at checkout)",
        f"your limit {_money(risk.personal_daily_loss_limit)} per day" +
        (f"; stops new trades at {_money(0.9 * dll)} and closes trades at {_money(0.95 * dll)}" if dll else ""),
    ))

    if stage == "combine":
        cap = risk.daily_profit_target if risk.daily_profit_target is not None else DEFAULT_COMBINE_DAILY_CAP * plan.profit_target
        rows.append((
            "Profit target",
            f"{_money(plan.profit_target)}",
            "stops trading once reached" if risk.stop_at_profit_target else "keeps trading after it (stop_at_profit_target is off)",
        ))
        rows.append((
            "Consistency Target",
            f"best day at most {_money(plan.consistency_day_limit)} (55% of the target), or the target goes up",
            f"no new trades after {_money(cap)} in a day"
            + (f"; closes out at {_money(CONSISTENCY_GUARD_FRACTION * plan.profit_target)}" if risk.consistency_guard else
               " (consistency_guard is off)"),
        ))
    elif stage == "express":
        path = cfg.account.payout_path
        rows.append((
            "Payouts",
            "5 winning days of $150+" if path == "standard" else "3 trading days, best day at most 40% of net profit",
            "trades the same way; your TopstepX dashboard tracks payout progress",
        ))

    symbol = cfg.instrument.symbol
    is_micro = offline_contract(symbol).is_micro if symbol in SPECS else symbol.startswith("M")
    start = cfg.account.starting_balance if cfg.account.starting_balance is not None else starting_balance_for(plan, stage)
    cap_now = max_contracts_allowed(plan, stage, start, symbol, is_micro, start)
    unit = "micros" if is_micro else "contracts"
    topstep_cap = f"{cap_now} {symbol} {unit}"
    if stage == "express":
        topstep_cap += " today (Scaling Plan, grows with the balance)"
    if product_limit(symbol, plan) is not None:
        topstep_cap += f" (product limit for {symbol})"
    bot_cap = f"sizes from {_money(risk.risk_per_trade)} risk per trade, never above the limit"
    if risk.max_contracts:
        bot_cap += f" or your cap of {risk.max_contracts}"
    rows.append(("Position size", topstep_cap, bot_cap))

    s = cfg.session
    rows.append((
        "Trading hours",
        f"flat by {FLAT_BY:%H:%M} CT every day, no overnight or weekend positions",
        f"enters {s.trade_start:%H:%M}-{s.last_entry:%H:%M} CT, closes everything at {s.flatten_at:%H:%M} CT",
    ))
    news = cfg.news
    rows.append((
        "News",
        "no maximum-size position into a scheduled major release",
        (f"pauses entries {news.minutes_before} min before to {news.minutes_after} min after high-impact news; "
         if news.enabled else "news pauses are OFF; ") + "half size near releases; closes full-size positions before them",
    ))
    rows.append((
        "Automation",
        "your own computer only (no VPS/VPN), no high-frequency trading, not on Live Funded accounts",
        "warns on servers/VMs, stops and closes the trade after 30 order actions a minute, refuses Live accounts",
    ))
    return rows
