"""The probability calculator: pooled P(win) and expected value, the veto, Kelly sizing, the network, calibration."""

import json
from datetime import date

import pytest

from topstep_bot.config import BotConfig
from topstep_bot.factory import build_core
from topstep_bot.instruments import offline_contract
from topstep_bot.knowledge import KnowledgeBase, Observation, train_from_bars
from topstep_bot.models import Signal
from topstep_bot.probability import (
    MIN_CONVICTION,
    ROOT_P,
    Estimate,
    ProbabilityModel,
    calibration,
    calibration_text,
    load_network,
    network_paths,
)

from .conftest import bar, run
from .test_engine_and_dashboard import T0, make_core
from .test_knowledge import BARS_1M

DAY = date(2026, 3, 2)  # the day before T0's trading day


def obs(r, strategy="orb", side="LONG", slot="open", regime="calm", day=DAY, source="shadow", **extra):
    return Observation(day.isoformat(), "09:00", strategy, side, slot, regime, r, r * 50, source, **extra)


def outcomes(wins, losses, win_r=1.5, loss_r=-1.0, **kw):
    return [obs(win_r, **kw) for _ in range(wins)] + [obs(loss_r, **kw) for _ in range(losses)]


def model(rows=(), network=(), prior=()):
    return ProbabilityModel(rows, today=DAY, half_life_days=1000, real_weight=2.0, network=network, prior=prior)


# ------------------------------------------------------------------ the estimate

def test_with_no_evidence_it_says_so_and_never_claims_an_edge():
    est = model().estimate("orb", "LONG", "open", "calm")
    assert est.level == "none" and est.n == 0 and est.network_n == 0
    assert est.p_win == pytest.approx(ROOT_P) and est.ev_r < 0  # 45% at 1R either way: no edge assumed
    assert est.p_lo < 0.3 and est.p_hi > 0.6  # and very unsure about it
    assert not est.confident_loser(min_samples=8)  # an unknown signal is never vetoed for being unknown
    assert "no history yet" in est.text()


def test_evidence_moves_the_estimate_and_narrows_the_range():
    few = model(outcomes(6, 4)).estimate("orb", "LONG", "open", "calm")
    many = model(outcomes(120, 80)).estimate("orb", "LONG", "open", "calm")
    assert few.n == 10 and many.n == 200 and many.level == "cell"
    assert ROOT_P < few.p_win < many.p_win == pytest.approx(0.6, abs=0.02)  # few outcomes: pulled toward 45%
    assert many.win_r == pytest.approx(1.5, abs=0.05) and many.loss_r == pytest.approx(1.0, abs=0.05)
    assert many.ev_r == pytest.approx(0.6 * 1.5 - 0.4 * 1.0, abs=0.05)
    assert (many.p_hi - many.p_lo) < (few.p_hi - few.p_lo) / 2
    assert many.ev_lo > 0 and many.kelly > 0 and many.payoff == pytest.approx(1.5, abs=0.1)


def test_a_lucky_corner_cannot_outvote_the_rest_of_the_day():
    rows = outcomes(40, 160, win_r=1.0, slot="midday") + outcomes(5, 1, win_r=1.0, slot="open")  # 20% all day, 5/6 at 8:30
    est = model(rows).estimate("orb", "LONG", "open", "calm")
    assert est.p_win < 0.45 and est.ev_r < 0  # six outcomes move it, they don't make it a winner
    assert est.p_win > model(rows).estimate("orb", "LONG", "midday", "calm").p_win


def test_costs_count_and_real_fills_weigh_double():
    gross = model(outcomes(100, 100, win_r=1.0)).estimate("orb", "LONG", "open", "calm")
    net = model([obs(1.0, cost_r=0.2) for _ in range(100)] + [obs(-1.0, cost_r=0.2) for _ in range(100)])
    assert net.estimate("orb", "LONG", "open", "calm").ev_r < gross.ev_r - 0.15
    real = model([obs(1.0, source="real")] * 30 + [obs(-1.0)] * 30).estimate("orb", "LONG", "open", "calm")
    assert real.p_win > 0.6 and real.n_eff == pytest.approx(90)


def test_related_markets_inform_a_strategy_that_has_not_traded_here():
    network = outcomes(140, 60)  # 70% winners on related markets
    est = model(network=network).estimate("orb", "LONG", "open", "calm")
    assert est.level == "network" and est.network_n == 200 and est.n == 0
    assert est.p_win > 0.6
    # ... and this market's own outcomes win the argument once there are enough of them
    here = model(outcomes(30, 70), network=network).estimate("orb", "LONG", "open", "calm")
    assert here.p_win < 0.4 and here.level == "cell"
    assert model(network=network).estimate("orb", "SHORT", "open", "calm").level == "none"  # per side


def test_the_long_run_memory_is_a_background_that_recent_outcomes_overrule():
    prior = outcomes(150, 50)  # years of 75%
    alone = model(prior=prior).estimate("orb", "LONG", "open", "calm")
    assert alone.p_win > 0.5 and alone.n == alone.n_eff == 0 and alone.memory_n == 200  # informs, isn't "here"
    assert "200 in the long-run memory" in alone.text()
    assert not model(prior=outcomes(0, 400)).estimate("orb", "LONG", "open", "calm").confident_loser(8)
    recent = model(outcomes(20, 60), prior=prior).estimate("orb", "LONG", "open", "calm")
    assert recent.p_win < 0.4


# ------------------------------------------------------------------ what it changes

def test_the_veto_needs_enough_evidence_that_a_signal_loses():
    loser = model(outcomes(20, 80, win_r=1.0)).estimate("orb", "LONG", "open", "calm")
    assert loser.ev_hi < 0 and loser.confident_loser(min_samples=8)
    assert not loser.confident_loser(min_samples=500)  # not enough of its own outcomes yet
    thin = model(outcomes(1, 4, win_r=1.0)).estimate("orb", "LONG", "open", "calm")
    assert thin.ev_r < 0 and not thin.confident_loser(min_samples=4)  # losing, but nowhere near 90% sure


def test_quarter_kelly_conviction_stays_within_its_bounds():
    est = model(outcomes(120, 80)).estimate("orb", "LONG", "open", "calm")
    p, b = est.p_win, est.payoff
    assert est.kelly == pytest.approx((p - (1 - p) / b) / 4, abs=1e-6)
    assert est.conviction(1.5) == 1.5 and est.conviction(1.0) == 1.0  # a big edge: capped
    small = Estimate(p_win=0.5, win_r=1.1, loss_r=1.0, ev_r=0.05, p_lo=0.4, p_hi=0.6, ev_lo=-0.1, ev_hi=0.2, n=50,
                     n_eff=50, network_n=0, level="side", kelly=(0.5 - 0.5 / 1.1) / 4)
    assert MIN_CONVICTION < small.conviction(1.5) < 1.0  # a thin edge: smaller than usual
    none = model(outcomes(40, 60, win_r=1.0)).estimate("orb", "LONG", "open", "calm")
    assert none.kelly == 0 and none.conviction(1.5) == MIN_CONVICTION


def kb_core(mnq, rows, **cfg):
    core, broker, now = make_core(mnq, risk={"risk_per_trade": 200, "slippage_ticks": 0}, **cfg)
    kb = KnowledgeBase(None, min_samples=8, half_life_days=1000)
    for o in rows:
        kb.record(o)
    core.attach_knowledge(kb)
    run(core.begin_day(core.schedule.trading_day(T0), 50_000))
    core.last_price = 100.0
    return core


def signal():
    return Signal("long", stop_price=90.0, target_price=130.0, reason="test",
                  meta={"strategy": "orb", "slot": "open", "regime": "calm"})


def test_the_bot_skips_a_signal_it_is_sure_loses(mnq):
    core = kb_core(mnq, outcomes(20, 80, win_r=1.0))
    b = bar(T0, 100, 100, 100, 100)
    sig = signal()
    run(core._handle_entry(sig, b, core.context(b)))
    assert core.orders.trade is None
    assert "probability calculator is at least 90% sure it loses" in core.events[0]["message"]
    assert sig.meta["estimate"].confident_loser(8)
    # switched off: the same signal is traded
    off = kb_core(mnq, outcomes(20, 80, win_r=1.0), probability={"veto": False})
    run(off._handle_entry(signal(), b, off.context(b)))
    assert off.orders.trade is not None


def test_aggressive_sizing_follows_the_edge_and_never_the_hard_limits(mnq):
    strong = kb_core(mnq, outcomes(120, 80), aggressive={"enabled": True})
    weak = kb_core(mnq, outcomes(100, 100, win_r=1.05), aggressive={"enabled": True})
    calm = kb_core(mnq, outcomes(120, 80))  # not aggressive: the usual size
    sizes = {}
    for name, core in (("strong", strong), ("weak", weak), ("calm", calm)):
        sig = signal()
        core.estimate_signal(sig, T0)
        sizes[name] = (core.conviction(sig), core.plan_entry(sig, 100.0).size)
    assert sizes["strong"][0] == 1.5 and sizes["weak"][0] == MIN_CONVICTION and sizes["calm"][0] == 1.0
    assert sizes["strong"][1] > sizes["calm"][1] > sizes["weak"][1] >= 1
    no_sizing = kb_core(mnq, outcomes(120, 80), aggressive={"enabled": True}, probability={"sizing": False})
    sig = signal()
    no_sizing.estimate_signal(sig, T0)
    assert no_sizing.conviction(sig) == 1.0


def test_turned_off_it_gives_no_estimate(mnq):
    core = kb_core(mnq, outcomes(20, 80), probability={"enabled": False})
    sig = signal()
    assert core.estimate_signal(sig, T0) is None and core.conviction(sig) == 1.0


# ------------------------------------------------------------------ the knowledge network

def save_kb(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 2, "observations": [o.__dict__ for o in rows]}), encoding="utf-8")


def test_the_network_reads_related_markets_of_every_account(tmp_path):
    cfg = BotConfig.model_validate({"data_dir": str(tmp_path), "instrument": {"symbol": "MNQ"}})
    save_kb(tmp_path / "knowledge_MES_5m.json", outcomes(30, 10))
    save_kb(tmp_path / "accounts" / "other" / "knowledge_ES_5m.json", outcomes(5, 5))
    save_kb(tmp_path / "knowledge_GC_5m.json", outcomes(50, 0))  # gold: a different asset class
    save_kb(tmp_path / "knowledge_NQ_15m.json", outcomes(50, 0))  # another bar size
    (tmp_path / "knowledge_RTY_5m.json").write_text("{broken", encoding="utf-8")
    paths = network_paths(cfg, "MNQ")
    assert {p.name for p in paths} == {"knowledge_MES_5m.json", "knowledge_ES_5m.json", "knowledge_RTY_5m.json"}
    rows, markets = load_network(paths)
    assert sorted(markets) == ["ES", "MES"] and len(rows) == 50  # the broken file is skipped


def test_a_core_reads_the_network_into_its_estimates(tmp_path, mnq):
    save_kb(tmp_path / "knowledge_MES_5m.json", outcomes(140, 60))
    cfg = BotConfig.model_validate({"data_dir": str(tmp_path)})
    core = build_core(cfg, mnq, make_core(mnq)[1], clock=lambda: T0, account_label="test")
    core.attach_knowledge(KnowledgeBase(None, min_samples=8))
    est = core.probability.estimate("orb", "LONG", "open", "calm")
    assert core.probability.network_markets == ["MES"] and est.network_n == 200 and est.level == "network"
    off = build_core(BotConfig.model_validate({"data_dir": str(tmp_path), "probability": {"network": False}}), mnq,
                     make_core(mnq)[1], clock=lambda: T0, account_label="test")
    off.attach_knowledge(KnowledgeBase(None, min_samples=8))
    assert off.probability.estimate("orb", "LONG", "open", "calm").network_n == 0


# ------------------------------------------------------------------ how well it predicts

def test_calibration_scores_predictions_against_what_happened():
    assert calibration([])["n"] == 0 and "no scored predictions" in calibration_text(calibration([]))
    rows = ([obs(1.0, p_win=0.7, ev_r=0.4) for _ in range(7)] + [obs(-1.0, p_win=0.7, ev_r=0.4) for _ in range(3)]
            + [obs(1.0, p_win=0.3, ev_r=-0.4) for _ in range(3)] + [obs(-1.0, p_win=0.3, ev_r=-0.4) for _ in range(7)]
            + [obs(1.0)])  # never predicted: not scored
    cal = calibration(rows)
    assert cal["n"] == 20 and cal["win_rate"] == 0.5
    assert cal["brier"] == pytest.approx(0.21) and cal["base"] == pytest.approx(0.25) and cal["skill"] > 0
    assert [(b["band"], b["n"], b["predicted"], b["actual"]) for b in cal["buckets"]] == [
        ("0%-35%", 10, 0.3, 0.3), ("65%-100%", 10, 0.7, 0.7)]
    assert cal["ev_predicted"] == 0.0 and cal["ev_actual"] == 0.0
    text = calibration_text(cal)
    assert "20 predictions scored" in text and "said 65%-100%: 10 signals, predicted 70%, actually won 70%" in text
    assert calibration(rows, sources=("real",))["n"] == 0


def test_training_stamps_walk_forward_predictions(tmp_path):
    cfg = BotConfig.model_validate({"data_dir": str(tmp_path)})
    kb = KnowledgeBase(tmp_path / "kb.json", min_samples=3)
    run(train_from_bars(cfg, offline_contract("MNQ"), BARS_1M, kb))
    stamped = [o for o in kb.obs if o.p_win is not None]
    assert len(stamped) == len(kb.obs) > 20 and all(o.ev_r is not None for o in stamped)
    first_day = min(o.day for o in kb.obs)
    # The first replayed day knew nothing yet, so it was predicted from the prior alone.
    assert {o.p_win for o in kb.obs if o.day == first_day} == {ROOT_P}
    assert len({o.p_win for o in kb.obs}) > 5  # later days learned from the days before them
    assert calibration(kb.obs)["n"] == len(kb.obs)
