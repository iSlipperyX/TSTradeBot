"""Trading-session calendar: CME trading days, Topstep's flat-by deadline, entry windows.

A CME/Topstep trading day runs from 17:00 CT (previous evening) to 16:00 CT; Topstep
requires every position to be closed by 15:10 CT. A trading day is labelled by the
calendar date it ends on (Sunday 17:00 CT belongs to Monday's trading day).
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from topstep_bot.config import SessionConfig

UTC = timezone.utc
SESSION_OPEN = time(17, 0)


def to_local(ts: datetime, tz: ZoneInfo) -> datetime:
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return ts.astimezone(tz)


def trading_day_of(ts: datetime, tz: ZoneInfo) -> date:
    """Trading day a timestamp belongs to (rolls over at 17:00 local, weekends map to Monday)."""
    local = to_local(ts, tz)
    day = local.date()
    if local.time() >= SESSION_OPEN:
        day += timedelta(days=1)
    while day.weekday() >= 5:  # Saturday/Sunday sessions belong to Monday
        day += timedelta(days=1)
    return day


def session_open_for(day: date, tz: ZoneInfo) -> datetime:
    """17:00 local on the evening before (Sunday evening for Monday)."""
    return datetime.combine(day - timedelta(days=1), SESSION_OPEN, tzinfo=tz)


class SessionSchedule:
    """Answers 'may I enter now?', 'must I be flat now?' for a given SessionConfig."""

    def __init__(self, cfg: SessionConfig):
        self.cfg = cfg
        self.tz = ZoneInfo(cfg.timezone)
        self._no_trade = set(cfg.no_trade_dates)
        self.news = None  # optional NewsCalendar with automatic news blackouts

    def local(self, ts: datetime) -> datetime:
        return to_local(ts, self.tz)

    def trading_day(self, ts: datetime) -> date:
        return trading_day_of(ts, self.tz)

    def is_trade_day(self, day: date) -> bool:
        return day.weekday() in self.cfg.trade_weekdays and day not in self._no_trade

    def at(self, day: date, t: time) -> datetime:
        return datetime.combine(day, t, tzinfo=self.tz)

    def entry_window(self, day: date) -> tuple[datetime, datetime]:
        return self.at(day, self.cfg.trade_start), self.at(day, self.cfg.last_entry)

    def flatten_time(self, day: date) -> datetime:
        return self.at(day, self.cfg.flatten_at)

    def in_blackout(self, ts: datetime) -> str | None:
        local = self.local(ts).time()
        for w in self.cfg.blackout_windows:
            if w.start <= local < w.end:
                return w.label or f"blackout {w.start:%H:%M}-{w.end:%H:%M}"
        return None

    def entry_block_reason(self, ts: datetime) -> str | None:
        """None if a new entry is allowed at ts, otherwise a human-readable reason."""
        day = self.trading_day(ts)
        if not self.is_trade_day(day):
            return f"{day} is not a configured trading day"
        start, last = self.entry_window(day)
        local = self.local(ts)
        if local < start:
            return f"before trade_start {self.cfg.trade_start:%H:%M}"
        if local >= last:
            return f"after last_entry {self.cfg.last_entry:%H:%M}"
        reason = self.in_blackout(ts)
        if reason is None and self.news is not None:
            reason = self.news.blackout_reason(ts, self.tz)
        return reason

    def must_be_flat(self, ts: datetime) -> bool:
        """True from flatten_at until the next session opens at 17:00."""
        local = self.local(ts)
        day = self.trading_day(ts)
        return local >= self.flatten_time(day) and local.date() == day

    def market_open(self, ts: datetime) -> bool:
        """CME Globex hours: Sunday 17:00 to Friday 16:00 CT, with a daily 16:00-17:00 halt."""
        local = self.local(ts)
        wd, t = local.weekday(), local.time()
        if wd == 5:
            return False
        if wd == 6:
            return t >= SESSION_OPEN
        if time(16, 0) <= t < SESSION_OPEN:
            return False
        return not (wd == 4 and t >= time(16, 0))

    def is_rth(self, ts: datetime, open_: time, close: time) -> bool:
        t = self.local(ts).time()
        return open_ <= t < close

    # ------------------------------------------------------------------ market clock (dashboard timers)

    def next_market_open(self, ts: datetime) -> datetime:
        """The next time CME Globex reopens after ``ts`` (17:00 CT; Sunday evening after a weekend)."""
        local = self.local(ts)
        candidate = datetime.combine(local.date(), SESSION_OPEN, tzinfo=self.tz)
        while candidate <= local or not self.market_open(candidate):
            candidate += timedelta(days=1)
        return candidate

    def _next_day(self, day: date, ok) -> date:
        for _ in range(14):  # skips weekends and no-trade dates (holidays)
            day += timedelta(days=1)
            if ok(day):
                return day
        return day

    def clock(self, now: datetime, rth: tuple[time, time] = (time(8, 30), time(15, 0)), symbol: str = "") -> dict:
        """When the market and the bot's day open and close next, for the dashboard's countdowns.

        Every time comes from the same rules the bot trades by: the Globex session (17:00-16:00 CT),
        the entry window and flatten time in config.yaml, Topstep's 15:10 CT flat-by deadline, and
        ``no_trade_dates`` (holidays) / ``trade_weekdays`` for the days the bot doesn't trade.
        ``rth`` is the traded instrument's regular hours (index futures follow the stock market,
        08:30-15:00 CT; crude oil and gold have their own). Returns ISO times in UTC plus the
        Chicago wall-clock time of each event.
        """
        from topstep_bot.risk.topstep import FLAT_BY

        local = self.local(now)
        day = self.trading_day(now)
        weekday = lambda d: d.weekday() < 5  # noqa: E731

        def event(key: str, label: str, at: datetime, detail: str = "") -> dict:
            at_local = self.local(at)
            when = at_local.strftime("%H:%M CT") if at_local.date() == local.date() else at_local.strftime("%a %H:%M CT")
            return {"key": key, "label": label, "at": at.astimezone(UTC).isoformat(), "local": when, "detail": detail}

        events = []
        if self.market_open(now):
            close = self.at(day, time(16, 0))
            events.append(event("market", "Market closes", close,
                                "weekend close until Sunday 17:00 CT" if day.weekday() == 4 else "daily break until 17:00 CT"))
        else:
            events.append(event("market", "Market opens", self.next_market_open(now), "CME Globex session (17:00-16:00 CT)"))

        rth_day = day if weekday(day) else self._next_day(day, weekday)
        rth_open, rth_close = self.at(rth_day, rth[0]), self.at(rth_day, rth[1])
        if local >= rth_close:
            rth_day = self._next_day(rth_day, weekday)
            rth_open, rth_close = self.at(rth_day, rth[0]), self.at(rth_day, rth[1])
        hours = f"{(symbol + ' ') if symbol else ''}regular hours {rth[0]:%H:%M}-{rth[1]:%H:%M} CT"
        if local < rth_open:
            events.append(event("rth", "Regular hours open", rth_open, f"{hours}, the busiest hours"))
        else:
            events.append(event("rth", "Regular hours close", rth_close, hours))

        trade_day = day if self.is_trade_day(day) else self._next_day(day, self.is_trade_day)
        start, last = self.entry_window(trade_day)
        flatten = self.flatten_time(trade_day)
        if local >= flatten:
            trade_day = self._next_day(trade_day, self.is_trade_day)
            start, last = self.entry_window(trade_day)
            flatten = self.flatten_time(trade_day)
        if local < start:
            events.append(event("entries", "Bot starts trading", start, f"first entry at {self.cfg.trade_start:%H:%M} CT"))
        elif local < last:
            events.append(event("entries", "Last new entry", last, f"no new trades after {self.cfg.last_entry:%H:%M} CT"))
        events.append(event("flatten", "Bot closes all trades", flatten,
                            f"session flatten; Topstep requires flat by {FLAT_BY:%H:%M} CT"))
        flat_by_day = day if weekday(day) else self._next_day(day, weekday)
        flat_by = self.at(flat_by_day, FLAT_BY)
        if local >= flat_by:
            flat_by = self.at(self._next_day(flat_by_day, weekday), FLAT_BY)
        events.append(event("topstep", "Topstep flat-by", flat_by, "Topstep closes any open position from 15:08 CT"))

        holiday = None
        if weekday(day) and not self.is_trade_day(day):
            holiday = (f"{day:%a %d %b} is not a trading day for the bot (no_trade_dates / trade_weekdays). "
                       "CME may still be open: check its holiday calendar.")
        return {
            "now": now.astimezone(UTC).isoformat(), "local": local.strftime("%a %H:%M:%S CT"),
            "trading_day": day.isoformat(), "market_open": self.market_open(now),
            "in_entry_window": self.entry_block_reason(now) is None and self.is_trade_day(day),
            "holiday": holiday, "events": events,
        }
