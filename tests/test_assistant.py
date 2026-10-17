"""The AI trading assistant (assistant.py): switches, when it thinks, guardrails, records, learning and fallbacks."""

from __future__ import annotations

import json
from datetime import timedelta
from types import SimpleNamespace

import pytest

from topstep_bot.assistant import (
    ENTRY_ACTIONS,
    MANAGE_ACTIONS,
    Assistant,
    AssistantDesk,
    AssistantUnavailable,
    ClaudeDecider,
    Decision,
    decision_schema,
    parse_decision,
)
from topstep_bot.broker.paper import PaperBroker
from topstep_bot.config import BotConfig
from topstep_bot.factory import build_core
from topstep_bot.instruments import offline_contract
from topstep_bot.journal import Journal
from topstep_bot.knowledge import ASSISTANT, MANUAL, NOT_STRATEGIES, KnowledgeBase
from topstep_bot.models import OrderSide
from topstep_bot.recommendations import Recommendation, RecommendationBook

from .conftest import bar, ct, run

TUESDAY = ct(2026, 3, 3, 9, 0)


class FakeDecider:
    """Answers with the decisions it was given, in order (an exception is raised instead of returned)."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls: list[tuple[str, dict]] = []

    async def decide(self, kind, situation):
        self.calls.append((kind, situation))
        if not self.answers:
            return Decision("wait" if kind == "entry" else "hold", confidence=0.5, reason="nothing to do", cost=0.01)
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer


def make(tmp_path, *answers, start=TUESDAY, price=100.0, manage=False, trade=False, **assistant):
    """A paper core with a knowledge base, an idea book and the assistant, as the live runner builds it."""
    mnq = offline_contract("MNQ")
    cfg = BotConfig.model_validate({"data_dir": str(tmp_path),
                                    "assistant": {"manage": manage, "trade": trade, **assistant}})
    now = [start]
    broker = PaperBroker(mnq, 50_000, slippage_ticks=0, fees_round_turn=1.22, live=True)
    journal = Journal(tmp_path / "j.db")
    core = build_core(cfg, mnq, broker, clock=lambda: now[0], account_label="T", journal=journal)
    core.balance = 50_000
    core.recommender = RecommendationBook(core)
    core.attach_knowledge(KnowledgeBase(tmp_path / "kb.json", min_samples=4))
    run(core.begin_day(core.schedule.trading_day(now[0]), 50_000))
    run(broker.on_price(now[0], price))
    core.last_price = price
    decider = FakeDecider(*answers)
    desk = AssistantDesk(cfg, journal, "T", decider)
    core.assistant = Assistant(core, desk)
    return core, broker, now, core.assistant, decider


async def _step(a, t):
    await a.on_bar_closed(t)
    if a._task is not None:
        await a._task


def step(core, now, t, price=None):
    """Advance to the bar close ``t`` (optionally at a new price) and let the assistant think it through."""
    now[0] = t
    if price is not None:
        core.last_price = price
    run(_step(core.assistant, t))


def entry(action="enter_long", stop=98.0, target=104.0, confidence=0.8, reason="ORB long has worked here"):
    return Decision(action, stop, target, confidence, reason, ["orb +0.6R over 20 at open/calm"], cost=0.03)


def manage(action, stop=None, confidence=0.8, reason="news is close"):
    return Decision(action, stop, None, confidence, reason, ["CPI at 09:30"], cost=0.02)


def open_trade(core, side=OrderSide.BUY, stop=98.0, target=106.0, strategy="orb", reason="strategy signal"):
    t = run(core.orders.enter(side, 1, stop, target, reason, ref_price=core.last_price, strategy=strategy))
    assert t is not None and core.orders.trade is t
    return t


# ------------------------------------------------------------------ switches and defaults

def test_off_by_default_and_never_asks_while_off(tmp_path):
    assert BotConfig().assistant.manage is False and BotConfig().assistant.trade is False
    core, _, now, a, decider = make(tmp_path)
    assert a.state == "off" and "trades by itself" in a.message
    open_trade(core)
    step(core, now, ct(2026, 3, 3, 9, 5))
    run(core.orders.exit("test"))
    step(core, now, ct(2026, 3, 3, 9, 10))
    assert decider.calls == []
    v = a.view()
    assert v["manage"] is False and v["trade"] is False and v["today"]["calls"] == 0


def test_switches_apply_at_once_on_every_market_of_the_account(tmp_path):
    core, _, now, a, _ = make(tmp_path)
    other = SimpleNamespace(switched=lambda: None)
    a.desk.lanes.append(other)
    text = a.desk.switch(manage=True, source="Telegram")
    assert "watching and managing open trades ON" in text and "Telegram" in text
    assert a.desk.manage and not a.desk.trade and a.state == "ready" and "Watching open trades" in a.message
    assert any("AI assistant: watching" in e["message"] for e in core.events)
    assert "Nothing changed" in a.desk.switch(manage=True)
    assert "The bot trades as usual" in a.desk.switch(manage=False, trade=False)
    assert a.state == "off"


def test_names_and_rules_are_kept_apart_from_the_strategies():
    assert ASSISTANT in NOT_STRATEGIES
    assert set(decision_schema("manage")["properties"]["action"]["enum"]) == set(MANAGE_ACTIONS)
    assert set(decision_schema("entry")["properties"]["action"]["enum"]) == set(ENTRY_ACTIONS)
    s = decision_schema("entry")
    assert s["additionalProperties"] is False and set(s["required"]) == set(s["properties"])


# ------------------------------------------------------------------ its own trades

def test_it_places_its_own_trade_through_the_risk_rules_and_records_why(tmp_path):
    core, broker, now, a, decider = make(tmp_path, entry(), trade=True)
    step(core, now, ct(2026, 3, 3, 9, 5))
    kind, situation = decider.calls[0]
    assert kind == "entry" and situation["asked_because"] == "regular look"
    assert situation["market"]["price"] == 100.0 and situation["rules"]["max_contracts"] == 1
    assert situation["account"]["mll_room"] > 0 and "evidence" in situation and situation["bars"] is not None
    t = core.orders.trade
    assert t is not None and t.strategy == ASSISTANT and t.side == OrderSide.BUY and t.size == 1
    assert t.stop_price == 98.0 and t.target_price == 104.0 and t.reason.startswith("AI assistant:")
    assert t.context  # the market snapshot it learns from
    rec = core.recommender.items[0]
    assert rec.strategy == ASSISTANT and rec.status == "taken" and rec.trade_tag == t.tag and not rec.hypothetical
    saved = core.journal.assistant_decisions(day="2026-03-03")
    assert len(saved) == 1 and saved[0]["status"] == "done" and saved[0]["action"] == "enter_long"
    assert saved[0]["decision"]["reason"] == "ORB long has worked here" and saved[0]["trade"] == t.tag
    assert saved[0]["situation"]["kind"] == "entry"  # what it saw is kept with what it chose
    v = a.view()
    assert v["today"] == {"calls": 1, "cost": 0.03, "trades": 1, "max_trades": 2, "budget": 2.0}
    assert v["decisions"][0]["action"] == "enter_long" and v["decisions"][0]["confidence"] == 0.8
    assert any("AI assistant trade" in e["message"] for e in core.events)


def test_its_trade_is_capped_at_its_size_and_needs_a_real_stop(tmp_path):
    core, _, now, a, _ = make(tmp_path, entry(stop=99.0), trade=True, max_contracts=1)
    core.risk.cfg.risk_per_trade = 2_000  # the risk rules alone would allow several contracts
    step(core, now, ct(2026, 3, 3, 9, 5))
    assert core.orders.trade.size == 1

    core2, _, now2, a2, _ = make(tmp_path / "b", entry(stop=101.0), trade=True)  # a long with its stop above the price
    step(core2, now2, ct(2026, 3, 3, 9, 5))
    assert core2.orders.is_flat and a2.recent[0]["status"] == "refused" and "must be below" in a2.recent[0]["result"]

    core3, _, now3, a3, _ = make(tmp_path / "c", entry(stop=98.0, target=100.5), trade=True)  # target under 1R away
    step(core3, now3, ct(2026, 3, 3, 9, 5))
    assert core3.orders.is_flat and "less than 1R" in a3.recent[0]["result"]

    core4, _, now4, a4, _ = make(tmp_path / "d", entry(stop=98.0, target=None), trade=True)  # no target: 1.5R
    step(core4, now4, ct(2026, 3, 3, 9, 5))
    assert core4.orders.trade.target_price == pytest.approx(103.0)


def test_not_sure_enough_means_no_trade(tmp_path):
    core, _, now, a, _ = make(tmp_path, entry(confidence=0.55), trade=True, min_confidence=0.65)
    step(core, now, ct(2026, 3, 3, 9, 5))
    assert core.orders.is_flat
    d = a.recent[0]
    assert d["status"] == "held" and "55%" in d["result"] and "65%" in d["result"]


def test_it_never_asks_while_the_rules_block_entries(tmp_path):
    core, _, now, a, decider = make(tmp_path, trade=True)
    core.risk.paused = True
    step(core, now, ct(2026, 3, 3, 9, 5))
    assert decider.calls == [] and a.state == "waiting" and "Not looking for a trade" in a.message
    core.risk.paused = False
    step(core, now, ct(2026, 3, 3, 9, 10))
    assert len(decider.calls) == 1 and a.state == "ready"

    late, _, now2, a2, d2 = make(tmp_path / "late", trade=True, start=ct(2026, 3, 3, 14, 40))  # after the last entry
    step(late, now2, ct(2026, 3, 3, 14, 45))
    assert d2.calls == []


def test_it_thinks_on_a_schedule_and_on_fresh_signals_not_every_bar(tmp_path):
    core, _, now, a, decider = make(tmp_path, trade=True, think_every_minutes=15)
    step(core, now, ct(2026, 3, 3, 9, 5))
    step(core, now, ct(2026, 3, 3, 9, 10))
    assert len(decider.calls) == 1  # waited: the next regular look is at 09:20
    t = ct(2026, 3, 3, 9, 15)
    core.recommender.items.appendleft(Recommendation(
        id="R9", created=t, strategy="orb", title="ORB", active=False, side=OrderSide.SELL, entry=100.0, stop=102.0,
        target=97.0, size=1, risk_usd=40.0, reason="breakdown", status="idea"))
    step(core, now, t)
    assert len(decider.calls) == 2 and decider.calls[1][1]["asked_because"] == "ORB signalled SHORT"
    assert decider.calls[1][1]["evidence"]["live_ideas"][0]["strategy"] == "ORB"
    step(core, now, ct(2026, 3, 3, 9, 20))
    assert len(decider.calls) == 2  # the same idea is not a reason twice, and the clock restarted at 09:15


def test_its_daily_trade_cap_and_budget_stop_it(tmp_path):
    core, broker, now, a, decider = make(tmp_path, entry(), trade=True, max_trades_per_day=1, think_every_minutes=5)
    step(core, now, ct(2026, 3, 3, 9, 5))
    run(broker.on_price(now[0], 101.0))  # a small win: no cooling down after a loss
    core.last_price = 101.0
    run(core.orders.exit("test"))
    step(core, now, ct(2026, 3, 3, 9, 10))
    assert len(decider.calls) == 1 and "made its 1 trade(s) for today" in a.message

    core2, _, now2, a2, d2 = make(tmp_path / "b", trade=True, daily_budget_usd=0.05, think_every_minutes=5)
    d2.answers = [Decision("wait", confidence=0.5, reason="-", cost=0.05)]
    step(core2, now2, ct(2026, 3, 3, 9, 5))
    step(core2, now2, ct(2026, 3, 3, 9, 10))
    assert len(d2.calls) == 1 and a2.state == "unavailable" and "budget is used" in a2.message
    assert core2.orders.is_flat  # the bot is unaffected
    now2[0] = ct(2026, 3, 4, 9, 5)  # a new day: a fresh budget
    assert a2.desk.blocked("2026-03-04", now2[0]) is None


def test_its_own_trade_is_learned_from_under_its_own_name(tmp_path):
    core, broker, now, a, _ = make(tmp_path, entry(), trade=True)
    step(core, now, ct(2026, 3, 3, 9, 5))
    assert core.orders.trade is not None
    now[0] = ct(2026, 3, 3, 9, 20)
    run(broker.on_price(now[0], 104.5))  # through the target
    run(core.on_price(now[0], 104.5))
    assert core.orders.is_flat
    obs = [o for o in core.knowledge.obs if o.strategy == ASSISTANT]
    assert len(obs) == 1 and obs[0].source == "real" and obs[0].r > 0 and obs[0].basis
    assert core.knowledge.stats("orb", None, None, None).n == 0  # never a strategy's evidence
    row = core.journal.assistant_decision(a.recent[0]["id"])
    assert row["outcome"]["r"] > 0


# ------------------------------------------------------------------ open trades

def test_it_reviews_a_trade_when_it_opens_then_every_few_minutes_or_sooner_on_news_or_progress(tmp_path):
    core, _, now, a, decider = make(tmp_path, manage=True, manage_every_minutes=10)
    open_trade(core)
    step(core, now, ct(2026, 3, 3, 9, 5))
    assert decider.calls[0][0] == "manage" and decider.calls[0][1]["asked_because"] == "the trade just opened"
    trade = decider.calls[0][1]["trade"]
    assert trade["entry"] == 100.0 and trade["stop"] == 98.0 and trade["opened_by"] == "orb"
    step(core, now, ct(2026, 3, 3, 9, 10))
    assert len(decider.calls) == 1
    step(core, now, ct(2026, 3, 3, 9, 15), price=102.2)  # +1R
    assert decider.calls[-1][1]["asked_because"] == "it reached +1R"
    step(core, now, ct(2026, 3, 3, 9, 25))
    assert decider.calls[-1][1]["asked_because"] == "regular review"


def test_it_can_only_tighten_the_stop_never_widen_it(tmp_path):
    core, _, now, a, _ = make(tmp_path, manage("tighten_stop", stop=97.0), manage("tighten_stop", stop=99.9),
                              manage("tighten_stop", stop=99.0), manage=True, manage_every_minutes=5)
    t = open_trade(core)
    core.last_price = 100.2
    step(core, now, ct(2026, 3, 3, 9, 5))
    assert t.stop_price == 98.0 and a.recent[0]["status"] == "refused" and "never widened" in a.recent[0]["result"]
    step(core, now, ct(2026, 3, 3, 9, 10))
    assert t.stop_price == 98.0 and "too close to the price" in a.recent[0]["result"]
    step(core, now, ct(2026, 3, 3, 9, 15))
    assert t.stop_price == 99.0 and a.recent[0]["status"] == "done" and t.assisted == ["tighten_stop"]
    assert t.assistant_stop == 99.0
    assert any("moved the stop tighter" in e["message"] for e in core.events)


def test_breakeven_needs_the_price_past_the_entry(tmp_path):
    core, _, now, a, _ = make(tmp_path, manage("breakeven"), manage("breakeven"), manage=True, manage_every_minutes=5)
    t = open_trade(core)
    step(core, now, ct(2026, 3, 3, 9, 5))
    assert t.stop_price == 98.0 and "isn't far enough past the entry" in a.recent[0]["result"]
    step(core, now, ct(2026, 3, 3, 9, 10), price=101.5)
    assert t.stop_price == 100.0 and t.assisted == ["breakeven"]


def test_it_closes_early_only_when_sure_enough_and_says_so(tmp_path):
    core, _, now, a, _ = make(tmp_path, manage("close", confidence=0.5), manage("close", confidence=0.9), manage=True,
                              manage_every_minutes=5)
    t = open_trade(core)
    step(core, now, ct(2026, 3, 3, 9, 5))
    assert core.orders.trade is t and a.recent[0]["status"] == "held"
    step(core, now, ct(2026, 3, 3, 9, 10))
    assert t.exit_reason.startswith("AI assistant: news is close") and t.assisted == ["close"]


def test_your_manual_trades_are_left_alone_unless_you_say_so(tmp_path):
    core, _, now, _, decider = make(tmp_path, manage=True)
    open_trade(core, strategy=MANUAL, reason="manual from dashboard")
    step(core, now, ct(2026, 3, 3, 9, 5))
    assert decider.calls == []
    core2, _, now2, _, d2 = make(tmp_path / "b", manage=True, include_manual=True)
    open_trade(core2, strategy=MANUAL, reason="manual from dashboard")
    step(core2, now2, ct(2026, 3, 3, 9, 5))
    assert d2.calls and d2.calls[0][1]["trade"]["opened_by"] == "the owner (manual)"


def test_an_answer_for_a_trade_that_already_closed_is_not_acted_on(tmp_path):
    core, _, now, a, decider = make(tmp_path, manage=True)
    t = open_trade(core)

    async def decide(kind, situation):
        await core.orders.exit("stop loss")  # the trade ends while Claude is thinking
        return manage("close", confidence=0.9)

    decider.decide = decide
    step(core, now, ct(2026, 3, 3, 9, 5))
    assert t.exit_reason == "stop loss" and a.recent[0]["status"] == "skipped"


def test_a_slow_answer_is_too_late_to_act_on(tmp_path):
    core, _, now, a, decider = make(tmp_path, trade=True)

    async def decide(kind, situation):
        now[0] += timedelta(minutes=3)
        return entry()

    decider.decide = decide
    step(core, now, ct(2026, 3, 3, 9, 5))
    assert core.orders.is_flat and a.recent[0]["status"] == "late"


# ------------------------------------------------------------------ the strategy's record stays honest

def test_when_it_ends_a_strategy_trade_the_strategy_is_judged_on_its_own_plan(tmp_path):
    core, broker, now, a, _ = make(tmp_path, manage("close", confidence=0.9), manage=True)
    book = core.recommender
    t0 = ct(2026, 3, 3, 9, 0)
    book._day = core.schedule.trading_day(t0)  # the book has seen today's bars already
    rec = Recommendation(id="R1", created=t0, strategy="orb", title="ORB", active=True, side=OrderSide.BUY, entry=100.0,
                         stop=98.0, target=104.0, size=1, risk_usd=40.0, reason="breakout", status="taken")
    book.items.appendleft(rec)
    t = open_trade(core, stop=98.0, target=104.0)
    rec.trade_tag = t.tag
    step(core, now, ct(2026, 3, 3, 9, 5), price=100.5)
    assert t.exit_reason.startswith("AI assistant")
    run(core._on_trade_closed(t))
    assert rec.is_open and rec.hypothetical and rec.status == "tracking" and rec.assistant_real["changes"] == ["close"]
    # The strategy's plan goes on to its target: the strategy gets that result, the assistant the difference.
    run(core.on_bar(bar(ct(2026, 3, 3, 9, 5), 100.5, 104.5, 100.2, 104.2)))
    assert rec.result == "won" and rec.outcome_r == pytest.approx(2.0)
    obs = [o for o in core.knowledge.obs if o.strategy == "orb"]
    assert len(obs) == 1 and obs[0].source == "shadow" and obs[0].r == pytest.approx(2.0)
    m = a.desk.score_summary()
    assert m["n"] == 1 and m["hurt"] == 1 and m["r"] < 0
    assert any("AI assistant scorecard" in e["message"] and "cost" in e["message"] for e in core.events)


# ------------------------------------------------------------------ when the AI isn't there

def test_without_the_ai_the_bot_trades_as_usual_and_tries_again_later(tmp_path):
    core, _, now, a, decider = make(tmp_path, AssistantUnavailable("Claude couldn't be reached"), entry(), trade=True,
                                    think_every_minutes=5)
    step(core, now, ct(2026, 3, 3, 9, 5))
    assert core.orders.is_flat and a.state == "unavailable" and "couldn't be reached" in a.message
    assert any("AI assistant unavailable" in e["message"] for e in core.events)
    step(core, now, ct(2026, 3, 3, 9, 8))
    assert len(decider.calls) == 1  # waits a few minutes before trying again
    step(core, now, ct(2026, 3, 3, 9, 10))
    assert len(decider.calls) == 2 and core.orders.trade is not None and a.desk.unavailable is None
    assert any("AI assistant is working again" in e["message"] for e in core.events)


def test_without_a_key_claude_is_never_called(tmp_path, monkeypatch):
    monkeypatch.delenv("COACH_ANTHROPIC_API_KEY", raising=False)
    cfg = BotConfig.model_validate({"data_dir": str(tmp_path)})
    with pytest.raises(AssistantUnavailable) as exc:
        run(ClaudeDecider(cfg).decide("entry", {}))
    assert "no Claude API key" in str(exc.value) and exc.value.retry is False


def test_claude_is_asked_for_structured_json_and_the_cost_is_logged(tmp_path, monkeypatch):
    from topstep_bot import claude

    monkeypatch.setenv("COACH_ANTHROPIC_API_KEY", "sk-ant-test")
    sent = {}

    class FakeClient:
        async def close(self):
            sent["closed"] = True

    async def create(cl, request, model, state):
        sent["request"] = request
        text = json.dumps({"action": "tighten_stop", "stop": 99.5, "target": None, "confidence": 0.7,
                           "reason": "lock in some of the gain", "evidence": ["+1.2R open", "CPI in 10 minutes"]})
        return SimpleNamespace(model="claude-opus-5-5", stop_reason="end_turn", content=[SimpleNamespace(type="text", text=text)],
                               usage=SimpleNamespace(input_tokens=4000, output_tokens=300, cache_read_input_tokens=0,
                                                     cache_creation_input_tokens=0))

    monkeypatch.setattr(claude, "client", lambda key, **kw: FakeClient())
    monkeypatch.setattr(claude, "create", create)
    cfg = BotConfig.model_validate({"data_dir": str(tmp_path)})
    d = run(ClaudeDecider(cfg).decide("manage", {"kind": "manage"}))
    assert d.action == "tighten_stop" and d.stop == 99.5 and d.confidence == 0.7 and d.cost > 0
    req = sent["request"]
    assert req["model"] == "claude-opus-5-5" and "tool_choice" not in req and "tools" not in req
    assert req["output_config"]["format"]["type"] == "json_schema" and req["output_config"]["effort"] == "medium"
    assert sent["closed"]
    assert claude.ledger_for(cfg.data_path).spent(source="assistant") == pytest.approx(d.cost)


def test_a_bad_answer_is_never_acted_on():
    for bad in ({"action": "buy_more"}, {"action": "tighten_stop", "stop": -1}, "text", {"action": "hold", "confidence": "x"}):
        with pytest.raises((ValueError, TypeError)):
            parse_decision("manage", bad)
    with pytest.raises(ValueError):
        parse_decision("entry", {"action": "close"})  # a manage action is not an entry answer
    d = parse_decision("entry", {"action": "wait", "stop": None, "target": None, "confidence": 3, "reason": " a\n b ",
                                 "evidence": list("abcdef")})
    assert d.confidence == 1.0 and d.reason == "a b" and len(d.evidence) == 4


# ------------------------------------------------------------------ remote control

def test_the_bot_action_switches_it_and_reports_it(tmp_path):
    from topstep_bot.control import BotActions
    from topstep_bot.live import Controls

    core, _, now, a, _ = make(tmp_path)
    actions = BotActions(core, Controls())
    assert "ON" in run(actions.handle("assistant", {"manage": True, "source": "Telegram"}))
    assert a.desk.manage and not a.desk.trade
    text = run(actions.handle("assistant_text", {}))["text"]
    assert "managing open trades ON" in text and "No AI can promise" in text
    assert run(actions.handle("assistant", {"manage": "off"})).startswith("AI assistant: watching and managing open trades off")
    assert run(actions.handle("assistant_decisions", {}))["items"] == []
    assert core.snapshot()["assistant"]["manage"] is False


def test_version_4_1_is_documented_honestly():
    from pathlib import Path

    from topstep_bot import __version__

    root = Path(__file__).resolve().parents[1]
    assert __version__ == "4.1.0" and 'version = "4.1.0"' in (root / "pyproject.toml").read_text(encoding="utf-8")
    changelog = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    assert changelog.index("## 4.1.0") < changelog.index("## 4.0.1") and "No AI can promise" in changelog
    guide = (root / "docs" / "HOW_TO_USE.md").read_text(encoding="utf-8")
    assert "## 46. The AI trading assistant" in guide and "46. [The AI trading assistant]" in guide
    assert "`/assistant`" in guide and "assistant:" in guide and "No bot or AI can guarantee" in guide
    assert "## New in 4.1" in (root / "README.md").read_text(encoding="utf-8")
    example = (root / "config.example.yaml").read_text(encoding="utf-8")
    assert "assistant:" in example and BotConfig.model_validate({}).assistant.daily_budget_usd == 2.0


def test_each_account_has_its_own_switches():
    cfg = BotConfig.model_validate({"assistant": {"max_contracts": 2},
                                    "accounts": [{"name": "c1", "account_id": 1, "assistant": {"manage": True}},
                                                 {"name": "c2", "account_id": 2}]})
    c1, c2 = cfg.for_account("c1").assistant, cfg.for_account("c2").assistant
    assert c1.manage and not c1.trade and c1.max_contracts == 2  # its own switch, the shared limits
    assert not c2.manage and c2.max_contracts == 2
