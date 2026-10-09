"""Manual trades from the dashboard's trade ticket: suggestions, risk guards and learning."""

from datetime import timedelta

import httpx
import pytest

from topstep_bot.broker.paper import PaperBroker
from topstep_bot.config import BotConfig
from topstep_bot.control import BotActions
from topstep_bot.factory import build_core
from topstep_bot.instruments import offline_contract
from topstep_bot.journal import Journal
from topstep_bot.knowledge import MANUAL, KnowledgeBase, Observation
from topstep_bot.live import Controls
from topstep_bot.manual import TicketError
from topstep_bot.models import OrderSide
from topstep_bot.recommendations import Recommendation, RecommendationBook

from .conftest import ct, run


def make_core(tmp_path, *, price=100.0, knowledge=True, **cfg_kw):
    mnq = offline_contract("MNQ")
    cfg = BotConfig.model_validate({"data_dir": str(tmp_path), **cfg_kw})
    now = [ct(2026, 3, 3, 9, 0)]  # a Tuesday, inside the default trading window ("open" slot)
    broker = PaperBroker(mnq, 50_000, slippage_ticks=0, fees_round_turn=1.22, live=True)
    core = build_core(cfg, mnq, broker, clock=lambda: now[0], account_label="T", journal=Journal(tmp_path / "j.db"))
    core.balance = 50_000
    core.recommender = RecommendationBook(core)
    if knowledge:
        core.attach_knowledge(KnowledgeBase(tmp_path / "kb.json", min_samples=4))
    run(core.begin_day(core.schedule.trading_day(now[0]), 50_000))
    run(broker.on_price(now[0], price))
    core.last_price = price
    return core, broker, now


def idea(core, rid, strategy, side, stop, target=None, minutes_ago=1):
    rec = Recommendation(id=rid, created=core.clock() - timedelta(minutes=minutes_ago), strategy=strategy,
                         title=strategy.replace("_", " ").title(), active=False, side=side, entry=core.last_price,
                         stop=stop, target=target, size=2, risk_usd=50.0, reason="test idea", status="idea")
    core.recommender.items.appendleft(rec)
    return rec


def seed(kb, strategy, r, n, side="LONG", slot="open", regime="calm", day="2026-03-02"):
    for i in range(n):
        kb.record(Observation(day=day, time=f"09:{i:02d}", strategy=strategy, side=side, slot=slot, regime=regime,
                              r=r, usd=r * 50, source="train"), save=False)


# ------------------------------------------------------------------ the ticket

def test_ticket_suggests_a_stop_sizes_it_and_lists_the_rules(tmp_path):
    core, _, _ = make_core(tmp_path)
    t = core.manual.ticket("buy")
    assert t["ok"] and t["blocked"] is None and t["problem"] is None
    assert t["side"] == "LONG" and t["price"] == 100.0 and t["contract"] == core.contract.name
    sug = t["suggested"]
    assert sug["stop"] < 100.0 < sug["target"] and "tick" in sug["basis"]  # no ATR yet: a default distance
    p = t["plan"]
    assert p["size"] >= 1 and p["size"] == p["max_size"] and p["risk_usd"] <= core.cfg.risk.risk_per_trade
    assert p["rr"] == pytest.approx(1.5, abs=0.05)
    texts = " | ".join(c["text"] for c in t["checks"])
    assert "trading window" in texts and "Daily loss room" in texts and "Maximum Loss Limit" in texts
    assert "Consistency guard" in texts and "Position limit" in texts
    assert all(c["ok"] is not False for c in t["checks"])

    # your own stop and size: the size is capped by the risk rules, never raised
    t2 = core.manual.ticket("short", stop="104", size="500")
    assert t2["ok"] and t2["plan"]["stop"] == 104.0 and t2["plan"]["size"] < 500
    assert any("size cut" in n for n in t2["plan"]["notes"])
    assert t2["plan"]["target"] < 100.0  # target follows your stop at the default reward-to-risk

    with pytest.raises(TicketError, match="Buy"):
        core.manual.ticket("sideways")
    with pytest.raises(TicketError, match="price"):
        core.manual.ticket("buy", stop="abc")
    bad = core.manual.ticket("buy", stop="105")  # a long's stop above the price
    assert not bad["ok"] and "wrong side" in bad["problem"]


def test_ticket_prefers_the_best_live_idea_on_that_side(tmp_path):
    core, _, _ = make_core(tmp_path)
    seed(core.knowledge, "ema_trend", 0.6, 10)  # working at open/calm
    seed(core.knowledge, "vwap_reversion", -0.5, 10)  # losing there
    idea(core, "R1", "vwap_reversion", OrderSide.BUY, stop=97.0, target=110.0)
    idea(core, "R2", "ema_trend", OrderSide.BUY, stop=95.5, target=108.0)
    idea(core, "R3", "orb", OrderSide.SELL, stop=103.0, minutes_ago=30)  # too old to count
    t = core.manual.ticket("long")
    assert t["suggested"]["stop"] == 95.5 and t["suggested"]["target"] == 108.0
    assert "Ema Trend idea" in t["suggested"]["basis"]
    k = t["knowledge"]
    assert k["slot"] == "open" and k["regime"] == "calm"
    assert k["strategies"][0]["name"] == "ema_trend" and k["strategies"][0]["allowed"]
    assert {i["id"] for i in k["ideas"]} == {"R1", "R2"}
    assert k["verdict"]["label"] == "Supported"
    assert k["sides"]["LONG"]["n"] == 20

    against = core.manual.ticket("short", stop="104")
    assert against["knowledge"]["verdict"]["label"] == "Evidence against"
    assert any("working at this time" in r["text"] for r in against["knowledge"]["reasons"])


def test_ticket_without_knowledge_or_ideas_says_so(tmp_path):
    core, _, _ = make_core(tmp_path, knowledge=False)
    k = core.manual.ticket("buy")["knowledge"]
    assert k["enabled"] is False and k["verdict"]["label"] == "No clear evidence"


def test_ticket_shows_blocks_from_the_same_risk_rules_as_the_bot(tmp_path):
    core, _, now = make_core(tmp_path)
    core.risk.paused = True  # pausing automatic entries doesn't block manual trades...
    t = core.manual.ticket("buy")
    assert t["ok"] and any("paused" in c["text"] and c["ok"] is None for c in t["checks"])
    core.risk.lock("personal daily loss limit $300 reached")  # ...but a loss limit does
    t = core.manual.ticket("buy")
    assert not t["ok"] and "daily loss limit" in t["blocked"]
    with pytest.raises(TicketError, match="daily loss limit"):
        run(core.manual.open("buy", 95, None, None, "test"))
    core.risk.lock_reason = None
    now[0] = ct(2026, 3, 3, 14, 50)  # after the last entry time
    t = core.manual.ticket("buy")
    assert not t["ok"] and "last_entry" in t["blocked"]
    assert any(c["ok"] is False for c in t["checks"])
    core.halted = "flatten requested from dashboard"
    assert "halted" in core.manual.ticket("buy")["blocked"]


# ------------------------------------------------------------------ placing and managing

def test_manual_trade_goes_through_the_risk_rules_and_the_order_guard(tmp_path):
    core, broker, now = make_core(tmp_path)
    message = run(core.manual.open("buy", "95", "110", "500", "dashboard", note="  breakout   retest "))
    run(broker.drain())
    t = core.orders.trade
    assert "Manual LONG" in message and t is not None and t.strategy == MANUAL
    assert 1 <= core.orders.position <= core.risk.max_contracts() and core.orders.position < 500
    assert t.planned_risk <= core.cfg.risk.risk_per_trade and t.stop_price == 95.0 and t.target_price == 110.0
    assert t.reason == "manual from dashboard: breakout retest"
    assert not core.owns_trade()  # the auto-traded strategy never exits it
    rec = core.recommender.items[0]
    assert rec.strategy == MANUAL and rec.status == "taken" and rec.trade_tag == t.tag and not rec.hypothetical
    snap = core.snapshot()
    assert snap["trade"]["strategy"] == MANUAL and snap["manual_block"].startswith("a trade is already open")
    assert "r_now" in snap["trade"] and snap["contract_info"]["tick_size"] == core.contract.tick_size

    with pytest.raises(TicketError, match="already open"):
        run(core.manual.open("buy", "95", None, None, "dashboard"))
    with pytest.raises(TicketError, match="stop"):
        run(core.manual.open("buy", None, None, None, "dashboard"))

    # the order guard still sits in front of the order path
    run(core.manual.close("test"))
    run(broker.drain())
    now[0] += timedelta(minutes=11)
    core.orders.guard.tripped = "too many order actions"
    with pytest.raises(TicketError, match="order guard"):
        run(core.manual.open("buy", "95", None, None, "dashboard"))


def test_close_keeps_the_bot_running_and_breakeven_waits_for_profit(tmp_path):
    core, broker, now = make_core(tmp_path)
    run(core.manual.open("buy", "95", None, "1", "dashboard"))
    run(broker.drain())
    with pytest.raises(TicketError, match="above the entry"):
        run(core.manual.stop_to_breakeven("dashboard"))
    assert core.snapshot()["trade"]["breakeven_ok"] is False
    run(broker.on_price(now[0], 103.0))
    core.last_price = 103.0
    assert core.snapshot()["trade"]["breakeven_ok"] is True
    assert "breakeven at 100.0" in run(core.manual.stop_to_breakeven("dashboard"))
    assert core.orders.trade.stop_price == 100.0
    assert "already" in run(core.manual.stop_to_breakeven("dashboard"))
    message = run(core.manual.close("dashboard"))
    run(broker.drain())
    assert "keeps running" in message and core.orders.is_flat and core.halted is None
    assert "No open trade" in run(core.manual.close("dashboard"))
    closed = core.snapshot()["trades_today"]
    assert closed and closed[0]["strategy"] == MANUAL and closed[0]["exit_reason"] == "closed from dashboard"


# ------------------------------------------------------------------ learning

def test_manual_results_go_into_the_knowledge_base_tagged_manual(tmp_path):
    core, broker, now = make_core(tmp_path)
    kb = core.knowledge
    run(core.manual.open("sell", "104", "92", "1", "dashboard"))
    run(broker.drain())
    run(broker.on_price(now[0], 98.0))
    core.last_price = 98.0
    run(core.manual.close("dashboard"))
    run(broker.drain())
    mine = [o for o in kb.obs if o.strategy == MANUAL]
    assert len(mine) == 1  # recorded once (not again by the recommendation book)
    o = mine[0]
    assert o.source == "manual" and o.side == "SHORT" and o.slot == "open" and o.r == pytest.approx(0.5)
    assert kb.counts()["manual"] == 1
    rec = core.recommender.items[0]
    assert rec.strategy == MANUAL and rec.result == "won"
    summary = core.snapshot()["knowledge"]
    assert summary["manual"]["overall"]["n"] == 1 and summary["manual"]["recent"][0]["side"] == "SHORT"
    assert "your manual trades 1" in core.knowledge_text()
    # manual observations never count as a strategy's evidence and survive retraining
    assert kb.stats("ema_trend").n == 0
    from datetime import date

    kb.replace_training([], first_day=date(2026, 1, 1), last_day=date(2026, 3, 3), days=40, bars=1, symbol="MNQ",
                        timeframe=5)
    assert kb.counts()["manual"] == 1
    reloaded = KnowledgeBase(tmp_path / "kb.json")
    assert reloaded.counts()["manual"] == 1


def test_your_manual_record_feeds_the_ticket(tmp_path):
    core, _, _ = make_core(tmp_path)
    for _ in range(6):
        core.knowledge.record(Observation(day="2026-03-02", time="09:05", strategy=MANUAL, side="LONG", slot="open",
                                          regime="calm", r=-1.0, usd=-50.0, source="manual"), save=False)
    k = core.manual.ticket("buy")["knowledge"]
    assert k["manual"]["here"]["n"] == 6 and k["manual"]["here"]["mean_r"] == -1.0
    assert k["verdict"]["label"] == "Evidence against"
    assert any("Your manual trades" in r["text"] for r in k["reasons"])
    assert k["sides"]["LONG"]["n"] == 0  # your trades aren't mixed into the strategies' side statistics


# ------------------------------------------------------------------ through the bot's API

def test_trade_ticket_actions_through_the_bot_api(tmp_path):
    from topstep_bot.worker_api import build_worker_api

    core, broker, _ = make_core(tmp_path)
    actions = BotActions(core, Controls())

    async def go():
        server = build_worker_api(actions, core.snapshot, 0, "tok")
        await server.start()
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{server.port}", headers={"X-Token": "tok"}) as client:
            ticket = (await client.post("/action/trade_ticket", json={"side": "long"})).json()
            bad = (await client.post("/action/manual_trade", json={"side": "long", "stop": "", "source": "dashboard"})).json()
            stop = ticket["ticket"]["suggested"]["stop"]
            placed = (await client.post("/action/manual_trade", json={"side": "long", "stop": stop, "size": 1,
                                                                       "source": "dashboard"})).json()
            await broker.drain()
            status = (await client.get("/status")).json()
            closed = (await client.post("/action/close_trade", json={"source": "dashboard"})).json()
            await broker.drain()
        await server.stop()
        return ticket, bad, placed, status, closed

    ticket, bad, placed, status, closed = run(go())
    assert ticket["ok"] and ticket["ticket"]["ok"] and ticket["ticket"]["plan"]["size"] >= 1
    assert bad["ok"] is False and "protective stop" in bad["message"]
    assert placed["ok"] and placed["message"].startswith("Manual LONG 1")
    assert status["bot"]["trade"]["strategy"] == MANUAL
    assert closed["ok"] and core.orders.is_flat
