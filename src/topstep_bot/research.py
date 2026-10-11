"""Research: the bot's knowledge grows from the internet while it runs.

The controller (one process, whatever the number of accounts) keeps a small research library in
``data/research/`` up to date, from public sources that need no account:

  calendar     every economic calendar it sees is archived. The free feed only covers the current
               week, so the archive is what lets the bot know, months later, which past days had
               market-moving reports - training then labels its replayed signals as news days or
               quiet days (knowledge.py), and the news/quiet comparison grows with every week kept.
  headlines    headlines for every market your accounts trade, archived (deduplicated) so the
               record of what was said about a market builds up next to the trade journal.
  daily        ten years of daily prices for every market (Yahoo Finance). Gives today's
               volatility a long-term context ("today's range is in the top 10% of the last decade")
               on the Market tab.
  rules watch  Topstep's help-center pages for the rules the bot enforces. When one changes, you
               get an alert (dashboard + Telegram) to review it - rules are never changed silently.
  your sources any extra RSS feeds or pages you list under ``research.sources``.

What research does NOT do: it never places, sizes or blocks a trade by itself, and nothing it
reads is treated as an instruction. Information reaches trading decisions only the usual way -
through measured results in the knowledge base, after enough evidence (the adaptive strategy's
gate). Fetched pages are size-limited, parsed as data (no scripts run, RSS without DTDs), and
only the URLs in this file or your config are contacted.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import re
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from topstep_bot.market_knowledge import product_for
from topstep_bot.news import NewsEvent, parse_feed, parse_rss

log = logging.getLogger(__name__)
UTC = timezone.utc
CT = ZoneInfo("America/Chicago")
MAX_PAGE_BYTES = 3_000_000
YAHOO_DAILY = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range=10y&interval=1d"

# The help-center pages behind the rules the bot enforces (risk/topstep.py, docs/TOPSTEP_RULES.md).
RULE_PAGES = {
    "Trading Combine parameters": "https://help.topstep.com/en/articles/8284197-trading-combine-parameters",
    "Maximum Loss Limit": "https://help.topstep.com/en/articles/8284204-what-is-the-maximum-loss-limit",
    "Consistency Target": "https://help.topstep.com/en/articles/8284208-what-is-the-consistency-target",
    "Express Funded Account parameters": "https://help.topstep.com/en/articles/8284215-express-funded-account-parameters",
    "Scaling Plan": "https://help.topstep.com/en/articles/8284223-what-is-the-scaling-plan",
    "Daily Loss Limit": "https://help.topstep.com/en/articles/10490293-daily-loss-limit-in-the-trading-combine-and-express-funded-account",
    "TopstepX API access": "https://help.topstep.com/en/articles/11187768-topstepx-api-access",
    "Prohibited trading strategies": "https://help.topstep.com/en/articles/10305426-prohibited-trading-strategies-at-topstep",
}


def page_text(html: str) -> str:
    """The readable text of a help-center article (or the whole page), without markup or scripts."""
    m = re.search(r"<article[^>]*>(.*?)</article>", html, re.S | re.I)
    body = m.group(1) if m else html
    body = re.sub(r"<(script|style|noscript)[^>]*>.*?</\1>", " ", body, flags=re.S | re.I)
    body = re.sub(r"<[^>]+>", " ", body)
    for entity, char in (("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"), ("&#x27;", "'"), ("&quot;", '"')):
        body = body.replace(entity, char)
    body = re.sub(r"(?i)\b(last )?updated (over |about |almost )?(an?|\d+) \w+ ago\b", " ", body)  # changes by itself
    return re.sub(r"\s+", " ", body).strip()


@dataclass
class DailyBar:
    day: str
    open: float
    high: float
    low: float
    close: float


def parse_yahoo_daily(data: dict) -> list[DailyBar]:
    """Daily bars from Yahoo Finance's chart API (skips incomplete rows)."""
    try:
        res = data["chart"]["result"][0]
        ts, q = res["timestamp"], res["indicators"]["quote"][0]
    except (KeyError, IndexError, TypeError):
        return []
    out = []
    for i, t in enumerate(ts):
        vals = [q[k][i] if i < len(q.get(k) or []) else None for k in ("open", "high", "low", "close")]
        if None in vals:
            continue
        day = datetime.fromtimestamp(t, UTC).astimezone(CT).date().isoformat()
        out.append(DailyBar(day, *(round(float(v), 4) for v in vals)))
    return out


def range_percentile(bars: list[DailyBar], todays_range: float) -> float | None:
    """Share of past days with a smaller high-low range than ``todays_range`` (0..1)."""
    ranges = [b.high - b.low for b in bars if b.high > b.low]
    if len(ranges) < 100 or todays_range <= 0:
        return None
    return sum(1 for r in ranges if r < todays_range) / len(ranges)


class ArchiveCalendar:
    """Read side of the calendar archive: which past days had market-moving reports (for training labels)."""

    def __init__(self, events: list[NewsEvent], covered: set[str], matters: Callable[[NewsEvent], bool]):
        self.events = [e for e in events if matters(e)]
        self.covered = covered  # ISO days the archive has a full calendar for
        self.tz = CT

    def covers(self, day: date) -> bool:
        return day.isoformat() in self.covered

    def on_day(self, day: date, tz: Any = CT) -> list[NewsEvent]:
        return [e for e in self.events if e.time.astimezone(tz).date() == day]


class ResearchLibrary:
    """The files in data/research/ (shared by every account; written only by the controller)."""

    def __init__(self, folder: Path):
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)

    # ---------------------------------------------------------------- calendar

    @property
    def calendar_path(self) -> Path:
        return self.folder / "calendar.json"

    def load_calendar(self) -> dict:
        try:
            return json.loads(self.calendar_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"events": {}, "covered": []}

    def add_calendar(self, feed: list[dict]) -> int:
        """Merge one week's feed into the archive. Returns how many events were new."""
        data = self.load_calendar()
        events: dict[str, dict] = data.get("events", {})
        covered = set(data.get("covered", []))
        new = 0
        days = set()
        for item in feed:
            try:
                parsed = parse_feed([item])[0]
            except IndexError:
                continue
            key = f"{parsed.time.isoformat()}|{parsed.country}|{parsed.title}"
            days.add(parsed.time.astimezone(CT).date())
            if key not in events:
                new += 1
            events[key] = {k: item.get(k) for k in ("title", "country", "date", "impact", "forecast", "previous", "actual")}
        if days:  # the feed is the whole week: Sunday to Saturday around the days it lists
            first, last = min(days), max(days)
            start = first - timedelta(days=(first.weekday() + 1) % 7)
            for i in range(max(7, (last - start).days + 1)):
                covered.add((start + timedelta(days=i)).isoformat())
        self._write(self.calendar_path, {"events": events, "covered": sorted(covered)})
        return new

    def archive_calendar(self, matters: Callable[[NewsEvent], bool]) -> ArchiveCalendar:
        data = self.load_calendar()
        return ArchiveCalendar(parse_feed(list(data.get("events", {}).values())), set(data.get("covered", [])), matters)

    # ---------------------------------------------------------------- headlines

    def headlines_path(self, root: str) -> Path:
        return self.folder / f"headlines_{root}.jsonl"

    def add_headlines(self, root: str, items: list) -> int:
        path = self.headlines_path(root)
        seen = set()
        with contextlib.suppress(OSError):
            for line in path.read_text(encoding="utf-8").splitlines():
                with contextlib.suppress(ValueError):
                    seen.add(json.loads(line).get("link"))
        new = [h for h in items if h.link not in seen]
        if new:
            with open(path, "a", encoding="utf-8") as fh:
                for h in new:
                    fh.write(json.dumps({**asdict(h), "seen": datetime.now(UTC).isoformat(timespec="minutes")}) + "\n")
        return len(new)

    def headline_count(self, root: str) -> int:
        try:
            return sum(1 for _ in self.headlines_path(root).open(encoding="utf-8"))
        except OSError:
            return 0

    # ---------------------------------------------------------------- daily history

    def daily_path(self, root: str) -> Path:
        return self.folder / f"daily_{root}.json"

    def save_daily(self, root: str, bars: list[DailyBar]) -> None:
        self._write(self.daily_path(root), {"saved": datetime.now(UTC).isoformat(timespec="seconds"),
                                            "bars": [asdict(b) for b in bars]})

    def load_daily(self, root: str) -> list[DailyBar]:
        try:
            raw = json.loads(self.daily_path(root).read_text(encoding="utf-8"))
            return [DailyBar(**b) for b in raw.get("bars", [])]
        except (OSError, ValueError, TypeError, AttributeError):
            return []

    # ---------------------------------------------------------------- watched pages

    @property
    def watch_path(self) -> Path:
        return self.folder / "watched_pages.json"

    def load_watch(self) -> dict:
        try:
            return json.loads(self.watch_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def save_watch(self, data: dict) -> None:
        self._write(self.watch_path, data)

    def _write(self, path: Path, data: Any) -> None:
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=0), encoding="utf-8")
        tmp.replace(path)


class Research:
    """The collector the controller runs: fetches what is due, archives it, and reports changes."""

    def __init__(self, cfg, folder: Path, symbols: list[str], *, alert: Callable[[str], None] | None = None,
                 transport: httpx.AsyncBaseTransport | None = None):
        self.cfg = cfg  # BotConfig (research and news sections)
        self.lib = ResearchLibrary(folder)
        self.roots = sorted({p.root for p in (product_for(s) for s in symbols) if p})
        self.alert = alert or (lambda text: None)
        self.transport = transport
        self.last: dict[str, datetime] = {}
        self.results: dict[str, str] = {}  # task -> last outcome, for the dashboard
        self.changes: list[dict] = []  # rule pages that changed since the bot started
        self._status: dict | None = None  # cached between collections

    # ---------------------------------------------------------------- fetching

    async def _get(self, client: httpx.AsyncClient, url: str) -> httpx.Response:
        resp = await client.get(url)
        resp.raise_for_status()
        if len(resp.content) > MAX_PAGE_BYTES:
            raise ValueError("page too large")
        return resp

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=20, transport=self.transport, follow_redirects=True,
                                 headers={"User-Agent": "Mozilla/5.0 topstep-bot research"})

    # ---------------------------------------------------------------- tasks

    async def collect_calendar(self, client: httpx.AsyncClient) -> str:
        resp = await self._get(client, self.cfg.news.url)
        new = self.lib.add_calendar(resp.json())
        data = self.lib.load_calendar()
        return f"{new} new event(s); {len(data['events'])} archived over {len(data['covered'])} days"

    async def collect_headlines(self, client: httpx.AsyncClient) -> str:
        total = 0
        for root in self.roots:
            product = product_for(root)
            for symbol in product.yahoo:
                with contextlib.suppress(httpx.HTTPError, ValueError):
                    resp = await self._get(client, self.cfg.news.headline_url.format(symbol=symbol))
                    total += self.lib.add_headlines(root, parse_rss(resp.content, 50))
        return f"{total} new headline(s) for {', '.join(self.roots) or 'no market'}"

    async def collect_daily(self, client: httpx.AsyncClient) -> str:
        done = []
        for root in self.roots:
            resp = await self._get(client, YAHOO_DAILY.format(symbol=product_for(root).yahoo[0]))
            bars = parse_yahoo_daily(resp.json())
            if bars:
                self.lib.save_daily(root, bars)
                done.append(f"{root} {len(bars)} days")
        return ", ".join(done) or "no data"

    async def watch_pages(self, client: httpx.AsyncClient) -> str:
        pages = dict(RULE_PAGES) if self.cfg.research.rules_watch else {}
        pages.update({s.name: s.url for s in self.cfg.research.sources if s.kind == "page"})
        known = self.lib.load_watch()
        checked, changed = 0, []
        for name, url in pages.items():
            try:
                text = page_text((await self._get(client, url)).text)
            except (httpx.HTTPError, ValueError) as exc:
                log.info("Research: could not read %s (%s)", url, exc)
                continue
            if len(text) < 200:  # a sign-in wall or an error page: don't treat it as a rule change
                continue
            checked += 1
            digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
            old = known.get(url)
            now = datetime.now(UTC).isoformat(timespec="minutes")
            if old and old.get("hash") != digest:
                changed.append(name)
                self.changes.insert(0, {"name": name, "url": url, "at": now})
                known[url] = {**old, "hash": digest, "changed": now, "checked": now, "chars": len(text)}
            else:
                known[url] = {"name": name, "hash": digest, "checked": now, "chars": len(text),
                              "first_seen": (old or {}).get("first_seen", now), "changed": (old or {}).get("changed")}
        self.lib.save_watch(known)
        for name in changed:
            self.alert(f"📏 Topstep changed its help page '{name}'. Please read it - the bot's rules may need updating "
                       f"(the bot doesn't change them by itself). {pages[name]}")
        return f"{checked} page(s) checked" + (f", CHANGED: {', '.join(changed)}" if changed else ", no changes")

    async def collect_sources(self, client: httpx.AsyncClient) -> str:
        total = 0
        for src in self.cfg.research.sources:
            if src.kind != "rss":
                continue
            with contextlib.suppress(httpx.HTTPError, ValueError):
                resp = await self._get(client, src.url)
                total += self.lib.add_headlines(f"source_{src.name}", parse_rss(resp.content, 50))
        return f"{total} new item(s) from your sources"

    # ---------------------------------------------------------------- scheduling

    def schedule(self) -> list[tuple[str, timedelta, Callable]]:
        r = self.cfg.research
        tasks = []
        if r.calendar:
            tasks.append(("calendar", timedelta(hours=6), self.collect_calendar))
        if r.headlines:
            tasks.append(("headlines", timedelta(minutes=r.headline_minutes), self.collect_headlines))
        if r.daily_history:
            tasks.append(("daily history", timedelta(hours=24), self.collect_daily))
        if r.rules_watch or any(s.kind == "page" for s in r.sources):
            tasks.append(("watched pages", timedelta(hours=r.rules_watch_hours), self.watch_pages))
        if any(s.kind == "rss" for s in r.sources):
            tasks.append(("your sources", timedelta(minutes=r.headline_minutes), self.collect_sources))
        return tasks

    async def run_due(self, now: datetime | None = None) -> None:
        now = now or datetime.now(UTC)
        due = [(name, fn) for name, every, fn in self.schedule() if now - self.last.get(name, datetime.min.replace(tzinfo=UTC)) >= every]
        if not due:
            return
        async with self._client() as client:
            for name, fn in due:
                self.last[name] = now
                try:
                    self.results[name] = await fn(client)
                except Exception as exc:  # noqa: BLE001 - one source failing never stops the others
                    self.results[name] = f"failed: {exc}"[:200]
                    log.info("Research %s failed: %s", name, exc)
        self._status = None

    async def run(self, check_every: float = 60.0) -> None:
        while True:
            await self.run_due()
            await asyncio.sleep(check_every)

    def status(self) -> dict:
        if self._status is None:
            self._status = self._build_status()
        return self._status

    def _build_status(self) -> dict:
        cal = self.lib.load_calendar()
        watch = self.lib.load_watch()
        return {
            "calendar": {"events": len(cal.get("events", {})), "days": len(cal.get("covered", []))},
            "headlines": {root: self.lib.headline_count(root) for root in self.roots},
            "daily": {root: len(self.lib.load_daily(root)) for root in self.roots},
            "pages": [{"name": v.get("name"), "url": url, "checked": v.get("checked"), "changed": v.get("changed")}
                      for url, v in watch.items()],
            "changes": self.changes[:10],
            "results": dict(self.results),
        }

    def text(self) -> str:
        st = self.status()
        lines = [f"🔎 Research library: {st['calendar']['events']} calendar events over {st['calendar']['days']} days"]
        for root in self.roots:
            lines.append(f"• {root}: {st['daily'].get(root, 0)} days of daily history, "
                         f"{st['headlines'].get(root, 0)} headlines archived")
        lines.append(f"• Watching {len(st['pages'])} rule page(s)" +
                     (": changed recently - " + ", ".join(c["name"] for c in st["changes"]) if st["changes"] else ", no changes"))
        lines.append("Research is information: it never places or sizes a trade by itself.")
        return "\n".join(lines)
