"""Why the bot did what it did: one decision record per signal, kept in the journal.

Every time the bot's own strategy signals, a record is written with everything the decision was
based on - so it can be explained later from what the bot actually knew, not reconstructed:

* the signal itself and every other strategy that signalled on the same bar, each with its
  verdict from the decision rule in use (policy.py);
* the evidence behind the chosen strategy: its recent record at three levels, the 1.3 estimate
  after costs, the long-run memory and the most similar past situations (evidence.py);
* the market at that moment, in words (market_context.py), and the bot's risk limits then;
* what happened: the status (taken / skipped by a rule / passed on), then the result. For a signal
  the bot passed on, the result is what it *would* have made (the recommendation book follows it).
* the lesson: how the knowledge for that strategy, time of day and market changed once the
  result was learned.

Only the live bot keeps records (backtests and training don't). Records are explanations: writing
one can fail without touching the trade.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any

from topstep_bot.knowledge import slot_for
from topstep_bot.market_context import describe

if TYPE_CHECKING:
    from topstep_bot.engine import TradePlan, TradingCore
    from topstep_bot.models import Signal
    from topstep_bot.recommendations import Recommendation
    from topstep_bot.strategies.base import StrategyContext

log = logging.getLogger("topstep_bot.reasoning")

STATUSES = ("taken", "skipped", "passed")  # passed = no strategy had enough evidence to trade


def decision_id(created: datetime, strategy: str, side: str) -> str:
    """Same key as the journal's recommendation rows: one per bar, strategy and side."""
    return f"{created:%Y%m%d-%H%M}-{strategy}-{side}"


class Reasoner:
    def __init__(self, core: TradingCore):
        self.core = core
        self._pending: dict | None = None  # the adaptive strategy's evaluation of the current bar
        self._open: dict[str, tuple[str, int | None]] = {}  # recommendation key -> (decision id, candidate index)
        self._load_open()

    # ------------------------------------------------------------------ inputs

    def on_evaluation(self, ev: dict) -> None:
        """The adaptive strategy weighed one bar's signals. Passed-on bars are recorded now; a chosen
        signal waits for the engine to say whether its order went in (``on_signal``)."""
        if ev.get("chosen"):
            self._pending = ev
            return
        self._pending = None
        top = max(ev["candidates"], key=lambda c: c["score"])
        rec = self._new_record(ev["bar_close"], top["strategy"], top["title"], top["side"], "passed",
                               note="no strategy had enough evidence to trade", signal=top["signal"],
                               plan=None, stop=top.get("stop"), target=top.get("target"), evaluation=ev)
        self._save(rec, ev)

    def on_signal(self, sig: Signal, ctx: StrategyContext, entry_ref: float, plan: TradePlan | None, status: str,
                  note: str, tag: str | None = None) -> None:
        """The engine acted on an entry signal of the bot's own strategy (called with its recommendation)."""
        from topstep_bot.strategies import STRATEGIES

        strategy = self.core.strategy
        name = sig.meta.get("strategy", strategy.name)
        title = STRATEGIES[name].title if name in STRATEGIES else strategy.title
        ev = self._pending if self._pending and self._pending["bar_close"] == ctx.bar_close else None
        self._pending = None
        if ev is None:  # a single strategy (not adaptive): it is the only candidate
            ev = {"bar_close": ctx.bar_close, "time": self.core.schedule.local(ctx.bar_close).strftime("%H:%M"),
                  "day": ctx.day.isoformat(), "slot": sig.meta.get("slot") or self.core.slot(ctx.bar_close),
                  "regime": sig.meta.get("regime") or self.core.regime.value, "policy": None, "chosen": name,
                  "candidates": [{"strategy": name, "title": title, "side": sig.side.label, "allowed": True,
                                  "level": None, "score": None, "why": "the only strategy the bot is set to trade",
                                  "signal": sig.reason}]}
        signal = next((c["signal"] for c in ev["candidates"] if c["strategy"] == name), sig.reason)
        rec = self._new_record(ctx.bar_close, name, title, sig.side.label, status, note=note, signal=signal, plan=plan,
                               stop=sig.stop_price, target=sig.target_price, evaluation=ev, entry_ref=entry_ref, tag=tag)
        self._save(rec, ev)
        if status == "taken":
            self._tell("entry", why_line(rec))

    def on_outcome(self, item: Recommendation, why: str) -> None:
        """A signal some record weighed has finished (traded or followed hypothetically): note its result."""
        key = decision_id(item.created, item.strategy, item.side.label)
        found = self._open.pop(key, None)
        if found is None or self.core.journal is None:
            return
        rec_id, index = found
        try:
            rec = self.core.journal.decision(rec_id)
            if rec is None:
                return
            result = {"result": item.result, "r": item.outcome_r, "usd": item.outcome_usd, "exit_price": item.exit_price,
                      "why": why, "mfe_r": item.mfe_r, "mae_r": item.mae_r, "hypothetical": item.hypothetical,
                      "closed": self.core.schedule.local(self.core.clock()).isoformat(timespec="minutes")}
            if index is not None:
                rec["candidates"][index]["outcome"] = result
            if item.strategy == rec["strategy"] and item.side.label == rec["side"]:
                rec["outcome"] = result
                rec["lesson"] = self._lesson(rec)
            self.core.journal.record_decision(rec)
            if rec["status"] == "taken" and rec.get("outcome") is result:
                from topstep_bot.brain.explain import lesson_text

                lesson = lesson_text(rec)
                if lesson:
                    self._tell("exit", "📘 " + lesson)
        except Exception:  # noqa: BLE001 - an explanation must never disturb trading
            log.exception("Could not record the outcome of decision %s", rec_id)

    def _tell(self, kind: str, text: str | None) -> None:
        core = self.core
        if text and core.notifier is not None and core.cfg.brain.explain_trades:
            core.notifier.notify(kind, text)

    # ------------------------------------------------------------------ building records

    def _new_record(self, created: datetime, strategy: str, title: str, side: str, status: str, *, note: str,
                    signal: str, plan: TradePlan | None, stop: float | None, target: float | None, evaluation: dict,
                    entry_ref: float | None = None, tag: str | None = None) -> dict[str, Any]:
        core = self.core
        local = core.schedule.local(created)
        day = core.schedule.trading_day(created)
        price = entry_ref if entry_ref is not None else (core.last_bar.close if core.last_bar else None)
        ctx = core.market_snapshot(price, created) if price is not None else {}
        slot = evaluation.get("slot") or slot_for(local.time())
        regime = evaluation.get("regime") or core.regime.value
        rec: dict[str, Any] = {
            "id": decision_id(created, strategy, side), "day": day.isoformat(), "time": local.strftime("%H:%M"),
            "created": created.isoformat(), "status": status, "strategy": strategy, "title": title, "side": side,
            "symbol": core.contract.root or core.cfg.instrument.symbol, "signal": signal, "note": note, "tag": tag,
            "entry": plan.entry_ref if plan else price, "stop": plan.stop if plan else stop,
            "target": plan.target if plan else target, "size": plan.size if plan else 0,
            "risk_usd": round(plan.planned_risk, 2) if plan else None,
            "slot": slot, "regime": regime, "policy": evaluation.get("policy"),
            "candidates": [dict(c) for c in evaluation["candidates"]], "chosen": evaluation.get("chosen"),
            "market": {"snapshot": ctx, "described": describe(ctx, side)},
            "rules": self._rules(), "outcome": None, "lesson": None,
        }
        judge = core.judge
        if judge is not None and strategy in {c["strategy"] for c in rec["candidates"]}:
            try:
                rec["evidence"] = judge.evidence(strategy, side, slot, regime, ctx or None, day)
            except Exception:  # noqa: BLE001
                log.exception("Could not gather the evidence for %s", rec["id"])
        return rec

    def _rules(self) -> dict[str, Any]:
        core = self.core
        price = core.last_price if core.last_price is not None else (core.last_bar.close if core.last_bar else None)
        r = core.risk.snapshot(core.balance, core.orders.open_pnl(price) if price is not None else 0.0)
        keep = ("day_pnl", "trades_today", "max_trades", "consecutive_losses", "daily_loss_limit", "mll_room",
                "risk_per_trade", "locked", "paused")
        out = {k: r.get(k) for k in keep}
        out["max_consecutive_losses"] = core.cfg.risk.max_consecutive_losses
        if r.get("combine"):
            out["combine"] = r["combine"].get("summary")
        return out

    def _save(self, rec: dict, ev: dict) -> None:
        journal = self.core.journal
        if journal is None:
            return
        try:
            journal.record_decision(rec)
        except Exception:  # noqa: BLE001 - an explanation must never disturb trading
            log.exception("Could not save decision %s", rec["id"])
            return
        for i, c in enumerate(rec["candidates"]):
            self._open[decision_id(ev["bar_close"], c["strategy"], c["side"])] = (rec["id"], i)

    def _load_open(self) -> None:
        """After a restart: keep following today's records that are still waiting for their result."""
        journal = self.core.journal
        if journal is None:
            return
        try:
            recent = journal.decisions(limit=60)
        except Exception:  # noqa: BLE001
            return
        for rec in recent:
            if rec.get("outcome") is not None:
                continue
            created = datetime.fromisoformat(rec["created"])
            for i, c in enumerate(rec.get("candidates") or []):
                if "outcome" not in c:
                    self._open.setdefault(decision_id(created, c["strategy"], c["side"]), (rec["id"], i))

    def _lesson(self, rec: dict) -> dict[str, Any] | None:
        """How the verdict on this strategy, time of day and market moved once the result was learned."""
        judge = self.core.judge
        chosen = next((c for c in rec["candidates"] if c["strategy"] == rec["strategy"]), None)
        if judge is None or chosen is None or chosen.get("score") is None:
            return None
        day = self.core.schedule.trading_day(self.core.clock())
        after = judge.verdict(rec["strategy"], rec["slot"], rec["regime"], day)
        before = {"allowed": chosen["allowed"], "score": chosen["score"], "why": chosen["why"]}
        return {"before": before, "after": {"allowed": after.allowed, "score": round(after.score, 3), "why": after.why}}


def why_line(rec: dict[str, Any]) -> str:
    """A taken trade's reason in two or three short sentences (the alert sent with the entry)."""
    from topstep_bot.fmt import r

    chosen = next((c for c in rec["candidates"] if c["strategy"] == rec["strategy"]), None)
    parts = [f"💡 Why {rec['title']} {rec['side']}: {chosen['why'] if chosen else rec['signal']}."]
    est = (rec.get("evidence") or {}).get("estimate")
    if est:
        parts.append(f"Expected after costs: {r(est['net_r'])} a trade.")
    others = [c for c in rec["candidates"] if c is not chosen]
    if others:
        parts.append("Also signalled: " + ", ".join(f"{c['title']} ({'allowed' if c['allowed'] else 'not allowed'})"
                                                 for c in others) + ".")
    market = (rec.get("market") or {}).get("described") or []
    if market:
        parts.append("Market: " + "; ".join(market[:3]) + ".")
    parts.append('Ask "why the last trade?" for the full story.')
    return " ".join(parts)
