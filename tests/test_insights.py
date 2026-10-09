"""Phase 1 of the roadmap: the bot records more about every trade and reports what it learned.

Market snapshot, price path (MFE / MAE), costs and real fill slippage go with every observation;
none of it may change a trading decision. Old knowledge files and journals must keep working.
"""

import csv
import json
import sqlite3
from datetime import date, time, timedelta

import pytest

from topstep_bot.backtest.data import synthetic_bars
from topstep_bot.bars import resample
from topstep_bot.broker.paper import PaperBroker
from topstep_bot.config import BotConfig
from topstep_bot.control import BotActions
from topstep_bot.execution import ManagedTrade, TradeState
from topstep_bot.factory import build_core
from topstep_bot.insights import build_report, csv_text, export_csv, oriented, report_text
from topstep_bot.instruments import offline_contract
from topstep_bot.journal import Journal
from topstep_bot.knowledge import KnowledgeBase, Observation, train_from_bars
from topstep_bot.live import Controls
from topstep_bot.market_context import FEATURES, MarketContext
from topstep_bot.models import Bar, OrderSide
from topstep_bot.recommendations import RecommendationBook
from topstep_bot.strategies import BASE_STRATEGIES, STRATEGIES

from .conftest import CT, ct, run

TODAY = date(2026, 10, 8)
BARS_1M = synthetic_bars("MNQ", days=45, seed=11, end=TODAY)
NAMES = [(n, STRATEGIES[n].title) for n in BASE_STRATEGIES]


def observation(strategy="ema_trend", r=0.5, side="LONG", source="shadow", **extra):
    return Observation(TODAY.isoformat(), "09:30", strategy, side, "open", "calm", r, r * 50, source, **extra)


# ------------------------------------------------------------------ market snapshot

def test_market_context_measures_in_day_ranges():
    mc = MarketContext(lookback_days=3, volume_bars=3)
    rth_open = time(8, 30)
    # three finished days with a 20-point regular-hours range, closing at 100
    for d in (1, 2, 3):
        day = date(2026, 9, d)
        mc.update(Bar(ct(2026, 9, d, 8, 30), 100, 110, 90, 100, 10), day, rth=True)
    day = date(2026, 9, 4)
    mc.update(Bar(ct(2026, 9, 4, 7, 0), 100, 130, 70, 104, 10), day, rth=False)  # overnight: ignored for the day
    mc.update(Bar(ct(2026, 9, 4, 8, 30), 104, 112, 102, 110, 10), day, rth=True)
    snap = mc.snapshot(110, ct(2026, 9, 4, 9, 0).astimezone(CT), 1.25, rth_open)
    assert snap["gap"] == pytest.approx(4 / 20)  # opened 4 points above yesterday's close
    assert snap["move"] == pytest.approx(6 / 20)
    assert snap["range_used"] == pytest.approx(10 / 20)
    assert snap["range_pos"] == pytest.approx(0.8)
    assert snap["min_open"] == 30 and snap["vol"] == 1.25 and snap["dow"] == 4
    assert snap["volume"] == pytest.approx(1.0)
    assert set(snap) <= set(FEATURES)
    # before three days are known, day-range measurements are simply left out
    fresh = MarketContext()
    fresh.update(Bar(ct(2026, 9, 1, 8, 30), 100, 110, 90, 100), date(2026, 9, 1), rth=True)
    assert "move" not in fresh.snapshot(100, ct(2026, 9, 1, 9, 0).astimezone(CT), None, rth_open)


def test_signed_measurements_are_seen_from_the_trade_side():
    long = observation(side="LONG", ctx={"move": 0.3, "range_pos": 0.9, "vol": 1.1})
    short = observation(side="SHORT", ctx={"move": 0.3, "range_pos": 0.9, "vol": 1.1})
    assert oriented(long, "move") == 0.3 and oriented(short, "move") == -0.3
    assert oriented(short, "range_pos") == pytest.approx(0.1) and oriented(short, "vol") == 1.1
    assert oriented(observation(), "move") is None


# ------------------------------------------------------------------ price path and fills

def test_managed_trade_tracks_path_and_slippage(mnq):
    t = ManagedTrade("tsb0000000001", OrderSide.BUY, 1, 99.0, None, "test", ct(2026, 9, 1, 9, 0), ref_price=100.0)
    t.note_prices(105, 95)  # not open yet: ignored
    assert t.best_price is None
    t.entry_price, t.state = 100.5, TradeState.OPEN  # filled 2 ticks worse than expected
    t.note_prices(102.5, 100.0)
    t.note_prices(101.0, 99.5)
    t.exit_ref, t.exit_price = 99.0, 98.75  # stopped 1 tick through the stop
    assert t.excursion_r() == (pytest.approx(round(2.0 / 1.5, 2)), pytest.approx(round(-1.75 / 1.5, 2)))
    assert t.slippage_ticks(mnq.tick_size) == (2.0, 1.0)
    short = ManagedTrade("tsb0000000002", OrderSide.SELL, 1, 101.0, None, "test", ct(2026, 9, 1, 9, 0), ref_price=100.0)
    short.entry_price, short.state, short.exit_ref, short.exit_price = 100.25, TradeState.OPEN, 98.0, 98.5
    assert short.slippage_ticks(mnq.tick_size) == (-1.0, 2.0)  # sold higher than expected: better, negative


# ------------------------------------------------------------------ knowledge file compatibility

def test_old_and_newer_knowledge_files_load(tmp_path):
    old = {"version": 1, "trained": None, "observations": [
        {"day": "2026-10-01", "time": "09:00", "strategy": "orb", "side": "LONG", "slot": "open", "regime": "calm",
         "r": 1.0, "usd": None, "source": "shadow", "why": "target hit"}]}
    path = tmp_path / "kb.json"
    path.write_text(json.dumps(old), encoding="utf-8")
    kb = KnowledgeBase(path)
    assert len(kb.obs) == 1 and kb.obs[0].ctx is None and kb.obs[0].net_r == 1.0
    kb.record(observation(ctx={"vol": 1.3}, mfe_r=1.2, mae_r=-0.4, bars=6, cost_r=0.05))
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["version"] == 2
    assert "ctx" not in saved["observations"][0] and saved["observations"][0]["usd"] is None  # required fields stay
    assert saved["observations"][1]["ctx"] == {"vol": 1.3}
    # a file written by a newer version with fields this one doesn't know still loads
    saved["observations"][1]["future_field"] = 7
    path.write_text(json.dumps(saved), encoding="utf-8")
    again = KnowledgeBase(path)
    assert len(again.obs) == 2 and again.obs[1].net_r == pytest.approx(0.45)


def test_journal_gains_learning_columns_on_old_files(tmp_path, mnq):
    path = tmp_path / "journal.sqlite"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE trades (tag TEXT PRIMARY KEY, trading_day TEXT, account TEXT, contract TEXT, strategy TEXT, "
                "side TEXT, size INTEGER, entry_time TEXT, entry_price REAL, exit_time TEXT, exit_price REAL, initial_stop REAL, "
                "target REAL, gross_pnl REAL, fees REAL, net_pnl REAL, r_multiple REAL, entry_reason TEXT, exit_reason TEXT)")
    con.commit()
    con.close()
    j = Journal(path)
    t = ManagedTrade("tsb0000000003", OrderSide.BUY, 1, 99.0, None, "test", ct(2026, 9, 1, 9, 0), ref_price=100.0)
    t.entry_price, t.exit_price, t.filled_size = 100.0, 102.0, 1
    j.record_trade(t, date(2026, 9, 1), "PAPER", mnq.name,
                   {"slip_in": 0.0, "slip_out": None, "mfe_r": 2.5, "mae_r": -0.2, "ctx": {"vol": 1.1}})
    row = j.trades()[0]
    assert row["mfe_r"] == 2.5 and row["entry_slip_ticks"] == 0.0 and json.loads(row["context"]) == {"vol": 1.1}
    assert row["ref_price"] == 100.0 and row["r_multiple"] == pytest.approx(2.0)


# ------------------------------------------------------------------ recording while trading

def test_training_records_context_path_and_costs(tmp_path):
    cfg = BotConfig.model_validate({"data_dir": str(tmp_path)})
    kb = KnowledgeBase(tmp_path / "kb.json", min_samples=3)
    run(train_from_bars(cfg, offline_contract("MNQ"), BARS_1M, kb))
    assert kb.obs
    with_ctx = [o for o in kb.obs if o.ctx]
    assert len(with_ctx) > 0.9 * len(kb.obs)
    assert all({"vol", "min_open", "dow"} <= set(o.ctx) for o in with_ctx)
    assert all(o.mfe_r is not None and o.mfe_r >= 0 >= o.mae_r for o in kb.obs)
    assert all(o.cost_r is not None and o.cost_r > 0 for o in kb.obs)
    for o in kb.obs:
        if o.why == "stop hit":
            assert o.mae_r <= -0.99 and o.r == pytest.approx(-1.0, abs=0.01)
        if o.why == "target hit":
            assert o.mfe_r >= o.r - 0.01


def test_learning_fields_change_no_decision(tmp_path):
    """The same history trains the same verdicts and trades with or without the new measurements."""
    cfg = BotConfig.model_validate({"data_dir": str(tmp_path)})
    kb = KnowledgeBase(None, min_samples=3)
    run(train_from_bars(cfg, offline_contract("MNQ"), BARS_1M, kb))
    bare = KnowledgeBase(None, min_samples=3)
    for o in kb.obs:
        bare.record(Observation(o.day, o.time, o.strategy, o.side, o.slot, o.regime, o.r, o.usd, o.source, o.why), save=False)
    for name, _ in NAMES:
        for slot in ("open", "midday", "close"):
            assert kb.verdict(name, slot, "calm", TODAY) == bare.verdict(name, slot, "calm", TODAY)


def live_core(tmp_path, slippage=1):
    mnq = offline_contract("MNQ")
    cfg = BotConfig.model_validate({"data_dir": str(tmp_path), "strategy": {"name": "adaptive", "params": {"trade_unproven": True}}})
    now = [ct(2026, 9, 1, 9, 0)]
    broker = PaperBroker(mnq, 50_000, slippage_ticks=slippage, fees_round_turn=1.22)
    core = build_core(cfg, mnq, broker, clock=lambda: now[0], account_label="T", journal=Journal(":memory:"))
    core.balance = 50_000
    core.recommender = RecommendationBook(core)
    core.attach_knowledge(KnowledgeBase(tmp_path / "kb.json", min_samples=5))
    return core, broker, now


def play(core, broker, now, bars_1m=BARS_1M, warm_days=16):
    bars = list(resample(bars_1m, 5))
    tf = timedelta(minutes=5)
    days = sorted({core.schedule.trading_day(b.ts) for b in bars})
    warm = set(days[:warm_days])

    async def go():
        for b in bars:
            now[0] = b.ts
            if core.schedule.trading_day(b.ts) in warm:
                core.warmup_bar(b)
                continue
            await core.roll_day_if_needed(b.ts)
            await broker.on_bar(b)
            now[0] = b.ts + tf
            await core.on_price(now[0], b.close)
            await core.on_bar(b)
            await core.on_clock(now[0])
            await broker.drain()
    run(go())


def test_real_trades_record_fills_path_and_snapshot(tmp_path):
    core, broker, now = live_core(tmp_path)
    play(core, broker, now)
    kb = core.knowledge
    real = [o for o in kb.obs if o.source == "real"]
    assert real, "trade_unproven should have produced real trades"
    assert all(o.ctx and o.mfe_r is not None and o.cost_r is not None and o.bars is not None for o in real)
    assert any(o.slip_in is not None for o in real) and any(o.slip_out is not None for o in real)
    rows = core.journal.trades()
    assert rows and all(r["ref_price"] is not None and r["context"] for r in rows)
    shadow = [o for o in kb.obs if o.source == "shadow"]
    assert shadow and all(o.slip_in is None and o.cost_r is not None for o in shadow)

    report = core.insights()
    assert report["coverage"]["total"] == len(kb.obs)
    assert report["execution"]["trades"] == len(real) and report["execution"]["entry_n"] > 0
    assert core.insights() is report  # cached until the knowledge base changes
    by_name = {r["name"]: r for r in report["strategies"]}
    assert sum(r["real_n"] for r in by_name.values()) == len(real)
    assert json.dumps(report)  # the dashboard gets it as JSON


# ------------------------------------------------------------------ the report

def test_report_numbers_and_hints():
    kb = KnowledgeBase(None, min_samples=3)
    # ema_trend wins in high volatility and loses in low volatility; orb has no pattern
    for i in range(60):
        vol = 0.6 + i * 0.02
        r = 1.0 if vol >= 1.2 else -1.0 if vol < 0.9 else 0.1
        kb.record(observation("ema_trend", r, ctx={"vol": round(vol, 2), "move": round(0.05 * (i % 7) - 0.1, 2), "dow": i % 5}, mfe_r=max(r, 0) + 0.5,
                              mae_r=-0.5, bars=4, cost_r=0.1), save=False)
        kb.record(observation("orb", 0.2 if i % 2 else -0.2, ctx={"vol": round(vol, 2)}, mfe_r=1.1 if i % 2 == 0 else 0.4,
                              mae_r=-1.0, cost_r=0.05), save=False)
    kb.record(observation("ema_trend", -1.0, source="real", slip_in=1.0, slip_out=3.0, cost_r=0.02), save=False)
    kb.record(observation("manual", 2.0, source="manual", slip_in=0.0), save=False)
    rep = build_report(kb, NAMES, slippage_ticks=1.0)

    ema = next(r for r in rep["strategies"] if r["name"] == "ema_trend")
    assert ema["n"] == 61 and ema["real_n"] == 1 and ema["sim_n"] == 60
    assert ema["net_r"] == pytest.approx(ema["gross_r"] - ema["cost_r"] * 61 / 61, abs=0.01)
    orb = next(r for r in rep["strategies"] if r["name"] == "orb")
    assert orb["gave_back"] == pytest.approx(1.0)  # every losing orb idea was up 1.1R first
    assert rep["manual"]["n"] == 1 and rep["manual"]["title"] == "Your manual trades"
    ex = rep["execution"]
    assert ex["trades"] == 2 and ex["entry"] == pytest.approx(0.5) and ex["exit"] == 3.0 and ex["exit_worst"] == 3.0

    vol_hints = [h for h in rep["hints"] if h["feature"] == "vol"]
    assert vol_hints and vol_hints[0]["strategy"] == "ema_trend"
    assert vol_hints[0]["best"]["label"].startswith("high") and vol_hints[0]["worst"]["label"].startswith("low")
    assert not [h for h in rep["hints"] if h["strategy"] == "orb"]  # no sign flip, no hint
    assert "move" in rep["conditions"] and "(in the trade's direction)" in rep["conditions"]["move"]["name"]
    assert rep["conditions"]["dow"]["labels"] == ["Mon", "Tue", "Wed", "Thu", "Fri"]
    assert [lb.split()[0] for lb in rep["conditions"]["vol"]["labels"]] == ["low", "middle", "high"]

    text = report_text(rep)
    assert "EMA Trend Crossover" in text and "Conditions worth testing" in text and "not proven" in text
    assert "Real fills slipped entries +0.5 ticks and exits +3.0 ticks" in text
    assert len(report_text(rep, compact=True)) < len(text)
    assert "empty" in report_text(build_report(KnowledgeBase(None), NAMES, slippage_ticks=1))


def test_csv_export(tmp_path):
    kb = KnowledgeBase(None)
    kb.record(observation(ctx={"vol": 1.4, "move": -0.2}, mfe_r=0.8, mae_r=-0.3, cost_r=0.1), save=False)
    kb.record(observation("orb", -1.0), save=False)
    out = tmp_path / "out" / "k.csv"
    assert export_csv(kb, out) == 2
    rows = list(csv.DictReader(out.open(encoding="utf-8")))
    assert rows[0]["vol"] == "1.4" and rows[0]["net_r"] == "0.4" and rows[1]["vol"] == "" and rows[1]["net_r"] == "-1.0"
    assert csv_text(kb).splitlines()[0] == out.read_text(encoding="utf-8").splitlines()[0]


def test_actions_and_telegram_text(tmp_path):
    mnq = offline_contract("MNQ")
    cfg = BotConfig.model_validate({"data_dir": str(tmp_path)})
    now = ct(2026, 10, 7, 9, 0)
    core = build_core(cfg, mnq, PaperBroker(mnq, 50_000), clock=lambda: now, account_label="T")
    run(core.begin_day(core.schedule.trading_day(now), 50_000))
    actions = BotActions(core, Controls())
    assert run(actions.handle("insights", {})) == {"report": None}
    with pytest.raises(RuntimeError, match="turned off"):
        run(actions.handle("insights_csv", {}))
    kb = KnowledgeBase(tmp_path / "knowledge_MNQ_5m.json", min_samples=2)
    for _ in range(6):
        kb.record(observation("ema_trend", 0.4, cost_r=0.05))
    core.attach_knowledge(kb)
    rep = run(actions.handle("insights", {}))["report"]
    assert rep["strategies"][0]["n"] == 6
    exported = run(actions.handle("insights_csv", {}))
    assert exported["filename"] == "knowledge_MNQ_5m.csv" and exported["csv"].count("\n") == 7
    text = run(actions.handle("knowledge_text", {}))["text"]
    assert "Knowledge base" in text and "What the bot learned" in text and "+0.35R over 6" in text
