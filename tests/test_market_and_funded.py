"""Stored API keys, market intelligence (levels, news, headlines), funded-account goals and rules."""

import json
import logging
from datetime import date, timedelta

import httpx
import pytest

from topstep_bot import funded
from topstep_bot.broker.paper import PaperBroker
from topstep_bot.config import BotConfig, FundedConfig, Secrets, load_secrets
from topstep_bot.control import BotActions
from topstep_bot.factory import build_core
from topstep_bot.journal import Journal
from topstep_bot.knowledge import KnowledgeBase, Observation
from topstep_bot.live import Controls
from topstep_bot.logging_setup import RedactingFilter
from topstep_bot.market_intel import MarketTracker
from topstep_bot.market_knowledge import event_matters, product_for
from topstep_bot.news import HeadlineFeed, NewsCalendar, NewsEvent, parse_feed, parse_rss
from topstep_bot.preflight import WARN, run_preflight
from topstep_bot.risk.topstep import PLANS
from topstep_bot.sessions import SessionSchedule
from topstep_bot.wizard import remove_env_keys, update_env_file

from .conftest import CT, UTC, bar, ct, run

T0 = ct(2026, 3, 3, 9, 0)


# ------------------------------------------------------------------ API keys

@pytest.fixture
def env(monkeypatch):
    for k in list(__import__("os").environ):
        if k.startswith("TOPSTEPX_"):
            monkeypatch.delenv(k)
    monkeypatch.setenv("TOPSTEPX_USERNAME", "me")
    monkeypatch.setenv("TOPSTEPX_API_KEY", "main-key-123456")
    monkeypatch.setenv("TOPSTEPX_API_KEY_C2", "c2-key-abcdefgh")
    monkeypatch.setenv("TOPSTEPX_API_KEY_OTHER_LOGIN", "other-key-zzzzzz")
    monkeypatch.setenv("TOPSTEPX_USERNAME_OTHER_LOGIN", "partner")


def test_each_account_can_use_its_own_key(env):
    s = load_secrets()
    assert set(s.logins) == {"c2", "other_login"}
    cfg = BotConfig.model_validate({"accounts": [{"name": "c1"}, {"name": "c2"}, {"name": "c3", "login": "other-login"},
                                                 {"name": "c4", "login": "gone"}]})
    c1, c2, c3 = (s.for_account(cfg.for_account(n)) for n in ("c1", "c2", "c3"))
    assert (c1.username, c1.api_key, c1.login_name) == ("me", "main-key-123456", None)       # the main key
    assert (c2.username, c2.api_key, c2.login_name) == ("me", "c2-key-abcdefgh", "c2")       # named like the account
    assert (c3.username, c3.api_key) == ("partner", "other-key-zzzzzz")                      # login: other-login
    with pytest.raises(ValueError, match="No API key named 'gone'"):
        s.for_account(cfg.for_account("c4"))
    assert BotConfig().account.login is None and s.for_account(BotConfig()) is s             # single account: unchanged


def test_every_stored_key_is_masked_in_logs(env):
    f = RedactingFilter(load_secrets().secret_values())
    assert "c2-key" not in f.redact("login with c2-key-abcdefgh failed") and "***" in f.redact("other-key-zzzzzz")


def test_env_file_add_and_remove(tmp_path):
    path = tmp_path / ".env"
    update_env_file(path, {"TOPSTEPX_API_KEY": "a", "TOPSTEPX_API_KEY_C2": "b", "TELEGRAM_CHAT_ID": "1"})
    assert remove_env_keys(path, {"TOPSTEPX_API_KEY_C2", "TOPSTEPX_USERNAME_C2"})
    text = path.read_text()
    assert "TOPSTEPX_API_KEY=a" in text and "C2" not in text and "TELEGRAM_CHAT_ID=1" in text
    assert not remove_env_keys(path, {"NOPE"})


def test_keys_command_lists_masked_keys(env, tmp_path, monkeypatch, capsys):
    from topstep_bot.cli import main

    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").write_text("accounts:\n  - {name: c1}\n  - {name: c2}\n  - {name: c3, login: missing}\n")
    assert main(["keys"]) == 1  # c3's key isn't stored
    out = capsys.readouterr().out
    assert "c2-key" not in out and "…efgh" in out and "keys add missing" in out


# ------------------------------------------------------------------ news per instrument

FEED = [
    {"title": "Crude Oil Inventories", "country": "USD", "date": "2026-03-04T10:30:00-05:00", "impact": "Medium",
     "forecast": "-1.2M", "previous": "0.8M"},
    {"title": "CPI m/m", "country": "USD", "date": "2026-03-05T08:30:00-05:00", "impact": "High", "forecast": "0.3%"},
    {"title": "ISM Services PMI", "country": "USD", "date": "2026-03-04T10:00:00-05:00", "impact": "Medium"},
    {"title": "Bank Holiday", "country": "USD", "date": "2026-03-06T00:00:00-05:00", "impact": "Holiday"},
    {"title": "ECB Press Conference", "country": "EUR", "date": "2026-03-05T08:45:00-05:00", "impact": "High"},
]


def calendar(symbol, per_instrument=True):
    cal = NewsCalendar("u", None, ["High"], ["USD"], 5, 10, symbol=symbol, per_instrument=per_instrument)
    cal.events = cal._relevant(parse_feed(FEED))
    return cal


def test_news_that_matters_depends_on_the_instrument():
    assert [e.title for e in calendar("MCL").events] == ["ISM Services PMI", "Crude Oil Inventories", "CPI m/m"]
    assert [e.title for e in calendar("MNQ").events] == ["ISM Services PMI", "CPI m/m"]
    assert [e.title for e in calendar("MCL", per_instrument=False).events] == ["CPI m/m"]
    oil = calendar("CL").events[1]
    assert (oil.forecast, oil.previous) == ("-1.2M", "0.8M")
    assert [e.title for e in calendar("MCL").on_day(date(2026, 3, 4), CT)] == ["ISM Services PMI", "Crude Oil Inventories"]
    assert event_matters("Fed Chairman Warsh Speaks", "MES") and not event_matters("Crude Oil Inventories", "MES")
    assert product_for("M2K").root == "RTY" and product_for("XYZ") is None


RSS = """<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel>
<item><title>Oil jumps on supply worries</title><link>https://example.com/a</link><pubDate>{recent}</pubDate></item>
<item><title>Bad link</title><link>javascript:alert(1)</link></item>
<item><title>Old story</title><link>https://example.com/old</link><pubDate>Mon, 05 Jan 2026 10:00:00 +0000</pubDate></item>
</channel></rss>"""


def rss(recent):
    return RSS.replace("{recent}", recent.strftime("%a, %d %b %Y %H:%M:%S +0000"))


def test_rss_parsing_is_safe():
    from datetime import datetime

    items = parse_rss(rss(datetime.now(UTC)))
    assert [h.title for h in items] == ["Oil jumps on supply worries", "Old story"]  # the javascript: link is dropped
    with pytest.raises(ValueError):
        parse_rss('<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><rss><channel></channel></rss>')


def test_headline_feed_merges_sources_and_drops_old_news(tmp_path):
    from datetime import datetime

    now = datetime.now(UTC)
    second = RSS.replace("{recent}", (now - timedelta(hours=1)).strftime("%a, %d %b %Y %H:%M:%S +0000")) \
        .replace("Oil jumps on supply worries", "Gold steady")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=(rss(now) if "CL" in str(request.url) else second).encode())

    feed = HeadlineFeed(["https://x/?s=CL=F", "https://x/?s=USO"], tmp_path / "h.json")
    assert run(feed.refresh(transport=httpx.MockTransport(handler)))
    assert [h.title for h in feed.items] == ["Oil jumps on supply worries", "Gold steady"]  # "Old story" too old, deduped
    fresh = HeadlineFeed("https://x", tmp_path / "h.json")
    assert fresh.load_cache() and len(fresh.items) == 2
    broken = HeadlineFeed("https://x", tmp_path / "h.json")
    assert run(broken.refresh(transport=httpx.MockTransport(lambda r: httpx.Response(500))))  # falls back to the cache
    assert len(broken.items) == 2


# ------------------------------------------------------------------ market tracker

def session_bars(day, open_, high, low, close, on_high, on_low):
    """Overnight bars then regular-hours bars for one Chicago trading day."""
    prev = day - timedelta(days=1)
    return [bar(ct(prev.year, prev.month, prev.day, 17, 0), close, on_high, on_low, close),
            bar(ct(day.year, day.month, day.day, 8, 30), open_, high, low, open_),
            bar(ct(day.year, day.month, day.day, 14, 55), close, close, close, close)]


def test_market_tracker_levels_atr_gap_and_journal(tmp_path, mnq):
    from topstep_bot.config import SessionConfig

    sched = SessionSchedule(SessionConfig())
    tracker = MarketTracker("MNQ", mnq, sched, tmp_path / "mj.jsonl")
    days = [date(2026, 3, 2), date(2026, 3, 3), date(2026, 3, 4), date(2026, 3, 5)]
    for d in days[:3]:
        for b in session_bars(d, 100, 110, 90, 105, 104, 98):
            tracker.update(b, record=False)  # warmup: nothing written
    assert not (tmp_path / "mj.jsonl").exists()
    for b in session_bars(days[3], 112, 116, 111, 115, 108, 104)[:2]:
        tracker.update(b)
    f = tracker.facts()
    assert f["atr_points"] == 20 and f["atr_usd"] == 20 * mnq.point_value
    assert f["gap"] == 7 and f["gap_atr"] == 0.35
    names = {lv["name"]: lv["price"] for lv in f["levels"]}
    assert names["Prior high"] == 110 and names["Prior close"] == 105 and names["Overnight high"] == 108
    notes = " ".join(tracker.notes(f))
    assert "gap up of 7" in notes and "above yesterday's high" in notes
    tracker.update(bar(ct(2026, 3, 5, 17, 0), 115, 115, 115, 115))  # next trading day: today is journaled
    line = json.loads((tmp_path / "mj.jsonl").read_text().splitlines()[-1])
    assert line["day"] == "2026-03-05" and line["gap"] == 7 and line["high"] == 116
    text = tracker.brief_text(ct(2026, 3, 5, 10, 0))
    assert "E-mini Nasdaq-100" in text and "Prior high: 116" in text  # the day rolled: 3-5 is now "prior"


# ------------------------------------------------------------------ funded-account goals

def test_combine_progress_and_consistency():
    st = funded.status(FundedConfig(), PLANS["50K"], "combine", [("2026-03-02", 1000.0, 2), ("2026-03-03", 200.0, 1)],
                       date(2026, 3, 4), 100.0, 51_300, 50_000, 5)
    assert st["progress"] == pytest.approx(1300 / 3000, abs=1e-3) and st["remaining"] == 1700
    assert st["consistency"] == pytest.approx(1000 / 1300, abs=1e-3) and not st["consistency_ok"]
    assert st["needed_for_consistency"] == 2000 and "you'd need $2,000" in funded.status_text(st)


def test_xfa_standard_path_counts_winning_days_since_the_last_payout():
    cfg = FundedConfig(last_payout_date=date(2026, 3, 2))
    days = [("2026-02-27", 900.0, 2), ("2026-03-02", 500.0, 1), ("2026-03-03", 160.0, 2), ("2026-03-04", 149.0, 1)]
    st = funded.status(cfg, PLANS["50K"], "express", days, date(2026, 3, 5), 150.0, 3_000, 0, 5, 2)
    assert st["winning_days"] == 2 and not st["eligible"] and st["today_is_winning"]  # Mar 3 + today
    assert st["payout_estimate"] == 1_500  # 50% of the balance (cap $2,000)
    st = funded.status(FundedConfig(), PLANS["50K"], "express", [(f"2026-03-0{i}", 200.0, 1) for i in range(1, 6)],
                       date(2026, 3, 9), 0.0, 10_000, 0, 5)
    assert st["eligible"] and st["payout_estimate"] == 2_000


def test_xfa_consistency_path_ratio_and_daily_cap():
    cfg = FundedConfig(payout_path="consistency")
    st = funded.status(cfg, PLANS["100K"], "express", [("2026-03-02", 600.0, 1), ("2026-03-03", 500.0, 2)],
                       date(2026, 3, 4), 400.0, 1_500, 0, 3, 1)
    assert st["trading_days"] == 3 and st["consistency"] == 0.4 and st["eligible"]
    assert st["daily_cap"] == 0.4 * 6_000 == funded.daily_cap(cfg, PLANS["100K"], "express")
    assert funded.daily_cap(FundedConfig(), PLANS["100K"], "express") is None


def make_core(mnq, stage="express", **funded_kw):
    cfg = BotConfig.model_validate({"account": {"stage": stage}, "risk": {"risk_per_trade": 500, "personal_daily_loss_limit": 1000,
                                                                          "slippage_ticks": 0}, "funded": funded_kw})
    broker = PaperBroker(mnq, 0, slippage_ticks=0)
    core = build_core(cfg, mnq, broker, clock=lambda: T0, account_label="X", journal=Journal(":memory:"))
    return core


def test_a_winning_day_is_protected_on_the_standard_path(mnq):
    core = make_core(mnq)
    core.risk.start_day(date(2026, 3, 3), 3_000.0)
    rpc = core.risk.risk_per_contract(100, 90)
    full = core.risk.position_size(100, 90, 3_000.0)
    assert core.risk.position_size(100, 90, 3_000.0 + 200) == int(50 // rpc)  # $200 up: may only risk the $50 above $150
    assert core.risk.position_size(100, 90, 3_000.0 + 140) == full            # not a winning day yet: normal size
    off = make_core(mnq, protect_winning_day=False)
    off.risk.start_day(date(2026, 3, 3), 3_000.0)
    assert off.risk.position_size(100, 90, 3_200.0) == full
    assert core.risk.snapshot(3_200.0, 0)["winning_day_floor"] == 150


def test_consistency_path_caps_the_day(mnq):
    core = make_core(mnq, payout_path="consistency", cycle_profit_goal=2_000)
    core.risk.start_day(date(2026, 3, 3), 3_000.0)
    assert core.risk.daily_profit_target == 800
    assert "daily profit target $800" in core.risk.entry_block_reason(T0, 3_800.0)


def test_rules_reference():
    xfa = {r.title for r in funded.rules_for("express")}
    assert {"Payout - Standard path", "Scaling Plan", "No hedging across accounts", "Maximum Loss Limit"} <= xfa
    assert "Consistency target" not in xfa and "Consistency target" in {r.title for r in funded.rules_for("combine")}
    assert funded.RULES_CHECKED in funded.rules_text("combine")


def test_preflight_flags_prohibited_settings():
    cfg = BotConfig.model_validate({"news": {"enabled": False}, "risk": {"breakeven_at_r": 0.5}, "account": {"stage": "express"}})
    rep = run(run_preflight(cfg, Secrets(), run_backtests=False))
    warned = {c.name for c in rep.checks if c.status == WARN}
    assert {"News filter", "Breakeven stop"} <= warned
    assert any(c.name == "Payout path" for c in rep.checks)


# ------------------------------------------------------------------ the core, the knowledge base and remote text

def test_snapshot_and_texts_include_market_and_funded(mnq):
    core = make_core(mnq, stage="combine")
    core.balance = 50_000
    run(core.begin_day(date(2026, 3, 3), 50_000))
    core.market = MarketTracker("MNQ", mnq, core.schedule)
    core.market.news = calendar("MNQ")
    for b in session_bars(date(2026, 3, 3), 100, 110, 90, 105, 104, 98):
        core.warmup_bar(b)
    snap = core.snapshot()
    json.dumps(snap)
    assert snap["market"]["profile"]["root"] == "NQ" and snap["funded"]["goal"] == "Pass the Combine"
    actions = BotActions(core, Controls())
    assert "E-mini Nasdaq-100" in run(actions.handle("brief_text", {}))["text"]
    assert "Pass the Combine" in run(actions.handle("funded_text", {}))["text"]
    assert "Profit target" in run(actions.handle("rules_text", {}))["text"]


def test_knowledge_base_separates_news_days(tmp_path):
    kb = KnowledgeBase(tmp_path / "kb.json")
    for i, (news, r) in enumerate([(True, -1.0), (True, -0.5), (False, 1.0), (None, 2.0)]):
        kb.record(Observation(f"2026-03-0{i + 1}", "09:00", "orb", "LONG", "open", "calm", r, None, "shadow", news=news))
    today = date(2026, 3, 5)
    assert kb.stats("orb", today=today, news=True).n == 2 and kb.stats("orb", today=today, news=False).n == 1
    assert kb.stats("orb", today=today).n == 4
    row = kb.summary([("orb", "ORB")], today=today)["strategies"][0]
    assert row["news_days"]["mean_r"] < 0 < row["quiet_days"]["mean_r"]
    assert KnowledgeBase(tmp_path / "kb.json").obs[0].news is True  # saved and reloaded
    assert "news days" in kb.text([("orb", "ORB")], today=today)


def test_old_knowledge_files_still_load(tmp_path):
    path = tmp_path / "kb.json"
    old = {"day": "2026-03-01", "time": "09:00", "strategy": "orb", "side": "LONG", "slot": "open", "regime": "calm",
           "r": 1.0, "usd": None, "source": "train", "why": "target"}
    path.write_text(json.dumps({"version": 1, "observations": [old]}))
    assert KnowledgeBase(path).obs[0].news is None


def test_events_serialise_for_the_dashboard():
    e = NewsEvent("CPI m/m", "USD", "High", ct(2026, 3, 5, 7, 30), "0.3%", "0.2%")
    d = e.to_dict(CT)
    assert d["local"] == "Thu 07:30" and d["forecast"] == "0.3%"


def test_logins_are_redacted_in_real_log_setup(env, tmp_path):
    from topstep_bot.logging_setup import setup_logging

    setup_logging(tmp_path, "INFO", secrets=load_secrets().secret_values())
    logging.getLogger("topstep_bot.test").warning("using other-key-zzzzzz now")
    for h in logging.getLogger().handlers:
        h.flush()
    assert "other-key-zzzzzz" not in (tmp_path / "errors.log").read_text(encoding="utf-8")
