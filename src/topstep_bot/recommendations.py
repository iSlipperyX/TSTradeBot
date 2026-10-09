"""Recommended trades: what every strategy wants to do right now, sized with your risk rules.

* The configured strategy's signals are traded automatically; each one appears here as
  "taken" or "skipped" (with the reason - outside trading hours, daily limit, too risky...).
* The other strategies run in "shadow" mode on the same bars. Their signals are shown as
  ideas you could act on yourself - the bot never trades them.
* Every recommendation is then followed bar by bar to a result (stop, target, strategy exit or
  session end), so you can see how good each strategy's ideas really are on live data.
  Results for ideas that weren't traded are hypothetical (stop-first if a bar hits both).
* Each result is also handed to the knowledge base (knowledge.py): this is how the bot learns
  which strategy works at which time of day, whether it traded the signal or not. With it go the
  market snapshot at signal time, how far the idea went for and against it on the way (MFE / MAE)
  and its costs, so the bot can later find out *when* a strategy works, not only *whether*.
"""

from __future__ import annotations

import itertools
import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any

from topstep_bot.knowledge import NOT_STRATEGIES, UNINFORMATIVE_EXITS, Observation
from topstep_bot.models import Bar, OrderSide, Signal
from topstep_bot.strategies import STRATEGIES, Strategy, StrategyContext, create_strategy

if TYPE_CHECKING:
    from topstep_bot.engine import TradePlan, TradingCore
    from topstep_bot.execution import ManagedTrade

log = logging.getLogger("topstep_bot.recommendations")

OPEN_STATUSES = ("idea", "tracking", "taken", "skipped")


@dataclass
class Recommendation:
    id: str
    created: datetime  # UTC time of the bar close that produced it
    strategy: str
    title: str
    active: bool  # True = traded (or skipped) by the bot itself
    side: OrderSide
    entry: float
    stop: float | None
    target: float | None
    size: int
    risk_usd: float | None
    reason: str
    status: str  # idea | tracking | taken | skipped | closed
    note: str = ""
    slot: str = ""  # time-of-day slot and regime at signal time (knowledge base keys)
    regime: str = ""
    trade_tag: str | None = None
    exit_price: float | None = None
    outcome_usd: float | None = None
    outcome_r: float | None = None
    result: str = ""  # won | lost | flat
    closed_at: datetime | None = None
    hypothetical: bool = True
    initial_stop: float | None = field(default=None)
    context: dict = field(default_factory=dict)  # market snapshot at signal time (market_context.py)
    mfe_r: float | None = None  # furthest it went in its favour so far, in R
    mae_r: float | None = None  # furthest it went against it so far, in R
    bars: int = 0  # bars followed
    facts: dict = field(default_factory=dict)  # a real trade's TradingCore.trade_facts, once it closes

    def __post_init__(self) -> None:
        if self.initial_stop is None:
            self.initial_stop = self.stop

    def note_path(self, favourable: float | None, adverse: float | None) -> None:
        """Extend the MFE / MAE with prices the idea reached (a hypothetical outcome's path)."""
        risk = abs(self.entry - self.initial_stop) if self.initial_stop is not None else 0.0
        if not risk:
            return
        mfe, mae = self.mfe_r or 0.0, self.mae_r or 0.0  # both start at the entry price
        if favourable is not None:
            mfe = max(mfe, (favourable - self.entry) * self.side.sign / risk)
        if adverse is not None:
            mae = min(mae, (adverse - self.entry) * self.side.sign / risk)
        self.mfe_r, self.mae_r = round(mfe, 2), round(mae, 2)

    @property
    def is_open(self) -> bool:
        return self.result == ""

    @property
    def reward_usd(self) -> float | None:
        if self.target is None or self.risk_usd is None or self.stop is None or self.entry == self.stop:
            return None
        return self.risk_usd * abs(self.target - self.entry) / abs(self.entry - self.stop)

    @property
    def rr(self) -> float | None:
        if self.target is None or self.stop is None or self.entry == self.stop:
            return None
        return abs(self.target - self.entry) / abs(self.entry - self.stop)


class RecommendationBook:
    def __init__(self, core: TradingCore, strategies: list[str] | None = None, *, max_items: int = 200,
                 quiet: bool = False):
        self.core = core
        self.quiet = quiet  # backtests/training: no logging, alerts or journal writes
        self.items: deque[Recommendation] = deque(maxlen=max_items)
        self._ids = itertools.count(1)
        self._day: date | None = None
        self.shadows: list[Strategy] = []
        wanted = strategies or [n for n in STRATEGIES if n != core.strategy.name]
        for name in wanted:
            if name == core.strategy.name or name not in STRATEGIES or name == "adaptive":
                continue
            try:
                self.shadows.append(create_strategy(name, {}, core.contract, core.cfg.instrument.timeframe_minutes))
            except ValueError as exc:
                log.warning("Strategy '%s' can't produce recommendations on this timeframe: %s", name, exc)

    # ------------------------------------------------------------- feeding bars

    def _context(self, bar: Bar, strategy_name: str, warmup: bool) -> StrategyContext:
        base = self.core.context(bar, warmup=warmup)
        rec = self._open_for(strategy_name)
        position = 0 if rec is None else rec.side.sign * max(rec.size, 1)
        return StrategyContext(base.bar_close, base.local_close, base.day, position,
                               rec.entry if rec else None, rec.stop if rec else None, warmup)

    def _new_day_check(self, bar: Bar) -> None:
        day = self.core.schedule.trading_day(bar.ts)
        if day == self._day:
            return
        self._day = day
        for strat in self.shadows:
            strat.on_new_day(day)
        for rec in self.items:  # anything left open from an earlier day (e.g. the bot was off)
            if rec.is_open and rec.status != "taken":
                self._close(rec, rec.entry if rec.exit_price is None else rec.exit_price, "expired (new day)")

    def warmup_bar(self, bar: Bar) -> None:
        self._new_day_check(bar)
        for strat in self.shadows:
            strat.on_bar(bar, self._context(bar, strat.name, warmup=True))

    def on_bar(self, bar: Bar) -> list[tuple[str, str, Any]]:
        """Advance outcomes with this bar, then collect fresh ideas from the shadow strategies.

        Returns actions for trades you took from ideas: ("exit", tag, reason) / ("stop", tag, price).
        """
        actions: list[tuple[str, str, Any]] = []
        self._new_day_check(bar)
        self._track(bar)
        close_time = bar.ts + timedelta(minutes=self.core.cfg.instrument.timeframe_minutes)
        for strat in self.shadows:
            ctx = self._context(bar, strat.name, warmup=False)
            try:
                sig = strat.on_bar(bar, ctx)
                open_rec = self._open_for(strat.name)
                if open_rec is not None and not open_rec.active:  # the bot manages its own trades itself
                    new_stop = strat.trailing_stop(bar, ctx)
                    if new_stop is not None and open_rec.stop is not None:
                        better = new_stop > open_rec.stop if open_rec.side == OrderSide.BUY else new_stop < open_rec.stop
                        if better and open_rec.status == "taken":
                            actions.append(("stop", open_rec.trade_tag, new_stop))
                        elif better:
                            open_rec.stop = self.core.contract.round_price(new_stop)
            except Exception:  # noqa: BLE001 - a shadow strategy must never disturb trading
                log.exception("Shadow strategy %s failed", strat.name)
                continue
            if sig is None:
                continue
            if sig.action == "exit":
                if open_rec is None or open_rec.active:
                    continue
                if open_rec.status == "taken":
                    actions.append(("exit", open_rec.trade_tag, f"{strat.title} exit: {sig.reason}"))
                else:
                    self._close(open_rec, bar.close, f"strategy exit: {sig.reason}")
            elif sig.side is not None and open_rec is None:
                self._add_idea(strat, sig, bar, close_time)
        return actions

    def swap_active(self, name: str) -> Strategy:
        """Make ``name`` the auto-traded strategy, reusing its warmed-up shadow; the old one becomes a shadow."""
        new = next((s for s in self.shadows if s.name == name), None)
        if new is None:
            new = create_strategy(name, {}, self.core.contract, self.core.cfg.instrument.timeframe_minutes)
        else:
            self.shadows.remove(new)
        if self.core.strategy.name != "adaptive":
            self.shadows.append(self.core.strategy)
        return new

    def _add_idea(self, strat: Strategy, sig: Signal, bar: Bar, close_time: datetime) -> None:
        core = self.core
        if any(r.active and r.strategy == strat.name and r.created == close_time for r in self.items):
            return  # the bot already acted on this very signal (adaptive strategy): don't count it twice
        entry = bar.close
        plan = core.plan_entry(sig, entry)
        note = ""
        if isinstance(plan, str):
            note, size, stop, target, risk = plan, 0, sig.stop_price, sig.target_price, None
        else:
            size, stop, target, risk = plan.size, plan.stop, plan.target, plan.planned_risk
        blocked = core.schedule.entry_block_reason(close_time)
        if blocked:
            note = f"outside your rules: {blocked}" + (f"; {note}" if note else "")
        rec = Recommendation(
            id=f"R{next(self._ids)}", created=close_time, strategy=strat.name, title=strat.title, active=False,
            side=sig.side, entry=entry, stop=stop, target=target, size=size, risk_usd=risk,
            reason=sig.reason, status="idea", note=note, slot=core.slot(close_time), regime=core.regime.value,
            context=core.market_snapshot(entry, close_time),
        )
        self._store(rec, new=True)

    def record_active(
        self,
        sig: Signal,
        ctx: StrategyContext,
        entry_ref: float,
        plan: TradePlan | None,
        status: str,
        note: str,
        tag: str | None = None,
    ) -> None:
        """Called by the engine for every entry signal of the configured strategy."""
        strat = self.core.strategy
        name = sig.meta.get("strategy", strat.name)  # the adaptive strategy names the sub-strategy that signalled
        title = STRATEGIES[name].title if name in STRATEGIES else strat.title
        rec = Recommendation(
            id=f"R{next(self._ids)}", created=ctx.bar_close, strategy=name, title=title, active=True,
            side=sig.side, entry=entry_ref,
            stop=plan.stop if plan else sig.stop_price, target=plan.target if plan else sig.target_price,
            size=plan.size if plan else 0, risk_usd=plan.planned_risk if plan else None,
            reason=sig.reason, status=status, note=note, trade_tag=tag, hypothetical=status != "taken",
            slot=sig.meta.get("slot") or self.core.slot(ctx.bar_close), regime=sig.meta.get("regime") or self.core.regime.value,
            context=self.core.market_snapshot(entry_ref, ctx.bar_close),
        )
        self._store(rec, new=True)

    def trade_closed(self, t: ManagedTrade) -> None:
        """A real trade finished: give its recommendation the actual result."""
        for rec in self.items:
            if rec.trade_tag == t.tag and rec.is_open:
                rec.size = t.filled_size or rec.size
                if t.entry_price:
                    rec.entry = t.entry_price
                rec.facts = self.core.trade_facts(t)
                rec.mfe_r, rec.mae_r = rec.facts["mfe_r"], rec.facts["mae_r"]
                self._close(rec, t.exit_price or rec.entry, t.exit_reason, outcome=t.net_pnl)
                return

    # ------------------------------------------------------------- outcomes

    def _open_for(self, strategy: str) -> Recommendation | None:
        for rec in self.items:
            if rec.strategy == strategy and rec.is_open:
                return rec
        return None

    def _track(self, bar: Bar) -> None:
        close_time = bar.ts + timedelta(minutes=self.core.cfg.instrument.timeframe_minutes)
        session_over = self.core.schedule.must_be_flat(close_time)
        live_tag = self.core.orders.trade.tag if self.core.orders.trade else None
        for rec in list(self.items):
            if not rec.is_open or bar.ts < rec.created:
                continue
            if rec.status == "taken":
                if rec.trade_tag != live_tag:  # the entry was cancelled before it filled
                    self._close(rec, rec.entry, "entry not filled (price moved away)", outcome=0.0)
                continue
            if rec.status == "idea":
                rec.status = "tracking"
            long = rec.side == OrderSide.BUY
            favourable, adverse = (bar.high, bar.low) if long else (bar.low, bar.high)
            rec.bars += 1
            # Stop first when a bar touches both (as the outcome itself): the path then never
            # counts the favourable extreme of a bar that stopped the idea out.
            if rec.stop is not None and ((long and bar.low <= rec.stop) or (not long and bar.high >= rec.stop)):
                rec.note_path(None, rec.stop)
                self._close(rec, rec.stop, "stop hit")
            elif rec.target is not None and ((long and bar.high >= rec.target) or (not long and bar.low <= rec.target)):
                rec.note_path(rec.target, adverse)
                self._close(rec, rec.target, "target hit")
            else:
                rec.note_path(favourable, adverse)
                if session_over:
                    self._close(rec, bar.close, "session end")

    def _close(self, rec: Recommendation, price: float, why: str, outcome: float | None = None) -> None:
        c = self.core.contract
        points = (price - rec.entry) * rec.side.sign
        risk_points = abs(rec.entry - rec.initial_stop) if rec.initial_stop is not None else 0
        rec.exit_price = price
        rec.outcome_r = round(points / risk_points, 2) if risk_points else None
        if outcome is None and rec.size:
            outcome = points * c.point_value * rec.size - self.core.orders.fees_round_turn * rec.size
        rec.outcome_usd = None if outcome is None else round(outcome, 2)
        basis = rec.outcome_usd if rec.outcome_usd is not None else points
        rec.result = "won" if basis > 0 else ("lost" if basis < 0 else "flat")
        rec.note = f"{rec.note}; {why}" if rec.note and rec.status != "taken" else why
        if rec.status in ("idea", "tracking"):
            rec.status = "closed"
        rec.closed_at = self.core.clock()
        self._store(rec)
        self._learn(rec, why)

    def _learn(self, rec: Recommendation, why: str) -> None:
        """Hand a finished recommendation to the knowledge base (if its ending says something)."""
        kb = self.core.knowledge
        if rec.strategy in NOT_STRATEGIES:  # filed by the trade ticket (manual.py) / first_trade.py themselves
            return
        if kb is None or rec.outcome_r is None or any(k in why for k in UNINFORMATIVE_EXITS):
            return
        local = self.core.schedule.local(rec.created)
        if rec.hypothetical:
            facts = {"ctx": rec.context or None, "mfe_r": rec.mfe_r, "mae_r": rec.mae_r, "bars": rec.bars,
                     "cost_r": self._idea_cost_r(rec)}
        else:
            facts = {**rec.facts, "ctx": rec.context or rec.facts.get("ctx")}
        kb.record(Observation(
            day=self.core.schedule.trading_day(rec.created).isoformat(), time=local.strftime("%H:%M"),
            strategy=rec.strategy, side=rec.side.label, slot=rec.slot or self.core.slot(rec.created),
            regime=rec.regime or "calm", r=rec.outcome_r, usd=rec.outcome_usd,
            source="shadow" if rec.hypothetical else "real", why=why, **facts,
        ), save=not self.quiet)

    def _idea_cost_r(self, rec: Recommendation) -> float | None:
        """What an idea would have cost in R if traded: fees plus the assumed slippage on both fills."""
        c = self.core.contract
        risk = abs(rec.entry - rec.initial_stop) if rec.initial_stop is not None else 0.0
        if not risk or not c.point_value:
            return None
        points = self.core.orders.fees_round_turn / c.point_value + 2 * self.core.cfg.risk.slippage_ticks * c.tick_size
        return round(points / risk, 3)

    # ------------------------------------------------------------- output

    def _store(self, rec: Recommendation, new: bool = False) -> None:
        if new:
            self.items.appendleft(rec)
        if self.quiet:
            return
        data = self.to_dict(rec)
        if new:
            log.info(
                "%s %s %s %s @ %s stop %s%s (%s)%s", "Trade" if rec.active else "Idea", rec.title, rec.side.label,
                rec.size or "-", rec.entry, rec.stop, f" target {rec.target}" if rec.target else "", rec.reason,
                f" - {rec.status}: {rec.note}" if rec.note else f" - {rec.status}",
                extra={"event": "recommendation", "data": data},
            )
            if not rec.active and self.core.notifier and rec.size and not rec.note:
                self.core.notifier.notify(
                    "idea", f"Idea ({rec.title}): {rec.side.label} {rec.size} @ ~{rec.entry} stop {rec.stop}"
                    + (f" target {rec.target}" if rec.target else "") + f" - {rec.reason}")
        else:
            log.info("Recommendation %s %s: %s%s", rec.id, rec.title, rec.result or rec.status,
                     f" ({rec.note})" if rec.note else "",
                     extra={"event": "recommendation_result", "data": data})
        if self.core.journal:
            self.core.journal.record_recommendation(data)

    def to_dict(self, rec: Recommendation) -> dict[str, Any]:
        local = self.core.schedule.local(rec.created)
        return {
            "id": rec.id,
            "time": local.strftime("%H:%M"),
            "date": local.date().isoformat(),
            "created": rec.created.isoformat(),
            "strategy": rec.strategy,
            "title": rec.title,
            "active": rec.active,
            "side": rec.side.label,
            "entry": rec.entry,
            "stop": rec.stop,
            "target": rec.target,
            "size": rec.size,
            "risk_usd": None if rec.risk_usd is None else round(rec.risk_usd, 2),
            "reward_usd": None if rec.reward_usd is None else round(rec.reward_usd, 2),
            "rr": None if rec.rr is None else round(rec.rr, 2),
            "reason": rec.reason,
            "status": rec.status,
            "note": rec.note,
            "slot": rec.slot,
            "regime": rec.regime,
            "result": rec.result,
            "exit_price": rec.exit_price,
            "outcome_usd": rec.outcome_usd,
            "outcome_r": rec.outcome_r,
            "hypothetical": rec.hypothetical,
        }

    def snapshot(self, limit: int = 25) -> dict[str, Any]:
        today = self.core.schedule.trading_day(self.core.clock())
        todays = [r for r in self.items if self.core.schedule.trading_day(r.created) == today]
        summary = []
        for strat in [self.core.strategy, *self.shadows]:
            active = strat is self.core.strategy
            mine = [r for r in todays if (r.active if active else (r.strategy == strat.name and not r.active))]
            closed = [r for r in mine if r.result]
            summary.append({
                "strategy": strat.name, "title": strat.title, "active": active,
                "ideas": len(mine), "closed": len(closed), "wins": sum(1 for r in closed if r.result == "won"),
                "pnl": round(sum(r.outcome_usd or 0 for r in closed), 2),
            })
        watching = {
            s.title: {k: (round(v, 2) if isinstance(v, float) else v) for k, v in s.state().items()} for s in self.shadows
        }
        return {
            "items": [self.to_dict(r) for r in list(self.items)[:limit]],
            "open": [self.to_dict(r) for r in self.items if r.status == "idea"],
            "summary": summary,
            "watching": watching,
        }

    def text(self, limit: int = 8) -> str:
        """Plain-text list for Telegram."""
        if not self.items:
            return "No recommendations yet - they appear when a strategy signals during market hours."
        lines = []
        for r in list(self.items)[:limit]:
            d = self.to_dict(r)
            head = f"{d['time']} {'★' if r.active else '•'} {r.title}: {r.side.label} {r.size or '-'} @ {r.entry} stop {r.stop}"
            if r.target is not None:
                head += f" tgt {r.target}"
            if r.result:
                money = f"{r.outcome_usd:+,.0f}$" if r.outcome_usd is not None else f"{r.outcome_r:+.1f}R"
                tail = f" -> {r.result.upper()} {money}" + (" (hypothetical)" if r.hypothetical else "")
            else:
                tail = f" -> {r.status}" + (f" ({r.note})" if r.note else "")
            lines.append(head + tail)
        return "Recommended trades (★ = traded by the bot, • = idea only):\n" + "\n".join(lines)
