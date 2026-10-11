"""The research library: what the bot gathers from the internet, against fake servers."""

import json
from datetime import date, datetime, timedelta, timezone

import httpx

from topstep_bot.config import BotConfig
from topstep_bot.knowledge import KnowledgeBase
from topstep_bot.market_intel import MarketTracker
from topstep_bot.news import NewsCalendar
from topstep_bot.research import (
    RULE_PAGES,
    DailyBar,
    Research,
    ResearchLibrary,
    page_text,
    parse_yahoo_daily,
    range_percentile,
)

from .conftest import ct, run

UTC = timezone.utc
WEEK = [
    {"title": "CPI m/m", "country": "USD", "date": "2026-03-04T08:30:00-05:00", "impact": "High", "forecast": "0.3%"},
    {"title": "Crude Oil Inventories", "country": "USD", "date": "2026-03-04T10:30:00-05:00", "impact": "Medium"},
    {"title": "ECB Speech", "country": "EUR", "date": "2026-03-05T08:00:00-05:00", "impact": "High"},
]
ARTICLE = "<html><script>var x=1</script><article><h1>Scaling Plan</h1><p>50K: 2 minis below $1,500.</p>{extra}" \
          "<p>Updated over a week ago</p>" + "<p>Lots of words about the rule. </p>" * 20 + "</article></html>"
RSS = ('<?xml version="1.0"?><rss><channel><item><title>{t}</title><link>https://n.example/{t}</link>'
       "<pubDate>{d}</pubDate></item></channel></rss>")


def yahoo(n=300, start=date(2016, 1, 4)):
    ts, o, h, lo, c = [], [], [], [], []
    for i in range(n):
        day = datetime(start.year, start.month, start.day, 15, tzinfo=UTC) + timedelta(days=i)
        ts.append(int(day.timestamp()))
        o.append(100.0)
        h.append(100.0 + 1 + i % 10)
        lo.append(100.0)
        c.append(101.0)
    c[5] = None  # an incomplete row is skipped
    return {"chart": {"result": [{"timestamp": ts, "indicators": {"quote": [{"open": o, "high": h, "low": lo, "close": c}]}}]}}


class FakeWeb:
    def __init__(self):
        self.page_extra = ""
        self.calls: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.calls.append(url)
        if "ff_calendar" in url:
            return httpx.Response(200, json=WEEK)
        if "finance/chart" in url:
            return httpx.Response(200, json=yahoo())
        if "help.topstep.com" in url:
            return httpx.Response(200, text=ARTICLE.replace("{extra}", self.page_extra))
        if "feeds.finance.yahoo.com" in url or "my-feed" in url:
            title = "Crude rallies" if "CL" in url or "USO" in url else "Other news"
            return httpx.Response(200, content=RSS.format(t=title, d="Mon, 02 Mar 2026 10:00:00 +0000").encode())
        return httpx.Response(404)


def make(tmp_path, symbols=("MCL",), **research):
    cfg = BotConfig.model_validate({"research": research})
    web = FakeWeb()
    alerts: list[str] = []
    r = Research(cfg, tmp_path / "research", list(symbols), alert=alerts.append, transport=httpx.MockTransport(web.handler))
    return r, web, alerts


def test_page_text_ignores_markup_scripts_and_relative_dates():
    text = page_text(ARTICLE.replace("{extra}", ""))
    assert text.startswith("Scaling Plan 50K: 2 minis below $1,500.") and "var x" not in text and "ago" not in text


def test_calendar_archive_grows_and_labels_past_days(tmp_path):
    lib = ResearchLibrary(tmp_path)
    assert lib.add_calendar(WEEK) == 3 and lib.add_calendar(WEEK) == 0  # the same week again adds nothing
    data = lib.load_calendar()
    assert len(data["covered"]) == 7 and "2026-03-01" in data["covered"] and "2026-03-07" in data["covered"]
    rules = NewsCalendar("u", tmp_path / "x.json", ["High"], ["USD"], 5, 10, symbol="MCL", per_instrument=True)
    archive = lib.archive_calendar(rules.matters)
    assert [e.title for e in archive.on_day(date(2026, 3, 4))] == ["CPI m/m", "Crude Oil Inventories"]
    assert archive.on_day(date(2026, 3, 5)) == []  # only a EUR event: quiet for MCL
    assert archive.covers(date(2026, 3, 5)) and not archive.covers(date(2026, 2, 20))


def test_yahoo_daily_history_and_percentiles():
    bars = parse_yahoo_daily(yahoo())
    assert len(bars) == 299 and isinstance(bars[0], DailyBar)
    assert range_percentile(bars, 0.5) == 0.0 and range_percentile(bars, 100) == 1.0
    assert range_percentile(bars[:50], 5) is None  # too little history to say
    assert parse_yahoo_daily({"chart": {"result": None}}) == []


def test_collector_runs_every_task_and_archives(tmp_path):
    r, web, alerts = make(tmp_path, sources=[{"name": "mine", "url": "https://my-feed.example/rss", "kind": "rss"}])
    run(r.run_due(datetime(2026, 3, 4, tzinfo=UTC)))
    st = r.status()
    assert st["calendar"] == {"events": 3, "days": 7}
    assert st["daily"]["CL"] == 299 and st["headlines"]["CL"] == 1  # CL=F and USO give the same story once
    assert len(st["pages"]) == len(RULE_PAGES) and not st["changes"] and not alerts
    assert r.lib.headline_count("source_mine") == 1
    assert "no changes" in st["results"]["watched pages"] and "Research library" in r.text()
    # nothing is due again a minute later: no new requests
    n = len(web.calls)
    run(r.run_due(datetime(2026, 3, 4, 0, 1, tzinfo=UTC)))
    assert len(web.calls) == n


def test_a_changed_rule_page_raises_an_alert_once(tmp_path):
    r, web, alerts = make(tmp_path, calendar=False, headlines=False, daily_history=False)
    t0 = datetime(2026, 3, 4, tzinfo=UTC)
    run(r.run_due(t0))
    assert not alerts  # the first look is the baseline
    web.page_extra = "<p>NEW: 50K now starts at 3 minis.</p>"
    run(r.run_due(t0 + timedelta(hours=13)))
    assert len(alerts) == len(RULE_PAGES) and "changed its help page" in alerts[0]
    assert r.status()["changes"] and json.loads(r.lib.watch_path.read_text())  # remembered
    alerts.clear()
    run(r.run_due(t0 + timedelta(hours=26)))
    assert not alerts  # unchanged since: no repeat


def test_one_failing_source_does_not_stop_the_others(tmp_path):
    r, web, _ = make(tmp_path)
    original = web.handler

    def flaky(request):
        return httpx.Response(500) if "ff_calendar" in str(request.url) else original(request)

    r.transport = httpx.MockTransport(flaky)
    run(r.run_due(datetime(2026, 3, 4, tzinfo=UTC)))
    assert r.results["calendar"].startswith("failed") and r.status()["daily"]["CL"] == 299


def test_training_labels_news_days_from_the_archive(tmp_path, mnq):
    """Replayed history gets news-day labels where the archive reaches; earlier days stay unknown."""
    from topstep_bot.backtest.data import synthetic_bars
    from topstep_bot.knowledge import train_from_bars

    lib = ResearchLibrary(tmp_path)
    bars = synthetic_bars("MNQ", days=40, seed=3)
    last = max(b.ts for b in bars).astimezone(ct(2026, 1, 1, 0, 0).tzinfo)
    week = [{**e, "date": (last - timedelta(days=2)).replace(hour=8, minute=30).isoformat()} for e in WEEK[:1]]
    lib.add_calendar(week)
    rules = NewsCalendar("u", tmp_path / "x.json", ["High"], ["USD"], 5, 10, symbol="MNQ", per_instrument=True)
    kb = KnowledgeBase(None)
    run(train_from_bars(BotConfig(), mnq, bars, kb, news_archive=lib.archive_calendar(rules.matters)))
    labels = {o.news for o in kb.obs}
    assert None in labels and (True in labels or False in labels)  # covered days are labelled, the rest unknown
    covered = set(lib.load_calendar()["covered"])
    assert all(o.news is None for o in kb.obs if o.day not in covered)


def test_market_tab_gets_long_term_context(tmp_path, mnq):
    from topstep_bot.config import SessionConfig
    from topstep_bot.sessions import SessionSchedule

    tracker = MarketTracker("MNQ", mnq, SessionSchedule(SessionConfig()))
    tracker.daily = parse_yahoo_daily(yahoo())
    f = tracker.facts()
    assert f["history_days"] == 299 and f["atr_pct"] is None  # no live sessions yet: no recent range to compare
