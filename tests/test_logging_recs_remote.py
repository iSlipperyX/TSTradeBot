"""Logging system, recommended trades, and remote settings/trades (dashboard + Telegram)."""

import asyncio
import json
import logging
import sys
from datetime import timedelta

import httpx
import pytest

from topstep_bot import logging_setup
from topstep_bot.backtest.data import synthetic_bars
from topstep_bot.bars import resample
from topstep_bot.broker.paper import PaperBroker
from topstep_bot.config import BotConfig, TelegramConfig
from topstep_bot.control import BotActions
from topstep_bot.factory import build_core
from topstep_bot.instruments import offline_contract
from topstep_bot.journal import Journal
from topstep_bot.live import Controls
from topstep_bot.models import OrderSide
from topstep_bot.recommendations import Recommendation, RecommendationBook
from topstep_bot.remote import RemoteControl, SettingError
from topstep_bot.telegram_control import TelegramController

from .conftest import ct, run
from .test_telegram_control import CHAT, FakeTelegram, button, msg


# ------------------------------------------------------------------ logging

@pytest.fixture
def logs(tmp_path):
    secret_key = "SUPERSECRETAPIKEY123"
    token = "123456789:AAHkLmnopQRSTuvwxYZ0123456789abcdefg"
    log_dir = logging_setup.setup_logging(tmp_path / "logs", "WARNING", secrets=[secret_key, token])
    yield log_dir, secret_key, token
    for h in list(logging.getLogger().handlers):
        logging.getLogger().removeHandler(h)
        h.close()
    sys.excepthook = sys.__excepthook__


def flush():
    for h in logging.getLogger().handlers:
        h.flush()


def test_log_files_and_secret_redaction(logs):
    log_dir, key, token = logs
    log = logging.getLogger("topstep_bot.test")
    log.info("login with %s ok", key)
    log.warning("Telegram send failed: POST https://api.telegram.org/bot%s/sendMessage", token)
    log.error("hub url wss://rtc.topstepx.com/hubs/user?access_token=eyJhbGciOi.payload.sig")
    try:
        raise RuntimeError(f"boom {key}")
    except RuntimeError:
        log.exception("failure")
    flush()
    everything = (log_dir / "bot.log").read_text(encoding="utf-8")
    errors = (log_dir / "errors.log").read_text(encoding="utf-8")
    events = (log_dir / "events.jsonl").read_text(encoding="utf-8")
    for text in (everything, errors, events):
        assert key not in text and "AAHkLmnop" not in text and "eyJhbGciOi" not in text
    assert "login with *** ok" in everything and "bot***" in everything
    assert "login with" not in errors  # info is not in errors.log
    assert "RuntimeError: boom ***" in errors
    rows = [json.loads(line) for line in events.splitlines()]
    assert {"ts", "level", "logger", "msg"} <= set(rows[0])


def test_events_carry_structured_data(logs):
    log_dir, _, _ = logs
    logging.getLogger("topstep_bot.recommendations").info("Idea", extra={"event": "recommendation", "data": {"id": "R1"}})
    flush()
    row = json.loads((log_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert row["event"] == "recommendation" and row["data"]["id"] == "R1"


def test_crash_report_and_stats(logs):
    log_dir, key, _ = logs
    before = logging_setup.stats.errors
    try:
        raise ValueError(f"bad thing {key}")
    except ValueError:
        sys.excepthook(*sys.exc_info())
    flush()
    crash = next(log_dir.glob("crash_*.txt")).read_text(encoding="utf-8")
    assert "ValueError: bad thing ***" in crash and key not in crash
    assert logging_setup.stats.errors > before


def test_spawned_task_failures_are_logged(logs):
    log_dir, _, _ = logs

    async def broken():
        raise KeyError("missing")

    async def go():
        task = logging_setup.spawn(broken(), name="clock")
        await asyncio.sleep(0.01)
        assert task.done()
    run(go())
    flush()
    assert "Background task 'clock' failed" in (log_dir / "errors.log").read_text(encoding="utf-8")


def test_log_dir_is_next_to_config(tmp_path, monkeypatch):
    from topstep_bot.config import load_config

    (tmp_path / "config.yaml").write_text("mode: paper\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path.parent)
    cfg = load_config(tmp_path / "config.yaml")
    assert cfg.log_dir == str(tmp_path / "logs") and cfg.data_dir == str(tmp_path / "data")


# ------------------------------------------------------------ recommendations

def make_core(tmp_path, **cfg_kw):
    mnq = offline_contract("MNQ")
    cfg = BotConfig.model_validate({"data_dir": str(tmp_path), **cfg_kw})
    now = [ct(2026, 3, 3, 9, 0)]
    broker = PaperBroker(mnq, 50_000, slippage_ticks=0, fees_round_turn=1.22)
    core = build_core(cfg, mnq, broker, clock=lambda: now[0], account_label="T", journal=Journal(tmp_path / "j.db"))
    core.balance = 50_000
    core.recommender = RecommendationBook(core)
    return core, broker, now


def drive(core, broker, now, bars):
    """Run bars through the core like the live runner does (no network)."""
    tf = timedelta(minutes=core.cfg.instrument.timeframe_minutes)
    days = sorted({core.schedule.trading_day(b.ts) for b in bars})
    warm = days[:16]

    async def go():
        for b in bars:
            if core.schedule.trading_day(b.ts) in warm:
                now[0] = b.ts
                core.warmup_bar(b)
                continue
            now[0] = b.ts
            await core.roll_day_if_needed(b.ts)
            await broker.on_bar(b)
            now[0] = b.ts + tf
            await core.on_price(now[0], b.close)
            await core.on_bar(b)
            await core.on_clock(now[0])
            await broker.drain()
    run(go())


def test_shadow_strategies_produce_tracked_ideas(tmp_path):
    core, broker, now = make_core(tmp_path)
    assert {s.name for s in core.recommender.shadows} == {"noise_breakout", "ema_trend", "vwap_reversion"}
    bars = list(resample(synthetic_bars("MNQ", days=30, seed=4), 5))
    drive(core, broker, now, bars)
    book = core.recommender
    assert book.items, "expected some recommendations"
    strategies = {r.strategy for r in book.items}
    assert strategies - {"orb"}, "expected ideas from the shadow strategies"
    closed = [r for r in book.items if r.result]
    assert closed and all(r.result in ("won", "lost", "flat") for r in closed)
    for r in closed:
        if r.outcome_usd is not None and r.risk_usd and r.result == "lost" and not r.active:
            assert r.outcome_usd >= -r.risk_usd * 1.6  # losses stay near the planned risk
    taken = [r for r in book.items if r.active and r.status == "taken"]
    assert all(not r.hypothetical for r in taken)
    snap = book.snapshot()
    assert snap["summary"][0]["active"] and json.dumps(snap, default=str)
    assert core.journal.recommendations(limit=5)
    assert "Recommended trades" in book.text()


def test_active_skips_are_recorded_with_reason(tmp_path):
    core, _, _ = make_core(tmp_path)
    from topstep_bot.models import Bar, Signal

    async def go():
        await core.begin_day(core.schedule.trading_day(ct(2026, 3, 3, 9, 0)), 50_000)
        core.risk.paused = True
        b = Bar(ct(2026, 3, 3, 8, 55), 100, 100, 100, 100, 10)
        core.last_price = 100.0
        await core._handle_entry(Signal("long", 90.0, 120.0, "test"), b, core.context(b))
    run(go())
    rec = core.recommender.items[0]
    assert rec.active and rec.status == "skipped" and "paused" in rec.note and rec.size > 0


# ------------------------------------------------------------ remote control

def test_settings_bounds_validation_and_persistence(tmp_path):
    core, _, _ = make_core(tmp_path)
    run(core.begin_day(core.schedule.trading_day(ct(2026, 3, 3, 9, 0)), 50_000))
    rc = RemoteControl(core, tmp_path / "remote.json")
    assert rc.preview("risk", "200")["riskier"] is True
    assert rc.preview("risk", "100")["riskier"] is False
    msg_ = rc.apply("risk", "$200", "test")
    assert core.cfg.risk.risk_per_trade == 200 and "150" in msg_ and "200" in msg_
    with pytest.raises(SettingError, match="between"):
        rc.apply("risk", "5000", "test")  # above 15% of the MLL
    with pytest.raises(SettingError, match="smaller than the risk per trade"):
        rc.apply("dailyloss", "100", "test")  # below the $200 risk per trade
    with pytest.raises(SettingError, match="Unknown setting"):
        rc.apply("mode", "live", "test")  # never changeable remotely
    with pytest.raises(SettingError):
        rc.apply("last_entry", "15:05", "test")  # after flatten time
    rc.apply("news", "off", "test")
    rc.apply("max_contracts", "3", "test")
    assert core.cfg.news.enabled is False and core.risk.max_contracts() == 3
    rc.apply("max_contracts", "", "test")
    assert core.cfg.risk.max_contracts is None
    saved = json.loads((tmp_path / "remote.json").read_text(encoding="utf-8"))
    assert saved["risk_per_trade"] == 200 and saved["news_filter"] is False

    # a fresh start re-applies saved changes
    core2, _, _ = make_core(tmp_path)
    rc2 = RemoteControl(core2, tmp_path / "remote.json")
    rc2.load_saved()
    assert core2.cfg.risk.risk_per_trade == 200 and core2.cfg.news.enabled is False
    rc2.reset("test")
    assert core2.cfg.risk.risk_per_trade == 150 and core2.cfg.news.enabled is True
    assert json.loads((tmp_path / "remote.json").read_text(encoding="utf-8")) == {}


def test_strategy_switch_reuses_warm_shadow_and_waits_when_in_trade(tmp_path):
    core, broker, now = make_core(tmp_path)
    run(core.begin_day(core.schedule.trading_day(ct(2026, 3, 3, 9, 0)), 50_000))
    rc = RemoteControl(core, tmp_path / "remote.json")
    shadow = next(s for s in core.recommender.shadows if s.name == "ema_trend")
    rc.apply("strategy", "ema_trend", "test")
    assert core.strategy is shadow and core.orders.strategy_name == "ema_trend"
    assert "orb" in {s.name for s in core.recommender.shadows}

    async def go():
        broker.live = True
        await broker.on_price(now[0], 100.0)
        await core.orders.enter(OrderSide.BUY, 1, 90.0, None, "t", ref_price=100.0)
        await broker.drain()
    run(go())
    message = rc.apply("strategy", "orb", "test")
    assert "once the current trade closes" in message and core.strategy.name == "ema_trend"
    run(core.orders.exit("done"))
    run(broker.drain())
    rc.on_flat()
    assert core.strategy.name == "orb"


def test_take_idea_trades_it_with_risk_sizing(tmp_path):
    core, broker, now = make_core(tmp_path)
    run(core.begin_day(core.schedule.trading_day(now[0]), 50_000))
    core.remote = RemoteControl(core, tmp_path / "remote.json")
    core.risk.paused = True  # pausing automatic entries doesn't block a manual take
    broker.live = True
    run(broker.on_price(now[0], 100.0))
    core.last_price = 100.0
    rec = Recommendation(id="R9", created=now[0], strategy="ema_trend", title="EMA Trend Crossover", active=False,
                         side=OrderSide.BUY, entry=100.0, stop=90.0, target=130.0, size=4, risk_usd=100.0,
                         reason="cross", status="idea")
    core.recommender.items.appendleft(rec)
    actions = BotActions(core, Controls())
    message = run(actions.take_idea("R9", "test", size=2))
    run(broker.drain())
    assert "Took" in message and core.orders.position == 2
    assert core.orders.trade.strategy == "ema_trend" and not core.owns_trade()
    assert rec.status == "taken" and rec.trade_tag == core.orders.trade.tag
    another = Recommendation(id="R11", created=now[0], strategy="vwap_reversion", title="VWAP", active=False,
                             side=OrderSide.BUY, entry=100.0, stop=90.0, target=None, size=2, risk_usd=50.0,
                             reason="x", status="idea")
    core.recommender.items.appendleft(another)
    with pytest.raises(SettingError, match="Already in a trade"):
        run(actions.take_idea("R11", "test"))
    # a requested size can never exceed what the risk rules allow
    run(core.orders.exit("x"))
    run(broker.drain())
    now[0] += timedelta(minutes=11)  # that close was a (fee-only) loss: wait out the cooldown, which manual takes respect
    rec2 = Recommendation(id="R10", created=now[0], strategy="ema_trend", title="EMA", active=False, side=OrderSide.SELL,
                          entry=100.0, stop=110.0, target=None, size=4, risk_usd=100.0, reason="x", status="idea")
    core.recommender.items.appendleft(rec2)
    run(actions.take_idea("R10", "test", size=500))
    run(broker.drain())
    assert abs(core.orders.position) <= core.risk.max_contracts() and abs(core.orders.position) < 500


def test_old_or_closed_ideas_cannot_be_taken(tmp_path):
    core, _, now = make_core(tmp_path)
    run(core.begin_day(core.schedule.trading_day(now[0]), 50_000))
    core.remote = RemoteControl(core, tmp_path / "remote.json")
    old = Recommendation(id="R1", created=now[0] - timedelta(minutes=30), strategy="ema_trend", title="E", active=False,
                         side=OrderSide.BUY, entry=100, stop=90, target=None, size=1, risk_usd=10, reason="x", status="idea")
    core.recommender.items.appendleft(old)
    with pytest.raises(SettingError, match="too old"):
        run(core.remote.take_idea("R1", "test"))
    with pytest.raises(SettingError, match="not found"):
        run(core.remote.take_idea("R404", "test"))


# --------------------------------------------------------------- telegram

def telegram(core):
    fake = FakeTelegram()
    ctl = TelegramController("TOKEN", CHAT, BotActions(core, Controls()), TelegramConfig(),
                             transport=httpx.MockTransport(fake.handler))
    return ctl, fake


def test_telegram_set_requires_confirmation(tmp_path):
    core, _, _ = make_core(tmp_path)
    run(core.begin_day(core.schedule.trading_day(ct(2026, 3, 3, 9, 0)), 50_000))
    core.remote = RemoteControl(core, tmp_path / "remote.json")
    ctl, fake = telegram(core)
    run(ctl.handle_update(msg("/set risk 200")))
    prompt = fake.sent[-1]
    assert "150" in prompt["text"] and "200" in prompt["text"] and "increases your risk" in prompt["text"]
    assert core.cfg.risk.risk_per_trade == 150  # nothing changed yet
    run(ctl.handle_update(button("yes:set", prompt["message_id"])))
    assert core.cfg.risk.risk_per_trade == 200 and fake.edits[-1]["text"].startswith("✅")
    run(ctl.handle_update(msg("/set risk 99999")))
    assert fake.sent[-1]["text"].startswith("❌")
    run(ctl.handle_update(msg("/settings")))
    assert "risk_per_trade: 200" in fake.sent[-1]["text"]
    run(ctl.handle_update(msg("/reset")))
    run(ctl.handle_update(button("yes:reset", fake.sent[-1]["message_id"])))
    assert core.cfg.risk.risk_per_trade == 150


def test_telegram_take_idea_flow(tmp_path):
    core, broker, now = make_core(tmp_path)
    run(core.begin_day(core.schedule.trading_day(now[0]), 50_000))
    core.remote = RemoteControl(core, tmp_path / "remote.json")
    broker.live = True
    run(broker.on_price(now[0], 100.0))
    core.last_price = 100.0
    core.recommender.items.appendleft(Recommendation(
        id="R5", created=now[0], strategy="vwap_reversion", title="VWAP Mean Reversion", active=False,
        side=OrderSide.BUY, entry=100.0, stop=90.0, target=110.0, size=4, risk_usd=100.0, reason="stretched", status="idea"))
    ctl, fake = telegram(core)
    run(ctl.handle_update(msg("/ideas")))
    buttons = json.dumps(fake.sent[-1]["reply_markup"])
    assert "take:R5" in buttons and "takeh:R5" in buttons
    run(ctl.handle_update(button("takeh:R5", fake.sent[-1]["message_id"])))
    prompt = fake.sent[-1]
    assert "Yes, take 2" in json.dumps(prompt["reply_markup"])
    run(ctl.handle_update(button("yes:take", prompt["message_id"])))
    run(broker.drain())
    assert core.orders.position == 2 and fake.edits[-1]["text"].startswith("✅ Took")


def test_dashboard_post_json_actions(tmp_path):
    from topstep_bot.dashboard import DashboardServer

    core, _, _ = make_core(tmp_path)
    run(core.begin_day(core.schedule.trading_day(ct(2026, 3, 3, 9, 0)), 50_000))
    core.remote = RemoteControl(core, tmp_path / "remote.json")
    actions = BotActions(core, Controls())

    async def go():
        server = DashboardServer("127.0.0.1", 0, core.snapshot, {
            "set_setting": lambda p: actions.change_setting(p["key"], p["value"], "dashboard"),
        })
        server._server = await asyncio.start_server(server._handle, "127.0.0.1", 0)
        port = server._server.sockets[0].getsockname()[1]
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
            ok = await client.post("/api/set_setting", json={"key": "risk", "value": 120}, headers={"X-Token": server.token})
            bad = await client.post("/api/set_setting", json={"key": "risk", "value": 99999}, headers={"X-Token": server.token})
            status = await client.get("/api/status")
        await server.stop()
        return ok.json(), bad.json(), status.json()

    ok, bad, status = run(go())
    assert ok["ok"] and core.cfg.risk.risk_per_trade == 120
    assert bad["ok"] is False and "between" in bad["message"]
    assert any(s["key"] == "risk_per_trade" and s["changed"] for s in status["settings"])
