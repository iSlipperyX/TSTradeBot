"""The first trade after the bot starts (first_trade.py): timing, what it follows, risk guards, learning."""

import pytest

from topstep_bot.backtest.data import synthetic_bars
from topstep_bot.backtest.first_trade_test import first_trade_test, result_text
from topstep_bot.broker.paper import PaperBroker
from topstep_bot.config import BotConfig
from topstep_bot.factory import build_core
from topstep_bot.first_trade import FirstTradePlanner, rank
from topstep_bot.instruments import offline_contract
from topstep_bot.journal import Journal
from topstep_bot.knowledge import FIRST_TRADE, KnowledgeBase, Observation
from topstep_bot.models import OrderSide
from topstep_bot.recommendations import Recommendation, RecommendationBook

from .conftest import ct, run

LEARN = {"strategy": {"name": "adaptive", "params": {"trade_unproven": True}}, "first_trade": {"enabled": True}}


def make(tmp_path, start, *, price=100.0, restart=False, journal=None, **cfg_kw):
    """A paper core with a knowledge base and a first-trade planner, as the live runner builds it, started at ``start``."""
    mnq = offline_contract("MNQ")
    cfg = BotConfig.model_validate({"data_dir": str(tmp_path), **LEARN, **cfg_kw})
    now = [start]
    broker = PaperBroker(mnq, 50_000, slippage_ticks=0, fees_round_turn=1.22, live=True)
    journal = journal or Journal(tmp_path / "j.db")
    core = build_core(cfg, mnq, broker, clock=lambda: now[0], account_label="T", journal=journal)
    core.balance = 50_000
    core.recommender = RecommendationBook(core)
    core.attach_knowledge(KnowledgeBase(tmp_path / "kb.json", min_samples=4))
    run(core.begin_day(core.schedule.trading_day(now[0]), 50_000))
    run(broker.on_price(now[0], price))
    core.last_price = price
    planner = FirstTradePlanner(core, cfg.first_trade, journal=journal)
    core.first_trade = planner
    planner.request(start, restart=restart)
    return core, broker, now, planner


def seed(kb, strategy, r, n, side="LONG", slot="open", regime="calm", day="2026-03-02"):
    for _ in range(n):
        kb.record(Observation(day=day, time="09:00", strategy=strategy, side=side, slot=slot, regime=regime,
                              r=r, usd=r * 50, source="train"), save=False)


def idea(core, strategy, side, stop, target=None):
    rec = Recommendation(id=f"R{strategy}", created=core.clock(), strategy=strategy, title=strategy.title(), active=False,
                         side=side, entry=core.last_price, stop=stop, target=target, size=2, risk_usd=50.0,
                         reason="test idea", status="idea")
    core.recommender.items.appendleft(rec)
    return rec


def bar_close(core, now, planner, t):
    """Advance the clock to ``t`` (a bar close) and let the planner see it."""
    now[0] = t
    run(planner.on_bar_closed(t))
    run(planner.on_clock(t))


# ------------------------------------------------------------------ when

def test_outside_trading_hours_it_waits_for_the_session_and_says_so(tmp_path):
    saturday = ct(2026, 3, 7, 11, 0)
    core, _, now, planner = make(tmp_path, saturday)
    assert planner.state == "waiting"
    assert "Outside trading hours" in planner.message and "Mon 08:30 CT" in planner.message
    assert planner.anchor == ct(2026, 3, 9, 8, 30) and planner.deadline == ct(2026, 3, 9, 8, 45)
    view = planner.view()
    assert view["state"] == "waiting" and view["deadline_local"] == "Mon 08:45 CT" and view["seconds_left"] > 0
    run(planner.on_clock(ct(2026, 3, 9, 8, 30)))
    assert planner.state == "watching" and "08:45" in planner.message

    early = make(tmp_path / "early", ct(2026, 3, 3, 6, 50))[3]  # before trade_start on a weekday
    assert early.state == "waiting" and early.deadline == ct(2026, 3, 3, 8, 45)
    late = make(tmp_path / "late", ct(2026, 3, 3, 14, 25))[3]  # 5 minutes before the last entry: due before it
    assert late.state == "watching" and late.deadline == ct(2026, 3, 3, 14, 29)
    evening = make(tmp_path / "evening", ct(2026, 3, 3, 15, 30))[3]  # after the last entry: tomorrow
    assert evening.state == "waiting" and evening.deadline == ct(2026, 3, 4, 8, 45)
    holiday = make(tmp_path / "holiday", ct(2026, 12, 24, 9, 0))[3]  # a no-trade date
    assert holiday.anchor == ct(2026, 12, 28, 8, 30)


def test_it_acts_on_the_last_bar_before_the_deadline(tmp_path):
    core, broker, now, planner = make(tmp_path, ct(2026, 3, 3, 9, 2))  # Tuesday 09:02: due by 09:17
    bar_close(core, now, planner, ct(2026, 3, 3, 9, 5))
    bar_close(core, now, planner, ct(2026, 3, 3, 9, 10))
    assert planner.state == "watching" and core.orders.is_flat
    bar_close(core, now, planner, ct(2026, 3, 3, 9, 15))  # the next bar closes after 09:17: now
    t = core.orders.trade
    assert t is not None and t.strategy == FIRST_TRADE and t.size == 1
    assert planner.state == "done" and planner.taken["by"] == "first_trade" and planner.taken["size"] == 1


def test_the_strategy_trading_first_is_the_first_trade(tmp_path):
    core, _, now, planner = make(tmp_path, ct(2026, 3, 3, 9, 0))
    run(core.orders.enter(OrderSide.BUY, 1, 98.0, 104.0, "strategy signal", ref_price=100.0))
    bar_close(core, now, planner, ct(2026, 3, 3, 9, 5))
    assert planner.state == "done" and "strategy traded first" in planner.message
    assert core.orders.trade.strategy != FIRST_TRADE and planner.outcomes["strategy"] == 1
    assert core.journal.get_state("first_trade:T")["pending"] is None


# ------------------------------------------------------------------ what it follows

def test_it_follows_the_best_supported_signal_at_one_contract(tmp_path):
    core, broker, now, planner = make(tmp_path, ct(2026, 3, 3, 9, 0))
    kb = core.knowledge
    seed(kb, "orb_momentum", 0.6, 10)  # strongly positive at open/calm
    seed(kb, "vwap_reversion", 0.1, 10)
    now[0] = ct(2026, 3, 3, 9, 14)
    idea(core, "vwap_reversion", OrderSide.SELL, 102.0)
    idea(core, "orb_momentum", OrderSide.BUY, 97.0, 110.0)
    ranked = [c for c in rank(core, 100.0, now[0]) if not c.excluded]
    assert ranked[0].strategy == "orb_momentum" and ranked[0].kind == "signal"
    assert "+0.60R" in ranked[0].evidence

    bar_close(core, now, planner, ct(2026, 3, 3, 9, 15))
    t = core.orders.trade
    assert t.side == OrderSide.BUY and t.size == 1 and t.strategy == FIRST_TRADE
    assert t.stop_price == 97.0 and t.target_price == 110.0  # a signal keeps its own levels
    assert "Opening Range Momentum (5-min ORB) LONG signal" in t.reason and "its record +0.60R avg over 10 at open/calm" in t.reason
    assert planner.taken["basis"] == "orb_momentum long signal"
    rec = core.recommender.items[0]  # listed with the ideas so its result shows on the scoreboard
    assert rec.strategy == FIRST_TRADE and rec.status == "taken" and rec.trade_tag == t.tag
    assert any("First trade after starting" in e["message"] for e in core.events)


def test_a_strategy_switched_off_here_is_never_followed(tmp_path):
    core, _, now, planner = make(tmp_path, ct(2026, 3, 3, 9, 0))
    seed(core.knowledge, "orb_momentum", -0.8, 10, side="SHORT")  # losing at open/calm
    seed(core.knowledge, "noise_breakout", -0.5, 10, side="SHORT")  # shorts have been poor here
    now[0] = ct(2026, 3, 3, 9, 14)
    idea(core, "orb_momentum", OrderSide.BUY, 97.0)
    ranked = rank(core, 100.0, now[0])
    off = next(c for c in ranked if c.strategy == "orb_momentum")
    assert off.excluded and "switched" in off.excluded and ranked[-1] is off
    bar_close(core, now, planner, ct(2026, 3, 3, 9, 15))
    t = core.orders.trade
    assert t is not None and "orb_momentum" not in planner.taken["basis"]
    assert planner.taken["kind"] == "lean" and t.side == OrderSide.BUY  # no evidence on longs beats losing shorts
    assert t.stop_price < 100.0 < t.target_price  # a lean: an ATR / default stop and 1.5R
    assert (t.target_price - 100.0) == pytest.approx(1.5 * (100.0 - t.stop_price), abs=0.26)


# ------------------------------------------------------------------ the rules still apply

def test_risk_guards_block_it_and_it_tries_again_on_the_next_bar(tmp_path):
    core, _, now, planner = make(tmp_path, ct(2026, 3, 3, 9, 0))
    core.risk.paused = True
    bar_close(core, now, planner, ct(2026, 3, 3, 9, 15))
    assert core.orders.is_flat and planner.state == "blocked" and "paused" in planner.message
    assert planner.view()["state"] == "blocked"
    core.risk.paused = False
    bar_close(core, now, planner, ct(2026, 3, 3, 9, 20))
    assert core.orders.trade is not None and planner.state == "done"


def test_a_session_blocked_to_the_end_carries_over_to_the_next(tmp_path):
    core, _, now, planner = make(tmp_path, ct(2026, 3, 3, 14, 0))
    core.risk.lock("personal daily loss limit $500 reached")
    bar_close(core, now, planner, ct(2026, 3, 3, 14, 15))
    assert planner.state == "blocked"
    run(planner.on_clock(ct(2026, 3, 3, 14, 30)))  # entries close
    assert planner.state == "waiting" and "No first trade that session" in planner.message
    assert planner.deadline == ct(2026, 3, 4, 8, 45) and planner.missed_because["personal daily loss limit $500 reached"] == 1


def test_the_size_is_never_more_than_the_risk_rules_allow(tmp_path):
    core, _, now, planner = make(tmp_path, ct(2026, 3, 3, 9, 0), first_trade={"enabled": True, "contracts": 3},
                                 risk={"risk_per_trade": 20, "personal_daily_loss_limit": 500})
    now[0] = ct(2026, 3, 3, 9, 14)
    idea(core, "orb_momentum", OrderSide.BUY, 95.0)  # $10 a contract at risk: 2 fit in $20, not 3
    seed(core.knowledge, "orb_momentum", 0.5, 10)
    bar_close(core, now, planner, ct(2026, 3, 3, 9, 15))
    t = core.orders.trade
    assert t is not None and t.size <= 2 and t.planned_risk <= 20


def test_at_most_one_per_trading_day_even_after_a_restart(tmp_path):
    core, _, now, planner = make(tmp_path, ct(2026, 3, 3, 9, 0))
    bar_close(core, now, planner, ct(2026, 3, 3, 9, 15))
    assert core.orders.trade is not None
    run(core.orders.flatten_all("test"))
    # started again the same day (a crash or a manual restart)
    core2, _, now2, again = make(tmp_path / "x", ct(2026, 3, 3, 10, 0), journal=core.journal)
    bar_close(core2, now2, again, ct(2026, 3, 3, 10, 15))
    assert core2.orders.is_flat and again.state == "done" and "already made today" in again.message


def test_an_owed_trade_survives_a_maintenance_restart_but_a_quiet_restart_owes_nothing_new(tmp_path):
    journal = Journal(tmp_path / "j.db")
    first = make(tmp_path / "a", ct(2026, 3, 6, 20, 0), journal=journal)[3]  # Friday evening: owed for Monday
    assert first.state == "waiting"
    owed = make(tmp_path / "b", ct(2026, 3, 8, 16, 5), journal=journal, restart=True)[3]  # Sunday maintenance restart
    assert owed.state == "waiting" and owed.deadline == ct(2026, 3, 9, 8, 45)
    owed._done("placed")
    quiet = make(tmp_path / "c", ct(2026, 3, 9, 16, 5), journal=journal, restart=True)[3]
    assert quiet.state == "idle" and "restarted by itself" in quiet.message


def test_off_on_an_express_funded_account(tmp_path):
    core, _, now, planner = make(tmp_path, ct(2026, 3, 3, 9, 0), account={"stage": "express"})
    assert planner.state == "off" and "Express" in planner.message
    bar_close(core, now, planner, ct(2026, 3, 3, 9, 15))
    assert core.orders.is_flat


# ------------------------------------------------------------------ learning

def test_its_result_is_filed_as_a_first_trade_not_as_a_strategy(tmp_path):
    core, broker, now, planner = make(tmp_path, ct(2026, 3, 3, 9, 0))
    seed(core.knowledge, "orb_momentum", 0.5, 10)
    now[0] = ct(2026, 3, 3, 9, 14)
    idea(core, "orb_momentum", OrderSide.BUY, 97.0, 106.0)
    bar_close(core, now, planner, ct(2026, 3, 3, 9, 15))
    t = core.orders.trade
    assert t is not None
    before = core.knowledge.stats("orb_momentum", "open", "calm").n
    now[0] = ct(2026, 3, 3, 9, 30)
    run(broker.on_price(now[0], 96.0))  # stopped out
    run(core.on_price(now[0], 96.0))
    assert core.orders.is_flat
    obs = [o for o in core.knowledge.obs if o.strategy == FIRST_TRADE]
    assert len(obs) == 1 and obs[0].source == "real" and obs[0].basis == "orb_momentum long signal"
    assert obs[0].r < 0 and obs[0].slot == "open"
    assert core.knowledge.stats("orb_momentum", "open", "calm").n == before  # no evidence for the strategy
    assert core.knowledge.side_stats("LONG", "open", "calm").n == before  # nor for longs in general
    s = core.knowledge.summary([("orb_momentum", "ORB")])
    assert s["first_trade"]["n"] == 1
    assert "First trades after starting: 1" in core.knowledge.text([("orb_momentum", "ORB")])
    snap = core.snapshot()
    assert snap["first_trade"]["state"] == "done"


# ------------------------------------------------------------------ the backtest

def test_backtest_compares_the_first_trade_with_a_coin_flip():
    cfg = BotConfig.model_validate({**LEARN, "risk": {"max_trades_per_day": 8, "consistency_guard": False}})
    bars = synthetic_bars("MNQ", days=30, seed=4)
    result = run(first_trade_test(cfg, bars, offline_contract("MNQ")))
    keys = [v["key"] for v in result["variants"]]
    assert keys == ["off", "best", "random"]
    off, best, coin = result["variants"]
    assert "first_trades" not in off
    for v in (best, coin):
        o = v["outcomes"]
        assert o["requested"] > 5 and o["placed"] + o.get("strategy", 0) + o.get("missed", 0) >= o["requested"] - 1
        assert v["first_trades"]["all"]["n"] <= o["placed"]
    assert best["trades"] >= off["trades"]
    text = "\n".join(result_text(result))
    assert "Coin-flip first trade" in text and result["verdict"]
