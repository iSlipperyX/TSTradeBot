"""What the bot knows: a plain-language briefing in the bot's own words.

One place that answers "what do you know, and when are you trading next?": how much it has seen
(the knowledge base and the long-run memory), what it would and wouldn't trade right now and why,
which strategies have worked after costs (recent next to long-run), the lessons worth testing,
and the next-trade forecast (forecast.py). Shown at the top of the dashboard's Knowledge tab,
by /brief in Telegram, and built from the same numbers as the rest of the reports.

Nothing here changes a trading decision.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import TYPE_CHECKING, Any

from topstep_bot.insights import execution_line, hint_line
from topstep_bot.knowledge import KEPT_SOURCES

if TYPE_CHECKING:
    from topstep_bot.engine import TradingCore

GAVE_BACK_NOTE = 0.3  # losers up 1R first: worth a mention from this share on


def _r(v: float | None) -> str:
    return "-" if v is None else f"{v:+.2f}R"


def _pct(v: float | None) -> str:
    return "-" if v is None else f"{round(100 * v)}%"


def _memory_lines(core: TradingCore, today: date) -> list[str]:
    kb = core.knowledge
    c = kb.counts() if kb else {}
    total = len(kb.obs) if kb else 0
    if kb is None:
        lines = ["My knowledge base is turned off (knowledge.enabled: false), so I'm not learning from what I see."]
    elif not total:
        lines = ["My knowledge base is empty. Retrain it (Knowledge tab, /train or menu 5) and I'll replay recent history."]
    else:
        first, last = min(o.day for o in kb.obs), max(o.day for o in kb.obs)
        days = (kb.trained or {}).get("days")
        replay = f"{c['train']:,} from replaying the last {days} trading days of real prices" if days else f"{c['train']:,} from replays"
        lines = [f"I know the outcome of {total:,} trade signals from {first} to {last}: {replay}, {c['shadow']:,} live ideas I "
                 f"followed to their end, {c['real']:,} trades I took myself and {c['manual']:,} of yours."]
        week = (today - timedelta(days=7)).isoformat()
        fresh = sum(1 for o in kb.obs if o.source in ("shadow", *KEPT_SOURCES) and o.day >= week)
        if fresh:
            lines.append(f"In the last 7 days I added {fresh:,} new outcome(s) while running.")
    mem = core.memory
    if mem is None:
        if not core.cfg.knowledge.deep_learning:
            lines.append("My long-run memory is off (knowledge.deep_learning: false).")
        return lines
    st = mem.status()
    lib = st["library"]
    if not lib["bars"]:
        lines.append("My long-run memory is still empty. It fills each day outside trading hours (or when you press "
                     "Learn from history now): I download up to "
                     f"{core.cfg.knowledge.deep_history_days} days of history and replay all of it through every strategy.")
        return lines
    line = (f"My long-run memory holds {lib['bars']:,} price bars ({lib['days']:,} trading days since {lib['first']}, "
            f"{lib['mb']} MB on your PC).")
    if st["observations"]:
        t = st["trained"] or {}
        line += (f" Replaying them through every strategy gave {st['observations']:,} more outcomes"
                 + (f" (last run {t['at'][:16].replace('T', ' ')} UTC)." if t.get("at") else "."))
    lines.append(line)
    return lines


def _now_lines(core: TradingCore, today: date) -> tuple[str, list[str]]:
    slot, regime = core.slot(), core.regime.value
    title = f"Right now ({slot} session, {regime} market)" if slot != "off" else "Right now (outside regular hours)"
    summary = core.knowledge_summary()
    strat = core.strategy
    if summary is None:
        return title, [f"I trade {strat.title}. Without a knowledge base I can't say how it has been doing."]
    rows = {r["name"]: r for r in summary["strategies"]}
    subs = getattr(strat, "subs", None)
    if subs is None:
        row = rows.get(strat.name)
        o = row["overall"] if row else None
        seen = f"; I have seen {o['n']} of its signals ({_r(o['mean_r'])} average)" if o and o["n"] else ""
        return title, [f"I trade {strat.title} only, whatever the knowledge base says{seen}."]
    if slot == "off":
        return title, ["No strategy trades outside regular hours. At the open I'll check the knowledge base again for each one."]
    takes, avoids, unknown = [], [], []
    for s in subs:
        row = rows.get(s.name)
        if row is None:
            continue
        cell = row["cells"].get(f"{slot}|{regime}")
        if cell is None:
            continue
        name = row["title"]
        if strat.would_take(s.name, slot, regime, today):
            takes.append(f"{name} ({cell['why']})")
        elif cell["level"] == "unproven":
            unknown.append(name)
        else:
            avoids.append(f"{name} ({cell['why']})")
    lines = [f"I would trade: {'; '.join(takes)}." if takes else "I wouldn't trade any strategy here right now: none has proven itself at this time of day and regime."]
    if avoids:
        lines.append(f"I'm staying out of: {'; '.join(avoids)}.")
    if unknown:
        lines.append(f"Not enough evidence yet: {', '.join(unknown)}.")
    return title, lines


def _works_lines(core: TradingCore) -> list[str]:
    rep = core.insights()
    if not rep or not rep["coverage"]["total"]:
        return ["Nothing measured yet."]
    longrun = core.insights(longrun=True) or {}
    lr = {r["name"]: r for r in longrun.get("strategies", []) if r["n"] >= longrun.get("min_row", 5)}
    rows = sorted((r for r in rep["strategies"] if r["n"] >= rep["min_row"]), key=lambda r: r["net_r"] or 0, reverse=True)
    if not rows:
        return ["Too few signals per strategy to compare yet."]
    lines = []
    for r in rows[:3]:
        line = f"{r['title']}: {_r(r['net_r'])} per signal after costs over {r['n']} ({_pct(r['win_rate'])} won)"
        if r["name"] in lr:
            line += f"; over the long run {_r(lr[r['name']]['net_r'])} over {lr[r['name']]['n']:,}"
        lines.append(line + ".")
    worst = rows[-1]
    if len(rows) > 3 and (worst["net_r"] or 0) < 0:
        lines.append(f"Weakest lately: {worst['title']} at {_r(worst['net_r'])} over {worst['n']}.")
    m = rep.get("manual")
    if m and m["n"]:
        lines.append(f"Your manual trades: {_r(m['net_r'])} over {m['n']} ({_pct(m['win_rate'])} won).")
    return lines


def _lesson_lines(core: TradingCore) -> list[str]:
    rep = core.insights()
    if not rep or not rep["coverage"]["total"]:
        return []
    lines = []
    longrun = core.insights(longrun=True) or {}
    hints = rep["hints"] or longrun.get("hints") or []
    if hints:
        where = "" if rep["hints"] else " (from the long-run memory)"
        lines.append(f"Worth testing, not proven{where}: {hint_line(hints[0])}")
    for r in rep["strategies"]:
        if r["gave_back"] is not None and r["gave_back"] >= GAVE_BACK_NOTE and r["n_losers"] >= rep["min_row"]:
            lines.append(f"{_pct(r['gave_back'])} of {r['title']}'s losing signals were 1R in profit first: a breakeven stop or a "
                         "nearer target might help (an idea to test, not a rule).")
            break
    ex = execution_line(rep["execution"])
    if ex:
        lines.append(ex)
    return lines


def build_brief(core: TradingCore) -> dict[str, Any]:
    """The briefing as sections of plain sentences, plus the next-trade forecast it ends with."""
    today = core.schedule.trading_day(core.clock())
    sections = [{"title": "My memory", "lines": _memory_lines(core, today)}]
    title, lines = _now_lines(core, today)
    sections.append({"title": title, "lines": lines})
    sections.append({"title": "What has worked (after fees and slippage)", "lines": _works_lines(core)})
    lessons = _lesson_lines(core)
    if lessons:
        sections.append({"title": "Lessons so far", "lines": lessons})
    fc = core.forecast()
    if fc:
        sections.append({"title": "My next trade", "lines": [fc["headline"], *fc["basis"]]})
    return {"sections": sections, "forecast": fc}


def brief_text(brief: dict[str, Any]) -> str:
    """Plain text for Telegram and the command line."""
    out = ["🧠 What I know"]
    for s in brief["sections"]:
        out.append("")
        out.append(s["title"] + ":")
        out += [f"• {line}" for line in s["lines"]]
    fc = brief.get("forecast")
    if fc and fc["state"] not in ("in_trade", "blocked"):
        out.append(fc["caveat"])
    return "\n".join(out)
