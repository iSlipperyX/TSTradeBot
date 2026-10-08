"""Adaptive All-Day: runs every strategy all session and trades only what is proven to be working.

Each bar goes to every sub-strategy. When one of them wants to enter, the knowledge base
(knowledge.py) is asked whether that strategy has been profitable at this time of day in the
current volatility regime. Only proven signals are traded; when several strategies signal on
the same bar the one with the best evidence wins. The sub-strategy that opened the trade keeps
managing it (exits and trailing stops).

The bot keeps learning while this runs: every signal from every sub-strategy - traded or not - is
followed to its outcome and added to the knowledge base, and the base is retrained on recent
history each day. See docs/HOW_TO_USE.md, "Training and the knowledge base".
"""

from __future__ import annotations

import logging
from collections import deque
from collections.abc import Callable
from dataclasses import replace
from datetime import date

from topstep_bot.knowledge import KnowledgeBase, Verdict, slot_for
from topstep_bot.models import Bar, Signal
from topstep_bot.strategies.base import Strategy, StrategyContext

log = logging.getLogger("topstep_bot.adaptive")

OWNER_GRACE_BARS = 2  # bars a sub-strategy stays "owner" after its signal while the entry fills


class AdaptiveAllDay(Strategy):
    name = "adaptive"
    title = "Adaptive All-Day"
    description = (
        "Runs all the other strategies through the whole session and takes a signal only when the "
        "bot's knowledge base shows that strategy has been working at this time of day in the current "
        "volatility regime (calm/volatile). Learns continuously from real and hypothetical outcomes and "
        "retrains on recent history daily. Trades nothing until it has evidence - run 'train' first."
    )
    defaults = {
        "strategies": [],  # empty = every other strategy
        "trade_unproven": False,  # True = also trade strategies the knowledge base knows nothing about yet
        "direction": "both",
    }

    def setup(self) -> None:
        from topstep_bot.strategies import STRATEGIES

        names = list(self.p["strategies"]) or [n for n in STRATEGIES if n != self.name]
        self.subs: list[Strategy] = []
        for n in names:
            if n == self.name:
                continue
            cls = STRATEGIES.get(n)
            if cls is None:
                raise ValueError(f"adaptive: unknown strategy '{n}'. Available: {', '.join(k for k in STRATEGIES if k != self.name)}")
            try:
                self.subs.append(cls(self.contract, self.tf, self.rth_open, self.rth_close, {}))
            except ValueError as exc:
                log.warning("adaptive: %s can't run on %d-minute bars (%s) - skipped", n, self.tf, exc)
        if not self.subs:
            raise ValueError("adaptive: no usable sub-strategy")
        self.knowledge: KnowledgeBase | None = None
        self.regime_fn: Callable[[], str] = lambda: "calm"
        self.owner: str | None = None
        self._owner_bars = 0
        self.decisions: deque[dict] = deque(maxlen=20)
        self._warned = False
        self._allowed_cache: tuple[tuple, list[str]] | None = None

    def bind_knowledge(self, knowledge: KnowledgeBase | None, regime_fn: Callable[[], str]) -> None:
        """The engine attaches the knowledge base and the live regime detector."""
        self.knowledge = knowledge
        self.regime_fn = regime_fn
        self._allowed_cache = None

    @property
    def warmup_days(self) -> int:
        return max(s.warmup_days for s in self.subs)

    def on_new_day(self, day: date) -> None:
        for s in self.subs:
            s.on_new_day(day)

    # ------------------------------------------------------------------ bars

    def on_bar(self, bar: Bar, ctx: StrategyContext) -> Signal | None:
        if ctx.position != 0:
            self._owner_bars = 0
        elif self.owner is not None:
            self._owner_bars += 1
            if self._owner_bars > OWNER_GRACE_BARS:
                self.owner = None
        exit_signal: Signal | None = None
        candidates: list[tuple[Strategy, Signal]] = []
        for s in self.subs:
            own = s.name == self.owner
            sctx = ctx if own else replace(ctx, position=0, entry_price=None, stop_price=None)
            try:
                sig = s.on_bar(bar, sctx)
            except Exception:  # noqa: BLE001 - one broken sub-strategy must not stop the others
                log.exception("adaptive: %s failed on a bar", s.name)
                continue
            if sig is None:
                continue
            if sig.action == "exit":
                if own:
                    exit_signal = sig
            elif sig.side is not None:
                candidates.append((s, sig))
        if ctx.position != 0:
            return exit_signal
        if ctx.warmup or not candidates:
            return None
        slot, regime = slot_for(ctx.local_close.time()), self.regime_fn()
        best: tuple[Strategy, Signal, Verdict] | None = None
        for s, sig in candidates:
            if not self.allows(sig.action):
                continue
            v = self._verdict(s.name, slot, regime, ctx.day)
            self.decisions.appendleft({"time": ctx.local_close.strftime("%H:%M"), "strategy": s.name, "side": sig.side.label,
                                       "allowed": v.allowed, "why": v.why})
            log.info("adaptive: %s %s at %s (%s/%s) - %s", s.title, sig.side.label, ctx.local_close.strftime("%H:%M"),
                     slot, regime, "TAKING: " + v.why if v.allowed else "skipped: " + v.why)
            if v.allowed and (best is None or v.score > best[2].score):
                best = (s, sig, v)
        if best is None:
            return None
        s, sig, v = best
        self.owner, self._owner_bars = s.name, 0
        sig.reason = f"{s.title}: {sig.reason} [{slot}/{regime}: {v.why}]"
        sig.meta.update(strategy=s.name, slot=slot, regime=regime, score=round(v.score, 3))
        return sig

    def trailing_stop(self, bar: Bar, ctx: StrategyContext) -> float | None:
        sub = self._owner_strategy()
        return sub.trailing_stop(bar, ctx) if sub else None

    # --------------------------------------------------------------- knowledge

    def _verdict(self, name: str, slot: str, regime: str, day: date) -> Verdict:
        if self.knowledge is None:
            if self.p["trade_unproven"]:
                return Verdict(True, "unproven", 0.0, "no knowledge base - trading unproven signals (trade_unproven)")
            if not self._warned:
                self._warned = True
                log.warning("adaptive: no knowledge base attached - no trades until one is (run 'topstep-bot train')")
            return Verdict(False, "unproven", 0.0, "no knowledge base")
        v = self.knowledge.verdict(name, slot, regime, day)
        if not v.allowed and v.level == "unproven" and self.p["trade_unproven"]:
            return Verdict(True, "unproven", 0.0, v.why + " (trading anyway: trade_unproven)")
        return v

    def allowed_now(self, slot: str, regime: str, day: date) -> list[str]:
        key = (slot, regime, day, self.knowledge.updated if self.knowledge else -1)
        if self._allowed_cache and self._allowed_cache[0] == key:
            return self._allowed_cache[1]
        names = [s.name for s in self.subs if self._verdict(s.name, slot, regime, day).allowed]
        self._allowed_cache = (key, names)
        return names

    def _owner_strategy(self) -> Strategy | None:
        return next((s for s in self.subs if s.name == self.owner), None)

    def state(self) -> dict:
        out: dict = {"regime": self.regime_fn(), "managing": self.owner or "-"}
        sub = self._owner_strategy()
        if sub:
            out.update({f"{sub.name}.{k}": v for k, v in sub.state().items()})
        if self.decisions:
            d = self.decisions[0]
            out["last_signal"] = f"{d['time']} {d['strategy']} {d['side']}: {'taken' if d['allowed'] else 'skipped'} - {d['why']}"
        return out
