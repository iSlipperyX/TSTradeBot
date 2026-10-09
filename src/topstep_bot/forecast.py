"""When will the bot trade next? An honest estimate, with what it is based on.

Three things decide when the next automatic trade can come:

1. **The rules.** When the bot may enter at all: its entry window and trading days, blackout
   windows and news, the daily limits (trade count, losing streak, cooldown, loss limits), a pause
   or halt, and one trade at a time. These are exact: ``next_allowed`` walks the same rules the
   bot trades by to the first moment an entry is allowed.
2. **History.** On the past days in the knowledge base, when did the first signal come that the
   bot would take *with what it knows today* (for the adaptive strategy: a strategy the knowledge
   base allows at that time of day and regime)? Starting from the first allowed moment, those days
   give a typical wait, a likely window and the chance of a trade before today's last entry.
3. **The setups forming now** (setups.py): the auto-traded setup closest to firing, what it still
   waits for and, when it has one, the time it can fire (a checkpoint or decision time).

It is an estimate, not a promise: a signal can still be skipped by a risk check, markets change,
and a quiet day can have no trade at all. Every answer says what it is based on.
"""

from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import TYPE_CHECKING, Any

from topstep_bot.knowledge import MANUAL

if TYPE_CHECKING:
    from topstep_bot.engine import TradingCore
    from topstep_bot.knowledge import Observation
    from topstep_bot.sessions import SessionSchedule

UTC = timezone.utc
LOOKBACK_DAYS = 90  # calendar days of signal history the estimate uses
MIN_DAYS = 10  # trading days of history needed before the estimate is given
AHEAD_DAYS = 15  # trading days the estimate looks ahead
SOURCES = ("train", "shadow", "real")
CAVEAT = ("An estimate from the bot's own history, not a promise: a signal can still be skipped by a risk check, "
          "markets change, and some days have no trade at all.")


# --------------------------------------------------------------------------- the rules

def _next_trade_day(schedule: SessionSchedule, day: date) -> date:
    for _ in range(30):  # skips weekends and no-trade dates (holidays)
        day += timedelta(days=1)
        if schedule.is_trade_day(day):
            return day
    return day


def session_clear(schedule: SessionSchedule, t: datetime, days: int = 21) -> datetime | None:
    """The first moment at or after ``t`` when the session rules allow a new entry (None within ``days``)."""
    end = t + timedelta(days=days)
    for _ in range(5000):
        if t >= end:
            return None
        day = schedule.trading_day(t)
        if not schedule.is_trade_day(day):
            t = schedule.entry_window(_next_trade_day(schedule, day))[0]
            continue
        start, last = schedule.entry_window(day)
        if t < start:
            t = start
            continue
        if t >= last:
            t = schedule.entry_window(_next_trade_day(schedule, day))[0]
            continue
        if schedule.in_blackout(t) or schedule.news_blackout(t):
            t = t.replace(second=0, microsecond=0) + timedelta(minutes=1)
            continue
        return t
    return None


def next_allowed(core: TradingCore, now: datetime) -> tuple[datetime | None, str | None]:
    """(earliest moment the bot may open an automatic trade, why not now).

    ``(now, None)``: it may enter right now. ``(None, reason)``: it can't say (paused, halted, at the
    Max Loss Limit cushion, Combine passed); the reason says what has to happen first.
    """
    if core.halted:
        return None, f"the bot is halted ({core.halted}): restart it to trade again"
    guard = core.orders.guard
    if guard is not None and guard.tripped:
        return None, f"the order guard stopped trading ({guard.tripped}): restart the bot to trade again"
    risk, schedule = core.risk, core.schedule
    reason = risk.entry_block_reason(now, core.balance, core.orders.open_pnl())
    if reason is None:
        return now, None
    if reason == "new trades are paused":
        return None, "automatic trades are paused: press Resume (dashboard or /resume)"
    if "Maximum Loss Limit" in reason or "Combine profit target reached" in reason:
        return None, reason
    if risk.lock_reason is None and schedule.entry_block_reason(now) is not None:
        start = now  # only the session rules (window, trading day, blackout, news): walk them forward
    elif reason == "cooling down after a loss" and risk.last_loss_at is not None:
        start = risk.last_loss_at + timedelta(minutes=risk.cfg.cooldown_minutes_after_loss)
    else:  # done for today (limits, losing streak, profit lock): the next trading day starts fresh
        start = schedule.entry_window(_next_trade_day(schedule, schedule.trading_day(now)))[0]
    return session_clear(schedule, start), reason


# --------------------------------------------------------------------------- history

@dataclass
class SignalHistory:
    """First-signal times (minutes after midnight, exchange time) of every past day with data."""

    days: list[date]
    times: dict[date, list[int]]  # sorted signal times per day (only days with a qualifying signal)
    what: str  # which signals count, in words
    signals: int

    @property
    def per_day(self) -> float:
        return self.signals / len(self.days) if self.days else 0.0


def _minutes(hhmm: str) -> int | None:
    try:
        h, m = hhmm.split(":")[:2]
        return int(h) * 60 + int(m)
    except (ValueError, AttributeError):
        return None


def signal_history(core: TradingCore, today: date) -> SignalHistory | None:
    """The signals the bot would take today, on each past day in the knowledge base (cached per change)."""
    kb = core.knowledge
    if kb is None:
        return None
    strat = core.strategy
    key = (id(kb), kb.updated, len(kb.obs), today, strat.name, id(strat))
    cached = getattr(core, "_signal_history", None)
    if cached and cached[0] == key:
        return cached[1]

    cfg = core.schedule.cfg
    lo, hi = cfg.trade_start.hour * 60 + cfg.trade_start.minute, cfg.last_entry.hour * 60 + cfg.last_entry.minute
    blackouts = [(w.start.hour * 60 + w.start.minute, w.end.hour * 60 + w.end.minute) for w in cfg.blackout_windows]
    first = (today - timedelta(days=LOOKBACK_DAYS)).isoformat()
    subs = getattr(strat, "subs", None)
    takes = getattr(strat, "would_take", None)
    verdicts: dict[tuple[str, str, str], bool] = {}

    def counts(o: Observation) -> bool:
        if subs is None:
            return o.strategy == strat.name
        if takes is None:
            return False
        k = (o.strategy, o.slot, o.regime)
        if k not in verdicts:
            verdicts[k] = o.strategy in {s.name for s in subs} and takes(o.strategy, o.slot, o.regime, today)
        return verdicts[k]

    days: set[date] = set()
    times: dict[date, list[int]] = {}
    n = 0
    for o in kb.obs:
        if o.source not in SOURCES or o.strategy == MANUAL or not first <= o.day < today.isoformat():
            continue
        try:
            d = date.fromisoformat(o.day)
        except ValueError:
            continue
        if not core.schedule.is_trade_day(d):
            continue
        days.add(d)  # a day the bot has data for, whether or not it would have traded
        m = _minutes(o.time)
        if m is None or not lo <= m < hi or any(a <= m < b for a, b in blackouts) or not counts(o):
            continue
        times.setdefault(d, []).append(m)
        n += 1
    for v in times.values():
        v.sort()
    if subs is None:
        what = f"{strat.title} signals"
    else:
        what = "signals from the strategies the knowledge base lets the Adaptive strategy trade at that time of day and regime"
    hist = SignalHistory(sorted(days), times, what, n)
    core._signal_history = (key, hist)
    return hist


def estimate(hist: SignalHistory, schedule: SessionSchedule, start: datetime) -> dict[str, Any] | None:
    """Typical wait from ``start`` (a moment the bot may enter) to the first signal, from past days.

    Today: each past day counts as one way today could go, with its first signal after ``start``'s time
    of day. Days with none after that pass the wait on to the next trading days, which signal with
    the history's share of signal days at its first-signal times.
    """
    days = hist.days
    if len(days) < MIN_DAYS:
        return None
    n = len(days)
    local = schedule.local(start)
    m0 = local.hour * 60 + local.minute
    day0 = schedule.trading_day(start)
    firsts = [hist.times[d][0] for d in days if d in hist.times]
    after = []
    for d in days:
        ts = hist.times.get(d)
        if ts:
            i = bisect_left(ts, m0)
            if i < len(ts):
                after.append(ts[i])
    p_today = len(after) / n
    q = len(firsts) / n
    points: list[tuple[datetime, float]] = [(_at(schedule, day0, m), 1 / n) for m in after]
    rest, day = 1.0 - p_today, day0
    for _ in range(AHEAD_DAYS):
        if rest < 1e-4 or not firsts:
            break
        day = _next_trade_day(schedule, day)
        points += [(_at(schedule, day, m), rest / n) for m in firsts]
        rest *= 1.0 - q
    points.sort(key=lambda p: p[0])

    def quantile(level: float) -> datetime | None:
        acc = 0.0
        for at, w in points:
            acc += w
            if acc >= level - 1e-9:
                return max(at, start)
        return None

    mid = quantile(0.5)
    if mid is None:
        return {"at": None, "p_today": round(p_today, 3), "q": round(q, 3), "days": n, "per_day": round(hist.per_day, 2)}
    low, high = quantile(0.25), quantile(0.75)
    return {"at": mid, "low": low, "high": high, "p_today": round(p_today, 3), "q": round(q, 3), "days": n,
            "per_day": round(hist.per_day, 2)}


def _at(schedule: SessionSchedule, day: date, minutes: int) -> datetime:
    return schedule.at(day, time(minutes // 60, minutes % 60))


# --------------------------------------------------------------------------- putting it together

def _when(schedule: SessionSchedule, now: datetime, at: datetime) -> str:
    """'10:45 CT' today, else 'Mon 10:45 CT'."""
    local, ref = schedule.local(at), schedule.local(now)
    return local.strftime("%H:%M CT") if local.date() == ref.date() else local.strftime("%a %H:%M CT")


def wait_text(seconds: float) -> str:
    """'now', '8 min', '1 h 05 min', '2 days 3 h'."""
    if seconds < 60:
        return "now"
    minutes = int(seconds // 60)
    d, h, m = minutes // 1440, minutes % 1440 // 60, minutes % 60
    if d:
        return f"{d} day{'s' if d > 1 else ''}" + (f" {h} h" if h else "")
    return f"{h} h {m:02d} min" if h else f"{m} min"


def _nearest_setup(core: TradingCore, now: datetime) -> dict[str, Any] | None:
    """The auto-traded setup closest to firing (most conditions met), with when it can fire."""
    try:
        view = core.setups.view()
    except Exception:  # noqa: BLE001 - informational only
        return None
    bot = [i for i in view["items"] if i["role"] == "bot"]
    if not bot:
        return None
    best = max(bot, key=lambda i: (i["progress"], -len(i["conditions"])))
    todo = [c["text"] for c in best["conditions"] if not c["met"]]
    tf = core.tf
    epoch = datetime(2000, 1, 1, tzinfo=UTC)
    next_close = epoch + ((now - epoch) // tf + 1) * tf
    at = None
    if best.get("at"):
        local = core.schedule.local(now)
        h, m = (int(x) for x in best["at"].split(":"))
        at = local.replace(hour=h, minute=m, second=0, microsecond=0).astimezone(UTC)
        if at < now:
            at = None
    fires = at or next_close
    return {"title": best["title"], "side": best["side"], "met": best["met"], "total": best["total"], "waiting_for": todo[:2],
            "at": fires.isoformat(), "at_local": _when(core.schedule, now, fires), "fixed_time": at is not None,
            "entry": best.get("entry"), "price": view.get("price")}


def forecast(core: TradingCore, now: datetime | None = None) -> dict[str, Any]:
    """Everything the dashboard, Telegram and the menu say about the next trade."""
    now = now or core.clock()
    schedule = core.schedule
    today = schedule.trading_day(now)
    out: dict[str, Any] = {"now": now.isoformat(), "state": "waiting", "headline": "", "basis": [], "caveat": CAVEAT,
                           "earliest": None, "earliest_local": None, "blocked": None, "estimate": None, "setup": None}
    basis: list[str] = out["basis"]

    t = core.orders.trade
    if t is not None or core.orders.position:
        out["state"] = "in_trade"
        out["headline"] = "In a trade now. The bot takes one trade at a time; the next can come after this one closes."
        return out

    earliest, blocked = next_allowed(core, now)
    out["blocked"] = blocked
    if earliest is None:
        out["state"] = "blocked"
        out["headline"] = f"No automatic trade until this changes: {blocked}."
        return out
    out["earliest"], out["earliest_local"] = earliest.astimezone(UTC).isoformat(), _when(schedule, now, earliest)
    if earliest > now:
        out["state"] = "closed"
        basis.append(f"The rules allow the next entry at {out['earliest_local']} (in {wait_text((earliest - now).total_seconds())}): {blocked}.")

    hist = signal_history(core, today)
    est = estimate(hist, schedule, earliest) if hist else None
    if hist is None:
        basis.append("The knowledge base is turned off, so there is no history to estimate from.")
    elif est is None:
        basis.append(f"Not enough history yet: {len(hist.days)} trading day(s) in the knowledge base, {MIN_DAYS} needed. "
                     "Retrain it (Knowledge tab, /train) or let the bot run.")
    else:
        e = {k: v for k, v in est.items() if not isinstance(v, datetime)}
        for k in ("at", "low", "high"):
            v = est.get(k)
            if v is not None:
                e[k], e[k + "_local"] = v.astimezone(UTC).isoformat(), _when(schedule, now, v)
        out["estimate"] = e
        basis.append(f"History: {est['days']} trading days in the knowledge base. Counted: {hist.what}, inside the bot's "
                     f"entry window ({schedule.cfg.trade_start:%H:%M}-{schedule.cfg.last_entry:%H:%M} CT): "
                     f"{est['per_day']:.1f} a day on average; {round(100 * est['q'])}% of days had at least one.")
        if earliest.date() == now.date() or schedule.trading_day(earliest) == today:
            basis.append(f"On {round(100 * est['p_today'])}% of those days one came after "
                         f"{schedule.local(earliest):%H:%M} CT: that is the chance of a trade before today's last entry.")

    setup = _nearest_setup(core, now) if out["state"] != "closed" else None
    if setup:
        out["setup"] = setup
        left = (", waiting for: " + "; ".join(setup["waiting_for"])) if setup["waiting_for"] else ""
        when = f"can fire at {setup['at_local']}" if setup["fixed_time"] else "is judged at every bar close"
        basis.append(f"Closest setup the bot would trade: {setup['title']} {setup['side']}, {setup['met']} of {setup['total']} "
                     f"conditions met{left}. It {when}.")

    # The headline: the most useful single sentence.
    if est is None or est.get("at") is None:
        if earliest > now:
            out["headline"] = f"No trade before {out['earliest_local']} (in {wait_text((earliest - now).total_seconds())})."
        elif est is not None:
            out["headline"] = "No signal the bot would take is likely in the next few weeks, going by its history."
        else:
            out["headline"] = "The bot is watching for a signal, but has too little history to say when one is likely."
        if setup and setup["met"] == setup["total"] - 1 and out["state"] != "closed":
            out["state"] = "ready"
            out["headline"] = f"A setup is one condition away: {setup['title']} {setup['side']} {when.replace('is judged', 'judged')}."
        return out
    at = est["at"]
    wait = wait_text((at - now).total_seconds())
    rng = ""
    if est.get("low") and est.get("high") and est["high"] > est["low"]:
        rng = f", usually between {_when(schedule, now, est['low'])} and {_when(schedule, now, est['high'])}"
    lead = f"No trade before {out['earliest_local']}. " if earliest > now else ""
    out["headline"] = f"{lead}Next trade: most likely around {_when(schedule, now, at)} (in about {wait}){rng}."
    if setup and setup["met"] == setup["total"] - 1 and out["state"] != "closed":
        out["state"] = "ready"
        out["headline"] += f" A setup is one condition away: {setup['title']} {setup['side']}."
    return out


def forecast_text(f: dict[str, Any]) -> str:
    """Plain text for Telegram and the command line."""
    lines = ["⏳ " + f["headline"]]
    lines += [f"• {b}" for b in f["basis"]]
    if f["state"] not in ("in_trade", "blocked"):
        lines.append(f["caveat"])
    return "\n".join(lines)
