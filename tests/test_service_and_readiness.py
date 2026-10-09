"""24/7 operation (autostart, daily restart, crash recovery) and same-day readiness
(news blackouts, ramp-up, preflight)."""

import sys
from datetime import timedelta, timezone

import httpx
import pytest

from topstep_bot import autostart
from topstep_bot.api.rest import ProjectXClient
from topstep_bot.broker.paper import PaperBroker
from topstep_bot.config import BotConfig, Secrets, SessionConfig
from topstep_bot.execution import OrderManager, TradeState
from topstep_bot.factory import build_core
from topstep_bot.live import Controls, LiveRunner
from topstep_bot.models import OrderSide, OrderType
from topstep_bot.news import NewsCalendar
from topstep_bot.preflight import FAIL, OK, account_hints, run_preflight
from topstep_bot.sessions import SessionSchedule

from .conftest import ct, run
from .test_live_runner import FakeTopstepX

UTC = timezone.utc


# ------------------------------------------------------------------ calendar

def test_market_open_hours():
    s = SessionSchedule(SessionConfig())
    assert s.market_open(ct(2026, 3, 3, 10, 0))
    assert not s.market_open(ct(2026, 3, 3, 16, 30))  # daily halt
    assert s.market_open(ct(2026, 3, 3, 17, 0))
    assert not s.market_open(ct(2026, 3, 6, 16, 0))  # Friday close
    assert not s.market_open(ct(2026, 3, 7, 12, 0))  # Saturday
    assert not s.market_open(ct(2026, 3, 8, 16, 59))
    assert s.market_open(ct(2026, 3, 8, 17, 0))  # Sunday open


FEED = [
    {"title": "CPI m/m", "country": "USD", "date": "2026-03-03T08:30:00-05:00", "impact": "High", "forecast": "", "previous": ""},
    {"title": "Retail Sales", "country": "USD", "date": "2026-03-03T10:00:00-05:00", "impact": "Medium", "forecast": "", "previous": ""},
    {"title": "ECB Rate", "country": "EUR", "date": "2026-03-03T08:15:00-05:00", "impact": "High", "forecast": "", "previous": ""},
]


def make_calendar(tmp_path):
    return NewsCalendar("https://feed.test/cal.json", tmp_path / "news.json", ["High"], ["USD"], 5, 10)


def test_news_blackouts_from_feed(tmp_path):
    cal = make_calendar(tmp_path)
    transport = httpx.MockTransport(lambda r: httpx.Response(200, json=FEED))
    assert run(cal.refresh(transport))
    assert [e.title for e in cal.events] == ["CPI m/m"]  # Medium and EUR filtered out
    release = ct(2026, 3, 3, 7, 30)  # 8:30 ET = 7:30 CT
    assert cal.blackout_reason(release - timedelta(minutes=6)) is None
    assert "CPI" in cal.blackout_reason(release - timedelta(minutes=4))
    assert "CPI" in cal.blackout_reason(release + timedelta(minutes=9))
    assert cal.blackout_reason(release + timedelta(minutes=10)) is None
    assert cal.releasing_soon(release - timedelta(minutes=2)).title == "CPI m/m"
    assert cal.releasing_soon(release + timedelta(minutes=1)) is None

    schedule = SessionSchedule(SessionConfig(trade_start="07:00"))
    schedule.news = cal
    assert "news blackout" in schedule.entry_block_reason(release)


def test_news_falls_back_to_cache_when_offline(tmp_path):
    ok = make_calendar(tmp_path)
    run(ok.refresh(httpx.MockTransport(lambda r: httpx.Response(200, json=FEED))))

    def offline(request):
        raise httpx.ConnectError("no internet")

    cal = make_calendar(tmp_path)
    run(cal.refresh(httpx.MockTransport(offline)))
    assert [e.title for e in cal.events] == ["CPI m/m"]


def test_out_of_date_news_cache_counts_as_no_calendar(mnq, tmp_path):
    import json

    now = ct(2026, 3, 10, 7, 20)  # 10 minutes before this week's CPI, which the old cache doesn't have
    (tmp_path / "news.json").write_text(json.dumps({"fetched_at": (now - timedelta(days=9)).isoformat(), "events": FEED}))
    cal = make_calendar(tmp_path)
    assert run(cal.refresh(httpx.MockTransport(lambda r: httpx.Response(429)))) is True  # fell back to the cache
    assert cal.events and not cal.is_current(now)
    core = build_core(BotConfig(), mnq, PaperBroker(mnq, 50_000), clock=lambda: now, account_label="t")
    run(core.begin_day(core.schedule.trading_day(now), 50_000))
    core.risk.live_guards = True
    core.schedule.news = cal
    assert core.risk.news_size_cap(now) == core.risk.max_contracts_topstep() // 2
    cal.fetched_at, cal.events = now - timedelta(hours=1), []  # fresh, and nothing coming up
    assert cal.is_current(now) and core.risk.news_size_cap(now) is None
    # Forex Factory's feed covers one week (from Sunday 00:00 New York time): last week's file is out of date
    # even when it was downloaded only a few hours ago.
    saturday_night = ct(2026, 3, 7, 21, 0)  # 22:00 New York
    cal.fetched_at = saturday_night
    assert cal.is_current(saturday_night + timedelta(hours=2)) is False
    assert cal.is_current(saturday_night + timedelta(minutes=30)) is True


def test_news_calendar_loads_even_with_news_pauses_off(mnq, tmp_path):
    """Turning the news pause off must not leave the bot blind to releases (that halved every trade)."""
    import json
    from datetime import datetime

    cfg = BotConfig.model_validate({"data_dir": str(tmp_path), "news": {"enabled": False}})
    (tmp_path / "news_cache.json").write_text(json.dumps({"fetched_at": datetime.now(UTC).isoformat(), "events": []}))
    runner = LiveRunner(cfg, Secrets(username="u", api_key="k"))
    runner.core = core = build_core(cfg, mnq, PaperBroker(mnq, 50_000), clock=runner.now, account_label="t")
    run(core.begin_day(core.schedule.trading_day(runner.now()), 50_000))
    core.risk.live_guards = True
    run(runner._setup_news())  # the cache is fresh: nothing is downloaded
    assert core.schedule.news is not None
    assert core.risk.max_contracts(runner.now()) == core.risk.max_contracts_topstep()
    runner.journal.close()
    run(runner.client.close())


def test_engine_flattens_before_news_when_enabled(mnq, tmp_path):
    cfg = BotConfig.model_validate({"news": {"flatten_before": True}})
    now = [ct(2026, 3, 3, 7, 26)]
    broker = PaperBroker(mnq, 50_000, slippage_ticks=0, live=True)
    core = build_core(cfg, mnq, broker, clock=lambda: now[0], account_label="t")
    core.balance = 50_000
    cal = make_calendar(tmp_path)
    run(cal.refresh(httpx.MockTransport(lambda r: httpx.Response(200, json=FEED))))
    core.schedule.news = cal

    async def go():
        await core.begin_day(core.schedule.trading_day(now[0]), 50_000)
        await broker.on_price(now[0], 100.0)
        await core.orders.enter(OrderSide.BUY, 1, 90.0, None, "t", ref_price=100.0)
        await broker.drain()
        assert core.orders.position == 1
        await core.on_clock(now[0])
        await broker.drain()
        assert broker.position == 0
    run(go())


# ------------------------------------------------------------------ ramp-up

def test_ramp_up_scales_risk(mnq):
    cfg = BotConfig.model_validate({"risk": {"risk_per_trade": 200, "personal_daily_loss_limit": 1000}})
    core = build_core(cfg, mnq, PaperBroker(mnq, 50_000), clock=lambda: ct(2026, 3, 3, 9, 0), account_label="t")
    run(core.begin_day(core.schedule.trading_day(ct(2026, 3, 3, 9, 0)), 50_000))
    full = core.risk.position_size(100, 100 - 39 * 0.25, 50_000)
    core.risk.risk_scale = 0.5
    assert core.risk.position_size(100, 100 - 39 * 0.25, 50_000) == full // 2


def _journal_trade(journal, mnq, day, account="ACC", net=10.0):
    import uuid

    from topstep_bot.execution import ManagedTrade

    closed = ct(day.year, day.month, day.day, 10, 0)
    t = ManagedTrade(tag=f"tsb{uuid.uuid4().hex[:10]}", side=OrderSide.BUY, size=1, stop_price=95.0, target_price=None,
                     reason="t", created_at=closed, entry_price=100.0, filled_size=1, closed_at=closed, gross_pnl=net)
    journal.record_trade(t, day, account, mnq.name)


def test_runner_ramp_up_counts_days_with_trades(mnq, tmp_path):
    from datetime import date

    cfg = BotConfig.model_validate({"mode": "live", "data_dir": str(tmp_path), "risk": {"ramp_up_days": 2}})
    runner = LiveRunner(cfg, Secrets(username="u", api_key="k"), Controls())
    runner.core = build_core(cfg, mnq, PaperBroker(mnq, 50_000), clock=runner.now, account_label="ACC")
    runner._apply_ramp_up()
    assert runner.core.risk.risk_scale == 0.5  # brand-new account
    _journal_trade(runner.journal, mnq, date(2020, 1, 2))
    _journal_trade(runner.journal, mnq, date(2020, 1, 2))  # same day counts once
    _journal_trade(runner.journal, mnq, date(2020, 1, 3), account="OTHER")  # other accounts don't count
    runner._apply_ramp_up()
    assert runner.core.risk.risk_scale == 0.5
    _journal_trade(runner.journal, mnq, date(2020, 1, 6))
    runner._apply_ramp_up()
    assert runner.core.risk.risk_scale == 1.0  # two trading days done
    runner.journal.close()
    run(runner.client.close())


def test_daily_restart_only_when_supervised_and_flat(mnq, tmp_path):
    cfg = BotConfig.model_validate({"data_dir": str(tmp_path)})
    runner = LiveRunner(cfg, Secrets(username="u", api_key="k"), Controls())
    runner.core = build_core(cfg, mnq, PaperBroker(mnq, 50_000), clock=runner.now, account_label="ACC")
    runner.started_at = ct(2026, 3, 3, 9, 0)
    after = ct(2026, 3, 3, 16, 6)
    runner._maybe_daily_restart(after)
    assert not runner.controls.restart_requested  # not supervised
    runner.supervised = True
    runner._maybe_daily_restart(ct(2026, 3, 3, 16, 0))
    assert not runner.controls.restart_requested  # not due yet
    runner._maybe_daily_restart(after)
    assert runner.controls.restart_requested and runner.controls.stop.is_set()
    runner.journal.close()
    run(runner.client.close())


# ------------------------------------------------------------- crash recovery

def test_restart_recovers_bot_position_instead_of_flattening(mnq):
    async def go():
        broker = PaperBroker(mnq, 50_000)
        broker.position, broker.avg_price = 2, 100.0
        await broker.place_order(mnq.id, OrderType.STOP, OrderSide.SELL, 2, stop_price=95.0, tag="tsb0123456789-S2")
        await broker.place_order(mnq.id, OrderType.LIMIT, OrderSide.SELL, 2, limit_price=110.0, tag="tsb0123456789-T")
        om = OrderManager(broker, mnq, fees_round_turn=1.0, clock=lambda: ct(2026, 3, 3, 9, 0), orphan_policy="flatten")
        await broker.drain()
        await om.reconcile()
        t = om.trade
        assert broker.position == 2  # NOT flattened
        assert t.state == TradeState.OPEN and t.stop_price == 95.0 and t.target_price == 110.0 and t.stop_seq == 2
    run(go())


def test_foreign_position_still_flattened(mnq):
    async def go():
        broker = PaperBroker(mnq, 50_000, live=True)
        broker.position, broker.avg_price, broker.last_price = 1, 100.0, 100.0
        await broker.place_order(mnq.id, OrderType.STOP, OrderSide.SELL, 1, stop_price=95.0, tag="manual")
        om = OrderManager(broker, mnq, fees_round_turn=1.0, clock=lambda: ct(2026, 3, 3, 9, 0), orphan_policy="flatten")
        await broker.drain()
        await om.reconcile()
        await broker.drain()
        assert broker.position == 0
    run(go())


# ---------------------------------------------------------------- supervisor

@pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
def test_autostart_enable_disable(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    cfg = tmp_path / "proj" / "config.yaml"
    cfg.parent.mkdir()
    cfg.write_text("mode: paper\n", encoding="utf-8")
    assert not autostart.is_enabled()
    path = autostart.enable(cfg)
    text = path.read_text(encoding="utf-8")
    assert autostart.is_enabled() and "start --yes" in text and str(cfg.resolve()) in text
    assert autostart.disable() and not autostart.is_enabled()


# ----------------------------------------------------------------- preflight

def test_account_name_hints():
    assert account_hints("50KTC-SKU-V2-DLL-694439-95905424") == ("50K", "combine")
    assert account_hints("XFA-150K-1234") == ("150K", "express")
    assert account_hints("PRAC-V2-555") == (None, "practice")


def test_preflight_against_fake_topstepx(tmp_path):
    fake = FakeTopstepX()
    cfg = BotConfig.model_validate({"data_dir": str(tmp_path), "news": {"enabled": False}})
    client = ProjectXClient("u", "k", transport=httpx.MockTransport(fake.handler))
    report = run(run_preflight(cfg, Secrets(username="u", api_key="k"), client=client, run_backtests=False))
    by_name = {c.name: c for c in report.checks}
    assert report.failures == 0
    assert by_name["Account"].status == OK and by_name["Open positions/orders"].status == OK
    assert by_name["Max Loss Limit"].status == OK and "fresh account" in by_name["Max Loss Limit"].detail
    assert "MNQZ6" in by_name["Contract"].detail
    assert not {p for p, _ in fake.calls} & {"/api/Order/place", "/api/Order/cancel", "/api/Position/closeContract"}
    run(client.close())


def test_preflight_learns_mll_for_traded_account(tmp_path):
    fake = FakeTopstepX()
    original = fake.handler

    def handler(request):
        if request.url.path == "/api/Account/search":
            return httpx.Response(200, json={"success": True, "accounts": [{"id": 7, "name": "50KTC-1", "balance": 50_900.0, "canTrade": True}]})
        return original(request)

    cfg = BotConfig.model_validate({"data_dir": str(tmp_path), "news": {"enabled": False}})
    client = ProjectXClient("u", "k", transport=httpx.MockTransport(handler))
    report = run(run_preflight(cfg, Secrets(username="u", api_key="k"), client=client, run_backtests=False,
                               ask_mll=lambda prompt: "$48,900"))
    mll = next(c for c in report.checks if c.name == "Max Loss Limit")
    assert "48,900" in mll.detail and "saved" in mll.detail
    from topstep_bot.journal import Journal

    j = Journal(tmp_path / "journal_live.db")
    assert j.get_state("mll_floor:50KTC-1") == 48_900
    j.close()
    run(client.close())


def test_preflight_fails_without_credentials():
    report = run(run_preflight(BotConfig(), Secrets()))
    assert report.verdict == "NOT READY" and any(c.status == FAIL for c in report.checks)
