"""The day plan: each morning, each account's profit goal and a route the bot plans to reach it.

At the start of every trading day (``day_plan.ask_at``, 08:00 CT by default) the bot asks on
Telegram what each account should aim for today, with a suggestion for each; the dashboard's
Overview asks the same. You answer with a tap, by typing ("c1 400 c2 300"), with ``/goal c1 400``
or on the dashboard. With no answer the bot keeps your last goal (``day_plan.unanswered: last``)
or uses its suggestion.

**The goal never raises risk.** Trades are sized by the risk settings exactly as before, and every
loss limit, the Maximum Loss Limit, Topstep's caps and the Combine's consistency line apply as
always. A goal above what one day may make (the daily profit target, the consistency line or a
payout path's daily cap) is capped there. The goal can only make a day end sooner: once it is
reached (``day_plan.stop_at_goal``), no new trades open that day, so the profit is kept.

**The route** is worked out from the bot's own history: every past day in the knowledge base
(knowledge.py) is replayed with the signals the bot would take with what it knows today, under
today's rules (the entry window, trades per day, the stop after a losing streak, the cooldown,
the daily loss limit, the consistency line, the trend filter when it is on). From those days it
says when the trades usually come (the open, midday, the close) and from which strategies, how
many winners the goal needs at the current risk per trade, how often the goal was reached and
how often the day ended red, and - during the day - the same odds from where the day stands now.
The chart's trend (trend.py) is part of the picture: which way it points and how the bot's
signals have done with it and against it.

The suggestion: for a Combine, what is left to the (consistency-adjusted) profit target spread
over ``day_plan.days_to_target`` trading days; for an Express Funded account, a winning day with a
cushion (standard payout path) or the cycle goal spread the same way (consistency path). Always
capped by what one day may make.

A plan, not a promise: the odds are how the bot's own past days went, markets change, and no bot
can guarantee a profit or a passed Combine.
"""

from __future__ import annotations

import json
import logging
import math
import re
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from topstep_bot.config import BotConfig
    from topstep_bot.engine import TradingCore
    from topstep_bot.risk.book import AccountBook

log = logging.getLogger("topstep_bot.day_plan")
UTC = timezone.utc

GOALS_FILE = "day_goals.json"
KEEP_DAYS = 60  # days of goals kept in the file
LAST_GOAL_DAYS = 30  # "keep the last goal" looks back this far
LOOKBACK_DAYS = 90  # calendar days of history the route replays
MIN_DAYS = 10  # past trading days needed before the route gives odds
SOURCES = ("train", "shadow", "real")
WINDOWS = (("Open", time(8, 30), time(10, 0)), ("Midday", time(10, 0), time(13, 0)), ("Close", time(13, 0), time(15, 10)))
CAVEAT = ("A plan, not a promise: the odds are how the bot's own past days went with today's rules. Markets change, "
          "and no bot can guarantee a profit or a passed Combine.")
CACHE_SECONDS = 20
GOAL_LOCK = "today's goal"  # how the risk manager's lock at the goal starts (risk/manager.py)


# =============================================================================== goals on disk

def goals_path(cfg: BotConfig) -> Path:
    return cfg.account_dir / GOALS_FILE


def goal_day(cfg: BotConfig, now: datetime | None = None) -> str:
    """The trading day a goal given at ``now`` is for: today until 15:10 CT, then the next trading day."""
    from topstep_bot.market_pick import switch_day

    return switch_day(cfg, now or datetime.now(UTC))[0]


def load_goals(cfg: BotConfig) -> dict[str, Any]:
    try:
        data = json.loads(goals_path(cfg).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"days": {}}
    if not isinstance(data, dict) or not isinstance(data.get("days"), dict):
        return {"days": {}}
    return data


def save_goal(cfg: BotConfig, day: str, goal: float | None, source: str, now: datetime | None = None) -> dict[str, Any]:
    """Save ``goal`` (dollars; None = no goal today) for trading day ``day``. Returns the saved entry."""
    from topstep_bot.config import atomic_write_text

    if goal is not None:
        goal = round(float(goal), 2)
        if not math.isfinite(goal) or goal <= 0:
            raise ValueError("A goal must be a positive amount of dollars (or 'none' for no goal today)")
    data = load_goals(cfg)
    entry = {"goal": goal, "source": source, "at": (now or datetime.now(UTC)).isoformat(timespec="seconds")}
    days = data["days"]
    days[day] = entry
    for old in sorted(days)[:-KEEP_DAYS]:
        days.pop(old, None)
    path = goals_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(data, indent=1))
    return entry


AMOUNT = r"\$?\s*(\d{1,6}(?:[.,]\d{1,2})?)\s*(k)?"
NO_GOAL = ("none", "no", "off", "nogoal", "no goal", "skip", "-")


def parse_goals(text: str, accounts: list[str]) -> dict[str | None, float | None] | None:
    """Goals typed as text: "400", "$400", "c1 400 c2 300", "c1: 400, c2: $1.2k", "400 300" (in account order),
    "c1 none". Returns {account (None = every account): dollars or None for "no goal"}, or None if the text
    isn't (only) goals."""
    words = re.sub(r"(\d),(\d{3})(?!\d)", r"\1\2", text.strip().lower())  # "$1,200": a thousands separator
    words = re.sub(r"[,;:=]", " ", words)
    words = re.sub(r"\bno goal\b", "nogoal", words)
    tokens = words.split()
    if not tokens or len(tokens) > 2 * max(1, len(accounts)) + 1:
        return None
    if tokens[0] in ("goal", "goals"):
        tokens = tokens[1:]
    out: dict[str | None, float | None] = {}
    pending: str | None = None
    loose: list[float | None] = []
    names = [a.lower() for a in accounts]
    for tok in tokens:
        if tok in names or tok == "all":
            if pending is not None:
                return None
            pending = None if tok == "all" else tok
            if tok == "all":
                pending = "*"
            continue
        if tok in NO_GOAL:
            value: float | None = None
        else:
            m = re.fullmatch(AMOUNT, tok)
            if not m:
                return None
            value = float(m.group(1).replace(",", ".")) * (1000 if m.group(2) else 1)
            if value <= 0:
                return None
        if pending is None:
            loose.append(value)
        else:
            out[None if pending == "*" else pending] = value
            pending = None
    if pending is not None:
        return None
    if loose:
        if out:
            return None  # "400 c2 300": ambiguous
        if len(loose) == 1:
            out[None] = loose[0]
        elif names and len(loose) == len(names):
            out = dict(zip(names, loose, strict=True))
        else:
            return None
    return out or None


# =============================================================================== the account's frame

@dataclass
class Frame:
    """What the plan needs to know about the account (works with or without a running bot)."""

    stage: str  # combine | express
    plan: str  # 50K, 100K, 150K
    path: str  # payout path (express)
    profit_target: float
    remaining: float | None  # Combine: left to the (consistency-adjusted) target
    cap: float | None  # the most one day may make (settings' daily target, consistency line, payout cap)
    cap_why: str
    risk_usd: float  # what the next trade may lose at its stop
    daily_loss: float  # personal daily loss limit
    max_trades: int
    max_losses: int
    cooldown: int  # minutes after a loss
    passed: bool = False
    made: float | None = None  # total profit so far (balance - start), when known

    def to_dict(self) -> dict[str, Any]:
        return {k: (round(v, 2) if isinstance(v, float) else v) for k, v in asdict(self).items()}


def _caps(cfg: BotConfig) -> tuple[float | None, float | None]:
    """(settings' daily profit target, profit ceiling) without a running bot - as the risk manager works them out."""
    from topstep_bot import funded
    from topstep_bot.risk.manager import AGGRESSIVE_GOAL_SHARE, CONSISTENCY_GUARD_FRACTION, DEFAULT_COMBINE_DAILY_CAP
    from topstep_bot.risk.topstep import PLANS

    plan = PLANS[cfg.account.plan]
    stage, path = cfg.account.stage, cfg.account.payout_path
    payout_cap = funded.daily_cap(cfg.funded, plan, stage, path)
    if cfg.risk.daily_profit_target is not None:
        target = cfg.risk.daily_profit_target
    elif cfg.aggressive.enabled:
        goal = cfg.aggressive.daily_goal or AGGRESSIVE_GOAL_SHARE * plan.profit_target
        target = min(goal, payout_cap) if payout_cap is not None else goal
    elif stage == "combine":
        target = DEFAULT_COMBINE_DAILY_CAP * plan.profit_target
    else:
        target = payout_cap
    ceiling = CONSISTENCY_GUARD_FRACTION * plan.profit_target if stage == "combine" and cfg.risk.consistency_guard \
        else payout_cap
    return target, ceiling


def _cap_why(cfg: BotConfig, target: float | None, ceiling: float | None) -> str:
    """Which limit decides the most one day may make, in a few words."""
    stage = cfg.account.stage
    if target is None and ceiling is None:
        return ""
    if ceiling is not None and (target is None or ceiling <= target):
        return "the Combine's consistency line" if stage == "combine" else "the payout path's daily cap"
    if cfg.risk.daily_profit_target is not None:
        return "your daily profit target"
    if cfg.aggressive.enabled:
        return "the aggressive mode's daily goal"
    if stage == "combine":
        return "the bot's Combine day cap, 40% of the profit target"
    return "the payout path's daily cap"


def frame_from_config(cfg: BotConfig, made: float | None = None, best_day: float = 0.0) -> Frame:
    """The frame from the settings alone (the bot isn't running): progress only if ``made`` is known."""
    from topstep_bot.risk.topstep import PLANS, combine_progress

    plan = PLANS[cfg.account.plan]
    target, ceiling = _caps(cfg)
    caps = [c for c in (target, ceiling) if c is not None]
    remaining, passed = None, False
    if cfg.account.stage == "combine" and made is not None:
        p = combine_progress(plan, made, best_day)
        remaining, passed = max(0.0, p.remaining), p.passed
    r = cfg.risk
    return Frame(cfg.account.stage, plan.name, cfg.account.payout_path, plan.profit_target, remaining,
                 min(caps) if caps else None, _cap_why(cfg, target, ceiling), r.risk_per_trade,
                 r.personal_daily_loss_limit, r.max_trades_per_day, r.max_consecutive_losses, r.cooldown_minutes_after_loss,
                 passed, made)


def frame_from_core(core: TradingCore) -> Frame:
    risk = core.risk
    open_pnl = core.orders.open_pnl()
    progress = risk.combine_progress(core.balance, open_pnl)
    target, ceiling = risk.configured_daily_target, risk.profit_ceiling()
    return Frame(risk.stage, risk.plan.name, risk.account_cfg.payout_path, risk.plan.profit_target,
                 max(0.0, progress.remaining) if progress else None, risk.day_cap(), _cap_why(core.cfg, target, ceiling),
                 max(0.0, risk.trade_risk(core.balance)), risk.cfg.personal_daily_loss_limit, risk.max_trades_today,
                 risk.cfg.max_consecutive_losses, risk.cfg.cooldown_minutes_after_loss,
                 bool(progress and progress.passed), round(core.balance - core.tracker.starting_balance, 2))


def _round_goal(x: float) -> float:
    step = 10 if x < 200 else 25 if x < 1000 else 50
    return max(step, round(x / step) * step)


def suggest(frame: Frame, days_to_target: int) -> dict[str, Any]:
    """The bot's suggested goal for today, a steadier and a stretch option, and why."""
    from topstep_bot.risk.topstep import XFA_WINNING_DAY

    days = max(1, days_to_target)
    if frame.stage == "combine":
        left = frame.remaining if frame.remaining is not None else frame.profit_target
        if frame.passed or left <= 0:
            return {"suggested": None, "options": [], "cap": frame.cap,
                    "why": "The Combine's profit target is reached: the bot stops trading to protect the pass."}
        base = left / days
        why = f"${left:,.0f} left to the Combine target, spread over {days} trading days"
        if base > left:
            base = left
    elif frame.path == "standard":
        base = max(XFA_WINNING_DAY + 50, frame.profit_target / days)
        why = f"a winning day (${XFA_WINNING_DAY:,.0f}+ counts toward a payout) with a cushion for fees"
    else:
        base = frame.profit_target / days
        why = f"the payout goal spread over {days} trading days, under the 40% daily cap"
    suggested = _round_goal(base)
    if frame.remaining is not None and frame.stage == "combine":
        suggested = min(suggested, max(10.0, math.ceil(frame.remaining / 10) * 10))
    if frame.cap is not None and suggested > frame.cap:
        suggested = _round_goal(frame.cap * 0.9) if frame.cap * 0.9 >= 10 else frame.cap
        why += f", capped under {frame.cap_why} (${frame.cap:,.0f})"
    options = sorted({_round_goal(suggested * 0.6), suggested,
                      _round_goal(min(frame.cap if frame.cap is not None else math.inf, suggested * 1.6))})
    options = [o for o in options if o > 0 and (frame.cap is None or o <= frame.cap)]
    return {"suggested": suggested, "options": options, "cap": frame.cap, "cap_why": frame.cap_why, "why": why}


@dataclass
class Goal:
    amount: float | None  # dollars the day aims for (None = no goal today)
    kind: str  # set | last | suggested | off | none
    source: str = ""
    at: str = ""
    asked: float | None = None  # what was asked for, before any cap
    capped: bool = False
    day: str = ""
    from_day: str = ""  # "last": the day the goal was given

    @property
    def text(self) -> str:
        """Where the goal comes from, in a few words."""
        return {"set": "your goal", "last": "your last goal", "suggested": "the suggested goal", "off": "no goal today",
                "none": "no goal"}.get(self.kind, "goal")

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "text": self.text}


def resolve(cfg: BotConfig, goals: dict[str, Any], day: str, frame: Frame, suggestion: dict[str, Any] | None = None) -> Goal:
    """Today's goal: the one given for ``day``; else the last one given (or the suggestion); capped by the frame."""
    days = goals.get("days") or {}
    entry = days.get(day)
    if isinstance(entry, dict):
        if entry.get("goal") is None:
            return Goal(None, "off", entry.get("source", ""), entry.get("at", ""), day=day)
        goal = Goal(float(entry["goal"]), "set", entry.get("source", ""), entry.get("at", ""), day=day)
    else:
        goal = None
        if cfg.day_plan.unanswered == "last":
            oldest = (date.fromisoformat(day) - timedelta(days=LAST_GOAL_DAYS)).isoformat()
            for d in sorted(days, reverse=True):
                e = days[d]
                if d < day and d >= oldest and isinstance(e, dict) and e.get("goal"):
                    goal = Goal(float(e["goal"]), "last", e.get("source", ""), e.get("at", ""), day=day, from_day=d)
                    break
        if goal is None:
            s = suggestion if suggestion is not None else suggest(frame, cfg.day_plan.days_to_target)
            if s.get("suggested") is None:
                return Goal(None, "none", day=day)
            goal = Goal(float(s["suggested"]), "suggested", "the bot", day=day)
    goal.asked = goal.amount
    if frame.cap is not None and goal.amount is not None and goal.amount > frame.cap:
        goal.amount, goal.capped = round(frame.cap, 2), True
    return goal


# =============================================================================== the route

@dataclass
class Rules:
    """Today's limits in the units the replay works in (minutes after midnight CT, R)."""

    start: int
    last: int
    tf: int
    max_trades: int
    max_losses: int
    cooldown: int
    daily_loss_r: float
    ceiling_r: float | None = None
    blackouts: list[tuple[int, int]] = field(default_factory=list)


@dataclass
class PastSignal:
    minute: int
    net_r: float
    bars: int
    strategy: str
    side: str
    market: str
    slot: str


@dataclass
class DayPath:
    """One replayed day: the result after each trade, and the trades in order."""

    cumulative: list[float] = field(default_factory=list)
    taken: list[PastSignal] = field(default_factory=list)

    def final(self, goal_r: float | None = None) -> float:
        """The day's result in R; with ``goal_r``, where it stopped once the goal was reached."""
        if goal_r is not None:
            for c in self.cumulative:
                if c >= goal_r - 1e-9:
                    return c
        return self.cumulative[-1] if self.cumulative else 0.0

    def reached(self, goal_r: float) -> bool:
        return any(c >= goal_r - 1e-9 for c in self.cumulative)


def _minutes(hhmm: str) -> int | None:
    try:
        h, m = hhmm.split(":")[:2]
        return int(h) * 60 + int(m)
    except (ValueError, AttributeError):
        return None


def replay(signals: list[PastSignal], rules: Rules, *, start: int | None = None, day_r: float = 0.0, trades: int = 0,
           losses: int = 0) -> DayPath:
    """Take one past day's signals in order under the rules, from ``start`` (minutes CT) and a day already at
    ``day_r`` with ``trades`` taken and ``losses`` in a row. One trade at a time per market."""
    out = DayPath()
    lo = max(rules.start, start if start is not None else rules.start)
    busy: dict[str, int] = {}
    cooldown_until = -1
    for s in signals:
        m = s.minute
        if m < lo or m > rules.last or m < busy.get(s.market, -1) or m < cooldown_until:
            continue
        if any(a <= m < b for a, b in rules.blackouts):
            continue
        if trades >= rules.max_trades or losses >= rules.max_losses or day_r <= -rules.daily_loss_r:
            break
        if rules.ceiling_r is not None and day_r >= rules.ceiling_r:
            break
        net = s.net_r
        if rules.ceiling_r is not None:
            net = min(net, rules.ceiling_r - day_r)  # the bot caps a trade's target at the consistency line
        day_r += net
        trades += 1
        out.cumulative.append(round(day_r, 4))
        out.taken.append(s)
        busy[s.market] = m + max(1, s.bars or 1) * rules.tf
        if net < 0:
            losses += 1
            cooldown_until = busy[s.market] + rules.cooldown
        else:
            losses = 0
    return out


def past_days(cores: Iterable[TradingCore], today: date) -> tuple[list[str], dict[str, list[PastSignal]], str]:
    """Every past trading day in the knowledge base(s) and the signals the bot would take on it with what it knows
    today (deduplicated: a real trade and the idea following the same signal count once)."""
    from topstep_bot.forecast import takes_signal
    from topstep_bot.knowledge import NOT_STRATEGIES, slot_for
    from topstep_bot.trend_lab import blocks

    first = (today - timedelta(days=LOOKBACK_DAYS)).isoformat()
    end = today.isoformat()
    days: set[str] = set()
    signals: dict[str, dict[tuple, PastSignal]] = {}
    what = ""
    for core in cores:
        kb = core.knowledge
        if kb is None:
            continue
        counts = takes_signal(core, today)
        filt = core.trend_filter[0]
        t = core.cfg.trend
        market = core.contract.root or core.contract.name
        what = what or core.strategy.title
        for o in kb.obs:
            if o.source not in SOURCES or o.strategy in NOT_STRATEGIES or not first <= o.day < end:
                continue
            try:
                d = date.fromisoformat(o.day)
            except ValueError:
                continue
            if not core.schedule.is_trade_day(d):
                continue
            days.add(o.day)
            m = _minutes(o.time)
            if m is None or not counts(o) or (filt and blocks(o, t.min_score, t.min_adx)):
                continue
            key = (market, o.time, o.strategy, o.side)
            have = signals.setdefault(o.day, {}).get(key)
            if have is not None and o.source != "real":
                continue
            signals[o.day][key] = PastSignal(m, o.net_r, o.bars or 1, o.strategy, o.side, market,
                                          slot_for(time(m // 60, m % 60)))
    by_day = {d: sorted(v.values(), key=lambda s: (s.minute, s.strategy)) for d, v in signals.items()}
    return sorted(days), by_day, what


def _share(xs: list[bool]) -> float | None:
    return round(sum(xs) / len(xs), 3) if xs else None


def _median(xs: list[float]) -> float | None:
    if not xs:
        return None
    s = sorted(xs)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


def odds(days: list[str], by_day: dict[str, list[PastSignal]], rules: Rules, goal_r: float | None, **state: Any) -> dict[str, Any]:
    """How the past days went under the rules (from ``state``, see replay): reached, green, red, median."""
    paths = [replay(by_day.get(d, []), rules, **state) for d in days]
    start = float(state.get("day_r", 0.0))
    finals = [p.final(goal_r) for p in paths]
    out: dict[str, Any] = {"days": len(paths), "green": _share([f > start + 1e-9 for f in finals]),
                           "red": _share([f < -1e-9 for f in finals]), "median_r": _median(finals),
                           "no_trade": _share([not p.cumulative for p in paths])}
    if goal_r is not None:
        hits = [p.reached(goal_r) or start >= goal_r - 1e-9 for p in paths]
        out["reach"] = _share(hits)
        out["reach_n"] = sum(hits)
    peaks = sorted(max([start, *p.cumulative]) for p in paths)
    if peaks:
        out["half_r"] = peaks[len(peaks) // 2]  # a goal reached on about half of the days (in R above zero)
    return out


def windows(days: list[str], by_day: dict[str, list[PastSignal]], rules: Rules, titles: dict[str, str]) -> list[dict[str, Any]]:
    """When the bot's trades came on the replayed days (no goal stop): per part of the day, trades a day, average R
    and the strategies that gave them."""
    taken: list[PastSignal] = []
    for d in days:
        taken += replay(by_day.get(d, []), rules).taken
    n = max(1, len(days))
    out = []
    for name, a, b in WINDOWS:
        lo, hi = max(a.hour * 60 + a.minute, rules.start), min(b.hour * 60 + b.minute, rules.last + 1)
        if lo >= hi:
            continue
        here = [s for s in taken if lo <= s.minute < hi]
        by: dict[str, list[float]] = {}
        for s in here:
            by.setdefault(s.strategy, []).append(s.net_r)
        top = sorted(by.items(), key=lambda kv: -len(kv[1]))[:3]
        shown = min(hi, rules.last)
        out.append({"name": name, "from": f"{lo // 60:02d}:{lo % 60:02d}", "to": f"{shown // 60:02d}:{shown % 60:02d}",
                    "start": lo, "end": hi, "per_day": round(len(here) / n, 2),
                    "avg_r": round(sum(s.net_r for s in here) / len(here), 3) if here else None,
                    "strategies": [{"name": k, "title": titles.get(k, k), "n": len(v), "avg_r": round(sum(v) / len(v), 3)}
                                   for k, v in top]})
    return out


def _usd(x: float | None) -> str:
    if x is None:
        return "-"
    return f"-${abs(x):,.0f}" if x < 0 else f"${x:,.0f}"


def _pct(x: float | None) -> str:
    return "-" if x is None else f"{round(100 * x)}%"


class DayPlanner:
    """One account's goals (from ``day_goals.json``) and its plan for the day; held by the live desk (live.py)."""

    def __init__(self, cfg: BotConfig, label: str = ""):
        self.cfg = cfg
        self.label = label
        self.goals: dict[str, Any] = {"days": {}}
        self._mtime: float | None = None
        self.goal: Goal | None = None
        self._applied: tuple | None = None
        self._cache: tuple[tuple, float, dict[str, Any]] | None = None
        self.reload(force=True)

    def reload(self, force: bool = False) -> bool:
        """Read the goals file again if it changed (the controller writes it). True when it did."""
        path = goals_path(self.cfg)
        try:
            mtime = path.stat().st_mtime
        except OSError:
            mtime = None
        if not force and mtime == self._mtime:
            return False
        self._mtime = mtime
        self.goals = load_goals(self.cfg)
        self._cache = None
        return True

    def resolve(self, core: TradingCore, day: date | None = None) -> tuple[Goal, Frame, dict[str, Any]]:
        frame = frame_from_core(core)
        sug = suggest(frame, self.cfg.day_plan.days_to_target)
        d = (day or core.current_day or core.schedule.trading_day(core.clock())).isoformat()
        return resolve(self.cfg, self.goals, d, frame, sug), frame, sug

    def apply(self, core: TradingCore, book: AccountBook) -> Goal | None:
        """Put today's goal on the account book (the risk manager stops new trades once it is reached)."""
        goal, _, _ = self.resolve(core)
        self.goal = goal
        amount = goal.amount if self.cfg.day_plan.stop_at_goal else None
        book.goal, book.goal_text = amount, goal.text
        key = (goal.day, goal.amount, goal.kind)
        if key != self._applied:
            first = self._applied is None
            self._applied = key
            if not first and (book.lock_reason or "").startswith(GOAL_LOCK):
                book.lock_reason = None  # a new goal: the old one's stop no longer holds (re-checked on the next signal)
                core.event("info", "The day's goal changed, so the stop at the old goal is lifted. Every other limit "
                                   "still applies.")
            if goal.amount is not None:
                note = f" (you asked for {_usd(goal.asked)}; capped at {_usd(goal.amount)}, {frame_from_core(core).cap_why})" \
                    if goal.capped else ""
                stop = " - no new trades once it is reached" if self.cfg.day_plan.stop_at_goal else ""
                core.event("info", f"Today's goal: {_usd(goal.amount)} ({goal.text}){note}{stop}. Every risk rule applies "
                                   "as usual; the goal never makes a trade bigger.")
            elif not first or goal.kind == "off":
                core.event("info", f"Today: {goal.text} - the bot trades by its usual rules.")
        return goal

    # ------------------------------------------------------------------ the plan

    def view(self, core: TradingCore, cores: list[TradingCore] | None = None) -> dict[str, Any]:
        """Today's plan for the dashboard, Telegram, Ask and the assistant (cached for a few seconds)."""
        now = core.clock()
        risk = core.risk
        day_pnl = risk.day_pnl(core.balance, core.orders.open_pnl())
        key = (core.current_day, self._mtime, round(day_pnl / 5), risk.trades_today, risk.consecutive_losses,
               risk.lock_reason, core.trend_filter[0], core.trend.last.direction if core.trend.last else None,
               id(core.knowledge), getattr(core.knowledge, "updated", None))
        if self._cache and self._cache[0] == key and (now.timestamp() - self._cache[1]) < CACHE_SECONDS:
            return self._cache[2]
        plan = build_plan(self, core, cores or [core], now)
        self._cache = (key, now.timestamp(), plan)
        return plan


def build_plan(planner: DayPlanner, core: TradingCore, cores: list[TradingCore], now: datetime) -> dict[str, Any]:
    from topstep_bot.strategies import STRATEGIES

    cfg = planner.cfg
    goal, frame, sug = planner.resolve(core)
    risk = core.risk
    schedule = core.schedule
    scfg = schedule.cfg
    today = core.current_day or schedule.trading_day(now)
    r_usd = frame.risk_usd if frame.risk_usd > 0 else cfg.risk.risk_per_trade
    day_pnl = risk.day_pnl(core.balance, core.orders.open_pnl())
    ceiling = risk.profit_ceiling()
    rules = Rules(start=scfg.trade_start.hour * 60 + scfg.trade_start.minute,
                  last=scfg.last_entry.hour * 60 + scfg.last_entry.minute, tf=cfg.instrument.timeframe_minutes,
                  max_trades=risk.max_trades_today, max_losses=risk.cfg.max_consecutive_losses,
                  cooldown=risk.cfg.cooldown_minutes_after_loss, daily_loss_r=risk.cfg.personal_daily_loss_limit / r_usd,
                  ceiling_r=ceiling / r_usd if ceiling is not None else None,
                  blackouts=[(w.start.hour * 60 + w.start.minute, w.end.hour * 60 + w.end.minute)
                             for w in scfg.blackout_windows])
    plain_goal_r = goal.amount / r_usd if goal.amount is not None else None
    local = schedule.local(now)
    minute = local.hour * 60 + local.minute
    out: dict[str, Any] = {
        "day": today.isoformat(), "account": cfg.active_account or "main", "label": planner.label,
        "goal": goal.to_dict(), "suggest": sug, "frame": frame.to_dict(), "risk_usd": round(r_usd, 2),
        "goal_r": round(plain_goal_r, 2) if plain_goal_r is not None else None, "stop_at_goal": cfg.day_plan.stop_at_goal,
        "ask": {"enabled": cfg.day_plan.ask, "at": scfg_time(cfg.day_plan.ask_at)},
        "answered": goal.kind in ("set", "off"), "caveat": CAVEAT, "odds": None, "from_now": None, "windows": [],
    }

    # ---- history
    try:
        days, by_day, what = past_days(cores, today)
    except Exception:  # noqa: BLE001 - a plan without odds is still a plan
        log.exception("Day plan: could not read the history")
        days, by_day, what = [], {}, ""
    titles = {name: cls.title for name, cls in STRATEGIES.items()}
    if len(days) >= MIN_DAYS:
        out["odds"] = odds(days, by_day, rules, plain_goal_r)
        out["windows"] = windows(days, by_day, rules, titles)
        wins = [s.net_r for d in days for s in replay(by_day.get(d, []), rules).taken if s.net_r > 0]
        out["avg_win_r"] = round(sum(wins) / len(wins), 2) if wins else None
        if minute >= rules.start and day_pnl is not None:
            out["from_now"] = odds(days, by_day, rules, plain_goal_r, start=minute + 1, day_r=day_pnl / r_usd,
                                   trades=risk.trades_today, losses=risk.consecutive_losses)
        out["history"] = {"days": len(days), "what": what, "signals_per_day": round(sum(len(v) for v in by_day.values())
                                                                                     / len(days), 2)}

    # ---- the trend now
    tv = core.trend_view()
    out["trend"] = {"reading": tv["reading"], "history": tv["history"], "filter": tv["filter"]}

    # ---- progress
    reached = goal.amount is not None and day_pnl >= goal.amount - 1e-9
    state = "goal reached" if reached else ("stopped" if risk.lock_reason else
                                            "after hours" if minute > rules.last else
                                            "before the open" if minute < rules.start else "on the way")
    out["progress"] = {"made": round(day_pnl, 2), "left": round(goal.amount - day_pnl, 2) if goal.amount is not None else None,
                       "left_r": round((goal.amount - day_pnl) / r_usd, 2) if goal.amount is not None else None,
                       "share": round(max(0.0, min(1.0, day_pnl / goal.amount)), 3) if goal.amount else None,
                       "trades": risk.trades_today, "max_trades": risk.max_trades_today,
                       "losses_in_row": risk.consecutive_losses, "max_losses": risk.cfg.max_consecutive_losses,
                       "state": state, "stopped": risk.lock_reason}
    out["stops"] = stops(cfg, frame, goal, risk, r_usd)
    out["steps"] = steps(out, minute, rules)
    out["headline"] = headline(out)
    return out


def scfg_time(t: time) -> str:
    return t.strftime("%H:%M")


def stops(cfg: BotConfig, frame: Frame, goal: Goal, risk: Any, r_usd: float) -> list[str]:
    """When the day ends, in plain sentences."""
    out = []
    if goal.amount is not None and cfg.day_plan.stop_at_goal:
        out.append(f"Goal reached ({_usd(goal.amount)}): no new trades today, the profit is kept.")
    out.append(f"{risk.cfg.max_consecutive_losses} losses in a row, or down {_usd(risk.cfg.personal_daily_loss_limit)} "
               f"(your daily loss limit), or {risk.max_trades_today} trades: done for the day.")
    if frame.cap is not None:
        line = "consistency line" in frame.cap_why or "payout path" in frame.cap_why
        out.append(f"At most {_usd(frame.cap)} in one day ({frame.cap_why}): "
                   + ("a winning trade's target is capped there." if line else "no new trades once the day gets there."))
    out.append(f"Each trade risks up to {_usd(r_usd)} (your risk settings). Nothing is sized bigger to reach the goal.")
    return out


def steps(plan: dict[str, Any], minute: int, rules: Rules) -> list[dict[str, Any]]:
    """The route in order: the goal, each part of the day with what usually happens there, the finish."""
    out: list[dict[str, Any]] = []
    goal = plan["goal"]
    r_usd = plan["risk_usd"]
    if goal["amount"] is not None:
        wins = plan.get("avg_win_r")
        need = plan["goal_r"]
        detail = f"{_usd(goal['amount'])} is {need:.1f}R at {_usd(r_usd)} a trade"
        if wins:
            n = max(1, math.ceil(need / wins - 1e-9))
            detail += f": about {n} winning trade{'s' if n > 1 else ''} at the bot's average win ({wins:.2f}R) more than the losses"
        waiting = not plan["answered"] and minute < rules.start  # still time to answer the morning question
        out.append({"title": f"Goal: {_usd(goal['amount'])} ({goal['text']})", "detail": detail + ".",
                    "state": "now" if waiting else "done"})
    else:
        out.append({"title": "No goal today", "detail": "The bot trades by its usual rules and limits.", "state": "done"})
    reading = (plan.get("trend") or {}).get("reading")
    hist = (plan.get("trend") or {}).get("history") or {}
    for w in plan.get("windows") or []:
        state = "done" if minute >= w["end"] else "now" if minute >= w["start"] else "next"
        if w["per_day"] < 0.05:
            detail = "The bot rarely trades here."
        else:
            strat = ", ".join(s["title"] for s in w["strategies"][:2])
            avg = f", {w['avg_r']:+.2f}R each after costs" if w["avg_r"] is not None else ""
            detail = f"About {w['per_day']:.1f} trade(s) on an average day{avg}" + (f", mostly {strat}" if strat else "") + "."
        if state == "now" and reading:
            detail += f" Chart now: {reading['label'].lower()} ({reading['score']:+.2f})."
            w_ = hist.get("with") or {}
            a_ = hist.get("against") or {}
            if w_.get("n", 0) >= 15 and a_.get("n", 0) >= 15:
                detail += (f" The bot's signals with the trend have averaged {w_['avg_r']:+.2f}R, against it "
                           f"{a_['avg_r']:+.2f}R.")
        out.append({"title": f"{w['from']}-{w['to']} CT · {w['name']}", "detail": detail, "state": state})
    prog = plan["progress"]
    fin = "done" if prog["state"] in ("goal reached", "stopped", "after hours") else "next"
    end = {"goal reached": "Goal reached: done for the day.", "stopped": f"Stopped: {prog['stopped']}."}.get(
        prog["state"], "Flat by the session end; the day's result goes into tomorrow's plan.")
    out.append({"title": "Finish", "detail": end, "state": fin})
    return out


def headline(plan: dict[str, Any]) -> str:
    goal = plan["goal"]
    prog = plan["progress"]
    if goal["amount"] is None:
        lead = "No goal today: the bot trades by its usual rules."
    else:
        lead = f"Goal {_usd(goal['amount'])} ({goal['text']})"
        if goal.get("capped"):
            lead += f", capped from {_usd(goal['asked'])}"
        lead += f". Made {_usd(prog['made'])} so far" if prog["state"] not in ("before the open",) else "."
        if prog["state"] == "goal reached":
            return lead + " - goal reached, done for the day."
        if prog["state"] not in ("before the open",):
            lead += f", {_usd(max(0.0, prog['left']))} to go."
    o = plan.get("from_now") if prog["state"] == "on the way" else None
    o = o or plan.get("odds")
    if o is None:
        return lead + " Not enough history yet to put odds on it."
    if goal["amount"] is not None and o.get("reach") is not None:
        lead += (f" On {o['reach_n']} of {o['days']} past days ({_pct(o['reach'])}) the bot's signals got there under "
                 f"today's rules{' from where the day stands now' if o is plan.get('from_now') else ''}; "
                 f"{_pct(o['green'])} of days ended green.")
    else:
        lead += f" {_pct(o['green'])} of past days ended green under today's rules."
    return lead


def plan_text(plan: dict[str, Any] | None) -> str:
    """The plan for Telegram and the command line."""
    if not plan:
        return "No day plan yet (the bot isn't running)."
    lines = [f"🎯 Today's plan{(' - ' + plan['account']) if plan.get('account') not in (None, 'main') else ''}",
             plan["headline"]]
    o = plan.get("odds")
    if o and o.get("half_r") is not None and plan["goal"]["amount"] is not None:
        half = o["half_r"] * plan["risk_usd"]
        if half > 0:
            lines.append(f"A goal reached on about half of the past days: {_usd(half)}.")
    for s in plan["steps"]:
        mark = {"done": "✓", "now": "▶", "next": "○"}.get(s["state"], "•")
        lines.append(f"{mark} {s['title']}: {s['detail']}")
    lines.append("Stops: " + " ".join(plan["stops"]))
    reading = (plan.get("trend") or {}).get("reading")
    if reading:
        lines.append("Trend: " + " ".join(reading["words"][:2]))
    lines.append(plan["caveat"])
    return "\n".join(lines)


def brief(plan: dict[str, Any] | None) -> dict[str, Any] | None:
    """The plan in a few fields, for the AI trading assistant."""
    if not plan:
        return None
    g, p = plan["goal"], plan["progress"]
    o = plan.get("from_now") or plan.get("odds") or {}
    return {"goal_usd": g["amount"], "goal_from": g["text"], "made_today_usd": p["made"], "left_usd": p["left"],
            "left_r": p["left_r"], "trades": f"{p['trades']} of {p['max_trades']}", "state": p["state"],
            "stop_at_goal": plan["stop_at_goal"], "chance_of_goal_from_history": o.get("reach"),
            "risk_per_trade_usd": plan["risk_usd"]}


def offline_text(cfg: BotConfig, name: str, day: str) -> str:
    """The goal and suggestion while the bot isn't running (from the settings alone)."""
    frame = frame_from_config(cfg)
    sug = suggest(frame, cfg.day_plan.days_to_target)
    goal = resolve(cfg, load_goals(cfg), day, frame, sug)
    who = f"{name}: " if name and name != "main" else ""
    g = f"{_usd(goal.amount)} ({goal.text})" if goal.amount is not None else goal.text
    s = f"suggested {_usd(sug['suggested'])}" if sug.get("suggested") else sug.get("why", "")
    return f"🎯 {who}goal for {day}: {g}; {s}. The route is planned once the bot is running."


# =============================================================================== the morning question

def entry_for(cfg: BotConfig, account: str, plan: dict[str, Any] | None, day: str) -> dict[str, Any]:
    """One account's line in the morning question: its goal so far and the suggestions (from the running bot's plan,
    else from the settings alone)."""
    if plan is not None and plan.get("day") == day:
        goal, sug = plan["goal"], plan["suggest"]
    else:
        plan = None
        frame = frame_from_config(cfg)
        sug = suggest(frame, cfg.day_plan.days_to_target)
        goal = resolve(cfg, load_goals(cfg), day, frame, sug).to_dict()
    return {"account": account, "stage": cfg.account.stage, "plan": cfg.account.plan, "goal": goal["amount"],
            "kind": goal["kind"], "text": goal["text"], "suggested": sug.get("suggested"), "options": sug.get("options") or [],
            "cap": sug.get("cap"), "why": sug.get("why", ""), "headline": plan.get("headline") if plan else None}


def question(entries: list[dict[str, Any]], *, multi: bool, ask_by: str, unanswered: str) -> tuple[str, dict]:
    """The morning question's text and its buttons (Telegram). Callback data: goal:<account>:<dollars|none|suggested>."""
    lines = ["☀️ Good morning! What's today's profit goal" + (" for each account?" if multi else "?")]
    rows: list[list[dict[str, str]]] = []
    for e in entries:
        who = f"{e['account']} ({e['stage'].capitalize()} {e['plan']})" if multi else f"{e['stage'].capitalize()} {e['plan']}"
        if e["kind"] in ("set", "off"):
            have = _usd(e["goal"]) if e["goal"] is not None else "no goal"
            lines.append(f"• {who}: already set - {have}.")
        else:
            sug = f"suggested {_usd(e['suggested'])} ({e['why']})" if e.get("suggested") else e.get("why") or ""
            cap = f" At most {_usd(e['cap'])} today." if e.get("cap") else ""
            lines.append(f"• {who}: {sug}.{cap}")
        if e.get("suggested") is None and not e["options"]:
            continue
        prefix = f"{e['account']}: " if multi else ""
        row = [{"text": prefix + _usd(o) + (" ✓" if o == e.get("suggested") else ""),
                "callback_data": f"goal:{e['account']}:{o:g}"} for o in e["options"][:3]]
        row.append({"text": prefix + "No goal", "callback_data": f"goal:{e['account']}:none"})
        rows.append(row)
    if multi and any(e.get("suggested") for e in entries):
        rows.append([{"text": "✅ Use the suggestions", "callback_data": "goal:*:suggested"}])
    example = " ".join(f"{e['account']} {e.get('suggested') or 300:g}" for e in entries[:2]) if multi else "400"
    lines.append(f"Tap a goal, or type it (e.g. \"{example}\", or \"none\").")
    fallback = "your last goal (or my suggestion)" if unanswered == "last" else "my suggestion"
    lines.append(f"No answer by {ask_by} CT: I use {fallback}. The goal never makes a trade bigger - every risk rule "
                 "still applies; it only tells me when the day is done. A plan, not a promise.")
    return "\n".join(lines), {"inline_keyboard": rows}
