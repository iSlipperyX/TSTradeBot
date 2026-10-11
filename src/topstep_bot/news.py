"""Economic calendar and headlines for what the bot trades.

Calendar: this week's events from the free Forex Factory JSON feed (fetched at most every few
hours and cached in data/news_cache.json, so it keeps working briefly offline). An event pauses
new entries from ``minutes_before`` to ``minutes_after`` its release time (optionally flattening
just before it) when it is high impact for your currencies - or, with ``news.per_instrument``,
when it is one of the reports that move the instrument you trade, even at medium impact (e.g. the
weekly crude inventories for CL/MCL; see market_knowledge.py).

Headlines: recent news about the instrument from a public RSS feed. They are shown on the
dashboard and in Telegram for your information; the bot does not trade on headlines.
"""

from __future__ import annotations

import email.utils
import json
import logging
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone, tzinfo
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

from topstep_bot.api.parse import parse_ts
from topstep_bot.market_knowledge import event_matters

log = logging.getLogger(__name__)
UTC = timezone.utc


@dataclass(frozen=True)
class NewsEvent:
    title: str
    country: str
    impact: str
    time: datetime  # UTC
    forecast: str = ""
    previous: str = ""

    @property
    def label(self) -> str:
        return f"{self.country} {self.title}"

    def to_dict(self, tz: ZoneInfo) -> dict:
        return {"title": self.title, "country": self.country, "impact": self.impact, "forecast": self.forecast,
                "previous": self.previous, "time": self.time.isoformat(), "local": self.time.astimezone(tz).strftime("%a %H:%M")}


def parse_feed(data: list) -> list[NewsEvent]:
    events = []
    for item in data or []:
        try:
            ts = parse_ts(item["date"])
        except (KeyError, ValueError, TypeError):
            continue
        events.append(NewsEvent(str(item.get("title", "")), str(item.get("country", "")), str(item.get("impact", "")), ts,
                                str(item.get("forecast") or ""), str(item.get("previous") or "")))
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
        symbol: str | None = None,
        per_instrument: bool = False,
    ):
        self.url = url
        self.symbol = symbol
        self.per_instrument = per_instrument and bool(symbol)
        self.cache_path = cache_path
        self.impacts = {i.lower() for i in impacts}
        self.currencies = {c.upper() for c in currencies}
        self.before = timedelta(minutes=minutes_before)
        self.after = timedelta(minutes=minutes_after)
        self.events: list[NewsEvent] = []
        self.fetched_at: datetime | None = None

    def _relevant(self, events: list[NewsEvent]) -> list[NewsEvent]:
        return [e for e in events if self.matters(e)]

    def matters(self, e: NewsEvent) -> bool:
        """Does this event pause trading? High impact for your currencies, or a key report for your instrument."""
        if e.country.upper() not in self.currencies:
            return False
        if e.impact.lower() in self.impacts:
            return True
        return self.per_instrument and e.impact.lower() == "medium" and event_matters(e.title, self.symbol or "")

    def on_day(self, day: date, tz: ZoneInfo) -> list[NewsEvent]:
        """Relevant events releasing on a (Chicago) calendar day."""
        return [e for e in self.events if e.time.astimezone(tz).date() == day]

    def load_cache(self) -> bool:
        try:
            raw = json.loads(self.cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        if not isinstance(raw, dict):
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


# ------------------------------------------------------------------------------ headlines

MAX_FEED_BYTES = 1_000_000


@dataclass(frozen=True)
class Headline:
    title: str
    link: str
    published: str  # ISO time, or "" if the feed doesn't say
    source: str = ""


def parse_rss(data: str | bytes, limit: int = 20) -> list[Headline]:
    """Headlines from an RSS 2.0 document. Refuses DTDs/entities (they can be abused) and non-http links.

    Pass the raw bytes when you can, so the document's own encoding declaration is used.
    """
    raw = data.encode("utf-8") if isinstance(data, str) else data
    if len(raw) > MAX_FEED_BYTES or b"<!DOCTYPE" in raw[:2000].upper() or b"<!ENTITY" in raw.upper():
        raise ValueError("unexpected feed format")
    root = ET.fromstring(raw)
    out = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        if not title or not link.startswith(("https://", "http://")):
            continue
        published = ""
        raw = item.findtext("pubDate")
        if raw:
            try:
                published = email.utils.parsedate_to_datetime(raw).astimezone(UTC).isoformat(timespec="minutes")
            except (TypeError, ValueError):
                published = ""
        out.append(Headline(title[:300], link[:500], published, (item.findtext("source") or "").strip()[:60]))
        if len(out) >= limit:
            break
    return out


class HeadlineFeed:
    """Recent headlines about one market, refreshed every few minutes and cached on disk.

    Several feed URLs can be merged (a quiet market's own feed plus a related one); duplicates and
    anything older than ``max_age_days`` are dropped.
    """

    def __init__(self, urls: str | list[str], cache_path: Path, limit: int = 15, max_age_days: float = 3):
        self.urls = [urls] if isinstance(urls, str) else list(urls)
        self.cache_path = cache_path
        self.limit = limit
        self.max_age = timedelta(days=max_age_days)
        self.items: list[Headline] = []
        self.fetched_at: datetime | None = None

    def load_cache(self) -> bool:
        try:
            raw = json.loads(self.cache_path.read_text(encoding="utf-8"))
            self.items = [Headline(**h) for h in raw.get("items", [])][: self.limit]
            self.fetched_at = parse_ts(raw.get("fetched_at"))
        except (OSError, ValueError, TypeError, AttributeError):
            return False
        return True

    async def refresh(self, transport: httpx.AsyncBaseTransport | None = None) -> bool:
        items: list[Headline] = []
        ok = False
        async with httpx.AsyncClient(timeout=15, transport=transport, follow_redirects=True,
                                     headers={"User-Agent": "Mozilla/5.0 topstep-bot"}) as client:
            for url in self.urls:
                try:
                    resp = await client.get(url)
                    resp.raise_for_status()
                    items += parse_rss(resp.content, self.limit)
                    ok = True
                except (httpx.HTTPError, ValueError, ET.ParseError) as exc:
                    log.info("Headlines unavailable from %s (%s)", url.split("?")[0], exc)
        if not ok:
            return self.load_cache() if not self.items else False
        cutoff = (datetime.now(UTC) - self.max_age).isoformat(timespec="minutes")
        seen: set[str] = set()
        fresh = []
        for h in sorted(items, key=lambda h: h.published, reverse=True):
            key = h.title.lower()
            if key not in seen and (not h.published or h.published >= cutoff):
                seen.add(key)
                fresh.append(h)
        self.items = fresh[: self.limit]
        self.fetched_at = datetime.now(UTC)
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(json.dumps({"fetched_at": self.fetched_at.isoformat(),
                                                   "items": [asdict(h) for h in self.items]}), encoding="utf-8")
        except OSError as exc:
            log.debug("headline cache write failed: %s", exc)
        return True
