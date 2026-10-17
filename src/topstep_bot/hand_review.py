"""Your hand against the plan: what closing trades by hand, and trading by hand, has done on this account.

Built from the journal only (reports; no trading decision reads it):

* **Bot trades you closed by hand** (dashboard, phone, or on TopstepX itself). The adaptive strategy's
  sub-strategies are also followed as ideas, so each such trade usually has a record of what its own plan
  (stop and target) went on to do. The difference is what closing by hand made or cost.
* **Your own trades** (the trade ticket, and ideas you took from the dashboard): how winners and losers end.
* **Bot trades left alone**, for comparison.

Why it exists: in the first live week (Oct 2026, two Combine accounts) the bot's own trades made money and
the trades opened or closed by hand gave it back - winners were closed at about a quarter of R after
reaching most of one R, while losers ran to the full stop. Nothing here changes how the bot trades; it keeps
the score so the pattern is visible while you are deciding whether to step in.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from topstep_bot import fmt

if TYPE_CHECKING:
    from topstep_bot.journal import Journal

MANUAL = "manual"
FIRST_TRADE = "first_trade"
TAKEN_IDEA = "taken from dashboard"  # entry reason of an idea opened from the dashboard (manual.py)
OUTSIDE = "position closed outside the bot"
MATCH_WINDOW = timedelta(minutes=15)  # a bot trade's entry follows its signal's bar close within this


def closed_by_hand(exit_reason: str | None) -> bool:
    why = exit_reason or ""
    return why.startswith("closed from") or why == OUTSIDE


def _kind(t: dict) -> str:
    if t.get("strategy") == MANUAL:
        return "own"
    if (t.get("entry_reason") or "").startswith(TAKEN_IDEA):
        return "idea"
    return "bot"


def _when(text: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(text) if text else None
    except ValueError:
        return None


def _plan_for(trade: dict, decisions: list[dict], recs: list[dict]) -> dict | None:
    """The idea record that followed this bot trade's own plan after it was closed by hand, if there is one."""
    side = trade.get("side")
    created = strategy = None
    for d in decisions:  # the decision record names the sub-strategy and its signal time
        if d.get("tag") and d.get("tag") == trade.get("tag"):
            created, strategy = d.get("created"), d.get("strategy")
            break
    entry = _when(trade.get("entry_time"))
    if created is None and entry is not None:  # older trades: the bot's own record of the signal it took
        for r in recs:
            when = _when(r.get("created"))
            if (r.get("active") and r.get("status") == "taken" and r.get("side") == side and when is not None
                    and timedelta(0) <= entry - when <= MATCH_WINDOW):
                created, strategy = r.get("created"), r.get("strategy")
                break
    if created is None:
        return None
    for r in recs:
        if (not r.get("active") and r.get("strategy") == strategy and r.get("side") == side
                and r.get("created") == created and r.get("outcome_r") is not None and r.get("result") != "void"):
            return {"strategy": strategy, "r": r["outcome_r"], "why": (r.get("note") or "").split("; ")[-1]}
    return None


def _usd_per_r(t: dict) -> float | None:
    r, gross = t.get("r_multiple"), t.get("gross_pnl")
    if r is None or gross is None or abs(r) < 0.1:
        return None
    return abs(gross / r)


def _avg(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 2) if values else None


def _group(trades: list[dict]) -> dict[str, Any]:
    wins = [t for t in trades if (t.get("net_pnl") or 0) > 0]
    losses = [t for t in trades if (t.get("net_pnl") or 0) <= 0]
    hand_wins = [t for t in wins if closed_by_hand(t.get("exit_reason"))]
    return {
        "n": len(trades),
        "net_usd": round(sum(t.get("net_pnl") or 0 for t in trades), 2),
        "fees_usd": round(sum(t.get("fees") or 0 for t in trades), 2),
        "wins": len(wins),
        "avg_win_r": _avg([t["r_multiple"] for t in wins if t.get("r_multiple") is not None]),
        "avg_loss_r": _avg([t["r_multiple"] for t in losses if t.get("r_multiple") is not None]),
        "hand_closed": sum(1 for t in trades if closed_by_hand(t.get("exit_reason"))),
        # Winners you closed by hand: where they were closed, against the best point they had reached.
        "hand_wins": len(hand_wins),
        "hand_win_closed_r": _avg([t["r_multiple"] for t in hand_wins if t.get("r_multiple") is not None]),
        "hand_win_best_r": _avg([t["mfe_r"] for t in hand_wins if t.get("mfe_r") is not None]),
    }


def review(journal: Journal, account: str | None = None, *, limit: int = 2000) -> dict[str, Any] | None:
    """The report as a dict, or None before the first closed trade."""
    trades = [t for t in journal.trades(limit=limit, account=account) if t.get("exit_time")]
    if not trades:
        return None
    trades.sort(key=lambda t: t.get("exit_time") or "")
    decisions = journal.decisions(limit=limit)
    recs = journal.recommendations(limit=limit * 5)
    bot = [t for t in trades if _kind(t) == "bot"]
    own = [t for t in trades if _kind(t) != "bot"]

    stepped_in = []
    for t in bot:
        if not closed_by_hand(t.get("exit_reason")):
            continue
        plan = _plan_for(t, decisions, recs)
        real_r = t.get("r_multiple")
        row = {"day": t.get("trading_day"), "time": (t.get("entry_time") or "")[11:16], "side": t.get("side"),
               "real_r": None if real_r is None else round(real_r, 2), "real_usd": round(t.get("net_pnl") or 0, 2),
               "plan_r": None, "plan_why": None, "diff_r": None, "diff_usd": None}
        if plan is not None and real_r is not None:
            row.update(plan_r=round(plan["r"], 2), plan_why=plan["why"], strategy=plan["strategy"],
                       diff_r=round(real_r - plan["r"], 2))
            per_r = _usd_per_r(t)
            if per_r is not None:
                row["diff_usd"] = round(row["diff_r"] * per_r, 2)
        stepped_in.append(row)
    compared = [r for r in stepped_in if r["diff_r"] is not None]
    alone = [t for t in bot if not closed_by_hand(t.get("exit_reason"))]
    return {
        "first_day": trades[0].get("trading_day"),
        "last_day": trades[-1].get("trading_day"),
        "bot_alone": _group(alone),
        "own": _group(own),
        "stepped_in": stepped_in,
        "stepped_in_compared": len(compared),
        "stepped_in_diff_r": round(sum(r["diff_r"] for r in compared), 2) if compared else None,
        "stepped_in_diff_usd": (round(sum(r["diff_usd"] for r in compared if r["diff_usd"] is not None), 2)
                                if any(r["diff_usd"] is not None for r in compared) else None),
    }


def lines(rep: dict[str, Any] | None) -> list[str]:
    """The report in a few plain sentences (Telegram /knowledge, the Ask tab)."""
    if not rep:
        return []
    out = []
    a = rep["bot_alone"]
    if a["n"]:
        out.append(f"Bot trades left alone: {a['n']}, {fmt.money(a['net_usd'])} after costs.")
    if rep["stepped_in"]:
        n, done = len(rep["stepped_in"]), rep["stepped_in_compared"]
        text = f"Bot trades you closed by hand: {n}"
        if done:
            diff, usd = rep["stepped_in_diff_r"], rep["stepped_in_diff_usd"]
            verb = "made" if diff > 0 else "cost"
            money = f" (about {fmt.money(abs(usd))})" if usd is not None else ""
            text += f"; against what their own plans went on to do, closing them {verb} {abs(diff):.1f}R{money}"
            if done < n:
                text += f" ({done} of {n} had a plan to compare)"
        out.append(text + ".")
    o = rep["own"]
    if o["n"]:
        text = (f"Your own trades: {o['n']}, {fmt.money(o['net_usd'])} after {fmt.money(o['fees_usd'])} of fees; "
                f"{o['wins']} won")
        if o["avg_win_r"] is not None and o["avg_loss_r"] is not None:
            text += f" (average win {o['avg_win_r']:+.2f}R, average loss {o['avg_loss_r']:+.2f}R)"
        out.append(text + ".")
        if o["hand_wins"] and o["hand_win_best_r"] is not None and o["hand_win_closed_r"] is not None:
            out.append(f"Winners you closed by hand were closed at {o['hand_win_closed_r']:+.2f}R on average, after "
                       f"reaching {o['hand_win_best_r']:+.2f}R.")
    return out


def text(rep: dict[str, Any] | None) -> str:
    body = lines(rep)
    if not body:
        return ""
    return f"Your hand against the plan ({rep['first_day']} to {rep['last_day']}):\n" + "\n".join(body)
