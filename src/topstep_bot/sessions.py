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
            reason = self.news.blackout_reason(ts)
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
