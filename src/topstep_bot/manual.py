"""Manual trades from the dashboard: a trade ticket with suggestions, and the order itself.

Before you trade, the ticket answers three questions:

1. What would this trade be?   Stop, target, size and dollars at risk, worked out by the same
                               sizing code as the bot's own trades (``TradingCore.plan_entry``).
2. Is it allowed right now?    Every risk rule and Topstep guard the bot obeys itself: loss limits,
                               the Maximum Loss Limit buffer, the Combine guards, the session window,
                               news, cooldowns, one position at a time and the order guard's caps.
3. What does the bot know?     Which strategies have worked at this time of day in this regime, how
                               longs and shorts have done here, live ideas that agree or disagree,
                               and your own record with manual trades.

A manual trade is placed and managed exactly like a bot trade: protective stop, target, the
breakeven/trailing settings, risk flattening and the session flatten. The auto-traded strategy
never exits it. When it closes, its result is added to the knowledge base as a "manual"
observation (time of day, regime, result in R), so your own trades are part of what the bot
knows. They never switch a strategy on or off.
"""

from __future__ import annotations

import math
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from topstep_bot.knowledge import MANUAL, UNINFORMATIVE_EXITS, Observation
from topstep_bot.models import OrderSide, Signal

if TYPE_CHECKING:
    from topstep_bot.engine import TradePlan, TradingCore
    from topstep_bot.execution import ManagedTrade
    from topstep_bot.recommendations import Recommendation

DEFAULT_TARGET_R = 1.5  # suggested target when nothing better is known
ATR_STOP_MULTIPLE = 1.0  # suggested stop distance when no live idea fits
FALLBACK_STOP_TICKS = 16  # ...and when the ATR isn't ready yet
NEWS_LOOKAHEAD_MINUTES = 60
MIN_MANUAL_SAMPLES = 5  # manual trades needed before the ticket comments on your record


class TicketError(ValueError):
    """Bad input or a trade that isn't allowed; the message is shown to you as is."""


def parse_side(raw: Any) -> OrderSide:
    text = str(raw or "").strip().lower()
    if text in ("long", "buy", "b"):
        return OrderSide.BUY
    if text in ("short", "sell", "s"):
        return OrderSide.SELL
    raise TicketError("Choose Buy (long) or Sell (short)")


def parse_price(raw: Any, what: str) -> float | None:
    if raw is None or str(raw).strip() == "":
        return None
    try:
        value = float(str(raw).replace(",", "").replace("$", ""))
    except ValueError:
        raise TicketError(f"{what} must be a price") from None
    if not math.isfinite(value) or value <= 0:
        raise TicketError(f"{what} must be a positive price")
    return value


NO_TARGET = ("none", "off", "no", "-")


def wants_no_target(raw: Any) -> bool:
    """True when you asked for no profit target (the trade then exits by stop, by you, or at the session end)."""
    return str(raw or "").strip().lower() in NO_TARGET


def parse_size(raw: Any) -> int | None:
    if raw is None or str(raw).strip() == "":
        return None
    try:
        value = float(str(raw))
    except ValueError:
        raise TicketError("Size must be a whole number of contracts") from None
    if not math.isfinite(value) or value < 1 or value != int(value):
        raise TicketError("Size must be a whole number of contracts (at least 1)")
    return int(value)


def _money(v: float) -> str:
    return f"-${abs(v):,.0f}" if v < 0 else f"${v:,.0f}"


class ManualTrading:
    def __init__(self, core: TradingCore):
        self.core = core
        # trade tag -> (slot, regime) when it was opened, so the knowledge base files it correctly
        self._opened: dict[str, tuple[str, str]] = {}

    # ------------------------------------------------------------ helpers

    def _today(self):
        return self.core.schedule.trading_day(self.core.clock())

    def _price(self) -> float:
        if self.core.last_price is None:
            raise TicketError("No live price yet - wait a moment for the price feed")
        return self.core.last_price

    def _verdict(self, strategy: str):
        kb = self.core.knowledge
        if kb is None:
            return None
        return kb.verdict(strategy, self.core.slot(), self.core.regime.value, today=self._today())

    def fresh_ideas(self) -> list[Recommendation]:
        """Recommendations that can still be acted on (the same 15 minutes as taking an idea)."""
        from topstep_bot.remote import IDEA_MAX_AGE

        book = self.core.recommender
        if book is None:
            return []
        now = self.core.clock()
        return [r for r in book.items if r.is_open and r.strategy != MANUAL and r.status in ("idea", "tracking", "skipped")
                and now - r.created <= IDEA_MAX_AGE]

    def suggest(self, side: OrderSide, price: float) -> tuple[float, float, str]:
        """(stop, target, where it comes from): the best-rated live idea on this side, else an ATR stop."""
        c, sign = self.core.contract, side.sign
        best: tuple[tuple, Recommendation] | None = None
        for rec in self.fresh_ideas():
            if rec.side != side or rec.stop is None or (price - rec.stop) * sign <= 0:
                continue
            v = self._verdict(rec.strategy)
            key = (bool(v and v.allowed), v.score if v else 0.0, rec.created)
            if best is None or key > best[0]:
                best = (key, rec)
        if best is not None:
            rec = best[1]
            stop = rec.stop
            target = rec.target if rec.target is not None and (rec.target - price) * sign > 0 else None
            if target is None:
                target = price + sign * DEFAULT_TARGET_R * abs(price - stop)
            when = self.core.schedule.local(rec.created).strftime("%H:%M")
            return stop, c.round_price(target), f"the {rec.title} idea from {when}"
        rcfg = self.core.cfg.risk
        atr = self.core.atr.value
        ticks = c.ticks(ATR_STOP_MULTIPLE * atr) if atr else FALLBACK_STOP_TICKS
        ticks = min(max(math.ceil(ticks), rcfg.min_stop_ticks), rcfg.max_stop_ticks)
        dist = c.price_offset(ticks)
        stop = c.round_price(price - sign * dist, "down" if side == OrderSide.BUY else "up")
        target = c.round_price(price + sign * DEFAULT_TARGET_R * dist)
        basis = f"{ATR_STOP_MULTIPLE:g} x ATR(14) ({ticks} ticks)" if atr else f"a {ticks}-tick default (ATR not ready yet)"
        return stop, target, basis

    def block_reason(self) -> str | None:
        """Why no manual trade may be opened right now (None = allowed)."""
        core = self.core
        if core.halted:
            return f"the bot is halted ({core.halted}) - restart it to trade again"
        if not core.orders.is_flat:
            return "a trade is already open - one position at a time"
        guard = core.orders.guard
        if guard is not None and guard.tripped:
            return f"the order guard stopped trading: {guard.tripped}"
        reason = core.risk.entry_block_reason(core.clock(), core.balance, core.orders.open_pnl(), manual=True)
        return f"not allowed right now: {reason}" if reason else None

    def _plan(self, side: OrderSide, price: float, stop: float, target: float | None, size: int | None,
              reason: str) -> TradePlan | str:
        core = self.core
        plan = core.plan_entry(Signal("long" if side == OrderSide.BUY else "short", stop, target, reason), price)
        if isinstance(plan, str) or size is None or size >= plan.size:
            return plan
        plan.size = size
        slip = core.contract.price_offset(core.cfg.execution.max_entry_slippage_ticks or 0)
        plan.planned_risk = size * core.risk.risk_per_contract(price + side.sign * slip, plan.stop)
        return plan

    # ------------------------------------------------------------ the ticket

    def ticket(self, side: Any, stop: Any = None, target: Any = None, size: Any = None) -> dict[str, Any]:
        """Everything the dashboard's trade ticket shows. Never places an order."""
        core, c = self.core, self.core.contract
        side = parse_side(side)
        price = self._price()
        no_target = wants_no_target(target)
        stop_in, size_in = parse_price(stop, "Stop"), parse_size(size)
        target_in = None if no_target else parse_price(target, "Target")
        s_stop, s_target, basis = self.suggest(side, price)
        stop = stop_in if stop_in is not None else s_stop
        if no_target:
            target = None
        elif target_in is not None:
            target = target_in
        elif stop_in is None:
            target = s_target
        else:  # your own stop: keep the suggested reward-to-risk
            target = c.round_price(price + side.sign * DEFAULT_TARGET_R * abs(price - stop))

        blocked = self.block_reason()
        plan = self._plan(side, price, stop, target, size_in, "ticket preview")
        problem = plan if isinstance(plan, str) else None
        out: dict[str, Any] = {
            "side": side.label, "price": price, "contract": c.name, "mode": core.cfg.mode,
            "tick_size": c.tick_size, "tick_value": c.tick_value, "point_value": c.point_value,
            "decimals": c.price_decimals,
            "suggested": {"stop": s_stop, "target": s_target, "basis": basis, "target_r": DEFAULT_TARGET_R},
            "blocked": blocked, "problem": problem, "ok": blocked is None and problem is None,
            "checks": self._checks(side, price, None if problem else plan),
            "knowledge": self._knowledge(side),
        }
        if problem is None:
            assert not isinstance(plan, str)
            max_plan = self._plan(side, price, stop, target, None, "ticket preview")
            risk_pts = abs(plan.entry_ref - plan.stop)
            reward = (abs(plan.target - plan.entry_ref) * c.point_value * plan.size - core.orders.fees_round_turn * plan.size
                      if plan.target is not None else None)
            notes = []
            if stop_in is not None and abs(plan.stop - c.round_price(stop_in, "down" if side == OrderSide.BUY else "up")) > 1e-9:
                notes.append(f"stop widened to {plan.stop} (the minimum stop distance)")
            if target_in is not None and plan.target is None:
                notes.append("target dropped: it is on the wrong side of the price")
            if size_in is not None and size_in > plan.size:
                notes.append(f"size cut to {plan.size}: your risk rules allow no more")
            out["plan"] = {
                "size": plan.size, "max_size": max_plan.size if not isinstance(max_plan, str) else plan.size,
                "stop": plan.stop, "target": plan.target, "limit": plan.limit,
                "stop_ticks": round(c.ticks(risk_pts)), "risk_usd": round(plan.planned_risk, 2),
                "reward_usd": None if reward is None else round(reward, 2),
                "rr": round(abs(plan.target - plan.entry_ref) / risk_pts, 2) if plan.target is not None and risk_pts else None,
                "notes": notes,
            }
        return out

    def _checks(self, side: OrderSide, price: float, plan: TradePlan | None) -> list[dict[str, Any]]:
        """Rule checks shown on the ticket: ok True (fine), None (heads-up) or False (blocks the trade)."""
        core = self.core
        risk, now = core.risk, core.clock()
        open_pnl = core.orders.open_pnl()
        day_pnl = risk.day_pnl(core.balance, open_pnl)
        checks: list[dict[str, Any]] = []

        def add(ok: bool | None, text: str) -> None:
            checks.append({"ok": ok, "text": text})

        session = core.schedule.entry_block_reason(now)
        add(session is None, "Inside your trading window" if session is None else f"Trading window: {session}")
        add(core.orders.is_flat, "No other trade open" if core.orders.is_flat else "A trade is already open (one at a time)")

        dll_room = risk.cfg.personal_daily_loss_limit + day_pnl
        risk_usd = plan.planned_risk if plan else 0.0
        add(dll_room > risk_usd if plan else dll_room > 0,
            f"Daily loss room: {_money(dll_room)} more can be lost today (limit {_money(risk.cfg.personal_daily_loss_limit)})"
            + (f" - this trade risks {_money(risk_usd)}" if plan else ""))
        mll_room = core.tracker.room(core.balance + open_pnl) - risk.cfg.mll_buffer
        add(mll_room > risk_usd, f"Room above the Maximum Loss Limit (after the {_money(risk.cfg.mll_buffer)} buffer): {_money(mll_room)}")
        if risk.topstep_dll:
            add(day_pnl > -0.9 * risk.topstep_dll, f"Topstep daily loss limit {_money(risk.topstep_dll)}: today {_money(day_pnl)}")

        if risk.stage == "combine" and risk.cfg.consistency_guard:
            from topstep_bot.risk.manager import CONSISTENCY_GUARD_FRACTION

            line = CONSISTENCY_GUARD_FRACTION * risk.plan.profit_target
            room = line - day_pnl
            reward = None
            if plan and plan.target is not None:
                reward = abs(plan.target - plan.entry_ref) * core.contract.point_value * plan.size
            if reward is not None and reward > room:
                add(None, f"Consistency guard: the bot closes trades once today reaches {_money(line)} - "
                          f"{_money(room)} left, this target would make {_money(reward)}")
            else:
                add(True, f"Consistency guard: {_money(room)} of profit left today before trades are closed")

        cap = risk.max_contracts(now)
        topstep_cap = risk.max_contracts_topstep()
        news_cap = risk.news_size_cap(now)
        size_text = f"Position limit: {cap} contract{'s' if cap != 1 else ''}"
        if news_cap is not None and news_cap < topstep_cap:
            size_text += f" (half of Topstep's {topstep_cap} near scheduled news)"
        elif cap < topstep_cap:
            size_text += f" (your cap; Topstep allows {topstep_cap})"
        add(True, size_text)

        if risk.trades_today >= risk.cfg.max_trades_per_day:
            add(None, f"{risk.trades_today} trades today (your limit is {risk.cfg.max_trades_per_day}) - manual trades "
                      "may go past it; every loss limit still applies")
        if risk.consecutive_losses:
            add(None, f"{risk.consecutive_losses} loss{'es' if risk.consecutive_losses != 1 else ''} in a row today "
                      f"(done for the day at {risk.cfg.max_consecutive_losses})")
        if risk.paused:
            add(None, "Automatic entries are paused - manual trades are still allowed")

        news = core.schedule.news
        if news is not None:
            soon = [e for e in news.upcoming(now, hours=2) if now <= e.time <= now + timedelta(minutes=NEWS_LOOKAHEAD_MINUTES)]
            if soon:
                e = soon[0]
                mins = int((e.time - now).total_seconds() // 60)
                does = ("the bot pauses entries around it" + (" and closes trades before it" if core.cfg.news.flatten_before else "")
                        if core.cfg.news.enabled else "news pauses are off")
                add(None, f"News in {mins} min: {e.label} - {does}")
        guard = core.orders.guard
        if guard is not None and guard.tripped:
            add(False, f"Order guard tripped: {guard.tripped}")
        return checks

    def _knowledge(self, side: OrderSide) -> dict[str, Any]:
        """What the bot knows that bears on this trade, plus a plain-language reading of it."""
        core = self.core
        kb = core.knowledge
        slot, regime = core.slot(), core.regime.value
        out: dict[str, Any] = {"slot": slot, "regime": regime, "enabled": kb is not None, "strategies": [],
                               "sides": None, "manual": None, "ideas": [], "reasons": []}
        reasons: list[tuple[int, str]] = []  # (weight, text): positive supports this side

        if kb is not None and slot != "off":
            from topstep_bot.strategies import BASE_STRATEGIES, STRATEGIES

            today = self._today()
            rows = []
            for name in BASE_STRATEGIES:
                v = kb.verdict(name, slot, regime, today)
                rows.append({"name": name, "title": STRATEGIES[name].title, "allowed": v.allowed, "level": v.level,
                             "mean_r": round(v.stats.mean_r, 2), "n": v.stats.n, "why": v.why, "score": round(v.score, 3)})
            rows.sort(key=lambda r: (r["allowed"], r["score"]), reverse=True)
            out["strategies"] = rows

            mine, other = kb.side_stats(side.label, slot, regime, today), kb.side_stats(side.opposite.label, slot, regime, today)
            out["sides"] = {side.label: mine.to_dict(kb.min_samples), side.opposite.label: other.to_dict(kb.min_samples)}
            if mine.n_eff >= kb.min_samples:
                if mine.score(kb.min_samples) >= kb.min_edge_r and mine.mean_r > other.mean_r:
                    reasons.append((1, f"{side.label.title()} signals have averaged {mine.mean_r:+.2f}R over {mine.n} at "
                                       f"{slot}/{regime} ({side.opposite.label.lower()}s {other.mean_r:+.2f}R)"))
                elif mine.mean_r < 0:
                    reasons.append((-1, f"{side.label.title()} signals have averaged {mine.mean_r:+.2f}R over {mine.n} at "
                                        f"{slot}/{regime}"))

            manual_all = kb.stats(MANUAL, None, None, today)
            manual_here = kb.stats(MANUAL, slot, regime, today)
            out["manual"] = {"overall": manual_all.to_dict(kb.min_samples), "here": manual_here.to_dict(kb.min_samples)}
            if manual_here.n >= MIN_MANUAL_SAMPLES:
                if manual_here.mean_r < 0:
                    reasons.append((-1, f"Your manual trades at {slot}/{regime} have averaged {manual_here.mean_r:+.2f}R "
                                        f"over {manual_here.n}"))
                elif manual_here.mean_r > 0:
                    reasons.append((1, f"Your manual trades at {slot}/{regime} have averaged {manual_here.mean_r:+.2f}R "
                                       f"over {manual_here.n}"))
        elif slot == "off":
            reasons.append((0, "Outside regular hours - the knowledge base only covers 08:30-15:10 CT"))

        now = core.clock()
        for rec in self.fresh_ideas():
            v = self._verdict(rec.strategy)
            agrees = rec.side == side
            working = bool(v and v.allowed)
            out["ideas"].append({
                "id": rec.id, "title": rec.title, "side": rec.side.label, "agrees": agrees, "working": working,
                "age_min": int((now - rec.created).total_seconds() // 60), "stop": rec.stop, "target": rec.target,
                "why": v.why if v else "", "reason": rec.reason,
            })
            if working:
                reasons.append((2 if agrees else -2,
                                f"{rec.title}, which has been working at this time, has a live {rec.side.label} idea"))
            else:
                state = "hasn't been working" if v and v.level != "unproven" else "is unproven"
                reasons.append((1 if agrees else -1,
                                f"{rec.title} has a live {rec.side.label} idea, but it {state} at this time"))

        score = sum(w for w, _ in reasons)
        if not any(w for w, _ in reasons):
            label, tone = "No clear evidence", "neutral"
        elif score >= 2:
            label, tone = "Supported", "good"
        elif score > 0:
            label, tone = "Some support", "good"
        elif score == 0:
            label, tone = "Mixed evidence", "neutral"
        else:
            label, tone = "Evidence against", "bad"
        out["verdict"] = {"label": label, "tone": tone, "score": score}
        out["reasons"] = [{"weight": w, "text": t} for w, t in sorted(reasons, key=lambda x: -abs(x[0]))]
        return out

    # ------------------------------------------------------------ trading

    async def open(self, side_raw: Any, stop_raw: Any, target_raw: Any, size_raw: Any, source: str, note: str = "") -> str:
        """Place a manual trade after re-checking every rule at the current price."""
        core, c = self.core, self.core.contract
        side = parse_side(side_raw)
        stop = parse_price(stop_raw, "Stop")
        if stop is None:
            raise TicketError("A manual trade needs a protective stop")
        target = None if wants_no_target(target_raw) else parse_price(target_raw, "Target")
        size = parse_size(size_raw)
        note = " ".join(str(note or "").split())[:200]
        blocked = self.block_reason()
        if blocked:
            raise TicketError(blocked[0].upper() + blocked[1:])
        price = self._price()
        why = f"manual from {source}" + (f": {note}" if note else "")
        plan = self._plan(side, price, stop, target, size, why)
        if isinstance(plan, str):
            raise TicketError(f"Can't place it at {price}: {plan}")
        trade = await core.orders.enter(side, plan.size, plan.stop, plan.target, why, ref_price=price,
                                        limit_price=plan.limit, planned_risk=plan.planned_risk, strategy=MANUAL)
        if trade is None:
            raise TicketError("The order could not be placed - see the activity log")
        slot, regime = core.slot(), core.regime.value
        trade.context = core.market_snapshot(price)
        self._opened[trade.tag] = (slot, regime)
        if core.recommender is not None:  # list it with the ideas so its result shows on the scoreboard
            self._record_in_book(trade, plan, price, note, slot, regime)
        msg = (f"Manual {side.label} {plan.size} {c.name} @ ~{price}, stop {plan.stop}"
               + (f", target {plan.target}" if plan.target is not None else "") + f" (risk ${plan.planned_risk:,.0f})")
        core.event("warning", f"{msg} - placed from {source}" + (f" ({note})" if note else ""), "entry")
        return msg

    def _record_in_book(self, trade: ManagedTrade, plan: TradePlan, price: float, note: str, slot: str, regime: str) -> None:
        from topstep_bot.recommendations import Recommendation

        book = self.core.recommender
        rec = Recommendation(
            id=f"R{next(book._ids)}", created=self.core.clock(), strategy=MANUAL, title="Manual", active=False,
            side=plan.side, entry=price, stop=plan.stop, target=plan.target, size=plan.size, risk_usd=plan.planned_risk,
            reason=note or "your own trade", status="taken", trade_tag=trade.tag, hypothetical=False, slot=slot,
            regime=regime, context=dict(trade.context),
        )
        book._store(rec, new=True)

    async def close(self, source: str) -> str:
        """Close the open trade now, without halting the bot (unlike Flatten & halt)."""
        orders = self.core.orders
        if orders.is_flat:
            return "No open trade to close."
        t = orders.trade
        what = f"{t.side.label} {t.filled_size or t.size}" if t else f"{orders.position}-contract position"
        await orders.exit(f"closed from {source}")
        self.core.event("warning", f"Close requested from {source} ({what})")
        return f"Closing the {what} trade at the market. The bot keeps running."

    async def stop_to_breakeven(self, source: str) -> str:
        """Move the open trade's stop to its entry price (only once price is beyond it)."""
        core = self.core
        t, c = core.orders.trade, core.contract
        if t is None or t.entry_price is None or t.stop_order_id is None:
            raise TicketError("There is no open, filled trade with a stop to move")
        long = t.side == OrderSide.BUY
        entry = c.round_price(t.entry_price, "up" if long else "down")
        if (long and t.stop_price >= entry) or (not long and t.stop_price <= entry):
            return f"The stop ({t.stop_price}) is already at or past breakeven."
        price = core.last_price
        gap = c.price_offset(2)
        if price is None or (long and price < entry + gap) or (not long and price > entry - gap):
            raise TicketError(f"Price must be at least 2 ticks {'above' if long else 'below'} the entry ({entry}) "
                              "before the stop can move to breakeven")
        if not await core.orders.update_stop(entry):
            raise TicketError("The stop could not be moved - see the activity log")
        core.event("warning", f"Stop moved to breakeven ({entry}) from {source}")
        return f"Stop moved to breakeven at {entry}."

    # ------------------------------------------------------------ learning

    def trade_closed(self, t: ManagedTrade) -> None:
        """A manual trade finished: add it to the knowledge base, tagged manual."""
        slot, regime = self._opened.pop(t.tag, (None, None))
        kb = self.core.knowledge
        r = t.r_multiple()
        if kb is None or r is None or any(k in t.exit_reason for k in UNINFORMATIVE_EXITS):
            return
        opened = t.opened_at or t.created_at
        local = self.core.schedule.local(opened)
        kb.record(Observation(
            day=self.core.schedule.trading_day(opened).isoformat(), time=local.strftime("%H:%M"), strategy=MANUAL,
            side=t.side.label, slot=slot or self.core.slot(opened), regime=regime or self.core.regime.value,
            r=round(r, 2), usd=round(t.net_pnl, 2), source="manual", why=t.exit_reason, **self.core.trade_facts(t),
        ))
