"""Economic calendar: automatic no-trade windows around high-impact news.

Uses this week's calendar from the free Forex Factory JSON feed (fetched at most every few
hours and cached in data/news_cache.json, so it keeps working briefly offline: a copy more than
12 hours old, or from an earlier week, counts as no calendar at all). Each event
blocks new entries from ``minutes_before`` to ``minutes_after`` its release time; optionally
open trades are flattened just before the release.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone, tzinfo
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

from topstep_bot.api.parse import parse_ts

log = logging.getLogger(__name__)
UTC = timezone.utc
# The feed holds one Forex Factory week (Sunday to Saturday, New York time). A copy older than
# MAX_AGE, or from an earlier week, may be missing this week's releases.
FEED_TZ = ZoneInfo("America/New_York")
MAX_AGE = timedelta(hours=12)


def feed_week(ts: datetime) -> date:
    """The Sunday (New York time) that starts the Forex Factory week ``ts`` falls in."""
    local = ts.astimezone(FEED_TZ)
    return local.date() - timedelta(days=(local.weekday() + 1) % 7)


@dataclass(frozen=True)
class NewsEvent:
    title: str
    country: str
    impact: str
    time: datetime  # UTC

    @property
    def label(self) -> str:
        return f"{self.country} {self.title}"


def parse_feed(data: list) -> list[NewsEvent]:
    events = []
    for item in data or []:
        try:
            ts = parse_ts(item["date"])
        except (KeyError, ValueError, TypeError):
            continue
        events.append(NewsEvent(str(item.get("title", "")), str(item.get("country", "")), str(item.get("impact", "")), ts))
    return sorted(events, key=lambda e: e.time)


class NewsCalendar:
    def __init__(
        self,
        url: str,
        cache_path: Path,
        impacts: list[str],
        currencies: list[str],
        minutes_before: int,
        minutes_after: int,
    ):
        self.url = url
        self.cache_path = cache_path
        self.impacts = {i.lower() for i in impacts}
        self.currencies = {c.upper() for c in currencies}
        self.before = timedelta(minutes=minutes_before)
        self.after = timedelta(minutes=minutes_after)
        self.events: list[NewsEvent] = []
        self.fetched_at: datetime | None = None

    def is_current(self, now: datetime) -> bool:
        """True when the calendar was downloaded recently enough to list this week's releases."""
        return (self.fetched_at is not None and now - self.fetched_at <= MAX_AGE
                and feed_week(self.fetched_at) == feed_week(now))

    def _relevant(self, events: list[NewsEvent]) -> list[NewsEvent]:
        return [e for e in events if e.impact.lower() in self.impacts and e.country.upper() in self.currencies]

    def load_cache(self) -> bool:
        try:
            raw = json.loads(self.cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        self.events = self._relevant(parse_feed(raw.get("events", [])))
        self.fetched_at = parse_ts(raw.get("fetched_at"))
        return True

    async def refresh(self, transport: httpx.AsyncBaseTransport | None = None) -> bool:
        """Download the calendar; on failure keep (or load) the cached copy."""
        try:
            async with httpx.AsyncClient(timeout=15, transport=transport, headers={"User-Agent": "topstep-bot"}) as client:
                resp = await client.get(self.url)
                resp.raise_for_status()
                data = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("Economic calendar unavailable (%s); using cached copy if any", exc)
            return self.load_cache() if not self.events else False
        self.events = self._relevant(parse_feed(data))
        self.fetched_at = datetime.now(UTC)
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(json.dumps({"fetched_at": self.fetched_at.isoformat(), "events": data}), encoding="utf-8")
        except OSError as exc:
            log.debug("news cache write failed: %s", exc)
        log.info("Economic calendar loaded: %d relevant event(s) this week", len(self.events))
        return True

    def blackout_reason(self, ts: datetime, tz: tzinfo | None = None) -> str | None:
        """Why ``ts`` is inside a news blackout (release time shown in ``tz``, e.g. Chicago), or None."""
        for e in self.events:
            if e.time - self.before <= ts < e.time + self.after:
                when = f"{e.time.astimezone(tz):%H:%M} CT" if tz else f"{e.time:%H:%M} UTC"
                return f"news blackout: {e.label} at {when}"
        return None

    def releasing_soon(self, ts: datetime) -> NewsEvent | None:
        """An event whose release is within the 'before' window (used to flatten ahead of news)."""
        for e in self.events:
            if e.time - self.before <= ts < e.time:
                return e
        return None

    def upcoming(self, ts: datetime, hours: int = 24) -> list[NewsEvent]:
        end = ts + timedelta(hours=hours)
        return [e for e in self.events if ts - self.after <= e.time <= end]
