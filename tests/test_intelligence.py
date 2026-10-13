"""Version 2.2 (first built as 1.3): the evidence rule, the decision lab, decision records and the central intelligence."""

import json
from datetime import date, timedelta
from types import SimpleNamespace

import httpx
import pytest

from topstep_bot import decision_lab as lab_mod
from topstep_bot.bars import resample
from topstep_bot.brain import Brain, intents, llm
from topstep_bot.brain.explain import decision_text, headline
from topstep_bot.brain.facts import TOOLS, Facts, find_strategy
from topstep_bot.broker.paper import PaperBroker
from topstep_bot.config import BotConfig, TelegramConfig
from topstep_bot.control import BotActions
from topstep_bot.decision_lab import Lab, Rules, choose, compare, resolve_policy, result_text
from topstep_bot.evidence import EvidenceModel, evidence_verdict, shrink, similar
from topstep_bot.factory import build_core
from topstep_bot.instruments import offline_contract
from topstep_bot.journal import Journal
from topstep_bot.knowledge import KnowledgeBase, Observation
from topstep_bot.live import Controls
from topstep_bot.market_context import describe, orient
from topstep_bot.policy import Judge
from topstep_bot.reasoning import Reasoner
from topstep_bot.recommendations import RecommendationBook
from topstep_bot.telegram_control import TelegramController

from .conftest import ct, run
from .test_knowledge import BARS_1M, feed
from .test_telegram_control import CHAT, FakeTelegram, msg

TODAY = date(2026, 10, 8)


def obs(strategy, r, *, day=TODAY, slot="open", regime="calm", side="LONG", source="shadow", cost=None, ctx=None,
        time="09:00", bars=2):
    return Observation(day.isoformat(), time, strategy, side, slot, regime, r, r * 50, source, ctx=ctx, cost_r=cost,
                       bars=bars)


# ------------------------------------------------------------------ market snapshots in words

def test_snapshots_are_oriented_with_the_trade_and_described_in_words():
    ctx = {"vol": 1.4, "min_open": 35, "gap": 0.3, "move": -0.2, "vwap": -0.1, "trend": 0.25, "range_pos": 0.2}
    short = orient(ctx, "SHORT")
    assert short["move"] == 0.2 and short["vwap"] == 0.1 and short["trend"] == -0.25 and short["range_pos"] == 0.8
    assert orient(ctx, "LONG") == ctx and orient(None, "LONG") == {}
    words = describe(ctx, "LONG")
    assert words[0].startswith("volatile") and any("gap up" in w for w in words) and any("after the open" in w for w in words)
    assert describe({}) == []


# ------------------------------------------------------------------ the evidence rule

def test_shrink_pulls_small_samples_toward_the_prior():
    assert shrink(1.0, 0, 0.2, 8) == pytest.approx(0.2)
    assert shrink(1.0, 8, 0.0, 8) == pytest.approx(0.5)
    assert shrink(1.0, 1000, 0.0, 8) == pytest.approx(1.0, abs=0.01)


def test_evidence_rule_counts_costs_and_does_not_let_a_lucky_cell_outvote_the_strategy():
    recent = [obs("orb", -1.0 if i % 3 else 1.0, slot="midday") for i in range(60)]  # losing all day...
    recent += [obs("orb", 1.0) for _ in range(8)]  # ...but 8 lucky signals at the open
    kb = KnowledgeBase(None, min_samples=8, half_life_days=1000)
    kb.obs = list(recent)
    classic = kb.verdict("orb", "open", "calm", TODAY)
    assert classic.allowed and classic.level == "cell"  # 1.2: the 8 lucky trades decide alone
    model = EvidenceModel(recent, today=TODAY, half_life_days=1000)
    v = evidence_verdict(model, "orb", "open", "calm", min_samples=8, min_edge_r=0.05)
    assert not v.allowed and v.score < 0.05 and "after costs" in v.why  # 1.3: shrunk toward the losing strategy
    # costs: a +0.1R gross edge that costs 0.15R a trade is a loser after costs
    costly = [obs("vwap_pullback", 0.1, cost=0.15) for _ in range(100)]
    v = evidence_verdict(EvidenceModel(costly, today=TODAY), "vwap_pullback", "open", "calm", min_samples=8, min_edge_r=0.05)
    assert not v.allowed and v.score < 0
    assert evidence_verdict(EvidenceModel([], today=TODAY), "orb", "open", "calm", min_samples=8,
                            min_edge_r=0.05).level == "unproven"


def test_long_run_memory_backs_the_recent_record_without_drowning_it():
    old = TODAY - timedelta(days=400)
    prior = [obs("orb", 0.3, day=old) for _ in range(600)]  # a long winning record...
    recent = [obs("orb", -0.2, day=TODAY - timedelta(days=d % 30)) for d in range(40)]  # ...losing lately
    with_prior = EvidenceModel(recent, prior, today=TODAY, half_life_days=20)
    alone = EvidenceModel(recent, today=TODAY, half_life_days=20)
    whole = with_prior.level("orb")
    assert whole.n == 640 and whole.recent_n == 40 and whole.mean > alone.level("orb").mean
    assert whole.w - alone.level("orb").w == pytest.approx(600 * 0.05)  # each long-run signal counts at a floor weight
    blended = evidence_verdict(with_prior, "orb", "open", "calm", min_samples=8, min_edge_r=0.05).score
    assert evidence_verdict(alone, "orb", "open", "calm", min_samples=8, min_edge_r=0.05).score < blended < 0.3
    # enough recent losing evidence overrules the long record
    lately = [obs("orb", -0.2, day=TODAY - timedelta(days=d % 20)) for d in range(200)]
    assert not evidence_verdict(EvidenceModel(lately, prior, today=TODAY, half_life_days=20), "orb", "open", "calm",
                                min_samples=8, min_edge_r=0.05).allowed


def test_similar_situations_find_the_closest_past_signals():
    pool = []
    for i in range(40):
        good = i % 2 == 0
        ctx = {"vol": 1.5 if good else 0.7, "min_open": 30, "gap": 0.3 if good else -0.3, "move": 0.2 if good else -0.1,
               "vwap": 0.1, "trend": 0.2}
        pool.append(obs("orb", 1.0 if good else -1.0, ctx=ctx, day=TODAY - timedelta(days=i + 1)))
    s = similar(pool, "orb", "LONG", {"vol": 1.5, "min_open": 30, "gap": 0.3, "move": 0.2, "vwap": 0.1, "trend": 0.2}, k=10)
    assert s is not None and s.n == 10 and s.mean_net_r == pytest.approx(1.0) and s.baseline_net_r == pytest.approx(0.0)
    assert len(s.examples) == 3 and s.closeness > 0.5
    assert similar(pool[:5], "orb", "LONG", {"vol": 1.5, "min_open": 30, "gap": 0.3, "move": 0.2}) is None  # too few
    assert similar(pool, "orb", "LONG", {"vol": 1.5}) is None  # too few measurements to compare


# ------------------------------------------------------------------ the decision rule in the bot

def make_adaptive(kb, judge=None):
    from topstep_bot.strategies import create_strategy

    s = create_strategy("adaptive", {}, offline_contract("MNQ"), 5)
    s.bind_knowledge(kb, lambda: "calm", judge)
    return s


def orb_breakout(s, day):
    s.on_new_day(day)
    for mm in (30, 35, 40):
        feed(s, day, 8, mm, 100, 105, 100, 102)
    return feed(s, day, 8, 45, 102, 110, 101, 109)


def test_adaptive_asks_the_judge_and_reports_every_evaluation():
    day = date(2026, 10, 7)
    kb = KnowledgeBase(None, min_samples=8, half_life_days=1000)
    kb.obs = [obs("orb", -1.0 if i % 3 else 1.0, slot="midday", day=day) for i in range(60)]
    kb.obs += [obs("orb", 1.0, day=day) for _ in range(8)]
    seen = []
    classic = make_adaptive(kb, Judge(kb, policy="classic"))
    classic.on_evaluation = seen.append
    sig = orb_breakout(classic, day)
    assert sig is not None and sig.meta["level"] == "cell"
    ev = seen[-1]
    assert ev["chosen"] == "orb" and ev["slot"] == "open" and "classic" in ev["policy"]
    assert any(c["strategy"] == "orb" and c["allowed"] for c in ev["candidates"])
    evidence = make_adaptive(kb, Judge(kb, policy="evidence"))
    evidence.on_evaluation = seen.append
    assert orb_breakout(evidence, day) is None  # the evidence rule passes on the lucky cell
    assert seen[-1]["chosen"] is None and "evidence" in seen[-1]["policy"]


def test_engine_trades_classic_until_the_lab_says_otherwise(tmp_path):
    mnq = offline_contract("MNQ")
    cfg = BotConfig.model_validate({"data_dir": str(tmp_path), "strategy": {"name": "adaptive"}})
    core = build_core(cfg, mnq, PaperBroker(mnq, 50_000), clock=lambda: ct(2026, 10, 7, 9, 0), account_label="T")
    core.attach_knowledge(KnowledgeBase(None))
    assert core.judge.policy == "classic" and "waiting for the decision lab" in core.judge.why
    assert core.strategy.judge is core.judge
    core.set_policy("evidence", "automatic: the lab says so")
    assert core.strategy.judge.policy == "evidence" and "Decision rule" in core.events[0]["message"]
    cfg.knowledge.policy = "evidence"
    core.attach_knowledge(KnowledgeBase(None))
    assert core.judge.policy == "evidence" and core.judge.why == "set in config.yaml"


# ------------------------------------------------------------------ the decision lab

def lab_history(days=90, start=date(2026, 3, 2)):
    """One good strategy at the open, and a bad one with a lucky open cell in a losing record."""
    out, d = [], start
    while len({o.day for o in out}) < days:
        if d.weekday() < 5:
            for i in range(3):
                out.append(obs("orb", 0.6 if i else -1.0, day=d, time=f"08:{40 + i * 5}", cost=0.05))
            out.append(obs("ema_trend", -0.5, day=d, slot="midday", time="11:00"))
            out.append(obs("ema_trend", -0.5, day=d, slot="midday", time="12:00"))
            out.append(obs("ema_trend", 0.9 if d.day % 3 else -1.0, day=d, time="09:30"))
        d += timedelta(days=1)
    return out


def test_lab_classic_decides_exactly_like_the_knowledge_base():
    history = lab_history(40)
    rules = Rules(history_days=20, min_samples=4)
    lab = Lab(history, rules)
    day = lab.days[30]
    decide = lab._deciders(day)["classic"]
    kb = KnowledgeBase(None, half_life_days=rules.half_life_days, min_samples=4, min_edge_r=rules.min_edge_r)
    window = (date.fromisoformat(day) - timedelta(days=20)).isoformat()
    kb.obs = [o for o in history if window <= o.day < day]
    for o in [x for x in history if x.day == day]:
        v = kb.verdict(o.strategy, o.slot, o.regime, date.fromisoformat(day))
        assert decide(o) == (v.allowed, v.score)


def test_lab_respects_the_bots_limits_and_compares_without_peeking():
    rules = Rules(history_days=30, min_samples=4, max_trades=2, max_losses=1, cooldown_minutes=0, daily_loss_r=1.0)
    result = Lab(lab_history(90), rules).run()
    assert result["ok"] and result["test_days"] == 90 - lab_mod.WARMUP_DAYS
    for name in ("classic", "evidence"):
        s = result["policies"][name]
        per_day: dict[str, int] = {}
        for t in result["recent_trades"][name]:
            per_day[t["day"]] = per_day.get(t["day"], 0) + 1
        assert all(n <= 2 for n in per_day.values()) and s["trades"] <= 2 * result["test_days"]
    assert set(result["alone"]) == {"orb", "ema_trend"} and result["alone"]["orb"]["trades"] == result["test_days"]
    assert result["winner"] in ("classic", "evidence") and result["why"]
    assert "Decision lab" in result_text(result) and "Choice:" in result_text(result)
    too_short = Lab(lab_history(10), rules).run()
    assert not too_short["ok"] and "needs more than" in too_short["why"]


def test_auto_switches_only_on_clear_evidence(tmp_path):
    s = {"evidence": {"trades": 50}, "classic": {"trades": 50}}
    assert choose(s, {"p_better": 0.99, "diff_r": 12.0}, 30)[0] == "classic"  # too few days
    assert choose({"evidence": {"trades": 10}, "classic": {"trades": 50}}, {"p_better": 0.99, "diff_r": 12.0}, 80)[0] == "classic"
    assert choose(s, {"p_better": 0.90, "diff_r": 5.0}, 80)[0] == "classic"  # not sure enough
    winner, why = choose(s, {"p_better": 0.97, "diff_r": 12.0}, 80)
    assert winner == "evidence" and "+12.0R" in why
    c = compare({"a": 1.0, "b": 1.0, "c": 0.5}, {"a": 0.0, "b": 0.0, "c": 0.0})
    assert c["days"] == 3 and c["diff_r"] == 2.5 and c["p_better"] == 1.0
    cfg = BotConfig.model_validate({"data_dir": str(tmp_path)})
    assert resolve_policy(cfg)[0] == "classic" and "hasn't run" in resolve_policy(cfg)[1]
    lab_mod.save_result(cfg, {"ok": True, "winner": "evidence", "why": "it won"})
    assert resolve_policy(cfg) == ("evidence", "automatic: it won")
    cfg.knowledge.policy = "classic"
    assert resolve_policy(cfg)[0] == "classic"


# ------------------------------------------------------------------ decision records, live

def live_run(tmp_path, notifier=None):
    """A paper run of the adaptive strategy with a journal, like the live bot (trade_unproven so it trades)."""
    mnq = offline_contract("MNQ")
    cfg = BotConfig.model_validate({"data_dir": str(tmp_path), "strategy": {"name": "adaptive", "params": {"trade_unproven": True}}})
    now = [ct(2026, 9, 1, 9, 0)]
    broker = PaperBroker(mnq, 50_000, slippage_ticks=0, fees_round_turn=1.22)
    journal = Journal(tmp_path / "journal_paper.db")
    core = build_core(cfg, mnq, broker, clock=lambda: now[0], account_label="T", journal=journal, notifier=notifier)
    core.balance = 50_000
    core.recommender = RecommendationBook(core)
    core.attach_reasoner(Reasoner(core))
    core.attach_knowledge(KnowledgeBase(tmp_path / "kb.json", min_samples=5))
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
    return core, journal


class Alerts:
    def __init__(self):
        self.sent = []

    def notify(self, kind, text):
        self.sent.append((kind, text))


def test_every_decision_is_recorded_with_its_evidence_outcome_and_lesson(tmp_path):
    alerts = Alerts()
    core, journal = live_run(tmp_path, alerts)
    taken = journal.decisions(limit=500, status="taken")
    assert taken and len(taken) >= len(core.closed_trades) - 1
    rec = taken[-1]
    assert rec["candidates"] and rec["chosen"] == rec["strategy"] and rec["policy"]
    assert rec["market"]["snapshot"] and rec["rules"]["max_trades"] == core.cfg.risk.max_trades_per_day
    assert "recent" in rec["evidence"] and "estimate" in rec["evidence"]
    finished = [d for d in taken if d["outcome"]]
    assert finished and all(d["outcome"]["result"] in ("won", "lost", "flat") for d in finished)
    assert any(d["lesson"] for d in finished)
    text = decision_text(finished[0])
    assert finished[0]["title"] in text and "Result" in text and "What it learned" in text
    # the alert with each entry says why, and a lesson follows the exit
    assert any(kind == "entry" and text.startswith("💡 Why") for kind, text in alerts.sent)
    assert any(kind == "exit" and "What it learned" in text for kind, text in alerts.sent)
    # a restart keeps following decisions still waiting for their result
    open_ones = [d for d in journal.decisions(limit=60) if d["outcome"] is None]
    again = Reasoner(core)
    assert len(again._open) >= sum(1 for d in open_ones for c in d["candidates"] if "outcome" not in c) > 0 or not open_ones


# ------------------------------------------------------------------ the central intelligence

def seed_journal(tmp_path) -> BotConfig:
    cfg = BotConfig.model_validate({"data_dir": str(tmp_path)})
    j = Journal(cfg.journal_path)
    base = {"symbol": "MNQ", "note": "", "tag": None, "entry": 20100.0, "stop": 20080.0, "target": 20140.0, "size": 2,
            "risk_usd": 80.0, "slot": "open", "regime": "volatile", "policy": "the classic rule (1.2) - set in config.yaml",
            "market": {"snapshot": {"vol": 1.3}, "described": ["volatile (1.3x a normal range)", "35 min after the open"]},
            "rules": {"day_pnl": 0.0, "trades_today": 0, "max_trades": 4, "consecutive_losses": 0, "daily_loss_limit": 500},
            "evidence": {"recent": {"cell": {"n": 14, "mean_r": 0.32}, "slot": {"n": 30, "mean_r": 0.2},
                                    "strategy": {"n": 80, "mean_r": 0.1}},
                         "estimate": {"net_r": 0.18, "allowed": True, "why": "x"},
                         "similar": {"n": 30, "mean_net_r": 0.4, "win_rate": 0.55, "baseline_net_r": 0.1, "baseline_n": 80}}}
    j.record_decision({**base, "id": "20261009-1435-orb-LONG", "day": "2026-10-09", "time": "09:35",
                       "created": "2026-10-09T14:35:00+00:00", "status": "taken", "strategy": "orb",
                       "title": "Opening Range Breakout", "side": "LONG", "signal": "close above the opening range",
                       "chosen": "orb", "candidates": [
                           {"strategy": "orb", "title": "Opening Range Breakout", "side": "LONG", "allowed": True, "level": "cell",
                            "score": 0.25, "why": "+0.32R avg over 14 at open/volatile", "signal": "close above the range"},
                           {"strategy": "ema_trend", "title": "EMA Trend Crossover", "side": "LONG", "allowed": False,
                            "level": "slot", "score": -0.1, "why": "-0.12R avg over 20 at open - not working", "signal": "x"}],
                       "outcome": {"result": "won", "r": 1.6, "usd": 128.0, "why": "target hit", "mfe_r": 1.9, "mae_r": -0.3,
                                   "hypothetical": False},
                       "lesson": {"before": {"allowed": True, "score": 0.25, "why": ""},
                                  "after": {"allowed": True, "score": 0.29, "why": ""}}})
    j.record_decision({**base, "id": "20261009-1600-vwap_pullback-SHORT", "day": "2026-10-09", "time": "11:00",
                       "created": "2026-10-09T16:00:00+00:00", "status": "passed", "strategy": "vwap_pullback",
                       "title": "VWAP Trend Pullback", "side": "SHORT", "signal": "pullback to VWAP", "chosen": None,
                       "slot": "midday", "candidates": [
                           {"strategy": "vwap_pullback", "title": "VWAP Trend Pullback", "side": "SHORT", "allowed": False,
                            "level": "unproven", "score": 0.0, "why": "unproven: only 3 observation(s), need 8", "signal": "x",
                            "outcome": {"result": "lost", "r": -1.0, "hypothetical": True}}],
                       "outcome": {"result": "lost", "r": -1.0, "usd": None, "why": "stop hit", "hypothetical": True},
                       "lesson": None})
    j.close()
    return cfg


def test_facts_answer_from_the_files_while_the_bot_is_stopped(tmp_path):
    cfg = seed_journal(tmp_path)
    facts = Facts(cfg, offline="🤖 Bot is STOPPED (PAPER mode).")
    assert facts.call("status")["text"].startswith("🤖 Bot is STOPPED")
    d = facts.call("decisions")
    assert [x["status"] for x in d["items"]] == ["passed", "taken"] and "would have lost" in d["items"][0]["summary"]
    e = facts.call("explain_decision")  # default: the latest trade taken
    assert e["found"] and e["record"]["strategy"] == "orb"
    for words in ("took Opening Range Breakout LONG 2 @ 20100.0", "won +1.60R", "EMA Trend Crossover LONG: not allowed",
                  "Recent record", "most similar past situations", "volatile (1.3x", "What it learned",
                  "+0.25R to +0.29R"):
        assert words in e["text"], words
    assert facts.call("explain_decision", {"time": "10:15"})["record"]["strategy"] == "orb"  # latest at or before
    assert facts.call("explain_decision", {"time": "11:00"})["record"]["status"] == "passed"
    assert not facts.call("explain_decision", {"time": "08:00"})["found"]
    assert facts.call("lessons")["items"][0]["lesson"].startswith("What it learned")
    assert "classic" in facts.call("lab")["text"]
    assert "Maximum Loss Limit" in facts.call("rules")["text"] or "MLL" in facts.call("rules")["text"]
    assert "needs the bot running" in facts.call("forecast")["text"]
    assert facts.call("strategy", {"name": "nope"})["error"].startswith("no strategy")
    assert facts.call("unknown")["error"] and facts.call("decisions", {"limit": "x"})["error"]


def test_strategy_names_in_questions():
    assert find_strategy("how is ORB doing?") == "orb"
    assert find_strategy("what about orb momentum lately") == "orb_momentum"
    assert find_strategy("is the VWAP pullback working") == "vwap_pullback"
    assert find_strategy("noise area breakout?") == "noise_breakout"
    assert find_strategy("hello there") is None


def test_built_in_answers_route_questions_to_the_right_records(tmp_path):
    cfg = seed_journal(tmp_path)
    facts = Facts(cfg, offline="Bot is STOPPED.")
    asked = []

    async def fetch(name, args):
        asked.append((name, args))
        return facts.call(name, args)

    def answer(q):
        return run(intents.answer(q, fetch))

    assert "took Opening Range Breakout" in answer("Why did you take the last trade?")
    assert asked[-1][0] == "explain_decision"
    assert "passed on VWAP Trend Pullback" in answer("why didn't you trade at 11:00?")
    assert "passed on" in answer("Why didn't you trade today?")
    assert "What it learned" in answer("what did you learn?")
    assert asked[-1][0] == "lessons"
    assert "Opening Range Breakout (orb)" in answer("how is ORB doing?")
    assert asked[-1][0] == "lab" or "lab" in answer("which decision rule are you using?")
    assert "STOPPED" in answer("how am I doing today?")
    assert answer("help").startswith("I'm the bot's central intelligence")
    assert "not sure" in answer("tell me a joke")


def test_brain_falls_back_to_built_in_answers(tmp_path, monkeypatch):
    cfg = seed_journal(tmp_path)
    facts = Facts(cfg)

    async def fetch(name, args):
        return facts.call(name, args)

    monkeypatch.delenv("COACH_ANTHROPIC_API_KEY", raising=False)
    brain = Brain(cfg)
    out = run(brain.ask("why the last trade?", fetch))
    assert out["by"] == "built-in" and out["note"] is None and "Opening Range Breakout" in out["answer"]
    cfg.brain.ai = True
    assert "no Claude API key" in brain.mode_text()
    monkeypatch.setenv("COACH_ANTHROPIC_API_KEY", "sk-test")

    async def broken(*a, **kw):
        raise llm.AIUnavailable("Anthropic couldn't be reached (internet connection?)")
    monkeypatch.setattr(llm, "ask", broken)
    out = run(brain.ask("why the last trade?", fetch))
    assert out["by"] == "built-in" and "couldn't be reached" in out["note"] and "Opening Range" in out["answer"]
    assert run(brain.ask("   ", fetch))["answer"] == intents.HELP


class FakeMessages:
    def __init__(self, script, log):
        self.script, self.log = script, log

    async def create(self, **kw):
        self.log.append(kw)
        return self.script.pop(0)


class FakeAnthropic:
    """Stands in for anthropic.AsyncAnthropic: plays back scripted responses and records the requests."""
    script: list = []
    requests: list = []

    def __init__(self, **kw):
        self.messages = FakeMessages(FakeAnthropic.script, FakeAnthropic.requests)
        self.beta = SimpleNamespace(messages=FakeMessages(FakeAnthropic.script, FakeAnthropic.requests))

    async def close(self):
        pass


def block(**kw):
    return SimpleNamespace(**kw)


def test_ai_answers_look_up_the_records_with_read_only_tools(tmp_path, monkeypatch):
    import anthropic

    cfg = seed_journal(tmp_path)
    facts = Facts(cfg)
    looked_up = []

    async def fetch(name, args):
        looked_up.append((name, args))
        return facts.call(name, args)

    FakeAnthropic.requests = []
    FakeAnthropic.script = [
        block(stop_reason="tool_use", content=[block(type="tool_use", id="t1", name="explain_decision", input={})]),
        block(stop_reason="end_turn", content=[block(type="text", text="I took ORB because it had +0.32R over 14.")]),
    ]
    monkeypatch.setattr(anthropic, "AsyncAnthropic", FakeAnthropic)
    answer = run(llm.ask("why?", [], fetch, api_key="k", model="claude-opus-5-5", context="ctx"))
    assert answer.startswith("I took ORB") and looked_up == [("explain_decision", {})]
    first, second = FakeAnthropic.requests
    assert first["fallbacks"] == "default" and first["betas"] == [llm.FALLBACK_BETA]
    assert {t["name"] for t in first["tools"]} == set(TOOLS) and first["output_config"] == {"effort": "medium"}
    assert "Never invent" in first["system"] and "ctx" in first["system"]
    result = second["messages"][-1]["content"][0]
    assert result["type"] == "tool_result" and "Opening Range Breakout" in json.loads(result["content"])["text"]
    # the lookup budget is enforced: the last round may not ask for more
    FakeAnthropic.requests = []
    FakeAnthropic.script = [
        block(stop_reason="tool_use", content=[block(type="tool_use", id="t1", name="status", input={})]),
        block(stop_reason="end_turn", content=[block(type="text", text="done")]),
    ]
    assert run(llm.ask("x", [], fetch, api_key="k", model="claude-sonnet-5-5", max_tool_calls=1)) == "done"
    assert "fallbacks" not in FakeAnthropic.requests[0] and FakeAnthropic.requests[1]["tool_choice"] == {"type": "none"}
    FakeAnthropic.script = [block(stop_reason="refusal", content=[])]
    with pytest.raises(llm.AIUnavailable, match="declined"):
        run(llm.ask("x", [], fetch, api_key="k", model="claude-sonnet-5-5"))


def test_the_running_bot_answers_lookups_and_telegram_takes_questions(tmp_path):
    core, journal = live_run(tmp_path)
    actions = BotActions(core, Controls())
    data = run(actions.handle("facts", {"name": "decisions", "args": {"limit": 3}}))["data"]
    assert len(data["items"]) == 3
    assert "decision_rule" in run(actions.handle("facts", {"name": "status"}))["data"]
    assert run(actions.handle("facts", {"name": "strategy", "args": {"name": "orb"}}))["data"]["verdicts"]

    class Asker:
        def __init__(self):
            self.questions = []

        async def ask(self, question, account=None):
            self.questions.append((question, account))
            return "answer: " + question

    fake = FakeTelegram()
    asker = Asker()
    tg = TelegramController("TOKEN", CHAT, asker, TelegramConfig(), transport=httpx.MockTransport(fake.handler))

    async def go():
        await tg.handle_update(msg("why did you take the last trade?"))
        await tg.handle_update(msg("/ask what did you learn?"))
        await tg.idle()
    run(go())
    assert asker.questions == [("why did you take the last trade?", None), ("what did you learn?", None)]
    assert fake.sent[-1]["text"] == "answer: what did you learn?"


def test_journal_decisions_round_trip(tmp_path):
    j = Journal(tmp_path / "j.db")
    j.record_decision({"id": "a", "day": "2026-10-09", "created": "2026-10-09T14:00:00+00:00", "status": "taken",
                       "strategy": "orb", "x": 1})
    j.record_decision({"id": "a", "day": "2026-10-09", "created": "2026-10-09T14:00:00+00:00", "status": "taken",
                       "strategy": "orb", "x": 2})  # the outcome updates the same record
    j.record_decision({"id": "b", "day": "2026-10-08", "created": "2026-10-08T14:00:00+00:00", "status": "passed",
                       "strategy": "ema_trend"})
    assert [d["id"] for d in j.decisions()] == ["a", "b"] and j.decision("a")["x"] == 2
    assert [d["id"] for d in j.decisions(day="2026-10-08")] == ["b"] and j.decisions(status="taken")[0]["id"] == "a"
    assert j.decision("zzz") is None
    assert headline({"time": "09:35", "day": "2026-10-09", "status": "skipped", "title": "ORB", "side": "LONG",
                     "outcome": None}) == "09:35 2026-10-09 - skipped ORB LONG (a rule stopped it)"
    j.close()
