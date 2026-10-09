"""The first trade after the bot starts: an educated trade within a set time ("Teach the bot").

On an account used to teach the bot, a real trade teaches it more than a morning of watching:
real fills, real slippage, a real result. With ``first_trade.enabled`` (the dashboard's Setup tab
turns it on with the "Teach the bot" goal) the bot makes sure one happens soon after it starts:

* The clock starts when the bot starts, or when its entry window next opens if it starts outside
  it (evening, weekend, holiday, before ``session.trade_start``). The dashboard counts it down
  and says when it is waiting for the session.
* If the bot's own strategy trades before the deadline, that is the first trade: nothing more
  happens.
* Otherwise, on the last bar that closes before the deadline, it takes the setup its knowledge
  base supports best (see ``rank``): a strategy signal from the last few minutes that wasn't
  traded, a strategy setup that is forming, or failing those the side the evidence favours at
  this time of day. A strategy the knowledge base has switched off for this time of day is never
  followed. The trade uses ``first_trade.contracts`` (default 1, the smallest size).
* Every Topstep rule and risk limit applies: the entry goes through the same checks as a strategy
  signal (daily loss limits, Maximum Loss Limit, trade count, pauses, news, entry window). If one
  of them blocks it, the dashboard says why and the bot tries again on each new bar while entries
  are open; when they close it waits for the next session.
* At most one such trade per trading day (kept in the journal, so a restart can't add another).
  A trade owed when the bot restarts by itself (daily maintenance) is still owed afterwards.
* The trade is filed in the knowledge base under the strategy name "first_trade" (source "real")
  with ``basis`` naming what it followed, so its results never count as a strategy's own evidence
  and can be studied on their own.

It never trades on a Topstep Express Funded Account: forcing trades is for learning accounts.
No setting here makes a profit more likely; it makes the bot learn sooner.
"""

from __future__ import annotations

import logging
import math
import random
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from topstep_bot.knowledge import FIRST_TRADE, NOT_STRATEGIES, UNINFORMATIVE_EXITS, Observation
from topstep_bot.models import OrderSide, Signal

if TYPE_CHECKING:
    from topstep_bot.config import FirstTradeConfig
    from topstep_bot.engine import TradePlan, TradingCore
    from topstep_bot.execution import ManagedTrade
    from topstep_bot.journal import Journal

log = logging.getLogger("topstep_bot.first_trade")
UTC = timezone.utc

TITLE = "First trade"
# Ranking weights, in R. The strategy's own evidence decides; how this side has done here, how far
# the setup has formed and the trend only break near-ties.
SIDE_WEIGHT = 0.5
PROGRESS_WEIGHT = 0.05
TREND_WEIGHT = 0.01
KB_PREFIX = "Knowledge base allows"  # the adaptive strategy's own condition on its sub-strategies' setups
CLOCK_RETRY = timedelta(seconds=60)
STATES = ("off", "waiting", "watching", "blocked", "done", "idle")


@dataclass
class Choice:
    """One trade the bot could take as its first trade, and how well its knowledge supports it."""

    side: OrderSide
    kind: str  # signal (fired, not traded) | setup (forming) | lean (no setup: the side the evidence favours)
    strategy: str = ""  # whose signal or setup ("" for a lean)
    title: str = ""
    stop: float | None = None
    target: float | None = None
    progress: float = 0.0  # share of the setup's conditions met (1 for a signal)
    score: float = 0.0  # ranking score in R
    evidence: str = ""  # plain words for the dashboard and the log
    excluded: str = ""  # why it may not be followed (the knowledge base switched the strategy off)

    @property
    def basis(self) -> str:
        what = {"signal": "signal", "setup": "setup", "lean": "lean"}[self.kind]
        return f"{self.strategy} {self.side.label.lower()} {what}" if self.strategy else f"{self.side.label.lower()} {what}"

    def describe(self) -> str:
        head = {"signal": f"{self.title} {self.side.label} signal (not traded by the strategy)",
                "setup": f"{self.title} {self.side.label} setup ({round(self.progress * 100)}% of its conditions met)",
                "lean": f"{self.side.label} (no setup is forming; the side the evidence favours)"}[self.kind]
        return f"{head}: {self.evidence}" if self.evidence else head

    def to_dict(self) -> dict[str, Any]:
        return {"side": self.side.label, "kind": self.kind, "strategy": self.strategy, "title": self.title,
                "progress": round(self.progress, 2), "score": round(self.score, 3), "text": self.describe(),
                "basis": self.basis}


def entry_window(core: TradingCore, now: datetime) -> tuple[datetime, datetime] | None:
    """(start, last entry) of the entry window that is open now or opens next; None if none within two weeks."""
    sched = core.schedule
    local = sched.local(now)
    day = sched.trading_day(now)
    for _ in range(15):
        if sched.is_trade_day(day):
            start, last = sched.entry_window(day)
            if local < last:
                return max(start, local), last
        day += timedelta(days=1)
    return None


def _when(core: TradingCore, at: datetime, now: datetime) -> str:
    local, today = core.schedule.local(at), core.schedule.local(now).date()
    return local.strftime("%H:%M CT") if local.date() == today else local.strftime("%a %H:%M CT")


def rank(core: TradingCore, price: float, now: datetime) -> list[Choice]:
    """Every first trade the bot could take now, best supported first (switched-off ones last, marked).

    Score (in R) = the knowledge base's expected R for the strategy at this time of day and regime
    (shrunk toward zero while evidence is thin: KnowledgeBase.verdict) + half the expected R of all
    signals on this side here (KnowledgeBase.side_stats) + a little for how far a setup has formed
    + a hair for trading with the 50-bar trend. With an empty knowledge base that leaves "the most
    formed setup, with the trend".
    """
    kb = core.knowledge
    slot, regime = core.slot(now), core.regime.value
    today = core.schedule.trading_day(now)
    snap = core.market_snapshot(price, now)
    trend = snap.get("trend")
    choices: list[Choice] = []
    seen: set[tuple[str, OrderSide]] = set()

    from topstep_bot.strategies import STRATEGIES

    def title_of(name: str) -> str:
        return STRATEGIES[name].title if name in STRATEGIES else name

    for rec in core.manual.fresh_ideas():  # signals from the last minutes that no one traded
        if rec.strategy in NOT_STRATEGIES or (rec.strategy, rec.side) in seen:
            continue
        seen.add((rec.strategy, rec.side))
        choices.append(Choice(rec.side, "signal", rec.strategy, title_of(rec.strategy), rec.stop, rec.target, 1.0))
    for _, _, st in core.setups.collect(price, now):  # setups that are forming
        side = OrderSide.BUY if st.side == "long" else OrderSide.SELL
        if (st.strategy, side) in seen:
            continue
        conds = [ok for text, ok in st.conditions if not text.startswith(KB_PREFIX)]
        met = sum(1 for ok in conds if ok)
        if not met:
            continue
        seen.add((st.strategy, side))
        choices.append(Choice(side, "setup", st.strategy, title_of(st.strategy), st.stop, st.target, met / len(conds)))
    for side in (OrderSide.BUY, OrderSide.SELL):
        choices.append(Choice(side, "lean"))

    side_cache: dict[OrderSide, tuple[float, str]] = {}

    def side_evidence(side: OrderSide) -> tuple[float, str]:
        if side not in side_cache:
            if kb is None:
                side_cache[side] = (0.0, "")
            else:
                s, where = kb.side_stats(side.label, slot, regime, today), f"{slot}/{regime}"
                if s.n_eff < kb.min_samples:
                    s, where = kb.side_stats(side.label, slot, None, today), f"{slot}"
                text = f"{side.label.lower()}s at {where} {s.mean_r:+.2f}R avg over {s.n}" if s.n else ""
                side_cache[side] = (s.score(kb.min_samples), text)
        return side_cache[side]

    for ch in choices:
        parts = []
        strat_score = 0.0
        if ch.strategy and kb is not None:
            v = kb.verdict(ch.strategy, slot, regime, today)
            if not v.allowed and v.level != "unproven":
                ch.excluded = f"the knowledge base has switched {ch.title} off here ({v.why})"
            strat_score = v.score if v.level != "unproven" else 0.0
            parts.append(f"its record {v.why}")
        side_score, side_text = side_evidence(ch.side)
        if side_text:
            parts.append(side_text)
        with_trend = trend is not None and trend * ch.side.sign > 0
        if ch.kind == "lean" and trend is not None:
            parts.append("with the trend" if with_trend else "against the trend")
        ch.score = (strat_score + SIDE_WEIGHT * side_score + PROGRESS_WEIGHT * ch.progress
                    + (TREND_WEIGHT if with_trend else 0.0))
        ch.evidence = "; ".join(parts) if parts else "no evidence yet"
    choices.sort(key=lambda c: (not c.excluded, c.score, c.progress), reverse=True)
    return choices


def _reason_key(why: str) -> str:
    """A blocking reason without its numbers, so the same rule counts once ("max trades per day (8)" -> ...)."""
    return why.split(" (")[0].split(": ")[0].strip()


class FirstTradePlanner:
    """Owns the first trade after a start: when it is due, what it follows, and placing it."""

    def __init__(self, core: TradingCore, cfg: FirstTradeConfig, *, journal: Journal | None = None,
                 choose: str = "best", seed: int | None = None):
        self.core = core
        self.cfg = cfg
        self.journal = journal
        self.choose = choose  # "best" | "random": the coin-flip baseline the backtest compares against
        self._rng = random.Random(seed)
        self.within = timedelta(minutes=cfg.within_minutes)
        self.state = "idle"
        self.message = "Waiting for the bot to start."
        self.requested_at: datetime | None = None
        self.anchor: datetime | None = None  # when the countdown started (the start, or entries opening)
        self.deadline: datetime | None = None
        self.last_entry: datetime | None = None  # end of the entry window the countdown belongs to
        self.preview: Choice | None = None  # what it would take now (refreshed each bar while watching)
        self.taken: dict[str, Any] | None = None  # the trade it placed (or the strategy trade that came first)
        self.records: list[dict[str, Any]] = []  # every first trade placed (the backtest reads these)
        self.outcomes: Counter[str] = Counter()  # requested / placed / strategy / missed (the backtest reports these)
        self.missed_because: Counter[str] = Counter()  # what blocked the sessions that passed without one
        self._why_blocked = ""
        self._open: dict[str, tuple[str, str, str]] = {}  # trade tag -> (slot, regime, basis) when opened
        self._last_try: datetime | None = None
        self._busy = False
        self.off_reason = self._off_reason()
        if self.off_reason:
            self.state, self.message = "off", self.off_reason

    def _off_reason(self) -> str | None:
        if self.core.cfg.account.stage == "express":
            return "Off on an Express Funded Account: the first trade after starting is for accounts that teach the bot."
        return None

    # ------------------------------------------------------------ persistence

    @property
    def _key(self) -> str:
        return f"first_trade:{self.core.account_label}"

    def _saved(self) -> dict[str, Any]:
        return (self.journal.get_state(self._key, {}) or {}) if self.journal else {}

    def _save(self, **changes: Any) -> None:
        if self.journal:
            self.journal.set_state(self._key, {**self._saved(), **changes})

    # ------------------------------------------------------------ starting

    def request(self, now: datetime, *, restart: bool = False) -> None:
        """The bot started at ``now``. ``restart``: it restarted by itself (only a trade still owed carries on)."""
        if self.off_reason:
            return
        if restart:
            pending = self._saved().get("pending")
            if not pending:
                self.state, self.message = "idle", ("Not this time: the bot restarted by itself. The first trade is "
                                                    "made after you start the bot.")
                return
            now = datetime.fromisoformat(pending)
        else:
            self._save(pending=now.isoformat())
        self.requested_at = now
        self.taken = None
        self.outcomes["requested"] += 1
        self._plan_window(now)

    def _plan_window(self, now: datetime) -> None:
        window = entry_window(self.core, now)
        if window is None:
            self.state, self.message = "waiting", "No trading day in the next two weeks (no_trade_dates / trade_weekdays)."
            self.anchor = self.deadline = self.last_entry = None
            return
        start, last = window
        self.anchor, self.last_entry = start, last
        self.deadline = max(start, min(start + self.within, last - timedelta(minutes=1)))
        self._last_try = None
        if start > self.core.schedule.local(now):
            self.state = "waiting"
            self.message = (f"Outside trading hours: waiting for the session. Entries open {_when(self.core, start, now)}; "
                            f"the first trade follows by {_when(self.core, self.deadline, now)}.")
        else:
            self._watching(now)

    def _watching(self, now: datetime) -> None:
        self.state = "watching"
        self.message = (f"Watching for a strategy signal. If none comes by {_when(self.core, self.deadline, now)}, the bot "
                        f"takes the best-supported setup at {self.cfg.contracts} contract"
                        f"{'s' if self.cfg.contracts != 1 else ''}.")

    # ------------------------------------------------------------ driving

    def _done(self, message: str, taken: dict[str, Any] | None = None) -> None:
        self.state, self.message = "done", message
        self.taken = taken
        self.preview = None
        self._save(pending=None)

    def _strategy_traded(self) -> ManagedTrade | None:
        """A strategy trade that is open now, or was opened since the start: the bot is already trading."""
        core, since = self.core, self.requested_at
        if since is None:
            return None
        t = core.orders.trade
        if t is not None and t.strategy not in NOT_STRATEGIES:
            return t
        for t in core.closed_trades[-5:]:
            if t.strategy not in NOT_STRATEGIES and t.created_at >= since:
                return t
        return None

    def _update(self, now: datetime) -> bool:
        """Advance the state; True when it is waiting for its moment (watching / blocked)."""
        if self.state not in ("waiting", "watching", "blocked"):
            return False
        t = self._strategy_traded()
        if t is not None:
            when = self.core.schedule.local(t.created_at).strftime("%H:%M")
            self.outcomes["strategy"] += 1
            self._done(f"The strategy traded first ({t.side.label} at {when} CT), so no extra trade was needed.",
                       {"by": "strategy", "side": t.side.label, "time": when, "strategy": t.strategy})
            return False
        if self.deadline is None or self.anchor is None:
            return False
        local = self.core.schedule.local(now)
        if self.state == "waiting" and local >= self.anchor:
            self._watching(now)
        elif self.state in ("watching", "blocked") and self.last_entry is not None and local >= self.last_entry:
            missed = self._why_blocked if self.state == "blocked" else ""
            self.outcomes["missed"] += 1
            self.missed_because[_reason_key(missed or "no bar closed in time")] += 1
            self._plan_window(now)
            if missed:
                self.message = f"No first trade that session: {missed}. {self.message}"
            return False
        return self.state in ("watching", "blocked")

    async def on_bar_closed(self, close_time: datetime) -> None:
        if not self._update(close_time):
            return
        price = self.core.last_price
        if price is not None and self.choose == "best" and self.core.setups.enabled:
            try:  # what it would take now, for the dashboard
                ranked = rank(self.core, price, close_time)
                self.preview = next((c for c in ranked if not c.excluded), None)
            except Exception:  # noqa: BLE001 - a dashboard preview must never disturb trading
                log.exception("First trade: preview failed")
        if close_time + self.core.tf > self.deadline:  # no later bar closes before the deadline
            await self._attempt(close_time)

    async def on_clock(self, now: datetime) -> None:
        """Safety net when no bar closes in time (long timeframes, a late bar)."""
        if not self._update(now) or now < self.deadline:
            return
        if self._last_try is not None and now - self._last_try < max(CLOCK_RETRY, self.core.tf):
            return
        await self._attempt(now)

    # ------------------------------------------------------------ placing it

    def _blocked(self, now: datetime, why: str) -> None:
        message = f"Due, but not allowed right now: {why}. Trying again on the next bar while entries are open."
        if message != self.message:
            log.info("First trade blocked: %s", why)
        self.state, self.message, self._why_blocked = "blocked", message, why

    async def _attempt(self, now: datetime) -> None:
        if self._busy:
            return
        self._busy = True
        self._last_try = now
        try:
            await self._try_enter(now)
        finally:
            self._busy = False

    async def _try_enter(self, now: datetime) -> None:
        core = self.core
        day = core.schedule.trading_day(now).isoformat()
        if self._saved().get("day") == day or any(r["day"] == day for r in self.records):
            self._done("A first trade was already made today; the next one is owed after a start on another day.")
            return
        blocked = core.setups.guard()
        if blocked:
            self._blocked(now, blocked)
            return
        price = core.last_price
        if price is None:
            self._blocked(now, "no price yet")
            return
        if self.choose == "random":
            side = OrderSide.BUY if self._rng.random() < 0.5 else OrderSide.SELL
            ranked = [Choice(side, "lean", evidence="coin flip (backtest baseline)")]
        else:
            ranked = [c for c in rank(core, price, now) if not c.excluded]
        problems = []
        for ch in ranked:
            stop, target = self._levels(ch, price)
            plan = self._plan(ch, price, stop, target)
            if isinstance(plan, str):
                problems.append(f"{ch.basis}: {plan}")
                continue
            await self._enter(ch, plan, price, now)
            return
        self._blocked(now, problems[0] if problems else "nothing to follow")

    def _levels(self, ch: Choice, price: float) -> tuple[float, float]:
        """A strategy signal keeps its own stop and target (when they still make sense at ``price``).

        A setup hasn't triggered, so its levels were drawn for a different entry: it and a lean get
        a stop of 1 ATR(14) and a 1.5R target, the trade ticket's defaults. That also keeps the
        first trades comparable with each other (and with the backtest's coin flip)."""
        from topstep_bot.manual import ATR_STOP_MULTIPLE, DEFAULT_TARGET_R, FALLBACK_STOP_TICKS

        c, sign, rcfg = self.core.contract, ch.side.sign, self.core.cfg.risk
        own = ch.kind == "signal"
        stop = ch.stop if own and ch.stop is not None and (price - ch.stop) * sign > 0 else None
        if stop is None or c.ticks(abs(price - stop)) > rcfg.max_stop_ticks:
            atr = self.core.atr.value
            ticks = c.ticks(ATR_STOP_MULTIPLE * atr) if atr else FALLBACK_STOP_TICKS
            ticks = min(max(math.ceil(ticks), rcfg.min_stop_ticks), rcfg.max_stop_ticks)
            stop = price - sign * c.price_offset(ticks)
        target = ch.target if own and ch.target is not None and (ch.target - price) * sign > 0 else None
        if target is None:
            target = price + sign * DEFAULT_TARGET_R * abs(price - stop)
        return stop, target

    def _plan(self, ch: Choice, price: float, stop: float, target: float) -> TradePlan | str:
        core = self.core
        action = "long" if ch.side == OrderSide.BUY else "short"
        plan = core.plan_entry(Signal(action, stop, target, f"{TITLE}: {ch.basis}"), price)
        if isinstance(plan, str) or plan.size <= self.cfg.contracts:
            return plan
        plan.size = self.cfg.contracts
        slip = core.contract.price_offset(core.cfg.execution.max_entry_slippage_ticks or 0)
        plan.planned_risk = plan.size * core.risk.risk_per_contract(price + ch.side.sign * slip, plan.stop)
        return plan

    async def _enter(self, ch: Choice, plan: TradePlan, price: float, now: datetime) -> None:
        core = self.core
        reason = f"{TITLE} after starting: {ch.describe()}"
        trade = await core.orders.enter(plan.side, plan.size, plan.stop, plan.target, reason, ref_price=price,
                                        limit_price=plan.limit, planned_risk=plan.planned_risk, strategy=FIRST_TRADE)
        if trade is None:
            self._blocked(now, "the order could not be placed (see the activity log)")
            return
        slot, regime = core.slot(now), core.regime.value
        trade.context = core.market_snapshot(price, now)
        self._open[trade.tag] = (slot, regime, ch.basis)
        day = core.schedule.trading_day(now).isoformat()
        when = core.schedule.local(now).strftime("%H:%M")
        taken = {"by": "first_trade", "side": plan.side.label, "time": when, "size": plan.size, "entry": price,
                 "stop": plan.stop, "target": plan.target, "risk_usd": round(plan.planned_risk, 2), "tag": trade.tag,
                 **ch.to_dict()}
        self.outcomes["placed"] += 1
        self.records.append({"day": day, "time": when, "slot": slot, "regime": regime, "tag": trade.tag,
                             "choice": ch.to_dict()})
        self._save(day=day)
        if core.recommender is not None:
            self._record_in_book(trade, plan, price, ch, slot, regime, now)
        self._done(f"First trade placed at {when} CT: {plan.side.label} {plan.size} {core.contract.name}, "
                   f"following {ch.describe()}.", taken)
        core.event("info", f"First trade after starting: {plan.side.label} {plan.size} {core.contract.name} @ ~{price}, "
                           f"stop {plan.stop}, target {plan.target} (risk ${plan.planned_risk:,.0f}). "
                           f"Why: {ch.describe()}")

    def _record_in_book(self, trade: ManagedTrade, plan: TradePlan, price: float, ch: Choice, slot: str,
                        regime: str, now: datetime) -> None:
        """List it with the ideas, so its result shows on the dashboard's scoreboard."""
        from topstep_bot.recommendations import Recommendation

        book = self.core.recommender
        rec = Recommendation(
            id=f"R{next(book._ids)}", created=now, strategy=FIRST_TRADE, title=TITLE, active=False, side=plan.side,
            entry=price, stop=plan.stop, target=plan.target, size=plan.size, risk_usd=plan.planned_risk,
            reason=ch.describe(), status="taken", trade_tag=trade.tag, hypothetical=False, slot=slot, regime=regime,
            context=dict(trade.context),
        )
        book._store(rec, new=True)

    # ------------------------------------------------------------ learning

    def trade_closed(self, t: ManagedTrade) -> None:
        """File the finished first trade in the knowledge base (strategy "first_trade", source "real")."""
        slot, regime, basis = self._open.pop(t.tag, (None, None, None))
        for rec in self.records:
            if rec["tag"] == t.tag:
                rec.update(r=t.r_multiple(), usd=round(t.net_pnl, 2), why=t.exit_reason)
        kb = self.core.knowledge
        r = t.r_multiple()
        if kb is None or r is None or any(k in t.exit_reason for k in UNINFORMATIVE_EXITS):
            return
        opened = t.opened_at or t.created_at
        core = self.core
        kb.record(Observation(
            day=core.schedule.trading_day(opened).isoformat(), time=core.schedule.local(opened).strftime("%H:%M"),
            strategy=FIRST_TRADE, side=t.side.label, slot=slot or core.slot(opened), regime=regime or core.regime.value,
            r=round(r, 2), usd=round(t.net_pnl, 2), source="real", why=t.exit_reason, basis=basis,
            **core.trade_facts(t),
        ))

    # ------------------------------------------------------------ dashboard

    def view(self) -> dict[str, Any]:
        now = self.core.clock()
        out: dict[str, Any] = {"state": self.state, "message": self.message, "within": self.cfg.within_minutes,
                               "contracts": self.cfg.contracts, "taken": self.taken,
                               "preview": self.preview.to_dict() if self.preview and self.state in ("watching", "blocked") else None}
        if self.deadline is not None and self.state in ("waiting", "watching", "blocked"):
            out["deadline"] = self.deadline.astimezone(UTC).isoformat()
            out["deadline_local"] = _when(self.core, self.deadline, now)
            out["seconds_left"] = max(0, round((self.deadline - now).total_seconds()))
            if self.anchor is not None:
                out["opens_local"] = _when(self.core, self.anchor, now)
        return out
