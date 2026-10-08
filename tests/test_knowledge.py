"""Knowledge base, training, and the adaptive all-day strategy."""

import json
from datetime import date, time, timedelta

import httpx
import pytest

from topstep_bot.backtest.data import synthetic_bars
from topstep_bot.backtest.runner import run_backtest
from topstep_bot.bars import resample
from topstep_bot.broker.paper import PaperBroker
from topstep_bot.config import BotConfig, TelegramConfig
from topstep_bot.control import BotActions
from topstep_bot.factory import build_core
from topstep_bot.instruments import offline_contract
from topstep_bot.knowledge import KnowledgeBase, Observation, RegimeTracker, slot_for, train_from_bars
from topstep_bot.live import Controls
from topstep_bot.models import Bar, Signal
from topstep_bot.recommendations import RecommendationBook
from topstep_bot.strategies import BASE_STRATEGIES, STRATEGIES, StrategyContext, create_strategy
from topstep_bot.telegram_control import TelegramController

from .conftest import CT, ct, run
from .test_telegram_control import CHAT, FakeTelegram, msg

TODAY = date(2026, 10, 8)
BARS_1M = synthetic_bars("MNQ", days=45, seed=11, end=TODAY)


def obs(strategy, slot, regime, r, day=TODAY, source="shadow"):
    return Observation(day.isoformat(), "09:00", strategy, "LONG", slot, regime, r, r * 50, source)


# ------------------------------------------------------------------ building blocks

def test_slots_and_regime():
    assert slot_for(time(8, 30)) == "open" and slot_for(time(9, 59)) == "open"
    assert slot_for(time(10, 0)) == "midday" and slot_for(time(13, 0)) == "close"
    assert slot_for(time(15, 10)) == "off" and slot_for(time(7, 0)) == "off"
    rt = RegimeTracker(fast=3, slow=10)
    for _ in range(20):
        rt.update(101, 99, 100)
    assert rt.value == "calm"
    for _ in range(3):
        rt.update(104, 96, 100)  # range doubles -> volatile
    assert rt.value == "volatile" and rt.ratio > 1.2


def test_verdict_uses_most_specific_evidence_and_blocks_losers():
    kb = KnowledgeBase(None, min_samples=4, min_edge_r=0.05, half_life_days=1000)
    for _ in range(6):
        kb.record(obs("orb", "open", "calm", 0.8))
    for _ in range(6):
        kb.record(obs("orb", "midday", "calm", -0.5))
    v = kb.verdict("orb", "open", "calm", TODAY)
    assert v.allowed and v.level == "cell"
    v = kb.verdict("orb", "midday", "calm", TODAY)
    assert not v.allowed and v.level == "cell" and "not working" in v.why
    # unseen regime in a slot falls back to the slot, then to the strategy overall
    assert kb.verdict("orb", "open", "volatile", TODAY).level == "slot"
    assert kb.verdict("orb", "close", "calm", TODAY).level == "strategy"
    assert kb.verdict("ema_trend", "open", "calm", TODAY).level == "unproven"
    assert kb.allowed_now(["orb", "ema_trend"], "open", "calm", TODAY) == ["orb"]


def test_recency_and_real_trade_weights():
    kb = KnowledgeBase(None, min_samples=1, half_life_days=10, real_weight=2.0)
    kb.record(obs("orb", "open", "calm", 1.0, day=TODAY - timedelta(days=10)))
    kb.record(obs("orb", "open", "calm", -1.0, day=TODAY, source="real"))
    s = kb.stats("orb", "open", "calm", TODAY)
    assert s.n == 2 and s.n_eff == pytest.approx(2.5)  # 0.5 (10 days old) + 2.0 (real trade)
    assert s.mean_r == pytest.approx((0.5 * 1.0 - 2.0 * 1.0) / 2.5)


def test_persistence_and_retraining_replaces_only_the_training_layer(tmp_path):
    path = tmp_path / "kb.json"
    kb = KnowledgeBase(path, min_samples=2)
    kb.record(obs("orb", "open", "calm", 0.5, day=date(2026, 9, 1), source="train"))
    kb.record(obs("orb", "open", "calm", 0.5, day=date(2026, 9, 20)))  # shadow, inside the new training range
    kb.record(obs("orb", "open", "calm", 0.5, day=date(2026, 10, 7)))  # shadow, after it
    kb.record(obs("orb", "open", "calm", -1.0, day=date(2026, 9, 20), source="real"))
    kb.replace_training([obs("ema_trend", "open", "calm", 0.2, day=date(2026, 9, 15))], first_day=date(2026, 9, 1),
                        last_day=date(2026, 10, 1), days=22, bars=5000, symbol="MNQ", timeframe=5, at=ct(2026, 10, 7, 16, 5))
    again = KnowledgeBase(path)
    sources = sorted((o.strategy, o.source, o.day) for o in again.obs)
    assert sources == [("ema_trend", "train", "2026-09-15"), ("orb", "real", "2026-09-20"), ("orb", "shadow", "2026-10-07")]
    assert again.trained["days"] == 22 and not again.training_due(ct(2026, 10, 8, 9, 0), retrain_hours=20)
    assert again.training_due(ct(2026, 10, 8, 16, 10), retrain_hours=20)  # a day later: refresh after the close
    assert KnowledgeBase(tmp_path / "missing.json").training_due(ct(2026, 10, 8, 9, 0), 20)


# ------------------------------------------------------------------ training

def test_training_records_every_strategy_without_trading(tmp_path):
    cfg = BotConfig.model_validate({"data_dir": str(tmp_path)})
    kb = KnowledgeBase(tmp_path / "kb.json", min_samples=3)
    result = run(train_from_bars(cfg, offline_contract("MNQ"), BARS_1M, kb))
    assert result["observations"] == len(kb.obs) > 20
    assert set(result["per_strategy"]) == set(BASE_STRATEGIES)
    assert all(o.source == "train" and o.slot in ("open", "midday", "close") for o in kb.obs)
    assert all(o.why in ("stop hit", "target hit", "session end") or o.why.startswith("strategy exit") for o in kb.obs)
    assert kb.trained and kb.trained["days"] == result["days"]
    saved = json.loads((tmp_path / "kb.json").read_text(encoding="utf-8"))
    assert saved["trained"]["observations"] == result["observations"]
    with pytest.raises(ValueError, match="trading days"):
        run(train_from_bars(cfg, offline_contract("MNQ"), BARS_1M[: 10 * 1380], kb))


# ------------------------------------------------------------------ adaptive strategy

def make_adaptive(kb):
    mnq = offline_contract("MNQ")
    strat = create_strategy("adaptive", {}, mnq, 5)
    strat.bind_knowledge(kb, lambda: "calm")
    return strat


def feed(strat, day, hh, mm, o, h, lo, c, position=0):
    start = ct(day.year, day.month, day.day, hh, 0) + timedelta(minutes=mm)
    close = start + timedelta(minutes=5)
    ctx = StrategyContext(close, close.astimezone(CT), day, position, None, None)
    return strat.on_bar(Bar(start, o, h, lo, c, 100), ctx)


def test_adaptive_trades_only_proven_sub_strategies():
    kb = KnowledgeBase(None, min_samples=3, half_life_days=1000)
    day = date(2026, 10, 7)
    strat = make_adaptive(kb)
    assert {s.name for s in strat.subs} == set(BASE_STRATEGIES) and strat.warmup_days >= 15
    strat.on_new_day(day)

    def orb_breakout(s):
        # 15-minute opening range 100-105, then a close above it at 08:50
        s.on_new_day(day)
        for mm in (30, 35, 40):
            feed(s, day, 8, mm, 100, 105, 100, 102)
        return feed(s, day, 8, 45, 102, 110, 101, 109)

    assert orb_breakout(strat) is None  # nothing is proven yet
    assert strat.decisions and not strat.decisions[0]["allowed"] and "unproven" in strat.decisions[0]["why"]
    for _ in range(5):
        kb.record(obs("orb", "open", "calm", 1.0, day=day))
    sig = orb_breakout(strat)
    assert sig is not None and sig.action == "long" and sig.meta["strategy"] == "orb" and sig.meta["slot"] == "open"
    assert "Opening Range Breakout" in sig.reason and strat.owner == "orb"
    # once proven losers are switched off, even if the overall picture is fine
    for _ in range(8):
        kb.record(obs("orb", "open", "calm", -1.0, day=day))
    assert orb_breakout(strat) is None
    assert "not working" in strat.decisions[0]["why"]


def test_adaptive_without_knowledge_trades_nothing_unless_told_to():
    mnq = offline_contract("MNQ")
    day = date(2026, 10, 7)
    cautious = create_strategy("adaptive", {"strategies": ["orb"]}, mnq, 5)
    bold = create_strategy("adaptive", {"strategies": ["orb"], "trade_unproven": True}, mnq, 5)
    for s in (cautious, bold):
        s.on_new_day(day)
        for mm in (30, 35, 40):
            feed(s, day, 8, mm, 100, 105, 100, 102)
    assert feed(cautious, day, 8, 45, 102, 110, 101, 109) is None
    assert feed(bold, day, 8, 45, 102, 110, 101, 109).action == "long"
    with pytest.raises(ValueError, match="unknown strategy"):
        create_strategy("adaptive", {"strategies": ["nope"]}, mnq, 5)


def test_adaptive_backtest_is_walk_forward_and_learns():
    cfg = BotConfig.model_validate({"strategy": {"name": "adaptive"}, "knowledge": {"min_samples": 3}})
    res = run(run_backtest(cfg, BARS_1M, offline_contract("MNQ")))
    assert res.final_balance - res.starting_balance == pytest.approx(sum(t.net_pnl for t in res.trades), abs=0.01)
    # every trade was opened by a sub-strategy that had evidence at the time
    assert all("[" in t.reason and "R avg over" in t.reason for t in res.trades)
    for t in res.trades:
        assert t.filled_size <= 50


def test_bot_learns_while_running_and_dedupes_its_own_signals(tmp_path):
    """Live-style run: shadows + the active strategy feed the knowledge base; nothing is counted twice."""
    mnq = offline_contract("MNQ")
    cfg = BotConfig.model_validate({"data_dir": str(tmp_path), "strategy": {"name": "adaptive", "params": {"trade_unproven": True}}})
    now = [ct(2026, 9, 1, 9, 0)]
    broker = PaperBroker(mnq, 50_000, slippage_ticks=0, fees_round_turn=1.22)
    core = build_core(cfg, mnq, broker, clock=lambda: now[0], account_label="T")
    core.balance = 50_000
    core.recommender = RecommendationBook(core)
    kb = KnowledgeBase(tmp_path / "kb.json", min_samples=5)
    core.attach_knowledge(kb)
    bars = list(resample(BARS_1M, 5))
    tf = timedelta(minutes=5)
    days = sorted({core.schedule.trading_day(b.ts) for b in bars})
    warm = set(days[:16])

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
    assert core.closed_trades, "trade_unproven should have produced trades"
    real = [o for o in kb.obs if o.source == "real"]
    shadow = [o for o in kb.obs if o.source == "shadow"]
    assert real and shadow and all(o.strategy in BASE_STRATEGIES for o in kb.obs)
    # the same signal never appears both as the bot's trade and as a shadow idea
    keys = [(r.strategy, r.created) for r in core.recommender.items if r.result]
    assert len(keys) == len(set(keys))
    assert (tmp_path / "kb.json").exists()
    snap = core.snapshot()
    assert snap["knowledge"]["total"] == len(kb.obs) and snap["regime"] in ("calm", "volatile")
    assert json.dumps(snap, default=str)


# ------------------------------------------------------------------ control surfaces

def test_knowledge_actions_and_telegram(tmp_path):
    mnq = offline_contract("MNQ")
    cfg = BotConfig.model_validate({"data_dir": str(tmp_path)})
    now = ct(2026, 10, 7, 9, 0)
    core = build_core(cfg, mnq, PaperBroker(mnq, 50_000), clock=lambda: now, account_label="T")
    run(core.begin_day(core.schedule.trading_day(now), 50_000))
    kb = KnowledgeBase(None, min_samples=2)
    for _ in range(3):
        kb.record(obs("ema_trend", "open", "calm", 0.4, day=date(2026, 10, 6)))
    core.attach_knowledge(kb)
    calls = []

    async def retrain(source):
        calls.append(source)
        return "trained"

    actions = BotActions(core, Controls(), retrain=retrain)
    text = run(actions.handle("knowledge_text", {}))["text"]
    assert "3 observations" in text and "EMA Trend Crossover" in text and "✅" in text
    assert run(actions.handle("train", {"source": "test"})) == "trained" and calls == ["test"]
    with pytest.raises(RuntimeError, match="only available"):
        run(BotActions(core, Controls()).train("x"))

    fake = FakeTelegram()
    tg = TelegramController("TOKEN", CHAT, actions, TelegramConfig(), transport=httpx.MockTransport(fake.handler))
    run(tg.handle_update(msg("/knowledge")))
    assert "Knowledge base" in fake.sent[-1]["text"]

    async def train():
        await tg.handle_update(msg("/train"))
        await tg.idle()  # training answers in the background, so Telegram keeps working meanwhile
    run(train())
    assert fake.sent[-1]["text"] == "trained" and calls[-1].startswith("Telegram")


def test_vwap_pullback_signals_resumption_after_pullback(mnq):
    strat = create_strategy("vwap_pullback", {"trend_ema": 3, "atr_period": 3, "min_minutes_after_open": 0, "band_k": 1.0}, mnq, 5)
    day = date(2026, 10, 7)
    strat.on_new_day(day)
    price = 100.0
    sig = None
    # steady uptrend, a dip back to VWAP, then a close above the previous bar's high
    for i in range(8):
        price += 1
        sig = feed(strat, day, 8, 30 + 5 * i, price - 1, price + 0.2, price - 1.2, price)
        assert sig is None
    vwap = strat.vwap.value
    feed(strat, day, 9, 10, price, price, vwap - 0.5, vwap + 0.2)  # pullback into the band
    assert strat.state()["setup"] == "long"
    sig = feed(strat, day, 9, 15, vwap + 0.2, price + 1.5, vwap, price + 1.2)
    assert sig is not None and sig.action == "long" and sig.stop_price < vwap - 0.5 and sig.target_price > sig.stop_price
    assert feed(strat, day, 9, 20, price, price, vwap - 3, vwap - 2, position=1).action == "exit"


def test_all_strategies_are_registered_and_describable():
    assert list(STRATEGIES)[0] == "adaptive" and "vwap_pullback" in STRATEGIES
    for cls in STRATEGIES.values():
        assert cls.title and cls.description and isinstance(cls.defaults, dict)
    assert create_strategy("adaptive", {}, offline_contract("MNQ"), 5).state()["managing"] == "-"
    assert isinstance(Signal("long", 1.0).meta, dict)
