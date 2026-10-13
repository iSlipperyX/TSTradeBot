"""Version 3.0: one Claude layer, the market network, and the aggressive daily-profit mode with its two risk fixes
(no trade can carry a Combine day past the consistency line; every hard limit allows for a sudden jump past the stop)."""

import json
import math
import os
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from topstep_bot import __version__, claude, network
from topstep_bot.brain import intents
from topstep_bot.brain.facts import TOOLS, Facts
from topstep_bot.config import AggressiveConfig, BotConfig, load_config
from topstep_bot.engine import MIN_CEILING_TICKS
from topstep_bot.models import Bar, Signal
from topstep_bot.research import DailyBar, ResearchLibrary
from topstep_bot.risk.manager import AGGRESSIVE_GOAL_SHARE, CONSISTENCY_GUARD_FRACTION

from .conftest import run
from .test_dashboard_setup import FORM, setup_scenario
from .test_dashboard_setup import clean_env as clean_env  # noqa: PLC0414 - the fixture, by its name
from .test_engine_and_dashboard import T0, make_core

UTC = timezone.utc
ROOT = Path(__file__).parent.parent
DASHBOARD = ROOT / "src" / "topstep_bot" / "dashboard" / "index.html"
DAY = date(2026, 3, 3)


# ------------------------------------------------------------------------------ one Claude layer

def test_one_model_catalogue_with_prices_and_refusal_fallbacks():
    assert claude.DEFAULT_MODEL == "claude-opus-5-5" and set(claude.MODELS) == {
        "claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5", "claude-fable-5-1"}
    assert claude.fallback_args("claude-opus-5-5") == {"betas": [claude.FALLBACK_BETA], "fallbacks": "default"}
    assert claude.fallback_args("claude-haiku-5-5") == {} and claude.fallback_args("unknown") == {}
    usage = {"input_tokens": 1_000_000, "output_tokens": 1_000_000}
    assert claude.price("claude-opus-5-5", usage) == pytest.approx(24.0)
    assert claude.price("claude-opus-5-5", None) == 0.0
    # a response that fell back to another model is priced attempt by attempt
    fell_back = {"iterations": [{"model": "claude-opus-5-5", "input_tokens": 1_000_000},
                                {"model": "claude-sonnet-5-5", "input_tokens": 1_000_000, "output_tokens": 100_000}]}
    assert claude.price("claude-opus-5-5", fell_back) == pytest.approx(4.0 + 2.0 + 1.0)


def test_the_coach_and_the_ask_answers_share_one_spending_ledger(tmp_path):
    ledger = claude.ledger_for(tmp_path)
    ledger.log("claude-opus-5-5", {"input_tokens": 100_000}, "coach")
    ledger.log("claude-haiku-5-5", {"output_tokens": 1_000_000}, "ask")
    assert ledger.spent(source="coach") == 0.4 and ledger.spent(source="ask") == 0.5 and ledger.spent() == 0.9
    assert ledger.spent("1999-01") == 0.0
    ledger.check(1.0)
    with pytest.raises(claude.ClaudeError, match="budget is used up"):
        ledger.check(0.9)
    assert (tmp_path / "coach" / "usage.jsonl").exists()


def test_the_key_is_saved_once_for_everything_and_removed_everywhere(tmp_path, monkeypatch):
    from topstep_bot.config import Secrets

    monkeypatch.delenv(claude.KEY_ENV, raising=False)
    env, secrets = tmp_path / ".env", Secrets()
    claude.store_key(env, "sk-ant-api03-abc", secrets)
    assert f"{claude.KEY_ENV}=sk-ant-api03-abc" in env.read_text() and claude.current_key() == "sk-ant-api03-abc"
    assert secrets.anthropic_api_key == "sk-ant-api03-abc"
    claude.store_key(env, None, secrets)
    assert claude.KEY_ENV not in env.read_text() and claude.current_key() is None and secrets.anthropic_api_key is None
    assert os.environ.get(claude.KEY_ENV) is None


def test_a_key_that_is_not_a_claude_key_is_refused_without_a_call():
    with pytest.raises(claude.ClaudeError, match="doesn't look like a Claude API key"):
        run(claude.check_key("sk-proj-openai", "claude-opus-5-5"))


def test_errors_are_explained_in_plain_words():
    import anthropic
    import httpx

    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")

    def err(cls, status, message):
        return cls(message, response=httpx.Response(status, request=req), body={"error": {"message": message}})

    assert "refused the API key" in claude.explain(err(anthropic.AuthenticationError, 401, "invalid x-api-key"))
    assert "credit balance is too low" in claude.explain(err(anthropic.BadRequestError, 400, "Your credit balance is too low"))
    assert "rate-limiting" in claude.explain(err(anthropic.RateLimitError, 429, "slow down"))
    assert "pick another model" in claude.explain(err(anthropic.NotFoundError, 404, "nope"), "claude-fable-5-1")


# ------------------------------------------------------------------------------ the market network

def five_minute(n, start=100.0, jump_every=None, jump=0.0, tick=0.25):
    bars, price, ts = [], start, datetime(2026, 1, 5, 14, 30, tzinfo=UTC)
    for i in range(n):
        o = price + (jump if jump_every and i and i % jump_every == 0 else 0.0)
        c = o + (tick if i % 2 else -tick)
        bars.append(Bar(ts, o, max(o, c) + tick, min(o, c) - tick, c, 100))
        price, ts = c, ts + timedelta(minutes=5)
    return bars


def test_jump_allowance_comes_from_history_with_a_floor_and_a_cap():
    few = network.jump_ticks(five_minute(100), 0.25, 5, "equity")
    assert few["basis"] == "floor" and few["ticks"] == network.JUMP_FLOOR_TICKS["equity"]
    quiet = network.jump_ticks(five_minute(2_000), 0.25, 5, "equity")
    assert quiet["ticks"] == network.JUMP_FLOOR_TICKS["equity"] and quiet["measured"] is not None
    # one bar in 100 opens 20 ticks away from the last close: the allowance is set by those jumps
    jumpy = network.jump_ticks(five_minute(2_000, jump_every=100, jump=5.0), 0.25, 5, "equity")
    assert jumpy["basis"] == "history" and jumpy["ticks"] == pytest.approx(20, abs=1)
    wild = network.jump_ticks(five_minute(2_000, jump_every=50, jump=500.0), 0.25, 5, "equity")
    assert wild["ticks"] == network.JUMP_FLOOR_TICKS["equity"] * network.JUMP_CAP_MULTIPLE
    # a session break or a roll is not a jump
    gap = five_minute(1_000)
    later = [Bar(b.ts + timedelta(days=3), b.open + 50, b.high + 50, b.low + 50, b.close + 50, 100) for b in five_minute(1_000)]
    assert network.jump_ticks(gap + later, 0.25, 5, "equity")["basis"] == "floor"


def daily(n, drift, vol_late=1.0, seed=1, shocks=None):
    import random

    rnd, price, out = random.Random(seed), 100.0, []
    day = date(2016, 1, 4)
    for i in range(n):
        r = (shocks[i] if shocks else rnd.gauss(0, 0.01)) * (vol_late if i > n - 25 else 1.0) + drift
        o, price = price, price * (1 + r)
        out.append(DailyBar(day.isoformat(), o, max(o, price) * 1.002, min(o, price) * 0.998, price))
        day += timedelta(days=1)
    return out


def seed_research(tmp_path):
    import random

    rnd = random.Random(7)
    common = [rnd.gauss(0, 0.01) for _ in range(400)]
    own = [rnd.gauss(0, 0.003) for _ in range(400)]
    lib = ResearchLibrary(tmp_path / "research")
    lib.save_daily("ES", daily(400, 0.002, shocks=[c + o for c, o in zip(common, own, strict=True)]))
    lib.save_daily("NQ", daily(400, 0.002, shocks=[1.2 * c - o for c, o in zip(common, own, strict=True)]))
    lib.save_daily("ZN", daily(400, -0.001, shocks=[-0.5 * c + o for c, o in zip(common, own, strict=True)]))
    lib.save_daily("GC", daily(400, 0.0, seed=3, vol_late=4.0))  # its volatility jumps in the last month
    return BotConfig.model_validate({"data_dir": str(tmp_path)})


def test_the_network_reads_every_market_together(tmp_path):
    cfg = seed_research(tmp_path)
    now = datetime.now(UTC)
    picture = network.build(cfg, now)
    m = picture["markets"]
    assert picture["coverage"]["daily"] >= 4 and picture["coverage"]["markets"] == len(m) >= 40
    assert {"ES", "MES", "NQ", "MNQ", "CL", "ZC"} <= set(m)
    assert m["MES"]["daily"] == m["ES"]["daily"]  # a micro reads its full-size market's history
    assert m["ES"]["peers"][0]["symbol"] == "NQ" and m["ES"]["peers"][0]["corr"] > 0.8
    assert any(p["symbol"] == "ZN" and p["corr"] < 0 for p in m["ES"]["peers"])
    assert m["GC"]["daily"]["state"] == "stressed" and m["ES"]["daily"]["trend"] in ("up", "down", "sideways")
    assert picture["board"]["mood"] in ("risk-on", "risk-off", "mixed", "risk-off, stressed") and "ES" in " ".join(picture["board"]["appetite_basis"])
    assert m["CL"]["daily"] is None and m["CL"]["jump"]["ticks"] == network.JUMP_FLOOR_TICKS["energy"]
    network.save(cfg, picture)
    assert network.load(cfg) == json.loads(json.dumps(picture, default=str)) and network.fresh(network.load(cfg))
    # protection only: what the risk manager takes from it
    gc = network.market_risk(cfg, "MGC")
    assert gc.stressed and "volatility in the top" in gc.why
    assert not network.market_risk(cfg, "ES").stressed
    stale = {**picture, "built": (now - network.MAX_AGE - timedelta(hours=1)).isoformat()}
    assert network.market_risk(cfg, "GC", stale) == network.MarketRisk(network.default_jump_ticks("GC"), False,
                                                                       "default jump allowance (no network picture yet)")
    words = network.text(picture, ["GC"])
    assert "Market network" in words and "GC:" in words and "never picks a direction" in words
    view = network.view(picture, ["ES"])
    assert view["fresh"] and next(r for r in view["rows"] if r["symbol"] == "ES")["focus"]


def test_the_network_is_answered_by_the_bot_and_the_coach(tmp_path):
    from topstep_bot.coach import TOOLS as COACH_TOOLS
    from topstep_bot.coach import Coach

    cfg = seed_research(tmp_path)
    facts = Facts(cfg)
    assert "network" in TOOLS and "No market network yet" in facts.call("network")["text"]
    network.refresh(cfg)
    out = facts.call("network", {"symbol": "gold"})
    assert "Market network" in out["text"] and out["fresh"]

    async def fetch(name, args):
        return facts.call(name, args)

    assert "GC:" in run(intents.answer("is gold stressed?", fetch))
    assert "Board:" in run(intents.answer("how are the markets moving together?", fetch))
    assert "get_market_network" in {t["name"] for t in COACH_TOOLS}
    assert Coach(cfg)._network()["markets"]


def test_the_cli_builds_the_network(tmp_path, capsys):
    from topstep_bot import cli

    cfg = seed_research(tmp_path)
    path = tmp_path / "config.yaml"
    path.write_text(f"data_dir: {json.dumps(str(tmp_path))}\n", encoding="utf-8")
    args = cli.build_parser().parse_args(["-c", str(path), "network", "--symbols", "ES"])
    assert args.func(args) == 0
    assert network.load(cfg) is not None and "ES:" in capsys.readouterr().out


# ------------------------------------------------------------------------------ risk: sudden jumps past the stop

def test_hard_limits_allow_for_a_jump_past_the_stop(mnq):
    core, _, _ = make_core(mnq, risk={"risk_per_trade": 300, "personal_daily_loss_limit": 300, "slippage_ticks": 0})
    rm = core.risk
    rm.start_day(DAY, 50_000)
    rpc, worst = rm.risk_per_contract(100, 90), rm.worst_loss_per_contract(100, 90)
    assert worst == pytest.approx(rpc + rm.jump_ticks * mnq.tick_value) and rm.jump_ticks == 8
    size = rm.position_size(100, 90, 50_000)
    assert size == math.floor(300 / worst) < math.floor(300 / rpc)
    assert size * worst <= 300  # a stop that fills the jump late still can't reach the daily loss limit
    core.apply_network({"version": 1, "built": datetime.now(UTC).isoformat(), "markets": {
        "MNQ": {"jump": {"ticks": 30, "basis": "history"}, "daily": {"state": "normal", "vol_pctile": 0.5}}}})
    assert rm.jump_ticks == 30 and rm.position_size(100, 90, 50_000) == math.floor(300 / rm.worst_loss_per_contract(100, 90))
    assert "30 ticks" in rm.snapshot(50_000, 0.0)["network"]


def test_the_mll_cushion_holds_against_the_jump(mnq):
    core, _, _ = make_core(mnq, risk={"risk_per_trade": 1_000, "personal_daily_loss_limit": 1_000, "slippage_ticks": 0,
                                      "mll_buffer": 100})
    rm = core.risk
    rm.start_day(DAY, 50_000)
    rm.tracker.floor = 49_500  # $500 above the MLL, $400 after the buffer
    size = rm.position_size(100, 90, 50_000)
    assert size >= 1 and size * rm.worst_loss_per_contract(100, 90) <= 400


# ------------------------------------------------------------------------------ risk: the consistency line

def combine_core(mnq, **risk):
    core, _, _ = make_core(mnq, risk={"risk_per_trade": 500, "personal_daily_loss_limit": 1_000, "slippage_ticks": 0,
                                      **risk})
    run(core.begin_day(core.schedule.trading_day(T0), 50_000))
    return core


def test_no_trade_can_carry_a_combine_day_past_the_consistency_line(mnq):
    core = combine_core(mnq)
    line = CONSISTENCY_GUARD_FRACTION * 3_000
    assert core.risk.profit_ceiling() == line == 1_500
    far = Signal("long", stop_price=90.0, target_price=200.0, reason="t")  # 40-tick stop, 400-tick target
    # early in the day: plenty of room, the strategy's own plan stands
    plan = core.plan_entry(far, 100.0)
    assert plan.target == 200.0 or plan.size * core.contract.ticks(plan.target - 100.0) * mnq.tick_value <= line
    # up $1,400: $100 of room. 5 contracts x 40 ticks x $0.50 = $100 exactly, so the target moves to the line
    core.balance = 51_400
    plan = core.plan_entry(far, 100.0)
    assert (plan.size, plan.target) == (5, 110.0)
    assert plan.size * core.contract.ticks(plan.target - 100.0) * mnq.tick_value <= 100
    # a near target that fits is kept, at the size that keeps it inside
    near = Signal("long", stop_price=90.0, target_price=102.0, reason="t")
    plan = core.plan_entry(near, 100.0)
    assert plan.target == 102.0 and plan.size * 8 * mnq.tick_value <= 100
    # shorts too
    short = Signal("short", stop_price=110.0, target_price=0.0, reason="t")
    plan = core.plan_entry(short, 100.0)
    assert plan.target == 90.0 and plan.size == 5
    # at the line, or too close to it to be worth the costs: no trade
    core.balance = 51_499
    assert "ticks of profit left" in core.plan_entry(far, 100.0)
    core.balance = 51_500
    assert "consistency line" in core.plan_entry(far, 100.0)


def test_the_ceiling_keeps_one_contract_with_a_short_target_when_nothing_else_fits(mnq):
    core = combine_core(mnq)
    core.balance = 51_490  # $10 of room = 20 ticks on one MNQ, less than the 40-tick stop
    plan = core.plan_entry(Signal("long", stop_price=90.0, reason="t"), 100.0)
    assert plan.size == 1 and plan.target == 105.0 and MIN_CEILING_TICKS <= 20


def test_without_the_guard_or_off_the_combine_there_is_no_ceiling(mnq):
    core = combine_core(mnq, consistency_guard=False)
    assert core.risk.profit_ceiling() is None and core.risk.reward_room(51_400) is None
    express, _, _ = make_core(mnq, account={"stage": "express", "payout_path": "standard"})
    assert express.risk.profit_ceiling() is None
    consistency, _, _ = make_core(mnq, account={"stage": "express", "payout_path": "consistency"})
    assert consistency.risk.profit_ceiling() == 0.4 * 3_000


# ------------------------------------------------------------------------------ the aggressive daily-profit mode

def aggressive_core(mnq, risk_per_trade=200, **aggr):
    core, _, _ = make_core(mnq, risk={"risk_per_trade": risk_per_trade, "personal_daily_loss_limit": 1_000,
                                      "slippage_ticks": 0}, aggressive={"enabled": True, **aggr})
    run(core.begin_day(core.schedule.trading_day(T0), 50_000))
    return core


def test_aggressive_mode_sets_a_daily_goal_under_the_line(mnq):
    rm = aggressive_core(mnq).risk
    assert rm.aggressive_goal == AGGRESSIVE_GOAL_SHARE * 3_000 == rm.daily_profit_target == 1_350
    assert rm.aggressive_goal < rm.profit_ceiling()
    assert aggressive_core(mnq, daily_goal=800).risk.daily_profit_target == 800
    snap = rm.snapshot(50_000, 0.0)["aggressive"]
    assert snap["goal"] == 1_350 and snap["locked_in"] is None and snap["lock_in_from"] == 675
    assert make_core(mnq)[0].risk.snapshot(50_000, 0.0)["aggressive"] is None


def test_aggressive_risk_presses_with_profit_and_backs_off_when_unproven_or_stressed(mnq):
    rm = aggressive_core(mnq).risk
    assert rm.trade_risk(50_000) == 200 and rm.trade_risk(50_000, conviction=0.5) == 100
    rm.record_trade(400, T0)  # house money: 35% of today's realized profit
    assert rm.trade_risk(50_400) == pytest.approx(340)
    rm.record_trade(2_000, T0)
    assert rm.trade_risk(52_400) == 500  # never past 2.5 times the risk per trade
    rm.stressed = True
    assert rm.trade_risk(52_400) == 250
    rm.stressed = False
    rm.tracker.floor = 52_400 - 900  # $900 above the MLL, $650 after the default $250 buffer
    assert rm.trade_risk(52_400) == pytest.approx(0.25 * (900 - rm.cfg.mll_buffer))


def test_unproven_signals_trade_at_reduced_size(mnq):
    core = aggressive_core(mnq, risk_per_trade=400)
    proven = core.plan_entry(Signal("long", stop_price=90.0, reason="t", meta={"level": "cell"}), 100.0)
    unproven = core.plan_entry(Signal("long", stop_price=90.0, reason="t", meta={"level": "unproven"}), 100.0)
    assert unproven.size < proven.size and unproven.planned_risk <= 0.5 * 400 + 1e-9


def test_aggressive_mode_locks_in_the_day_and_stops_when_it_gives_back_too_much(mnq):
    rm = aggressive_core(mnq).risk
    rm.record_trade(600, T0)
    assert rm.locked_floor() is None  # under half the $1,350 goal
    rm.record_trade(200, T0)
    assert rm.locked_floor() == pytest.approx(800 * 0.6)
    # a stop-out can't take the day under the locked-in $480
    size = rm.position_size(100, 90, 50_800)
    assert size * rm.worst_loss_per_contract(100, 90) <= 800 - 480
    assert rm.entry_block_reason(T0, 50_800) is None
    assert "gave back 40%" in rm.entry_block_reason(T0, 50_480)
    rm2 = aggressive_core(mnq).risk
    rm2.record_trade(1_350, T0)
    assert "daily profit target" in rm2.entry_block_reason(T0, 51_350)


def test_aggressive_mode_closes_before_high_impact_news(mnq):
    assert aggressive_core(mnq)._flatten_before_news
    assert not aggressive_core(mnq, flatten_before_news=False)._flatten_before_news
    assert not make_core(mnq)[0]._flatten_before_news


def test_a_daily_goal_past_the_consistency_line_is_refused():
    with pytest.raises(ValueError, match="must stay below"):
        BotConfig.model_validate({"aggressive": {"enabled": True, "daily_goal": 1_500}})
    BotConfig.model_validate({"aggressive": {"enabled": True, "daily_goal": 1_400}})
    assert AggressiveConfig().enabled is False  # off unless you choose it


# ------------------------------------------------------------------------------ the Setup tab

@pytest.mark.usefixtures("clean_env")
def test_the_setup_tab_turns_on_make_money_every_day(tmp_path):
    async def body(ctl, http, path):
        state = (await http.post("/api/setup/state")).json()
        assert state["choices"]["goal_targets"]["50K"] == {"profit_goal": 1_350, "line": 1_500}
        r = (await http.post("/api/setup/save", json={**FORM, "goal": "profit", "daily_goal": "1,200", "max_trades": "12"})).json()
        assert r["ok"], r
        cfg = load_config(path)
        assert cfg.aggressive.enabled and cfg.aggressive.daily_goal == 1_200 and cfg.risk.max_trades_per_day == 12
        assert cfg.strategy.name == "adaptive" and cfg.strategy.params == {"trade_unproven": True}
        assert cfg.risk.consistency_guard and cfg.risk.daily_profit_target is None
        assert (await http.post("/api/setup/state")).json()["values"]["goal"] == "profit"
        r = (await http.post("/api/setup/save", json={**FORM, "goal": "profit", "daily_goal": "1600"})).json()
        assert not r["ok"] and "below $1,500" in r["message"]
        r = (await http.post("/api/setup/save", json={**FORM, "goal": "pass"})).json()
        assert r["ok"], r
        cfg = load_config(path)
        assert not cfg.aggressive.enabled and cfg.strategy.params == {}
        assert (await http.post("/api/setup/state")).json()["values"]["goal"] == "pass"
    setup_scenario(tmp_path, body)


# ------------------------------------------------------------------------------ the release

def test_version_3_0_is_the_first_release():
    assert __version__ == "3.0.0"
    assert 'version = "3.0.0"' in (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "## 3.0.0" in changelog and "no bot" in changelog.lower()
    release = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    assert "contents: write" in release and "pyproject.toml" in release


def test_the_dashboard_shows_the_new_panels_once():
    html = DASHBOARD.read_text(encoding="utf-8")
    ids = re.findall(r'\bid="([^"]+)"', html)
    assert sorted({i for i in ids if ids.count(i) > 1}) == []
    for needed in ("aggpanel", "agg-bar", "agg-txt", "agg-lines", "net-panel", "net-table", "net-board", "su-dgoal"):
        assert needed in ids
    assert 'data-goal="profit"' in html and "v3.0" in html


def test_telegram_has_a_network_command():
    from topstep_bot.telegram_control import COMMANDS

    assert "network" in dict(COMMANDS)
