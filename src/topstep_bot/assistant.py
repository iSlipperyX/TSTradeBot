"""The AI trading assistant: a Claude model that watches the market with the bot and, if you let it, acts.

Two switches per account, both off until you turn them on (Setup tab, the Overview's AI assistant
panel, or Telegram /assistant):

* **Watch and manage open trades** (``assistant.manage``). While a trade is open it is reviewed
  after a bar closes: right after it opens, every ``manage_every_minutes``, and sooner when it
  reaches +1R (+2R, ...), comes close to its stop or scheduled news is near. The assistant may
  hold, tighten the stop, move the stop to breakeven or close the trade. It can never widen a stop,
  add contracts, remove the protection or move the target. Trades you opened yourself are left
  alone unless ``include_manual`` is on.
* **Place its own trades** (``assistant.trade``). While the account is flat inside the entry window
  it looks for a trade when a strategy signals, when a setup is nearly ready, and otherwise every
  ``think_every_minutes``. A trade it asks for is sized and checked by exactly the code the bot's own
  trades go through (``TradingCore.plan_entry``, the risk manager's entry rules, the consistency
  ceiling, the order guard, the hedge guard), capped at ``max_contracts`` and ``max_trades_per_day``.

Every decision - including "wait" and "hold" - is saved in the journal with the situation it saw,
what it chose, how sure it was, why, and what the bot then did with it (``assistant_decisions``).
Its own trades are filed in the knowledge base under the strategy name "assistant", so they never
count as a strategy's evidence. When it changes how a strategy's trade ends (an early close, or a
stop it tightened that was then hit), the recommendation book keeps following the strategy's
original plan to its own end: the strategy's record stays honest, and the difference is the
assistant's management score.

It needs the Claude API key saved on the Setup tab (shared with the Ask answers and the AI coach)
and counts toward the same monthly budget, with a daily cap of its own. Without a key, with a
budget spent, or with Claude unreachable, it does nothing and says why: the bot trades exactly as
it would without it. Nothing here makes a profit more likely by itself, and no bot or AI can
promise a Combine pass.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Protocol

from topstep_bot.knowledge import ASSISTANT, MANUAL, NOT_STRATEGIES, UNINFORMATIVE_EXITS, Observation
from topstep_bot.models import OrderSide, Signal

if TYPE_CHECKING:
    from topstep_bot.config import AssistantConfig, BotConfig
    from topstep_bot.engine import TradePlan, TradingCore
    from topstep_bot.execution import ManagedTrade
    from topstep_bot.journal import Journal

log = logging.getLogger("topstep_bot.assistant")
UTC = timezone.utc

TITLE = "AI assistant"
EXIT_PREFIX = "AI assistant"  # every exit reason it causes starts with this
MANAGE_ACTIONS = ("hold", "tighten_stop", "breakeven", "close")
ENTRY_ACTIONS = ("wait", "enter_long", "enter_short")
READY_SETUP = 0.75  # share of a setup's conditions met for it to count as nearly ready
DEFAULT_TARGET_R = 1.5  # target when it asks for an entry without one
MIN_TARGET_R = 1.0  # its own target must be at least this far away, in R
MIN_GAP_TICKS = 2  # a tightened stop stays at least this far from the price...
MIN_GAP_ATR = 0.2  # ...and this share of an ATR
NEAR_STOP_R = -0.6  # open loss (in R) that counts as "close to the stop"
NEWS_SOON = timedelta(minutes=15)
STALE_AFTER = timedelta(minutes=2)  # a decision that took longer than this is not acted on
RETRY_AFTER = timedelta(minutes=5)  # after Claude couldn't be reached
BARS_SHOWN = 30
RECENT = 12  # decisions kept in memory for the dashboard
SCORES_KEPT = 200
STATES = ("off", "ready", "thinking", "waiting", "unavailable")


class AssistantUnavailable(Exception):
    """Claude can't be asked right now (no key, budget used, no connection, refused, ...). The message says why."""

    def __init__(self, message: str, cost: float = 0.0, retry: bool = True):
        super().__init__(message)
        self.cost = cost
        self.retry = retry  # False: nothing changes until you do something (save a key, raise the budget)


@dataclass
class Decision:
    action: str
    stop: float | None = None
    target: float | None = None
    confidence: float = 0.0
    reason: str = ""
    evidence: list[str] = field(default_factory=list)
    model: str = ""
    cost: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {"action": self.action, "stop": self.stop, "target": self.target, "confidence": round(self.confidence, 3),
                "reason": self.reason, "evidence": list(self.evidence), "model": self.model, "cost": round(self.cost, 5)}


class Decider(Protocol):
    async def decide(self, kind: str, situation: dict[str, Any]) -> Decision: ...


# ------------------------------------------------------------------------------------------------ Claude

SYSTEM = """You are the AI trading assistant inside an automated futures trading bot. The bot trades a Topstep \
account (a Combine or a funded account) for its owner, a retail trader, next to its own strategies. Each request \
gives you the situation now as JSON and asks one question, named by "kind":

- "manage": a trade is open. Choose hold, tighten_stop, breakeven or close.
- "entry": the account is flat. Choose enter_long or enter_short (with a stop and a target), or wait.

What the bot lets you do:
- Every decision is checked by the bot's own risk code before anything happens: the daily loss limits, the room \
above the Maximum Loss Limit, the Combine's consistency line, trade counts, news blackouts, the entry window and \
Topstep's position caps. Anything that breaks a rule is refused and recorded as refused.
- You can never widen a stop, add contracts, remove the stop or move the target of an open trade. A tightened stop \
must improve on the current stop and stay at least "min_stop_gap" away from the price on the protective side. \
Breakeven moves the stop to the entry price and needs the price to be past the entry.
- Your own trades are sized by the bot's risk rules and capped at "max_contracts". They need a protective stop on \
the right side of the price; the bot may widen a stop that is closer than its minimum distance and refuses one \
wider than its maximum ("stop_limits"). Give a target at least 1R away. Every trade is flattened by the session end \
(Topstep: flat by 15:10 CT for index futures).

How to decide:
- Use only the data in the situation; never invent prices, levels or news. Times are Chicago time (CT). R is a \
multiple of the money a trade risks (+1R = won what it risked). "net" values are after fees and slippage.
- The owner uses this account to teach the bot: every real trade is recorded and learned from. A losing trade still \
costs real room on the account, so take a trade only when the evidence gives it an edge: strategy records at this \
time of day and market regime, the probability calculator's after-cost estimates, live ideas and setups that are \
forming, the market picture and the news timing. Thin evidence (few trades) proves little. Waiting is a good answer.
- When managing, don't micromanage: closing early or tightening hard turns winners into small losers. Act when \
something has changed: news is close, the reason for the trade is gone, the market turned sharply against it, a \
large open profit is at risk, or the session is about to end.
- confidence: your honest probability, 0 to 1, that your choice turns out better than doing nothing (hold or wait). \
The bot only enters or closes early when it is at least "min_confidence".
- reason: one or two plain sentences the owner reads on a phone. evidence: up to 4 short facts from the situation \
that decided it. stop and target: prices in the market's ticks, or null when the action doesn't use them.
- Never promise profit. No bot or AI can guarantee a winning trade or a Combine pass."""


def decision_schema(kind: str) -> dict[str, Any]:
    nullable = {"anyOf": [{"type": "number"}, {"type": "null"}]}
    return {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": list(MANAGE_ACTIONS if kind == "manage" else ENTRY_ACTIONS)},
            "stop": nullable,
            "target": nullable,
            "confidence": {"type": "number"},
            "reason": {"type": "string"},
            "evidence": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["action", "stop", "target", "confidence", "reason", "evidence"],
        "additionalProperties": False,
    }


def parse_decision(kind: str, data: Any) -> Decision:
    """A Decision from the model's JSON. Raises ValueError for anything that doesn't fit."""
    if not isinstance(data, dict):
        raise ValueError("not an object")
    action = str(data.get("action") or "")
    if action not in (MANAGE_ACTIONS if kind == "manage" else ENTRY_ACTIONS):
        raise ValueError(f"unknown action '{action}'")

    def price(key: str) -> float | None:
        v = data.get(key)
        if v is None:
            return None
        v = float(v)
        if not math.isfinite(v) or v <= 0:
            raise ValueError(f"{key} is not a price")
        return v

    conf = float(data.get("confidence") or 0.0)
    if not math.isfinite(conf):
        raise ValueError("confidence is not a number")
    evidence = [" ".join(str(e).split())[:200] for e in (data.get("evidence") or [])][:4]
    return Decision(action, price("stop"), price("target"), min(1.0, max(0.0, conf)),
                    " ".join(str(data.get("reason") or "").split())[:400], evidence)


class ClaudeDecider:
    """Asks a Claude model, with the shared key and the shared spending log (claude.py)."""

    MAX_TOKENS = 12000

    def __init__(self, cfg: BotConfig):
        self.cfg = cfg
        self._state: dict[str, Any] = {}  # remembers when the account refused refusal fallbacks

    async def decide(self, kind: str, situation: dict[str, Any]) -> Decision:
        from topstep_bot import claude

        key = claude.current_key()
        if not key:
            raise AssistantUnavailable("no Claude API key is saved (dashboard > Setup tab)", retry=False)
        ledger = claude.ledger_for(self.cfg.data_path)
        try:
            ledger.check(self.cfg.coach.monthly_budget_usd)
            cl = claude.client(key, timeout=90.0, retries=1)
        except claude.ClaudeError as exc:  # e.g. this month's budget is used: checked again every few minutes
            raise AssistantUnavailable(str(exc)) from None
        import anthropic

        a = self.cfg.assistant
        request = {
            "model": a.model, "max_tokens": self.MAX_TOKENS, "system": SYSTEM,
            "messages": [{"role": "user", "content": json.dumps(situation, default=str, separators=(",", ":"))}],
            "output_config": {"effort": a.effort, "format": {"type": "json_schema", "schema": decision_schema(kind)}},
            "cache_control": {"type": "ephemeral"},
        }
        try:
            response = await claude.create(cl, request, a.model, self._state)
        except anthropic.APIError as exc:
            raise AssistantUnavailable(claude.explain(exc, a.model)) from exc
        finally:
            await cl.close()
        model = getattr(response, "model", None) or a.model
        try:
            cost = ledger.log(model, getattr(response, "usage", None), "assistant")
        except OSError:  # the spending log is busy: price it anyway, so the daily cap still counts it
            log.warning("Could not write the Claude spending log", exc_info=True)
            cost = claude.price(model, getattr(response, "usage", None))
        if response.stop_reason == "refusal":
            raise AssistantUnavailable("the AI declined to decide this one", cost)
        text = "".join(b.text for b in response.content if getattr(b, "type", "") == "text").strip()
        try:
            decision = parse_decision(kind, json.loads(text))
        except (ValueError, TypeError) as exc:
            raise AssistantUnavailable(f"the AI's answer could not be read ({exc})", cost) from None
        decision.model, decision.cost = model, cost
        return decision


# ------------------------------------------------------------------------------------------------ the account

class AssistantDesk:
    """What the assistant shares across an account's markets: its switches, today's spending and trades, its record."""

    def __init__(self, cfg: BotConfig, journal: Journal | None, label: str, decider: Decider | None = None):
        self.cfg = cfg
        self.acfg: AssistantConfig = cfg.assistant
        self.journal = journal
        self.label = label
        self.decider: Decider = decider or ClaudeDecider(cfg)
        self.manage = self.acfg.manage
        self.trade = self.acfg.trade
        self.lanes: list[Assistant] = []
        self.unavailable: str | None = None  # why it can't ask Claude right now
        self.retry_at: datetime | None = None
        self._said: str | None = None  # the last "unavailable" reason announced in the activity feed

    @property
    def on(self) -> bool:
        return self.manage or self.trade

    # ------------------------------------------------------------ switches

    def switch(self, manage: bool | None = None, trade: bool | None = None, source: str = "dashboard") -> str:
        """Turn either part on or off for every market of this account (takes effect at once)."""
        changes = []
        if manage is not None and bool(manage) != self.manage:
            self.manage = bool(manage)
            changes.append(f"watching and managing open trades {'ON' if self.manage else 'off'}")
        if trade is not None and bool(trade) != self.trade:
            self.trade = bool(trade)
            changes.append(f"placing its own trades {'ON' if self.trade else 'off'}")
        if not changes:
            return f"Nothing changed: {self.describe()}."
        text = f"AI assistant: {' and '.join(changes)} (from {source})."
        for lane in self.lanes:
            lane.switched()
        if self.lanes:
            self.lanes[0].core.event("warning", text, "risk")
        return text + ("" if self.on else " The bot trades as usual.")

    def describe(self) -> str:
        parts = [f"managing open trades {'on' if self.manage else 'off'}", f"own trades {'on' if self.trade else 'off'}"]
        return "AI assistant " + ", ".join(parts)

    # ------------------------------------------------------------ today's usage (kept in the journal)

    @property
    def _key(self) -> str:
        return f"assistant:{self.label}"

    def usage(self, day: str) -> dict[str, Any]:
        saved = (self.journal.get_state(self._key, {}) if self.journal else getattr(self, "_usage", {})) or {}
        if saved.get("day") != day:
            saved = {"day": day, "calls": 0, "cost": 0.0, "trades": 0}
        return saved

    def _save_usage(self, u: dict[str, Any]) -> None:
        if self.journal:
            self.journal.set_state(self._key, u)
        else:
            self._usage = u

    def spent(self, day: str, cost: float) -> None:
        u = self.usage(day)
        u["calls"] += 1
        u["cost"] = round(u["cost"] + cost, 5)
        self._save_usage(u)

    def traded(self, day: str) -> None:
        u = self.usage(day)
        u["trades"] += 1
        self._save_usage(u)

    # ------------------------------------------------------------ can it ask Claude?

    def blocked(self, day: str, now: datetime) -> str | None:
        """Why Claude won't be asked now (None = it may be). Checked before every call."""
        u = self.usage(day)
        if u["cost"] >= self.acfg.daily_budget_usd:
            return (f"today's AI assistant budget is used (${u['cost']:.2f} of ${self.acfg.daily_budget_usd:.2f}); it "
                    "starts again tomorrow")
        if self.unavailable and (self.retry_at is None or now < self.retry_at):
            return self.unavailable
        return None

    def failed(self, reason: str, now: datetime, retry: bool) -> None:
        self.unavailable = reason
        self.retry_at = now + RETRY_AFTER if retry else None
        if reason != self._said and self.lanes:
            self._said = reason
            self.lanes[0].core.event("warning", f"AI assistant unavailable: {reason}. The bot trades as usual meanwhile.",
                                     "error")

    def worked(self) -> None:
        if self.unavailable and self.lanes:
            self.lanes[0].core.event("info", "AI assistant is working again.")
        self.unavailable = self.retry_at = self._said = None

    def key_changed(self) -> None:
        """A key was saved or the budget raised: try again at once."""
        self.unavailable = self.retry_at = None

    # ------------------------------------------------------------ management record

    @property
    def _scores_key(self) -> str:
        return f"assistant_scores:{self.label}"

    def scores(self) -> list[dict[str, Any]]:
        return (self.journal.get_state(self._scores_key, []) if self.journal else getattr(self, "_scores", [])) or []

    def add_score(self, row: dict[str, Any]) -> None:
        rows = [*self.scores(), row][-SCORES_KEPT:]
        if self.journal:
            self.journal.set_state(self._scores_key, rows)
        else:
            self._scores = rows

    def score_summary(self) -> dict[str, Any]:
        rows = self.scores()
        diff = [r["real_r"] - r["plan_r"] for r in rows if r.get("real_r") is not None and r.get("plan_r") is not None]
        usd = [r["real_usd"] - r["plan_usd"] for r in rows if r.get("real_usd") is not None and r.get("plan_usd") is not None]
        return {"n": len(diff), "helped": sum(1 for d in diff if d > 0.05), "hurt": sum(1 for d in diff if d < -0.05),
                "r": round(sum(diff), 2), "usd": round(sum(usd), 2) if usd else None, "recent": rows[-5:][::-1]}


# ------------------------------------------------------------------------------------------------ one market

class Assistant:
    """The assistant on one market: when to think, what to show Claude, and what to do with its answer."""

    def __init__(self, core: TradingCore, desk: AssistantDesk):
        self.core = core
        self.desk = desk
        self.acfg = desk.acfg
        self.state = "off"
        self.message = ""
        self.thinking: str | None = None  # "entry" or "manage" while Claude is being asked
        self.thinking_since: datetime | None = None
        self.recent: deque[dict[str, Any]] = deque(maxlen=RECENT)
        self._task: asyncio.Task | None = None
        self._last_entry: datetime | None = None
        self._seen: set[str] = set()  # idea ids and setups it has already looked at
        self._reviews: dict[str, tuple[datetime, int, bool]] = {}  # trade tag -> (last review, R step, news seen)
        self._opened: dict[str, tuple[str, str, str, str]] = {}  # own trade tag -> (slot, regime, basis, decision id)
        self._records: dict[str, list[str]] = {}  # trade tag -> ids of decisions that acted on it
        desk.lanes.append(self)
        self._load_recent()
        self.switched()

    # ------------------------------------------------------------ helpers

    @property
    def symbol(self) -> str:
        return self.core.contract.name

    def _day(self, now: datetime | None = None) -> str:
        return self.core.schedule.trading_day(now or self.core.clock()).isoformat()

    def _local(self, ts: datetime) -> str:
        return self.core.schedule.local(ts).strftime("%H:%M")

    def switched(self) -> None:
        if not self.desk.on:
            self.state, self.message = "off", "Off. The bot trades by itself as usual."
        elif self.thinking is None:
            self.state, self.message = "ready", self._ready_text()

    def _ready_text(self) -> str:
        jobs = []
        if self.desk.manage:
            jobs.append("watching open trades")
        if self.desk.trade:
            jobs.append("looking for its own trades")
        return " and ".join(jobs).capitalize() + "."

    def _load_recent(self) -> None:
        j = self.core.journal
        if j is None:
            return
        try:
            rows = [r for r in j.assistant_decisions(limit=40, day=self._day()) if r.get("symbol") == self.symbol]
        except Exception:  # noqa: BLE001 - a dashboard list: start empty
            return
        for r in reversed(rows[:RECENT]):
            self.recent.appendleft(_brief(r))

    def manages(self, t: ManagedTrade | None) -> bool:
        """True if it may manage this trade (open, filled, with a stop; yours only with include_manual)."""
        from topstep_bot.execution import TradeState

        return (t is not None and t.state == TradeState.OPEN and t.entry_price is not None and t.stop_order_id is not None
                and t.risk_points > 0 and (t.strategy != MANUAL or self.acfg.include_manual))

    def _r_now(self, t: ManagedTrade, price: float) -> float:
        return (price - t.entry_price) * t.side.sign / t.risk_points if t.risk_points else 0.0

    @staticmethod
    def _step(r_now: float) -> int:
        """Which band the trade is in: +1, +2, ... once in profit by whole R; -1 when close to its stop."""
        if r_now >= 1:
            return int(r_now)
        return -1 if r_now <= NEAR_STOP_R else 0

    def _news_soon(self, now: datetime) -> str | None:
        news = self.core.schedule.news
        if news is None:
            return None
        for e in news.upcoming(now, hours=1):
            if now <= e.time <= now + NEWS_SOON:
                return f"{e.label} at {self._local(e.time)} CT"
        return None

    # ------------------------------------------------------------ when to think

    async def on_bar_closed(self, close_time: datetime) -> None:
        """Called by the trading core after each closed bar (live only). Starts thinking in the background."""
        if not self.desk.on or (self._task is not None and not self._task.done()):
            return
        try:
            job = self._due(close_time)
        except Exception:  # noqa: BLE001 - deciding whether to ask must never disturb trading
            log.exception("AI assistant: could not check what is due")
            return
        if job is None:
            return
        from topstep_bot.logging_setup import spawn

        self._task = spawn(self._think(*job), name=f"assistant {self.symbol}")

    def _due(self, now: datetime) -> tuple[str, str] | None:
        """("manage" | "entry", why) when Claude should be asked now, else None."""
        core = self.core
        price = core.last_price
        if price is None:
            return None
        t = core.orders.trade
        if t is not None:
            if not self.desk.manage or not self.manages(t):
                return None
            r_now = self._r_now(t, price)
            step, news = self._step(r_now), self._news_soon(now)
            last = self._reviews.get(t.tag)
            if last is None:
                return "manage", "the trade just opened"
            when, last_step, news_seen = last
            if news and not news_seen:
                return "manage", f"news soon ({news})"
            if step != 0 and step != last_step:
                return "manage", "it is close to its stop" if step < 0 else f"it reached +{step}R"
            if now - when >= timedelta(minutes=self.acfg.manage_every_minutes):
                return "manage", "regular review"
            return None
        if not self.desk.trade or not core.orders.is_flat:
            return None
        blocked = self._entry_block(now)
        if blocked:
            self.state, self.message = "waiting", f"Not looking for a trade: {blocked}."
            return None
        if self.state == "waiting":
            self.state, self.message = "ready", self._ready_text()
        fresh = [r for r in core.manual.fresh_ideas() if r.created == now and r.strategy not in NOT_STRATEGIES
                 and r.id not in self._seen]
        if fresh:
            return "entry", f"{fresh[0].title} signalled {fresh[0].side.label}"
        try:
            ready = [st for _, _, st in core.setups.collect(price, now) if st.progress >= READY_SETUP]
        except Exception:  # noqa: BLE001 - the setups are only a reason to look
            ready = []
        for st in ready:
            key = f"setup:{st.strategy}:{st.side}:{self._day(now)}"
            if key not in self._seen:
                self._seen.add(key)
                return "entry", f"a {st.strategy} {st.side} setup is nearly ready"
        if self._last_entry is None or now - self._last_entry >= timedelta(minutes=self.acfg.think_every_minutes):
            return "entry", "regular look"
        return None

    def _entry_block(self, now: datetime) -> str | None:
        core = self.core
        if core.closing:
            return core.closing
        reason = core.setups.guard()
        if reason:
            return reason
        u = self.desk.usage(self._day(now))
        if u["trades"] >= self.acfg.max_trades_per_day:
            return f"it made its {self.acfg.max_trades_per_day} trade(s) for today"
        return None

    # ------------------------------------------------------------ thinking

    async def _think(self, kind: str, why: str) -> None:
        core, desk = self.core, self.desk
        now = core.clock()
        day = self._day(now)
        t = core.orders.trade
        if (kind == "manage" and not self.manages(t)) or (kind == "entry" and not core.orders.is_flat):
            return  # the position changed since the bar closed
        tag = t.tag if kind == "manage" else None
        if kind == "manage":
            self._reviews[t.tag] = (now, self._step(self._r_now(t, core.last_price)), self._news_soon(now) is not None)
        else:
            self._last_entry = now
            self._seen.update(r.id for r in core.manual.fresh_ideas())
        blocked = desk.blocked(day, now)
        if blocked:
            self.state, self.message = "unavailable", f"Can't ask the AI: {blocked}. The bot trades as usual."
            return
        try:
            situation = self.situation(kind, why, now)
        except Exception:  # noqa: BLE001 - never trade on a half-built picture
            log.exception("AI assistant: could not build the situation")
            return
        self.thinking, self.thinking_since = kind, now
        self.state = "thinking"
        self.message = ("Reviewing the open trade" if kind == "manage" else "Looking for a trade") + f" ({why})."
        record: dict[str, Any] = {
            "id": f"{now:%Y%m%d-%H%M%S}-{kind}-{self.symbol}", "day": day, "created": now.isoformat(),
            "time": self._local(now), "kind": kind, "symbol": self.symbol, "why_asked": why, "trade": tag,
            "situation": situation,
        }
        try:
            decision = await desk.decider.decide(kind, situation)
        except AssistantUnavailable as exc:
            if exc.cost:
                desk.spent(day, exc.cost)
            desk.failed(str(exc), core.clock(), exc.retry)
            self._finish(record, None, "unavailable", f"Could not ask the AI: {exc}")
            return
        except Exception as exc:  # noqa: BLE001 - a bug here must never stop trading
            log.exception("AI assistant: the decision failed")
            desk.failed(f"an unexpected error ({type(exc).__name__}, see logs/errors.log)", core.clock(), True)
            self._finish(record, None, "failed", "The decision failed unexpectedly (see logs/errors.log).")
            return
        finally:
            self.thinking = self.thinking_since = None
        desk.spent(day, decision.cost)
        desk.worked()
        took = core.clock() - now
        if took > STALE_AFTER:
            self._finish(record, decision, "late", f"The answer took {int(took.total_seconds())}s, too long to act on.")
            return
        try:
            status, result = await (self._act_manage(decision, tag) if kind == "manage" else self._act_entry(decision, record))
        except Exception:  # noqa: BLE001
            log.exception("AI assistant: acting on the decision failed")
            status, result = "failed", "Acting on it failed (see logs/errors.log)."
        self._finish(record, decision, status, result)

    def _finish(self, record: dict[str, Any], decision: Decision | None, status: str, result: str) -> None:
        record.update({"decision": decision.to_dict() if decision else None, "action": decision.action if decision else None,
                       "status": status, "result": result})
        if decision is not None and status in ("done",) and record.get("trade"):
            self._records.setdefault(record["trade"], []).append(record["id"])
        self._save(record)
        self.recent.appendleft(_brief(record))
        if status == "unavailable":
            self.state, self.message = "unavailable", f"{result} The bot trades as usual."
        else:
            self.state = "ready" if self.desk.on else "off"
            self.message = self._ready_text() if self.desk.on else "Off. The bot trades by itself as usual."
        log.info("AI assistant (%s, %s): %s -> %s: %s", self.symbol, record["kind"], record.get("action"), status, result,
                 extra={"event": "assistant", "data": _brief(record)})

    def _save(self, record: dict[str, Any]) -> None:
        if self.core.journal is None:
            return
        try:
            self.core.journal.record_assistant(record)
        except Exception as exc:  # noqa: BLE001 - a record is an explanation: the trade doesn't depend on it
            log.warning("Could not save the AI assistant's decision: %s", exc)

    # ------------------------------------------------------------ what it sees

    def situation(self, kind: str, why: str, now: datetime) -> dict[str, Any]:
        """Everything Claude is shown for one decision (also saved with the decision record)."""
        from topstep_bot.market_context import describe

        core, c = self.core, self.core.contract
        price = core.last_price
        rnd = c.round_price
        risk = core.risk
        open_pnl = core.orders.open_pnl()
        atr = core.atr.value
        snap = core.market_snapshot(price, now)
        out: dict[str, Any] = {
            "kind": kind, "asked_because": why,
            "time": core.schedule.local(now).strftime("%Y-%m-%d %H:%M CT"),
            "market": {"symbol": c.root or c.name, "contract": c.name, "tick_size": c.tick_size, "tick_value": c.tick_value,
                       "point_value": c.point_value, "price": price, "bid": core.bid, "ask": core.ask,
                       "atr14": round(atr, 4) if atr else None, "slot": core.slot(now), "regime": core.regime.value,
                       "picture": describe(snap), "snapshot": snap},
            "bars": [[self._local(b.ts), rnd(b.open), rnd(b.high), rnd(b.low), rnd(b.close)]
                     for b in list(core.chart_bars)[-BARS_SHOWN:]],
            "account": {
                "stage": risk.stage, "plan": risk.plan.name, "balance": round(core.balance, 2),
                "day_pnl": round(risk.day_pnl(core.balance, open_pnl), 2), "trades_today": risk.trades_today,
                "max_trades": risk.max_trades_today, "losses_in_a_row": risk.consecutive_losses,
                "daily_loss_room": round(risk.cfg.personal_daily_loss_limit + risk.day_pnl(core.balance, open_pnl), 2),
                "mll_room": round(core.tracker.room(core.balance + open_pnl) - risk.cfg.mll_buffer, 2),
                "profit_room_today": _round(risk.reward_room(core.balance)),
                "risk_per_trade": round(risk.cfg.risk_per_trade * risk.risk_scale, 2),
            },
            "session": {"flatten_at": core.schedule.cfg.flatten_at.strftime("%H:%M"),
                        "last_entry": core.schedule.cfg.last_entry.strftime("%H:%M")},
            "news": self._news(now),
            "rules": {"min_confidence": self.acfg.min_confidence, "max_contracts": self.acfg.max_contracts,
                      "min_stop_gap": rnd(self._min_gap()),
                      "stop_limits": {"min_ticks": core.cfg.risk.min_stop_ticks, "max_ticks": core.cfg.risk.max_stop_ticks}},
            "evidence": self._evidence(now, price),
            "your_record": self._record(),
        }
        if kind == "manage":
            out["trade"] = self._trade_view(core.orders.trade, price, now)
        else:
            u = self.desk.usage(self._day(now))
            out["your_trades_today"] = {"made": u["trades"], "allowed": self.acfg.max_trades_per_day}
        return out

    def _min_gap(self) -> float:
        c, atr = self.core.contract, self.core.atr.value
        return max(c.price_offset(MIN_GAP_TICKS), MIN_GAP_ATR * atr if atr else 0.0)

    def _news(self, now: datetime) -> list[dict[str, Any]]:
        news = self.core.schedule.news
        if news is None:
            return []
        return [{"time": self._local(e.time), "event": e.label, "minutes_away": int((e.time - now).total_seconds() // 60)}
                for e in news.upcoming(now, hours=3)][:6]

    def _evidence(self, now: datetime, price: float | None) -> dict[str, Any]:
        """What the bot itself knows right now: strategy records here, odds both ways, live ideas, forming setups."""
        core = self.core
        out: dict[str, Any] = {}
        manual = core.manual
        try:
            k = manual._knowledge(OrderSide.BUY)
            out["strategies_here"] = [{key: row[key] for key in ("title", "allowed", "level", "mean_r", "n", "why")}
                                      for row in k.get("strategies", [])]
            out["sides_here"] = k.get("sides")
            out["your_manual_trades_here"] = (k.get("manual") or {}).get("here")
        except Exception:  # noqa: BLE001 - evidence is advice: show what is available
            log.debug("AI assistant: no knowledge view", exc_info=True)
        try:
            from topstep_bot.strategies import BASE_STRATEGIES, STRATEGIES

            out["odds"] = {side.label: manual._odds(side, BASE_STRATEGIES, STRATEGIES) for side in (OrderSide.BUY, OrderSide.SELL)}
        except Exception:  # noqa: BLE001
            log.debug("AI assistant: no odds", exc_info=True)
        ideas = []
        for rec in manual.fresh_ideas():
            v = manual._verdict(rec.strategy)
            ideas.append({"strategy": rec.title, "side": rec.side.label, "age_min": int((now - rec.created).total_seconds() // 60),
                          "stop": rec.stop, "target": rec.target, "working_here": bool(v and v.allowed),
                          "record": v.why if v else "", "reason": rec.reason})
        out["live_ideas"] = ideas[:6]
        if price is not None:
            try:
                setups = []
                for _, active, st in core.setups.collect(price, now):
                    setups.append({"strategy": st.strategy, "side": st.side, "progress": round(st.progress, 2),
                                   "entry": st.entry, "stop": st.stop, "target": st.target, "auto_traded": active,
                                   "conditions": [f"{'met' if ok else 'not met'}: {text}" for text, ok in st.conditions][:6]})
                out["setups_forming"] = sorted(setups, key=lambda s: -s["progress"])[:6]
            except Exception:  # noqa: BLE001
                log.debug("AI assistant: no setups", exc_info=True)
            try:
                from topstep_bot.first_trade import rank

                out["bot_ranking"] = [ch.to_dict() for ch in rank(core, price, now) if not ch.excluded][:4]
            except Exception:  # noqa: BLE001
                log.debug("AI assistant: no ranking", exc_info=True)
        return out

    def _record(self) -> dict[str, Any]:
        kb = self.core.knowledge
        out: dict[str, Any] = {"management": {k: v for k, v in self.desk.score_summary().items() if k != "recent"}}
        if kb is not None:
            out["own_trades"] = kb.stats(ASSISTANT, None, None, self.core.schedule.trading_day(self.core.clock())).to_dict(
                kb.min_samples)
        out["today"] = [{k: d.get(k) for k in ("time", "kind", "action", "status", "result")} for d in list(self.recent)[:6]]
        return out

    def _trade_view(self, t: ManagedTrade, price: float, now: datetime) -> dict[str, Any]:
        mfe, mae = t.excursion_r()
        return {
            "side": t.side.label, "size": t.filled_size, "entry": t.entry_price, "stop": t.stop_price,
            "initial_stop": t.initial_stop, "target": t.target_price, "r_now": round(self._r_now(t, price), 2),
            "open_pnl": round(self.core.orders.open_pnl(price), 2), "best_r": mfe, "worst_r": mae,
            "minutes_open": int((now - (t.opened_at or t.created_at)).total_seconds() // 60),
            "opened_by": {MANUAL: "the owner (manual)", ASSISTANT: "you (the AI assistant)"}.get(t.strategy, t.strategy),
            "why_opened": t.reason, "odds_at_entry": t.odds, "breakeven_possible": self._breakeven_problem(t, price) is None,
            "your_changes": list(t.assisted),
        }

    # ------------------------------------------------------------ acting: open trades

    def _breakeven_problem(self, t: ManagedTrade, price: float | None) -> str | None:
        c = self.core.contract
        long = t.side == OrderSide.BUY
        entry = c.round_price(t.entry_price, "up" if long else "down")
        if (long and t.stop_price >= entry) or (not long and t.stop_price <= entry):
            return "the stop is already at or past breakeven"
        gap = max(c.price_offset(MIN_GAP_TICKS), self._min_gap())
        if price is None or (long and price < entry + gap) or (not long and price > entry - gap):
            return "the price isn't far enough past the entry yet"
        return None

    async def _act_manage(self, d: Decision, tag: str | None) -> tuple[str, str]:
        core = self.core
        t = core.orders.trade
        if t is None or t.tag != tag or not self.manages(t):
            return "skipped", "The trade had already closed or changed."
        if not self.desk.manage:
            return "skipped", "Managing trades was switched off meanwhile."
        price = core.last_price
        c = core.contract
        long = t.side == OrderSide.BUY
        if d.action == "hold":
            return "done", "Held the trade as it is."
        if d.action == "close":
            if d.confidence < self.acfg.min_confidence:
                return "held", (f"Wanted to close early but was only {d.confidence:.0%} sure (needs "
                                f"{self.acfg.min_confidence:.0%}), so the trade stays as it is.")
            r_now = self._r_now(t, price)
            t.assisted.append("close")
            await core.orders.exit(f"{EXIT_PREFIX}: {d.reason}"[:200])
            core.event("warning", f"AI assistant closed the {t.side.label} {t.filled_size} {c.name} trade at about "
                                  f"{price} ({r_now:+.2f}R): {d.reason}", "risk")
            return "done", f"Closed the trade at about {price} ({r_now:+.2f}R)."
        if d.action == "breakeven":
            problem = self._breakeven_problem(t, price)
            if problem:
                return "refused", f"Breakeven refused: {problem}."
            stop = c.round_price(t.entry_price, "up" if long else "down")
        else:  # tighten_stop
            if d.stop is None:
                return "refused", "Tighten refused: no stop price given."
            stop = c.round_price(d.stop, "down" if long else "up")
            if (long and stop <= t.stop_price) or (not long and stop >= t.stop_price):
                return "refused", f"Tighten refused: {stop} would not improve the stop ({t.stop_price}); it is never widened."
            gap = self._min_gap()
            if (long and stop > price - gap) or (not long and stop < price + gap):
                return "refused", (f"Tighten refused: {stop} is too close to the price ({price}); it needs at least "
                                   f"{c.round_price(gap)} of room.")
        old = t.stop_price
        if not await core.orders.update_stop(stop):
            return "refused", "The stop could not be moved (see the activity log)."
        t.assisted.append(d.action)
        t.assistant_stop = t.stop_price
        what = "to breakeven" if d.action == "breakeven" else "tighter"
        core.event("warning", f"AI assistant moved the stop {what}: {old} -> {t.stop_price} on the {t.side.label} "
                              f"{c.name} trade. {d.reason}", "risk")
        return "done", f"Moved the stop {what}: {old} -> {t.stop_price}."

    # ------------------------------------------------------------ acting: its own trades

    async def _act_entry(self, d: Decision, record: dict[str, Any]) -> tuple[str, str]:
        core, c = self.core, self.core.contract
        if d.action == "wait":
            return "done", "Waited: no trade."
        if not self.desk.trade:
            return "skipped", "Its own trades were switched off meanwhile."
        if d.confidence < self.acfg.min_confidence:
            return "held", (f"Saw a {d.action.removeprefix('enter_')} trade but was only {d.confidence:.0%} sure (needs "
                            f"{self.acfg.min_confidence:.0%}), so it waited.")
        if not core.orders.is_flat:
            return "skipped", "Another trade opened meanwhile."
        now = core.clock()
        blocked = self._entry_block(now)
        if blocked:
            return "refused", f"Not allowed now: {blocked}."
        price = core.last_price
        side = OrderSide.BUY if d.action == "enter_long" else OrderSide.SELL
        sign = side.sign
        if d.stop is None or (price - d.stop) * sign <= 0:
            return "refused", f"Refused: the stop ({d.stop}) must be {'below' if sign > 0 else 'above'} the price ({price})."
        dist = abs(price - d.stop)
        target = d.target
        if target is None or (target - price) * sign <= 0:
            target = price + sign * DEFAULT_TARGET_R * dist
        elif abs(target - price) < MIN_TARGET_R * dist - 1e-9:
            return "refused", f"Refused: the target ({target}) is less than {MIN_TARGET_R:g}R away."
        plan = core.plan_entry(Signal("long" if sign > 0 else "short", d.stop, target, f"{TITLE}: {d.reason}"), price)
        if isinstance(plan, str):
            return "refused", f"Refused by the risk rules: {plan}."
        if plan.size > self.acfg.max_contracts:
            self._resize(plan, self.acfg.max_contracts, price)
        hedge = core.entry_guard(sign) if core.entry_guard else None
        if hedge:
            return "refused", f"Refused: {hedge}."
        reason = f"{TITLE}: {d.reason}"[:300]
        trade = await core.orders.enter(side, plan.size, plan.stop, plan.target, reason, ref_price=price,
                                        limit_price=plan.limit, planned_risk=plan.planned_risk, strategy=ASSISTANT)
        if trade is None:
            return "failed", "The order could not be placed (see the activity log)."
        slot, regime = core.slot(now), core.regime.value
        trade.context = core.market_snapshot(price, now)
        basis = "; ".join(d.evidence[:2])[:200] or d.reason[:200]
        self._opened[trade.tag] = (slot, regime, basis, record["id"])
        record["trade"] = trade.tag
        self.desk.traded(self._day(now))
        if core.recommender is not None:
            self._record_in_book(trade, plan, price, d, slot, regime, now)
        msg = (f"{plan.side.label} {plan.size} {c.name} @ ~{price}, stop {plan.stop}"
               + (f", target {plan.target}" if plan.target is not None else "") + f" (risk ${plan.planned_risk:,.0f})")
        core.event("warning", f"AI assistant trade: {msg}. Why ({d.confidence:.0%} sure): {d.reason}", "entry")
        return "done", f"Placed {msg}."

    def _resize(self, plan: TradePlan, size: int, price: float) -> None:
        core = self.core
        plan.size = size
        slip = core.contract.price_offset(core.cfg.execution.max_entry_slippage_ticks or 0)
        plan.planned_risk = size * core.risk.risk_per_contract(price + plan.side.sign * slip, plan.stop)

    def _record_in_book(self, trade: ManagedTrade, plan: TradePlan, price: float, d: Decision, slot: str, regime: str,
                        now: datetime) -> None:
        """List it with the ideas, so its result shows on the dashboard's scoreboard."""
        from topstep_bot.recommendations import Recommendation

        book = self.core.recommender
        rec = Recommendation(
            id=f"R{next(book._ids)}", created=now, strategy=ASSISTANT, title=TITLE, active=False, side=plan.side,
            entry=price, stop=plan.stop, target=plan.target, size=plan.size, risk_usd=plan.planned_risk,
            reason=d.reason, status="taken", trade_tag=trade.tag, hypothetical=False, slot=slot, regime=regime,
            context=dict(trade.context),
        )
        book._store(rec, new=True)

    # ------------------------------------------------------------ results

    def trade_closed(self, t: ManagedTrade) -> None:
        """A trade closed. Its own: filed in the knowledge base ("assistant", source "real"). Any it acted on: the
        decisions that acted on it get the result."""
        r = t.r_multiple()
        ids = self._records.pop(t.tag, [])
        opened = self._opened.pop(t.tag, None)
        if opened is not None:
            ids = [opened[3], *ids]
        outcome = {"r": None if r is None else round(r, 2), "usd": round(t.net_pnl, 2), "exit": t.exit_reason}
        j = self.core.journal
        if j is not None:
            for rid in ids:
                try:
                    row = j.assistant_decision(rid)
                    if row:
                        j.record_assistant({**row, "outcome": outcome})
                except Exception as exc:  # noqa: BLE001
                    log.warning("Could not update the AI assistant's decision record: %s", exc)
        for item in self.recent:
            if item.get("id") in ids:
                item["outcome"] = outcome
        if t.strategy != ASSISTANT:
            return
        kb = self.core.knowledge
        if kb is None or r is None or any(k in t.exit_reason for k in UNINFORMATIVE_EXITS):
            return
        core = self.core
        when = t.opened_at or t.created_at
        slot, regime, basis, _ = opened or (None, None, None, None)
        kb.record(Observation(
            day=core.schedule.trading_day(when).isoformat(), time=core.schedule.local(when).strftime("%H:%M"),
            strategy=ASSISTANT, side=t.side.label, slot=slot or core.slot(when), regime=regime or core.regime.value,
            r=round(r, 2), usd=round(t.net_pnl, 2), source="real", why=t.exit_reason, basis=basis,
            **core.trade_facts(t),
        ))

    def plan_resolved(self, rec: Any) -> None:
        """A strategy trade it changed has been followed to where its own plan would have ended: score the difference."""
        real = getattr(rec, "assistant_real", None)
        if not real or rec.outcome_r is None:
            return
        row = {"day": self._day(rec.created), "time": self._local(rec.created), "symbol": self.symbol,
               "strategy": rec.title, "side": rec.side.label, "changes": real.get("changes", []),
               "real_r": real.get("r"), "plan_r": rec.outcome_r, "real_usd": real.get("usd"), "plan_usd": rec.outcome_usd}
        self.desk.add_score(row)
        diff = (row["real_r"] or 0.0) - row["plan_r"]
        self.core.event("info", f"AI assistant scorecard: the {rec.title} {rec.side.label} trade it managed made "
                                f"{_fmt_r(row['real_r'])}; left alone it would have made {_fmt_r(row['plan_r'])} "
                                f"({'helped' if diff > 0.05 else 'cost' if diff < -0.05 else 'no difference'}"
                                f"{f' {abs(diff):.2f}R' if abs(diff) > 0.05 else ''}).")

    def close(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
        if self in self.desk.lanes:
            self.desk.lanes.remove(self)

    # ------------------------------------------------------------ dashboard / Telegram

    def view(self) -> dict[str, Any]:
        desk, a = self.desk, self.acfg
        now = self.core.clock()
        day = self._day(now)
        u = desk.usage(day)
        kb = self.core.knowledge
        own = kb.stats(ASSISTANT, None, None, self.core.schedule.trading_day(now)).to_dict(kb.min_samples) if kb else None
        blocked = desk.blocked(day, now) if desk.on else None
        return {
            "manage": desk.manage, "trade": desk.trade, "include_manual": a.include_manual, "state": self.state,
            "message": self.message, "thinking": self.thinking,
            "thinking_since": self.thinking_since.astimezone(UTC).isoformat() if self.thinking_since else None,
            "model": a.model, "unavailable": blocked,
            "today": {"calls": u["calls"], "cost": round(u["cost"], 4), "trades": u["trades"],
                      "max_trades": a.max_trades_per_day, "budget": a.daily_budget_usd},
            "settings": {"max_contracts": a.max_contracts, "min_confidence": a.min_confidence,
                         "think_every": a.think_every_minutes, "manage_every": a.manage_every_minutes},
            "decisions": list(self.recent)[:8],
            "own": own, "management": desk.score_summary(),
        }

    def text(self) -> str:
        v = self.view()
        lines = [f"🤖 AI assistant on {self.symbol}: managing open trades {'ON' if v['manage'] else 'off'}, own trades "
                 f"{'ON' if v['trade'] else 'off'}"]
        lines.append(v["message"] if not v["unavailable"] else f"Can't ask the AI now: {v['unavailable']}.")
        t = v["today"]
        lines.append(f"Today: {t['calls']} decision(s), ${t['cost']:.2f} of ${t['budget']:.2f}, {t['trades']} of "
                     f"{t['max_trades']} own trade(s). Model: {v['model']}.")
        for d in v["decisions"][:5]:
            conf = f" ({d['confidence']:.0%})" if d.get("confidence") is not None else ""
            lines.append(f"• {d['time']} {d['kind']}: {d.get('action') or '-'}{conf} - {d.get('result') or ''}")
            if d.get("reason"):
                lines.append(f"  Why: {d['reason']}")
        own, m = v["own"], v["management"]
        if own and own.get("n"):
            lines.append(f"Its own trades so far: {own['n']}, {own['wins']} won, {own['mean_r']:+.2f}R avg.")
        if m["n"]:
            lines.append(f"Trades it managed vs left alone: {m['r']:+.2f}R over {m['n']} ({m['helped']} helped, {m['hurt']} cost).")
        lines.append("Switch: /assistant manage on|off, /assistant trade on|off, /assistant off. No AI can promise a "
                     "winning trade.")
        return "\n".join(lines)


def _round(v: float | None) -> float | None:
    return None if v is None else round(v, 2)


def _fmt_r(v: float | None) -> str:
    return "?" if v is None else f"{v:+.2f}R"


def _brief(record: dict[str, Any]) -> dict[str, Any]:
    """A decision as the dashboard lists it (without the situation it saw)."""
    d = record.get("decision") or {}
    return {"id": record.get("id"), "time": record.get("time"), "kind": record.get("kind"), "symbol": record.get("symbol"),
            "why_asked": record.get("why_asked"), "action": record.get("action"), "confidence": d.get("confidence"),
            "reason": d.get("reason"), "evidence": d.get("evidence") or [], "status": record.get("status"),
            "result": record.get("result"), "trade": record.get("trade"), "outcome": record.get("outcome"),
            "cost": d.get("cost")}
