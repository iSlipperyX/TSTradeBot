"""Version 2.0: the Simulation Lab (simlab.py), the AI coach (coach.py) and their dashboard routes (lab_service.py)."""

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import yaml

from topstep_bot import coach as coach_mod
from topstep_bot import controller as controller_mod
from topstep_bot import simlab
from topstep_bot.backtest.data import save_csv, synthetic_bars
from topstep_bot.coach import Coach, CoachError
from topstep_bot.config import BotConfig, Secrets, load_config
from topstep_bot.controller import Controller, ProxyActions
from topstep_bot.logging_setup import RedactingFilter
from topstep_bot.risk.topstep import PLANS

from .conftest import run

FAKE = [sys.executable, str(Path(__file__).parent / "fake_worker.py")]


def lab_cfg(tmp_path, days=45, symbols=("MNQ",)) -> BotConfig:
    cfg = BotConfig()
    cfg.data_dir = str(tmp_path / "data")
    Path(cfg.data_dir).mkdir(parents=True, exist_ok=True)
    for i, s in enumerate(symbols):
        save_csv(synthetic_bars(s, days=days, seed=3 + i), Path(cfg.data_dir) / f"{s}_1m.csv")
    return cfg


# ------------------------------------------------------------------------------------- Simulation Lab

def test_lab_runs_fresh_combines_through_the_real_core_and_saves_a_report(tmp_path):
    cfg = lab_cfg(tmp_path)
    steps = []
    rep = run(simlab.run_lab(cfg, starts=3, symbols=["MNQ"], strategies=["orb", "ema_trend"], workers=1,
                             progress=lambda d, t, s: steps.append((d, t))))
    t = rep["totals"]
    assert t["simulations"] == 6 and t["full_histories"] == 2 and t["errors"] == 0
    assert t["passed"] + t["failed"] + t["open"] == 6
    assert steps[-1][0] == steps[-1][1] == 8  # every simulation reported, the full histories included
    starts = sorted({r["start"] for r in rep["runs"] if r["strategy"] == "orb"})
    assert len(starts) == 3  # three different Combine start days
    for p in rep["playbooks"]:
        assert p["verdict"] in ("edge", "mixed", "losing", "thin") and p["why"]
        assert p["resampled"]["paths"] == simlab.MC_PATHS
        assert abs(sum(p["halves"]) - p["net"]) < 0.02
    assert rep["recommendation"]["text"] and rep["caveats"]
    # saved where the dashboard and the coach read it, with a CSV of every run
    assert simlab.load_report(cfg)["generated"] == rep["generated"]
    assert (tmp_path / "data" / "simlab" / "runs.csv").read_text().count("\n") == 7
    assert "Simulation Lab" in simlab.report_text(rep)
    assert "runs" not in simlab.report_view(rep) and len(simlab.report_view(rep, runs=2)["runs"]) == 2


def test_adaptive_starts_each_simulation_with_only_what_it_knew_that_morning(tmp_path, monkeypatch):
    cfg = lab_cfg(tmp_path, days=40)
    seen = []
    real = simlab._morning_knowledge

    async def spy(cfg, contract, bars, start):
        seen.append((start, max(b.ts for b in bars)))
        return await real(cfg, contract, bars, start)

    monkeypatch.setattr(simlab, "_morning_knowledge", spy)
    rep = run(simlab.run_lab(cfg, starts=2, symbols=["MNQ"], strategies=["adaptive"], workers=1, save=False))
    assert rep["totals"]["errors"] == 0 and len(seen) == 2
    assert not (tmp_path / "data" / "simlab" / "report.json").exists()  # an experiment: nothing saved


def test_experiments_check_their_settings_before_running(tmp_path):
    cfg = lab_cfg(tmp_path, days=30)
    with pytest.raises(ValueError, match="Unknown strategy"):
        run(simlab.run_lab(cfg, strategies=["nope"], workers=1, save=False))
    rep = run(simlab.run_lab(cfg, starts=1, symbols=["MNQ"], strategies=["orb"], params={"orb": {"target_r": 1.5}},
                             risk_per_trade=100, workers=1, save=False))
    assert rep["risk_per_trade"] == 100 and rep["params"] == {"orb": {"target_r": 1.5}}
    with pytest.raises(ValueError):
        run(simlab.run_lab(cfg, symbols=["MNQ"], strategies=["orb"], params={"orb": {"range_minutes": 7}}, workers=1, save=False))
    with pytest.raises(ValueError, match="No price history"):
        run(simlab.run_lab(cfg, symbols=["ZZZ"], workers=1, save=False))


def pb(net, halves, trades=30, pf=1.5):
    return {"net": net, "halves": halves, "trades": trades, "profit_factor": pf}


def test_verdicts_demand_steady_money_in_both_halves():
    assert simlab._verdict(pb(500, [250, 250], trades=4))[0] == "thin"
    assert simlab._verdict(pb(-10, [100, -110]))[0] == "losing"
    assert simlab._verdict(pb(500, [-100, 600]))[0] == "mixed"
    assert simlab._verdict(pb(2551, [6, 2545]))[0] == "mixed"  # one half close to flat: not an edge
    assert simlab._verdict(pb(900, [400, 500], pf=1.05))[0] == "mixed"
    assert simlab._verdict(pb(900, [400, 500]))[0] == "edge"


def test_ranking_puts_real_evidence_ahead_of_tiny_samples():
    daily = [(f"2026-01-0{i % 9 + 1}", 100.0 if i % 3 else -80.0, -50.0, 1) for i in range(30)]
    runs = [simlab.SimRun("MNQ", "orb", "", "full", daily=daily, trades=30, wins=20, trade_pnls=[50.0] * 20 + [-40.0] * 10),
            simlab.SimRun("MES", "vwap_reversion", "", "full", daily=daily[:2], trades=2, wins=2, trade_pnls=[300.0, 200.0])]
    out = simlab.summarize_playbooks(runs, PLANS["50K"])
    assert [p["strategy"] for p in out] == ["orb", "vwap_reversion"]
    assert out[1]["verdict"] == "thin"
    hold = simlab.holdout_check(out)
    assert hold["label"] == "MNQ · orb" and "second half" in hold["text"]


def test_teaching_puts_every_market_into_the_long_run_memory(tmp_path):
    cfg = lab_cfg(tmp_path, days=40, symbols=("MNQ", "MES"))
    out = run(simlab.teach(cfg, ["MNQ", "MES"]))
    assert {o["symbol"] for o in out} == {"MNQ", "MES"} and all(o["observations"] > 0 for o in out)
    for s in ("MNQ", "MES"):
        assert (Path(cfg.data_dir) / f"knowledge_{s}_5m_longrun.json").exists()
    # the training is stamped with the history's last bar, so a live bot still retrains on fresh data when it starts
    stamp = json.loads((Path(cfg.data_dir) / "knowledge_MNQ_5m_longrun.json").read_text())["trained"]["at"]
    assert stamp[:10] <= out[0]["to"] or stamp[:10] == (out[0]["to"])  # not "now"


# ------------------------------------------------------------------------------------------- AI coach

def usage(i=1000, o=500):
    return SimpleNamespace(input_tokens=i, output_tokens=o, cache_read_input_tokens=0, cache_creation_input_tokens=0, iterations=None)


def block(kind, **kw):
    return SimpleNamespace(type=kind, **kw)


class FakeClaude:
    """Plays a scripted conversation and records what the coach sent."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self.create))

    async def create(self, **kw):
        self.calls.append(json.loads(json.dumps(kw, default=lambda o: getattr(o, "__dict__", str(o)))))
        return self.script.pop(0)

    async def close(self):
        pass


def response(content, stop="tool_use", u=None):
    return SimpleNamespace(content=content, stop_reason=stop, usage=u or usage(), model="claude-opus-5-5")


def coach_for(tmp_path, monkeypatch, script, **coach_settings):
    cfg = lab_cfg(tmp_path, days=30)
    cfg.instrument.symbol, cfg.strategy.name = "M2K", "orb"
    for k, v in coach_settings.items():
        setattr(cfg.coach, k, v)
    c = Coach(cfg, api_key="sk-ant-test-0000000000")
    fake = FakeClaude(script)
    monkeypatch.setattr(c, "_client", lambda: fake)
    return c, fake


def test_a_session_reads_tests_records_lessons_and_proposes(tmp_path, monkeypatch):
    script = [
        response([block("thinking", thinking=""), block("tool_use", id="t1", name="get_account_overview", input={}),
                  block("tool_use", id="t2", name="run_simulation", input={"symbols": ["MNQ"], "strategies": ["orb"], "starts": 1,
                                                                             "params_json": "{}", "risk_per_trade": 0})]),
        response([block("tool_use", id="t3", name="record_lesson", input={"text": "M2K orb lost money.", "evidence": "26 trades, -$493",
                                                                          "confidence": "medium"}),
                  block("tool_use", id="t4", name="propose_change", input={"kind": "risk", "symbol": "", "strategy": "",
                        "setting": "risk_per_trade", "value": 100, "reason": "Survive longer.", "evidence": "lab"}),
                  block("tool_use", id="t5", name="propose_change", input={"kind": "risk", "symbol": "", "strategy": "",
                        "setting": "risk_per_trade", "value": 900, "reason": "More!", "evidence": ""})]),
        response([block("text", text="## Findings\n- No edge yet.\n\n**Lower risk.**")], stop="end_turn"),
    ]
    c, fake = coach_for(tmp_path, monkeypatch, script)
    stages = []
    session = run(c.run("Is M2K right?", progress=stages.append))
    assert session["report"].startswith("## Findings") and session["turns"] == 3
    assert [x["text"] for x in session["lessons"]] == ["M2K orb lost money."]
    assert [p["title"] for p in session["proposals"]] == ["Risk per trade: $150 -> $100"]  # the raise was refused
    # every tool result went back to Claude; the refused raise as an error, the simulation with real numbers
    results = {r["tool_use_id"]: r for m in fake.calls[2]["messages"] if m["role"] == "user" and isinstance(m["content"], list)
               for r in m["content"] if isinstance(r, dict)}
    assert results["t5"].get("is_error") and "only lower risk" in results["t5"]["content"]
    sim = json.loads(results["t2"]["content"])
    assert sim["totals"]["simulations"] == 1 and "Experiment only" in sim["note"]
    assert not (tmp_path / "data" / "simlab" / "report.json").exists()
    # the request: adaptive thinking, effort, caching, refusal fallback, strict tools, the owner's question and lessons
    first = fake.calls[0]
    assert first["model"] == "claude-opus-5-5" and first["thinking"] == {"type": "adaptive"}
    assert first["output_config"] == {"effort": "high"} and first["cache_control"] == {"type": "ephemeral"}
    assert first["fallbacks"] == "default" and first["betas"] == ["server-side-fallback-2026-07-01"]
    assert all(t["strict"] for t in first["tools"]) and "Is M2K right?" in first["messages"][0]["content"]
    # spending is priced and logged; the session, lessons and proposals are kept on disk
    assert session["cost_usd"] == round(3 * (1000 * 4 + 500 * 20) / 1e6, 4) and c.spent() == pytest.approx(session["cost_usd"])
    assert c.sessions(1)[0]["id"] == session["id"] and (c.folder / "latest_report.md").exists()
    # the next session starts from what this one learned
    assert "M2K orb lost money." in c._opening("") and "Risk per trade" in c._opening("")
    assert any("simulating" in s for s in stages)


def test_budgets_stop_a_session_and_block_new_ones(tmp_path, monkeypatch):
    big = usage(i=200_000, o=40_000)  # $1.60 at Opus prices
    script = [response([block("tool_use", id="t1", name="get_strategy_catalog", input={})], u=big),
              response([block("text", text="Final report.")], stop="end_turn")]
    c, fake = coach_for(tmp_path, monkeypatch, script, max_session_usd=1.0, monthly_budget_usd=1.5)
    session = run(c.run())
    assert session["stopped"] and "session budget" in session["stopped"] and len(fake.calls) == 1
    assert "ended before Claude wrote its report" in session["report"]
    with pytest.raises(CoachError, match="budget is used up"):
        run(c.run())


def test_the_last_affordable_turn_asks_for_the_report_without_tools(tmp_path, monkeypatch):
    script = [response([block("tool_use", id="t1", name="get_strategy_catalog", input={})], u=usage(i=60_000, o=4_000)),
              response([block("text", text="Report.")], stop="end_turn")]
    c, fake = coach_for(tmp_path, monkeypatch, script, max_session_usd=0.35)
    session = run(c.run())
    assert session["report"] == "Report."
    last = fake.calls[-1]
    assert last["tool_choice"] == {"type": "none"} and last["messages"][-1]["role"] == "system"


def test_api_problems_become_plain_messages(tmp_path, monkeypatch):
    import anthropic

    c, fake = coach_for(tmp_path, monkeypatch, [])
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")

    async def broke(**kw):
        raise anthropic.BadRequestError("bad", response=httpx.Response(400, request=req),
                                        body={"error": {"message": "Your credit balance is too low to access the API."}})

    fake.beta.messages.create = broke
    with pytest.raises(CoachError, match="Buy credits"):
        run(c.run())
    assert "Buy credits" in c.status()["error"] and not c.running
    c2 = Coach(c.cfg, api_key=None)
    monkeypatch.delenv(coach_mod.ENV_KEY, raising=False)
    with pytest.raises(CoachError, match="Add your Claude API key"):
        c2._client()


def test_pricing_counts_every_attempt_of_a_fallback():
    u = SimpleNamespace(input_tokens=1, output_tokens=1, cache_read_input_tokens=0, cache_creation_input_tokens=0,
                        iterations=[{"model": "claude-opus-5-5", "input_tokens": 1_000_000, "output_tokens": 0},
                                    {"model": "claude-sonnet-5-5", "input_tokens": 1_000_000, "output_tokens": 1_000_000}])
    assert Coach.price("claude-opus-5-5", u) == pytest.approx(4 + 2 + 10)
    assert Coach.price("claude-haiku-5-5", usage(1_000_000, 1_000_000)) == pytest.approx(0.6)


def test_proposals_are_checked_against_topstep_and_the_current_settings(tmp_path):
    cfg = lab_cfg(tmp_path, days=1)
    cfg.instrument.symbol, cfg.strategy.name = "M2K", "orb"
    c = Coach(cfg)
    with pytest.raises(ValueError, match="unknown strategy"):
        c.add_proposal({"kind": "playbook", "symbol": "MNQ", "strategy": "magic"}, "s")
    with pytest.raises(ValueError, match="already"):
        c.add_proposal({"kind": "playbook", "symbol": "M2K", "strategy": "orb"}, "s")
    with pytest.raises(ValueError, match="only lower"):
        c.add_proposal({"kind": "risk", "setting": "max_trades_per_day", "value": 9}, "s")
    p = c.add_proposal({"kind": "playbook", "symbol": "mnq", "strategy": "adaptive", "reason": "r"}, "s")
    assert p["symbol"] == "MNQ" and p["status"] == "pending"
    c.add_proposal({"kind": "playbook", "symbol": "MNQ", "strategy": "adaptive", "reason": "again"}, "s")
    assert len(c.proposals()) == 1  # the same pending proposal is replaced, not repeated
    assert c.set_proposal_status(c.proposals()[0]["id"], "dismissed")["status"] == "dismissed"
    lesson = c.add_lesson("  Trade   MNQ  ", "x", "bogus", "s")
    assert lesson["text"] == "Trade MNQ" and lesson["confidence"] == "low"
    assert c.forget_lesson(lesson["id"]) and not c.lessons()


def test_claude_keys_never_reach_a_log():
    f = RedactingFilter()
    assert f.redact("key=sk-ant-api03-AbCdEfGhIjKlMnOp_qr-123 done") == "key=sk-ant-*** done"


# ------------------------------------------------------------------------------- dashboard routes

TWO_ACCOUNTS = [{"name": "c1", "account_id": 1, "risk": {"risk_per_trade": 300}}, {"name": "c2", "account_id": 2}]


def scenario(tmp_path, monkeypatch, body, accounts=None, strategy=None):
    cfg_src = lab_cfg(tmp_path, days=30)
    path = tmp_path / "config.yaml"
    raw = {"mode": "paper", "data_dir": cfg_src.data_dir, "log_dir": str(tmp_path / "logs"),
           "account": {"plan": "50K", "stage": "combine", "account_id": 7},
           "instrument": {"symbol": "M2K"}, "strategy": strategy or {"name": "orb"},
           "risk": {"risk_per_trade": 150}, "research": {"enabled": False}}
    if accounts:
        raw["accounts"] = accounts
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    (tmp_path / ".env").write_text("TOPSTEPX_USERNAME=me\n", encoding="utf-8")
    monkeypatch.setattr(controller_mod, "notify", lambda *a: None)
    monkeypatch.delenv(coach_mod.ENV_KEY, raising=False)

    async def go():
        ctl = Controller(load_config(path), Secrets(), mode="paper", config_path=str(path), worker_command=FAKE, port=0,
                         poll_seconds=0.1)
        await ctl.server.start()
        http = httpx.AsyncClient(base_url=f"http://127.0.0.1:{ctl.server.port}", headers={"X-Token": ctl.token}, timeout=60)
        try:
            await body(ctl, http, path)
        finally:
            await http.aclose()
            await ctl.server.stop()
            for b in ctl.bots.values():
                await b.close()
    run(go())


def test_simulations_tab_runs_the_lab_and_applies_a_playbook(tmp_path, monkeypatch):
    async def body(ctl, http, path):
        s = (await http.get("/api/status")).json()
        assert s["lab"]["generated"] is None and not s["lab"]["running"]
        r = (await http.post("/api/lab/run", json={"starts": 1, "teach": False})).json()
        assert r["ok"] and "started" in r["message"]
        assert "already" in (await http.post("/api/lab/run", json={})).json()["message"]
        await asyncio.wait_for(ctl.lab.task, 300)
        st = (await http.post("/api/lab/state")).json()
        assert st["report"]["totals"]["simulations"] > 0 and st["error"] is None and st["history"] == ["MNQ"]
        assert (await http.get("/api/status")).json()["lab"]["totals"]["simulations"] == st["report"]["totals"]["simulations"]
        r = (await http.post("/api/lab/apply", json={"symbol": "MNQ", "strategy": "adaptive"})).json()
        assert r["ok"] and "MNQ" in r["message"]
        cfg = load_config(path)
        assert (cfg.instrument.symbol, cfg.strategy.name, cfg.instrument.auto_pick) == ("MNQ", "adaptive", False)
        assert (path.parent / "config.yaml.bak").exists()
        assert not (await http.post("/api/lab/apply", json={"symbol": "MNQ", "strategy": "magic"})).json()["ok"]
        actions = ProxyActions(ctl)  # Telegram's /lab and /coach
        assert "Simulation Lab" in await actions.lab_text() and "AI coach" in await actions.coach_text()
    scenario(tmp_path, monkeypatch, body)


def test_coach_tab_stores_the_key_and_applies_only_what_you_approve(tmp_path, monkeypatch):
    async def fake_test(self, key):
        if key.endswith("bad"):
            raise CoachError("Claude refused that key.")
        return "Key works."

    monkeypatch.setattr(Coach, "test_key", fake_test)

    async def body(ctl, http, path):
        st = (await http.post("/api/coach/state")).json()
        assert not st["key_saved"] and st["model"] == "claude-opus-5-5" and st["links"]["billing"].startswith("https://")
        assert not (await http.post("/api/coach/run", json={})).json()["ok"]  # no key yet
        assert not (await http.post("/api/coach/key", json={"key": "hello"})).json()["ok"]
        r = (await http.post("/api/coach/key", json={"key": "sk-ant-api03-bad"})).json()
        assert not r["ok"] and "refused" in r["message"]
        r = (await http.post("/api/coach/key", json={"key": "sk-ant-api03-good"})).json()
        assert r["ok"] and f"{coach_mod.ENV_KEY}=sk-ant-api03-good" in (path.parent / ".env").read_text()
        st = (await http.post("/api/coach/state")).json()
        assert st["key_saved"] and "sk-ant" not in json.dumps(st)  # never sent back to the page
        r = (await http.post("/api/coach/settings", json={"model": "claude-sonnet-5-5", "max_session_usd": "0.5"})).json()
        assert r["ok"] and load_config(path).coach.model == "claude-sonnet-5-5"
        assert not (await http.post("/api/coach/settings", json={"monthly_budget_usd": "99999"})).json()["ok"]

        c = ctl.coach.coach
        risk = c.add_proposal({"kind": "risk", "setting": "risk_per_trade", "value": 100, "reason": "r"}, "s")
        play = c.add_proposal({"kind": "playbook", "symbol": "MNQ", "strategy": "adaptive", "reason": "r"}, "s")
        assert (await http.get("/api/status")).json()["coach"]["pending"] == 2
        r = (await http.post("/api/coach/proposal", json={"id": risk["id"], "action": "apply"})).json()
        assert r["ok"] and load_config(path).risk.risk_per_trade == 100
        assert not (await http.post("/api/coach/proposal", json={"id": risk["id"], "action": "apply"})).json()["ok"]
        r = (await http.post("/api/coach/proposal", json={"id": play["id"], "action": "dismiss"})).json()
        assert r["ok"] and load_config(path).instrument.symbol == "M2K"
        assert (await http.post("/api/coach/key/remove")).json()["ok"]
        assert coach_mod.ENV_KEY not in (path.parent / ".env").read_text()
    scenario(tmp_path, monkeypatch, body)


def test_a_request_refused_over_refusal_fallbacks_is_sent_again_without_them(tmp_path, monkeypatch):
    import anthropic

    c, fake = coach_for(tmp_path, monkeypatch, [response([block("text", text="Done.")], stop="end_turn")])
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    real = fake.create

    async def picky(**kw):
        if "fallbacks" in kw:
            raise anthropic.BadRequestError("bad", response=httpx.Response(400, request=req),
                                            body={"error": {"message": "fallbacks: unknown parameter"}})
        return await real(**kw)

    fake.beta.messages.create = picky
    session = run(c.run())
    assert session["report"] == "Done." and "fallbacks" not in fake.calls[0]


async def apply(http, prop):
    return (await http.post("/api/coach/proposal", json={"id": prop["id"], "action": "apply"})).json()


@pytest.mark.parametrize("setting,shared,own,lower,lowest", [("risk_per_trade", 150, 300, 200, 100),
                                                             ("max_trades_per_day", 4, 8, 6, 3)])
def test_coach_apply_changes_only_the_reviewed_account(tmp_path, monkeypatch, setting, shared, own, lower, lowest):
    accounts = [{"name": "c1", "account_id": 1, "risk": {setting: own}}, {"name": "c2", "account_id": 2}]

    async def body(ctl, http, path):
        def values():
            cfg = load_config(path)
            return getattr(cfg.for_account("c1").risk, setting), getattr(cfg.for_account("c2").risk, setting), getattr(cfg.risk, setting)
        assert ctl.bot.account == "c1"
        r = await apply(http, ctl.coach.coach.add_proposal({"kind": "risk", "setting": setting, "value": lower, "reason": "r"}, "s"))
        assert r["ok"] and r["message"].startswith("Applied on c1") and values() == (lower, shared, shared)
        # Below the shared value: writing the shared section would have left c1 where it was and lowered c2.
        r = await apply(http, ctl.coach.coach.add_proposal({"kind": "risk", "setting": setting, "value": lowest, "reason": "r"}, "s"))
        assert r["ok"] and values() == (lowest, shared, shared)
        assert isinstance(load_config(path).profile("c1").risk[setting], int) or setting != "max_trades_per_day"
    scenario(tmp_path, monkeypatch, body, accounts=accounts, strategy={"name": "orb", "params": {"range_minutes": 30}})


def test_coach_lowers_every_account_through_the_shared_setting_only_when_none_has_its_own(tmp_path, monkeypatch):
    accounts = [{"name": "c1", "account_id": 1}, {"name": "c2", "account_id": 2}]

    async def body(ctl, http, path):
        r = await apply(http, ctl.coach.coach.add_proposal({"kind": "risk", "setting": "risk_per_trade", "value": 100, "reason": "r"}, "s"))
        cfg = load_config(path)
        assert r["ok"] and "every account" in r["message"] and cfg.risk.risk_per_trade == 100
        assert cfg.for_account("c2").risk.risk_per_trade == 100 and not cfg.profile("c1").risk
    scenario(tmp_path, monkeypatch, body, accounts=accounts)


def test_a_playbook_apply_keeps_the_strategy_settings_and_stays_on_its_account(tmp_path, monkeypatch):
    async def single(ctl, http, path):
        r = await apply(http, ctl.coach.coach.add_proposal({"kind": "playbook", "symbol": "MNQ", "strategy": "orb", "reason": "r"}, "s"))
        cfg = load_config(path)
        assert r["ok"] and cfg.instrument.symbol == "MNQ" and cfg.strategy.params == {"range_minutes": 30}
    scenario(tmp_path, monkeypatch, single, strategy={"name": "orb", "params": {"range_minutes": 30}})

    async def several(ctl, http, path):
        r = await apply(http, ctl.coach.coach.add_proposal({"kind": "playbook", "symbol": "MNQ", "strategy": "orb", "reason": "r"}, "s"))
        cfg = load_config(path)
        assert r["ok"] and "on c1" in r["message"]
        assert cfg.instrument.symbol == "M2K" and cfg.for_account("c2").instrument.symbol == "M2K"  # c2 untouched
        c1 = cfg.for_account("c1")
        assert c1.instrument.symbol == "MNQ" and c1.strategy.params == {"range_minutes": 30}
    (tmp_path / "config.yaml.bak").unlink(missing_ok=True)
    scenario(tmp_path, monkeypatch, several, accounts=[{"name": "c1", "account_id": 1}, {"name": "c2", "account_id": 2}],
             strategy={"name": "orb", "params": {"range_minutes": 30}})


def test_a_playbook_for_an_account_that_copies_another_is_refused(tmp_path, monkeypatch):
    async def body(ctl, http, path):
        assert ctl.bot.account == "c1"
        r = await apply(http, ctl.coach.coach.add_proposal({"kind": "playbook", "symbol": "MNQ", "strategy": "orb", "reason": "r"}, "s"))
        assert not r["ok"] and "c1 copies c2's calls" in r["message"]
        assert load_config(path).for_account("c1").instrument.symbol == "M2K"
    scenario(tmp_path, monkeypatch, body, accounts=[{"name": "c1", "account_id": 1, "follow": "c2"}, {"name": "c2", "account_id": 2}])
