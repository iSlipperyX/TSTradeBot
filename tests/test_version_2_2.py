"""Version 2.2: the central intelligence (1.3) on top of 2.1 - one Claude key and budget, and the 2.x records."""

import re
from pathlib import Path
from types import SimpleNamespace

from topstep_bot import cli
from topstep_bot import coach as coach_mod
from topstep_bot.brain import Brain, intents, llm
from topstep_bot.brain.facts import TOOLS, Facts
from topstep_bot.coach import Coach
from topstep_bot.config import CLAUDE_KEY_ENV, load_secrets
from topstep_bot.setup_service import ENV_KEYS

from .conftest import run
from .test_intelligence import FakeAnthropic, block, seed_journal
from .test_simlab_and_coach import scenario

DASHBOARD = Path(__file__).parent.parent / "src" / "topstep_bot" / "dashboard" / "index.html"


def test_one_claude_key_for_the_coach_and_the_ask_answers(monkeypatch):
    assert coach_mod.ENV_KEY == CLAUDE_KEY_ENV == ENV_KEYS["anthropic_api_key"] == "COACH_ANTHROPIC_API_KEY"
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-some-other-program")  # a system-wide key is never picked up
    monkeypatch.delenv(CLAUDE_KEY_ENV, raising=False)
    assert load_secrets().anthropic_api_key is None
    monkeypatch.setenv(CLAUDE_KEY_ENV, "sk-ant-shared")
    assert load_secrets().anthropic_api_key == "sk-ant-shared"


def test_ai_answers_are_priced_into_the_coach_budget_and_stop_when_it_is_used_up(tmp_path, monkeypatch):
    import anthropic

    cfg = seed_journal(tmp_path)
    cfg.brain.ai = True
    facts = Facts(cfg)

    async def fetch(name, args):
        return facts.call(name, args)

    usage = SimpleNamespace(input_tokens=1_000_000, output_tokens=0, cache_read_input_tokens=0, cache_creation_input_tokens=0)
    FakeAnthropic.requests = []
    FakeAnthropic.script = [block(stop_reason="end_turn", model="claude-opus-5-5", usage=usage,
                                  content=[block(type="text", text="ORB had the best record.")])]
    monkeypatch.setattr(anthropic, "AsyncAnthropic", FakeAnthropic)
    monkeypatch.setenv(CLAUDE_KEY_ENV, "sk-ant-test")
    brain = Brain(cfg)
    out = run(brain.ask("compare this week with last week", fetch))
    assert out["by"] == "ai" and out["answer"].startswith("ORB")
    coach = Coach(cfg)
    rows = (coach.folder / "usage.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(rows) == 1 and '"source": "ask"' in rows[0]
    spent = coach.spent()
    assert spent == round(Coach.price("claude-opus-5-5", usage), 4) and spent > 0
    # the month's budget, shared with the coach, is used up: built-in answers, and nothing is sent to Claude
    cfg.coach.monthly_budget_usd = spent
    FakeAnthropic.requests = []
    out = run(brain.ask("why the last trade?", fetch))
    assert out["by"] == "built-in" and "budget" in out["note"] and FakeAnthropic.requests == []
    assert "Opening Range Breakout" in out["answer"]


def test_usage_logging_failures_never_lose_the_answer(tmp_path, monkeypatch):
    import anthropic

    cfg = seed_journal(tmp_path)
    facts = Facts(cfg)

    async def fetch(name, args):
        return facts.call(name, args)

    def broken(model, usage):
        raise OSError("disk full")

    FakeAnthropic.script = [block(stop_reason="end_turn", usage=None, content=[block(type="text", text="fine")])]
    monkeypatch.setattr(anthropic, "AsyncAnthropic", FakeAnthropic)
    assert run(llm.ask("x", [], fetch, api_key="k", model="claude-sonnet-5-5", on_usage=broken)) == "fine"


def test_the_bot_answers_from_the_simulation_lab_and_the_coach(tmp_path):
    cfg = seed_journal(tmp_path)
    facts = Facts(cfg)
    assert {"simulations", "coach"} <= set(TOOLS)
    assert "No simulations yet" in facts.call("simulations")["text"]
    coach = Coach(cfg)
    coach.add_lesson("MNQ's opening range breakout lost money in both halves", "simlab, 40 Combines", "medium", "s1")
    coach.add_lesson("Afternoon signals are too few to judge", "decision lab", "low", "s2")
    out = facts.call("coach")
    assert out["lessons"][0]["text"].startswith("Afternoon")  # newest first
    assert "Latest lessons" in out["text"] and out["spent_month_usd"] == 0

    async def fetch(name, args):
        return facts.call(name, args)

    def answer(q):
        return run(intents.answer(q, fetch))
    assert "Afternoon signals" in answer("what has the coach learned?")
    assert "No simulations yet" in answer("what did the simulations find?")
    assert "No simulations yet" in answer("any opportunities in other markets?")
    assert "decision lab" in answer("which decision rule are you using?").lower()


def test_the_decision_lab_command_does_not_collide_with_the_simulation_lab():
    parser = cli.build_parser()
    assert parser.parse_args(["decision-lab"]).func is cli.cmd_decision_lab
    assert parser.parse_args(["simulate", "--show"]).func is cli.cmd_simulate


def test_the_dashboard_defines_each_function_and_id_once():
    html = DASHBOARD.read_text(encoding="utf-8")
    names = re.findall(r"^(?:async )?function (\w+)", html, re.M)
    ids = re.findall(r'\bid="([^"]+)"', html)
    assert sorted({n for n in names if names.count(n) > 1}) == []  # a second loadLab once broke the Simulations tab
    assert sorted({i for i in ids if ids.count(i) > 1}) == []
    assert 'data-tab="ask"' in html and 'id="tab-ask"' in html and "v3.0" in html


def test_saving_the_key_on_the_setup_tab_gives_it_to_the_coach(tmp_path, monkeypatch):
    from topstep_bot.brain import llm as llm_mod

    async def fine(key, model):
        return None

    monkeypatch.setattr(llm_mod, "check_key", fine)

    async def body(ctl, http, path):
        assert not (await http.post("/api/coach/state")).json()["key_saved"]
        r = (await http.post("/api/setup/ai", json={"api_key": "sk-ant-api03-shared", "enabled": True})).json()
        assert r["ok"], r
        env = (path.parent / ".env").read_text()
        assert f"{CLAUDE_KEY_ENV}=sk-ant-api03-shared" in env and "\nANTHROPIC_API_KEY=" not in env
        assert (await http.post("/api/coach/state")).json()["key_saved"] and ctl.coach.coach.api_key == "sk-ant-api03-shared"
        assert ctl.brain.api_key == "sk-ant-api03-shared"
        r = (await http.post("/api/setup/ai", json={"remove": True})).json()
        assert r["ok"] and "coach" in r["message"]
        assert ctl.coach.coach._api_key is None and not ctl.secrets.anthropic_api_key
    scenario(tmp_path, monkeypatch, body)
