"""What the bot learned: a plain-language report on the knowledge base's observations.

The knowledge base (knowledge.py) answers one question while trading: may this strategy trade
at this time of day in this regime? This module answers the questions you ask afterwards:

* What is each strategy's signal worth **after fees and slippage**?
* How did trades **get** there - how far did winners run, how many losers were up 1R first?
* Do **real fills** match the simulated ones, and how much do they slip?
* In which **market conditions** (market_context.py) did a strategy do best and worst?

Nothing here changes a trading decision. The condition "hints" in particular compare many
strategies on many measurements at once, so some differences are luck: they are ideas to test
on unseen data, never rules. Shown on the dashboard's Knowledge tab, after /knowledge in
Telegram, and by ``topstep-bot insights`` (which can also export every observation to CSV).
"""

from __future__ import annotations

import csv
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from topstep_bot.knowledge import FIRST_TRADE, MANUAL, KnowledgeBase, Observation
from topstep_bot.market_context import FEATURES, SIGNED

MIN_BUCKET = 15  # observations a condition bucket needs before it can be called a hint
MIN_ROW = 5  # observations before a strategy's averages are shown at all
DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
SIMULATED = ("train", "shadow")
FILLED = ("real", "manual")


def oriented(o: Observation, key: str) -> float | None:
    """A context value seen from the trade's side: "with the trade" is positive for longs and shorts alike."""
    if not o.ctx or key not in o.ctx:
        return None
    v = o.ctx[key]
    if key in SIGNED and o.side == "SHORT":
        return -v
    if key == "range_pos" and o.side == "SHORT":
        return round(1.0 - v, 3)
    return v


def _mean(values: Iterable[float | None]) -> float | None:
    vals = [v for v in values if v is not None]
    return round(sum(vals) / len(vals), 3) if vals else None


def _strategy_row(name: str, title: str, obs: list[Observation]) -> dict[str, Any]:
    costed = [o for o in obs if o.cost_r is not None]
    winners = [o for o in obs if o.r > 0 and o.mfe_r is not None]
    losers = [o for o in obs if o.r < 0 and o.mfe_r is not None]
    filled = [o for o in obs if o.source in FILLED]
    simulated = [o for o in obs if o.source in SIMULATED]
    return {
        "name": name, "title": title, "n": len(obs),
        "win_rate": round(sum(o.r > 0 for o in obs) / len(obs), 3) if obs else None,
        "gross_r": _mean(o.r for o in obs),
        "cost_r": _mean(o.cost_r for o in costed),
        # Costs are only known for observations recorded since they were measured; older ones count gross.
        "net_r": _mean(o.net_r for o in obs),
        "n_costed": len(costed),
        "mfe_r": _mean(o.mfe_r for o in obs), "mae_r": _mean(o.mae_r for o in obs),
        "n_path": sum(o.mfe_r is not None for o in obs),
        "winners_left_r": _mean(o.mfe_r - o.r for o in winners),  # how much of their best point winners gave back
        "gave_back": round(sum(o.mfe_r >= 1.0 for o in losers) / len(losers), 3) if losers else None,
        "n_losers": len(losers),
        "bars": _mean(o.bars for o in obs),
        "real_n": len(filled), "real_net_r": _mean(o.net_r for o in filled),
        "sim_n": len(simulated), "sim_net_r": _mean(o.net_r for o in simulated),
    }


def _cuts(values: list[float]) -> tuple[float, float] | None:
    """Tercile cut points (low / mid / high), or None when there aren't enough distinct values."""
    if len(values) < 3 * MIN_BUCKET:
        return None
    s = sorted(values)
    lo, hi = s[len(s) // 3], s[2 * len(s) // 3]
    return (lo, hi) if lo < hi else None


def _fmt(v: float) -> str:
    return f"{v:.0f}" if abs(v) >= 10 else f"{v:.2f}"


def _bucket_label(key: str, i: int, cuts: tuple[float, float]) -> str:
    lo, hi = cuts
    unit = " min" if key == "min_open" else ""
    return (f"low (under {_fmt(lo)}{unit})", f"middle ({_fmt(lo)} to {_fmt(hi)}{unit})", f"high ({_fmt(hi)}{unit} or more)")[i]


def _buckets(key: str, obs: list[Observation], cuts: tuple[float, float] | None) -> list[dict[str, Any]]:
    """Net R per bucket of one measurement for one strategy's observations."""
    groups: dict[str, list[float]] = {}
    order: list[str] = []
    for o in obs:
        v = oriented(o, key)
        if v is None:
            continue
        if key == "dow":
            label = DAYS[int(v)] if 0 <= int(v) < len(DAYS) else str(v)
        elif cuts is None:
            continue
        else:
            label = _bucket_label(key, 0 if v < cuts[0] else 1 if v < cuts[1] else 2, cuts)
        groups.setdefault(label, []).append(o.net_r)
    if key == "dow":
        order = [d for d in DAYS if d in groups]
    elif cuts is not None:
        order = [_bucket_label(key, i, cuts) for i in range(3) if _bucket_label(key, i, cuts) in groups]
    return [{"label": label, "n": len(groups[label]), "net_r": _mean(groups[label])} for label in order]


def build_report(kb: KnowledgeBase, strategies: list[tuple[str, str]], *, slippage_ticks: float,
                 min_bucket: int = MIN_BUCKET, max_hints: int = 8) -> dict[str, Any]:
    """Everything the dashboard, Telegram and the command line show about what the bot learned."""
    obs = kb.obs
    titles = dict(strategies)
    by_strategy: dict[str, list[Observation]] = {}
    for o in obs:
        by_strategy.setdefault(o.strategy, []).append(o)

    rows = [_strategy_row(name, title, by_strategy[name]) for name, title in strategies if by_strategy.get(name)]
    manual = _strategy_row(MANUAL, "Your manual trades", by_strategy[MANUAL]) if by_strategy.get(MANUAL) else None
    first = (_strategy_row(FIRST_TRADE, "First trades after starting", by_strategy[FIRST_TRADE])
             if by_strategy.get(FIRST_TRADE) else None)

    fills = [o for o in obs if o.source in FILLED]
    slip_in = [o.slip_in for o in fills if o.slip_in is not None]
    slip_out = [o.slip_out for o in fills if o.slip_out is not None]
    execution = {
        "trades": len(fills), "assumed": slippage_ticks,
        "entry_n": len(slip_in), "entry": _mean(slip_in), "entry_worst": max(slip_in) if slip_in else None,
        "exit_n": len(slip_out), "exit": _mean(slip_out), "exit_worst": max(slip_out) if slip_out else None,
    }

    # Conditions: cut points are shared by every strategy, so "high volatility" means the same everywhere.
    auto = [o for o in obs if o.strategy != MANUAL and o.strategy in titles]
    conditions: dict[str, dict[str, Any]] = {}
    hints: list[dict[str, Any]] = []
    for key, (fname, fhelp) in FEATURES.items():
        if key in SIGNED or key == "range_pos":  # see oriented()
            fname, fhelp = f"{fname} (in the trade's direction)", f"{fhelp}; turned around for shorts, so higher = further with the trade"
        cuts = None if key == "dow" else _cuts([v for o in auto if (v := oriented(o, key)) is not None])
        per: dict[str, list[dict[str, Any]]] = {}
        for name, title in strategies:
            buckets = _buckets(key, by_strategy.get(name, []), cuts)
            if not buckets:
                continue
            per[name] = buckets
            solid = [b for b in buckets if b["n"] >= min_bucket and b["net_r"] is not None]
            if len(solid) >= 2:
                best = max(solid, key=lambda b: b["net_r"])
                worst = min(solid, key=lambda b: b["net_r"])
                if best["net_r"] > 0 > worst["net_r"]:  # only differences that change the sign are worth a look
                    hints.append({"strategy": name, "title": title, "feature": key, "feature_name": fname,
                                  "best": best, "worst": worst, "spread": round(best["net_r"] - worst["net_r"], 3)})
        if per:
            labels = list(DAYS) if key == "dow" else [_bucket_label(key, i, cuts) for i in range(3)] if cuts else []
            seen = {b["label"] for buckets in per.values() for b in buckets}
            conditions[key] = {"name": fname, "help": fhelp, "labels": [lb for lb in labels if lb in seen],
                               "strategies": per}
    hints.sort(key=lambda h: h["spread"], reverse=True)

    return {
        "coverage": {
            "total": len(obs),
            "with_context": sum(1 for o in obs if o.ctx),
            "with_path": sum(1 for o in obs if o.mfe_r is not None),
            "with_costs": sum(1 for o in obs if o.cost_r is not None),
            "by_source": kb.counts(),
            "first_day": min((o.day for o in obs), default=None),
            "last_day": max((o.day for o in obs), default=None),
        },
        "strategies": rows,
        "manual": manual,
        "first_trade": first,
        "execution": execution,
        "conditions": conditions,
        "hints": hints[:max_hints],
        "min_bucket": min_bucket,
        "min_row": MIN_ROW,
    }


# --------------------------------------------------------------------------- text

def _r(v: float | None) -> str:
    return "-" if v is None else f"{v:+.2f}R"


def execution_line(e: dict[str, Any]) -> str | None:
    if not e["entry_n"] and not e["exit_n"]:
        return None
    parts = []
    if e["entry_n"]:
        parts.append(f"entries {e['entry']:+.1f} ticks")
    if e["exit_n"]:
        parts.append(f"exits {e['exit']:+.1f} ticks")
    return (f"Real fills slipped {' and '.join(parts)} on average over {e['trades']} trade(s); backtests and ideas "
            f"assume {e['assumed']:g} tick per fill.")


def hint_line(h: dict[str, Any]) -> str:
    b, w = h["best"], h["worst"]
    return (f"{h['title']}, {h['feature_name'][:1].lower() + h['feature_name'][1:]}: best {b['label']} {_r(b['net_r'])} over {b['n']}, "
            f"worst {w['label']} {_r(w['net_r'])} over {w['n']}.")


def report_text(rep: dict[str, Any], *, compact: bool = False) -> str:
    """Plain text for Telegram (``compact``) and the command line."""
    cov = rep["coverage"]
    if not cov["total"]:
        return "The knowledge base is empty - press Retrain now on the dashboard's Knowledge tab (or /train in Telegram)."
    lines = [f"What the bot learned ({cov['total']} observations, {cov['with_context']} with market context, "
             f"{cov['with_path']} with their price path):"]
    rows = [r for r in rep["strategies"] if r["n"] >= rep["min_row"]]
    if rows:
        lines.append("Average signal after fees and slippage:")
        for r in sorted(rows, key=lambda r: r["net_r"] or 0, reverse=True)[: 4 if compact else None]:
            line = f"  {r['title']}: {_r(r['net_r'])} over {r['n']} ({round(100 * (r['win_rate'] or 0))}% won)"
            if r["cost_r"] is not None and not compact:
                line += f", costs {r['cost_r']:.2f}R a signal"
            if r["real_n"] and r["sim_n"]:
                line += f"; your real trades {_r(r['real_net_r'])} over {r['real_n']} vs {_r(r['sim_net_r'])} simulated"
            lines.append(line)
            if not compact and r["n_path"] >= rep["min_row"]:
                path = f"    went {_r(r['mfe_r'])} for and {_r(r['mae_r'])} against on average"
                if r["winners_left_r"] is not None:
                    path += f"; winners closed {r['winners_left_r']:.2f}R below their best"
                if r["gave_back"] is not None and r["n_losers"] >= rep["min_row"]:
                    path += f"; {round(100 * r['gave_back'])}% of losers were up 1R first"
                lines.append(path)
    m = rep["manual"]
    if m and m["n"]:
        lines.append(f"Your manual trades: {_r(m['net_r'])} over {m['n']} ({round(100 * (m['win_rate'] or 0))}% won).")
    f = rep.get("first_trade")
    if f and f["n"]:
        lines.append(f"First trades after starting: {_r(f['net_r'])} over {f['n']} ({round(100 * (f['win_rate'] or 0))}% won).")
    ex = execution_line(rep["execution"])
    if ex:
        lines.append(ex)
    if rep["hints"]:
        lines.append("Conditions worth testing (hints, not proven - with this many comparisons some are luck):")
        lines += [f"  - {hint_line(h)}" for h in rep["hints"][: 3 if compact else None]]
    elif cov["with_context"] < 3 * rep["min_bucket"]:
        lines.append("Market conditions: not enough observations with market context yet - they build up as the bot runs.")
    return "\n".join(lines)


# --------------------------------------------------------------------------- export

CSV_COLUMNS = ("day", "time", "strategy", "side", "slot", "regime", "source", "r", "cost_r", "net_r", "usd",
               "mfe_r", "mae_r", "bars", "slip_in", "slip_out", "why", "basis")


def export_rows(obs: Iterable[Observation]) -> tuple[list[str], list[list[Any]]]:
    """Header and rows: one per observation, each market measurement in its own column."""
    header = [*CSV_COLUMNS, *FEATURES]
    rows = []
    for o in obs:
        base = {"net_r": round(o.net_r, 3), **{k: getattr(o, k) for k in CSV_COLUMNS if k != "net_r"}}
        ctx = o.ctx or {}
        rows.append([base[k] for k in CSV_COLUMNS] + [ctx.get(k) for k in FEATURES])
    return header, rows


def export_csv(kb: KnowledgeBase, path: Path) -> int:
    """Write every observation to ``path`` (opens in Excel). Returns the number of rows."""
    header, rows = export_rows(kb.obs)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        w.writerows(rows)
    return len(rows)


def csv_text(kb: KnowledgeBase) -> str:
    """The same export as a string (the dashboard's Download button)."""
    import io

    header, rows = export_rows(kb.obs)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    w.writerows(rows)
    return buf.getvalue()
