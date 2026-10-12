"""Decision records (reasoning.py) turned into plain sentences - no AI, only the recorded numbers."""

from __future__ import annotations

from typing import Any

from topstep_bot.fmt import money, pct, r

SLOT_WORDS = {"open": "the first part of the session", "midday": "midday", "close": "the last part of the session",
              "off": "outside regular hours"}
LEVEL_WORDS = {"cell": "at this time of day in this kind of market", "slot": "at this time of day",
               "strategy": "across the whole day", "unproven": "nowhere yet (not enough evidence)"}
STATUS_WORDS = {"taken": "took", "skipped": "skipped", "passed": "passed on"}


def headline(rec: dict[str, Any]) -> str:
    """One line: '10:35 2026-10-09 - took Opening Range Breakout LONG 2 @ 20105.25: won +1.6R ($160.00)'."""
    what = f"{rec['title']} {rec['side']}"
    if rec["status"] == "taken" and rec.get("size"):
        what += f" {rec['size']} @ {rec['entry']}"
    line = f"{rec['time']} {rec['day']} - {STATUS_WORDS.get(rec['status'], rec['status'])} {what}"
    if rec["status"] == "skipped":
        line += " (a rule stopped it)"
    out = rec.get("outcome")
    if out:
        would = "would have " if out.get("hypothetical") else ""
        verb = {"won": "won", "lost": "lost", "flat": "broken even"}.get(out.get("result") or "", "finished")
        usd = f" ({money(out['usd'])})" if out.get("usd") is not None else ""
        line += f": {would}{verb} {r(out.get('r'))}{usd}"
    elif rec["status"] == "taken":
        line += " (still open)"
    return line


def _verdict_line(c: dict[str, Any]) -> str:
    where = LEVEL_WORDS.get(c.get("level") or "", "")
    status = "allowed" if c.get("allowed") else "not allowed"
    return f"{c['title']} {c['side']}: {status} - {c['why']}" + (f" (judged {where})" if where and c.get("level") else "")


def evidence_lines(ev: dict[str, Any] | None, title: str) -> list[str]:
    if not ev:
        return []
    out = []
    rec = ev.get("recent") or {}
    names = {"cell": "at this time of day in this market", "slot": "at this time of day", "strategy": "all day"}
    parts = [f"{names[k]} {r(v['mean_r'])} over {v['n']}" for k, v in rec.items() if v.get("n")]
    if parts:
        out.append(f"Recent record of {title} (before costs): " + "; ".join(parts) + ".")
    est = ev.get("estimate")
    if est:
        out.append(f"Best estimate after fees and slippage, combining every level: {r(est['net_r'])} a trade.")
    lr = ev.get("longrun")
    if lr and lr.get("n"):
        out.append(f"Long-run memory ({lr['from']} to {lr['to']}): {r(lr['mean_net_r'])} a signal after costs over "
                   f"{lr['n']:,} signals, {pct(lr['win_rate'])} won.")
    sim = ev.get("similar")
    if sim:
        tilt = sim["mean_net_r"] - sim["baseline_net_r"]
        word = "better than" if tilt > 0.1 else "worse than" if tilt < -0.1 else "about the same as"
        out.append(f"In the {sim['n']} most similar past situations it averaged {r(sim['mean_net_r'])} after costs "
                   f"({pct(sim['win_rate'])} won) - {word} its usual {r(sim['baseline_net_r'])}. This explains, it doesn't decide.")
    return out


def decision_text(rec: dict[str, Any], *, detail: bool = True) -> str:
    """Why the bot did what it did on one signal, and what came of it."""
    lines = [headline(rec)]
    slot = SLOT_WORDS.get(rec.get("slot") or "", rec.get("slot") or "")
    if rec["status"] == "taken":
        risk = f", risking {money(rec['risk_usd'])}" if rec.get("risk_usd") else ""
        target = f", target {rec['target']}" if rec.get("target") is not None else ""
        lines.append(f"Order: stop {rec['stop']}{target}{risk}.")
    lines.append(f"Signal: {rec['signal']}")
    lines.append(f"When: {slot}, a {rec.get('regime') or '?'} market.")
    cands = rec.get("candidates") or []
    if rec.get("policy") and cands:
        lines.append(f"Decided with {rec['policy']}.")
    if len(cands) > 1 or (cands and cands[0].get("level")):
        lines.append("What each strategy that signalled was told:")
        lines += [f"  • {_verdict_line(c)}" for c in cands]
    if rec["status"] == "skipped" and rec.get("note"):
        lines.append(f"Not traded because: {rec['note']}.")
    elif rec["status"] == "passed":
        lines.append("Not traded: no strategy that signalled had enough evidence of an edge here.")
    if detail:
        lines += evidence_lines(rec.get("evidence"), rec["title"])
        market = (rec.get("market") or {}).get("described")
        if market:
            lines.append("The market then: " + "; ".join(market) + ".")
        rules = rec.get("rules") or {}
        if rules.get("max_trades") is not None:
            lines.append(f"Limits then: trade {rules['trades_today'] + (1 if rec['status'] == 'taken' else 0)} of "
                         f"{rules['max_trades']} today, day P&L {money(rules.get('day_pnl'))} against a "
                         f"{money(rules.get('daily_loss_limit'))} daily loss limit, "
                         f"{rules.get('consecutive_losses', 0)} loss(es) in a row.")
    out = rec.get("outcome")
    if out:
        path = ""
        if out.get("mfe_r") is not None and out.get("mae_r") is not None:
            path = f" On the way it went {r(out['mfe_r'])} in its favour at best and {r(out['mae_r'])} against."
        kind = "Hypothetical result (not traded)" if out.get("hypothetical") else "Result"
        lines.append(f"{kind}: {out.get('result') or '?'} {r(out.get('r'))} - {out.get('why')}.{path}")
    lesson = lesson_text(rec)
    if lesson:
        lines.append(lesson)
    return "\n".join(lines)


def lesson_text(rec: dict[str, Any]) -> str | None:
    les = rec.get("lesson")
    if not les:
        return None
    b, a = les["before"], les["after"]
    where = f"{rec['title']} in {SLOT_WORDS.get(rec.get('slot') or '', rec.get('slot'))} ({rec.get('regime')})"
    if a["allowed"] and not b["allowed"]:
        change = "it is now allowed to trade there"
    elif b["allowed"] and not a["allowed"]:
        change = "it is no longer allowed to trade there until the evidence improves"
    else:
        change = "still allowed there" if a["allowed"] else "still not allowed there"
    return f"What it learned: {where} went from {r(b['score'])} to {r(a['score'])} expected a trade - {change}."


def decisions_list(records: list[dict[str, Any]]) -> str:
    if not records:
        return "No decisions recorded yet. They start with the first signal the bot weighs while running (version 2.2+)."
    return "\n".join("• " + headline(rec) for rec in records)
