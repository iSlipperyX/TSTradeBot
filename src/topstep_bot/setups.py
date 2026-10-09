"""Trade setups: what every strategy is building toward, for the dashboard's "Setups" panel.

Each strategy reports the entries it could take next (``Strategy.setups``) with its entry rules in
plain words and which are met right now. This module adds what the bot would do with each one -
the risk-sized plan from ``TradingCore.plan_entry`` and whether a Topstep or personal guard would
block it - and keeps a short history of setups forming, firing (a signal) and being cancelled, so
you can follow a trade from the first condition to the order.

Purely informational: nothing here places or blocks a trade.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from topstep_bot.knowledge import NOT_STRATEGIES
from topstep_bot.models import Signal
from topstep_bot.strategies.base import Setup, Strategy

if TYPE_CHECKING:
    from topstep_bot.engine import TradingCore

FORMING = 0.5  # share of the conditions met before a setup counts as "forming"
RESET = 0.3  # ...and below which a forming setup counts as cancelled (a gap, so it doesn't flicker)
MAX_EVENTS = 40


@dataclass
class _Seen:
    progress: float
    conditions: list[tuple[str, bool]]
    title: str
    side: str
    announced: bool  # a "forming" event was recorded


class SetupTracker:
    def __init__(self, core: TradingCore):
        self.core = core
        self.enabled = False  # set by the live runner: backtests and training skip the bookkeeping
        self.events: deque[dict[str, Any]] = deque(maxlen=MAX_EVENTS)
        self._seen: dict[tuple[str, str], _Seen] = {}
        self._day = None

    # ------------------------------------------------------------- collecting

    def _sources(self) -> list[tuple[Strategy, bool]]:
        """(strategy, traded by the bot?) - the auto-traded strategy first, then the shadows not already covered."""
        core = self.core
        active = core.strategy
        out: list[tuple[Strategy, bool]] = [(active, True)]
        covered = {s.name for s in getattr(active, "subs", [])} | {active.name}
        if core.recommender:
            out += [(s, False) for s in core.recommender.shadows if s.name not in covered]
        return out

    def collect(self, price: float | None = None, now: datetime | None = None) -> list[tuple[Strategy, bool, Setup]]:
        core = self.core
        price = core.last_price if price is None else price
        local = core.schedule.local(now or core.clock())
        out = []
        for strat, active in self._sources():
            try:
                found = strat.setups(price, local)
            except Exception:  # noqa: BLE001 - a dashboard panel must never disturb trading
                continue
            book = core.recommender
            for st in found:
                st.strategy = st.strategy or strat.name
                if book is not None and book._open_for(st.strategy) is not None:
                    continue  # already in a trade (or tracking an idea): it won't signal again until that ends
                out.append((strat, active, st))
        return out

    def guard(self) -> str | None:
        """Why the bot couldn't enter right now, whatever the setup (None = it could)."""
        core = self.core
        if core.halted:
            return f"the bot is halted ({core.halted})"
        if not core.orders.is_flat:
            return "a trade is already open (one position at a time)"
        guard = core.orders.guard
        if guard is not None and guard.tripped:
            return f"the order guard stopped trading: {guard.tripped}"
        return core.risk.entry_block_reason(core.clock(), core.balance, core.orders.open_pnl())

    def _plan(self, st: Setup, ref: float | None) -> dict[str, Any]:
        core = self.core
        if ref is None:
            return {"problem": "no price yet"}
        if st.stop is None:
            return {"problem": "the stop is set when it fires"}
        plan = core.plan_entry(Signal(st.side, st.stop, st.target), ref)
        if isinstance(plan, str):
            return {"problem": plan}
        c = core.contract
        risk_pts = abs(plan.entry_ref - plan.stop)
        rr = round(abs(plan.target - plan.entry_ref) / risk_pts, 2) if plan.target is not None and risk_pts else None
        return {"size": plan.size, "stop": plan.stop, "target": plan.target, "risk_usd": round(plan.planned_risk, 2),
                "stop_ticks": round(c.ticks(risk_pts)), "rr": rr}

    def view(self, price: float | None = None) -> dict[str, Any]:
        """Everything the dashboard's Setups panel shows."""
        from topstep_bot.strategies import STRATEGIES

        core = self.core
        price = core.last_price if price is None else price
        blocked = self.guard()
        items, waiting = [], []
        for strat, active, st in self.collect(price):
            title = STRATEGIES[st.strategy].title if st.strategy in STRATEGIES else strat.title
            met = sum(1 for _, ok in st.conditions if ok)
            if met == 0:
                waiting.append(f"{title} {st.side}")
                continue
            kb_ok = all(ok for text, ok in st.conditions if text.startswith("Knowledge base allows"))
            ref = st.entry if st.entry is not None else price
            items.append({
                "key": f"{st.strategy}:{st.side}", "strategy": st.strategy, "title": title,
                "role": "bot" if active and kb_ok else "idea", "side": st.side.upper(), "symbol": core.contract.name,
                "conditions": [{"text": text, "met": ok} for text, ok in st.conditions],
                "met": met, "total": len(st.conditions), "progress": round(st.progress, 2),
                "entry": _round(core, st.entry), "entry_ref": _round(core, ref), "entry_is_level": st.entry is not None,
                "stop": _round(core, st.stop), "target": _round(core, st.target), "note": st.note,
                "at": st.at.strftime("%H:%M") if st.at else None,
                "plan": self._plan(st, ref), "blocked": blocked,
            })
        items.sort(key=lambda i: (i["role"] != "bot", -i["progress"], i["title"]))
        return {"items": items, "waiting": waiting, "blocked": blocked, "price": price,
                "events": list(self.events)}

    # ------------------------------------------------------------- history

    def on_bar_closed(self, close_time: datetime, price: float) -> None:
        """After every closed bar: note setups that started forming, fired or were cancelled."""
        if not self.enabled:
            return
        from topstep_bot.strategies import STRATEGIES

        core = self.core
        day = core.schedule.trading_day(close_time)
        if day != self._day:  # a new trading day: yesterday's setups are gone
            self._day = day
            self._seen.clear()
        when = core.schedule.local(close_time).strftime("%H:%M")
        fired: set[tuple[str, str]] = set()
        for rec in list(core.recommender.items)[:20] if core.recommender else []:
            if rec.created != close_time or rec.strategy in NOT_STRATEGIES:
                continue
            side = "long" if rec.side.sign > 0 else "short"
            key = (rec.strategy, side)
            fired.add(key)
            if rec.status == "taken":
                text = f"Fired: the bot placed a {rec.side.label} at {rec.entry:.2f}"
            elif rec.status == "skipped":
                text = f"Fired but skipped: {rec.note}" if rec.note else "Fired but skipped"
            else:
                text = "Fired: posted as an idea" + (f" ({rec.note})" if rec.note else "")
            self._event(when, "fired", rec.title, side, text, rec.strategy)
            self._seen.pop(key, None)

        current: dict[tuple[str, str], tuple[str, Setup]] = {}
        for strat, _, st in self.collect(price, close_time):
            title = STRATEGIES[st.strategy].title if st.strategy in STRATEGIES else strat.title
            current[(st.strategy, st.side)] = (title, st)

        for key, seen in list(self._seen.items()):
            if key in current:
                continue
            if seen.announced:
                took = next((side for strategy, side in fired if strategy == key[0]), None)
                why = (f"the strategy took the {took} trade instead" if took
                       else "no longer possible now (its time window closed or the setup reset)")
                self._event(when, "cancelled", seen.title, seen.side, f"Cancelled: {why}", key[0])
            del self._seen[key]

        for key, (title, st) in current.items():
            if key in fired:
                continue
            p = st.progress
            seen = self._seen.get(key)
            if seen is None:
                seen = self._seen[key] = _Seen(p, list(st.conditions), title, st.side, False)
            if not seen.announced and p >= FORMING:
                met = [text for text, ok in st.conditions if ok]
                todo = [text for text, ok in st.conditions if not ok]
                self._event(when, "forming", title, st.side,
                            f"Forming: {len(met)} of {len(st.conditions)} conditions met"
                            + (f"; waiting for: {todo[0]}" if todo else ""), key[0])
                seen.announced = True
            elif seen.announced and p < RESET:
                lost = [text for (text, was), (_, now) in zip(seen.conditions, st.conditions, strict=False) if was and not now]
                self._event(when, "cancelled", title, st.side,
                            "Cancelled: " + (f"no longer true: {lost[0]}" if lost else "conditions fell away"), key[0])
                seen.announced = False
            seen.progress, seen.conditions = p, list(st.conditions)

    def _event(self, when: str, kind: str, title: str, side: str, text: str, strategy: str) -> None:
        self.events.appendleft({"time": when, "kind": kind, "title": title, "strategy": strategy,
                                "side": side.upper(), "text": text})


def _round(core: TradingCore, price: float | None) -> float | None:
    return None if price is None else core.contract.round_price(price)
