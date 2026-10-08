"""Walk-forward training, the Opening Range Momentum strategy, noise-band checkpoint exits and the
ATR minimum stop distance."""

from datetime import date, timedelta

import pytest
import yaml

from topstep_bot.backtest.data import synthetic_bars
from topstep_bot.broker.paper import PaperBroker
from topstep_bot.config import BotConfig, load_config
from topstep_bot.factory import build_core
from topstep_bot.instruments import offline_contract
from topstep_bot.models import OrderSide
from topstep_bot.strategies import create_strategy
from topstep_bot.strategies.base import StrategyContext
from topstep_bot.training import (
    Candidate,
    Fold,
    Run,
    candidates,
    make_folds,
    save_strategy,
    stats_for,
    train,
    walk_forward,
)

from .conftest import CT, bar, ct, run

DAY = date(2026, 3, 3)


def ctx(close_ct, position=0):
    utc = close_ct.astimezone(CT)
    return StrategyContext(bar_close=close_ct, local_close=utc, day=DAY, position=position, entry_price=None, stop_price=None)


# --------------------------------------------------------- Opening Range Momentum

def test_orb_momentum_defaults_follow_the_2024_paper(mnq):
    s = create_strategy("orb_momentum", {}, mnq, 5)
    assert s.p["stop_mode"] == "atr" and s.p["atr_stop_frac"] == 0.10 and s.p["target_r"] == 0
    assert s.warmup_days == 15  # needs 14 sessions for the average daily range


def test_orb_momentum_trades_the_opening_candle_direction(mnq):
    s = create_strategy("orb_momentum", {"stop_mode": "range", "target_r": 10}, mnq, 5)  # the 2023 paper
    s.on_new_day(DAY)
    sig = s.on_bar(bar(ct(2026, 3, 3, 8, 30), 100.0, 104.0, 99.0, 103.0), ctx(ct(2026, 3, 3, 8, 35)))
    assert sig.side == OrderSide.BUY and sig.stop_price == 99.0  # stop at the candle's low
    assert sig.target_price == pytest.approx(103.0 + 10 * 4.0)  # 10R
    assert s.on_bar(bar(ct(2026, 3, 3, 8, 35), 103, 110, 102, 109), ctx(ct(2026, 3, 3, 8, 40))) is None  # once a day


def test_orb_momentum_skips_dojis_and_late_starts(mnq):
    s = create_strategy("orb_momentum", {"min_body_ticks": 2, "stop_mode": "range"}, mnq, 5)
    s.on_new_day(DAY)
    assert s.on_bar(bar(ct(2026, 3, 3, 8, 30), 100.0, 101.0, 99.0, 100.25), ctx(ct(2026, 3, 3, 8, 35))) is None
    s.on_new_day(DAY + timedelta(days=1))
    late = ct(2026, 3, 4, 9, 0)  # bot started after the opening candle: don't trade a stale signal
    assert s.on_bar(bar(late - timedelta(minutes=5), 100, 105, 99, 104), ctx(late)) is None


def test_orb_momentum_atr_stop_and_no_target(mnq):
    s = create_strategy("orb_momentum", {"stop_mode": "atr", "atr_days": 2, "atr_stop_frac": 0.25, "target_r": 0}, mnq, 5)
    for i, (hi, lo) in enumerate([(110, 90), (120, 100)]):  # two sessions with 20-point ranges
        d = DAY + timedelta(days=i)
        s.on_new_day(d)
        s.on_bar(bar(ct(d.year, d.month, d.day, 9, 0), lo + 5, hi, lo, hi - 5), ctx(ct(d.year, d.month, d.day, 9, 5)))
    s.on_new_day(DAY + timedelta(days=2))
    assert s.daily_atr == pytest.approx(20.0)
    sig = s.on_bar(bar(ct(2026, 3, 5, 8, 30), 120.0, 121.0, 116.0, 117.0), ctx(ct(2026, 3, 5, 8, 35)))
    assert sig.side == OrderSide.SELL and sig.stop_price == pytest.approx(117.0 + 0.25 * 20.0)
    assert sig.target_price is None


# ------------------------------------------------------------ late-day momentum

def _session(s, day, closes):
    """Feed one regular session: {"HH:MM" bar-close time: close price}."""
    s.on_new_day(day)
    out = None
    for hhmm, price in closes.items():
        h, m = map(int, hhmm.split(":"))
        close = ct(day.year, day.month, day.day, h, m)
        sig = s.on_bar(bar(close - timedelta(minutes=5), price, price + 1, price - 1, price), ctx(close))
        out = sig or out
    return out


def test_late_day_momentum_follows_the_morning_move(mnq):
    s = create_strategy("late_day_momentum", {}, mnq, 5)
    for _ in range(15):  # warm the ATR up
        s.atr.update(102.0, 98.0, 100.0)
    _session(s, DAY - timedelta(days=1), {"14:55": 100.0, "15:00": 100.0})  # yesterday closed at 100
    sig = _session(s, DAY, {"09:00": 101.0, "14:00": 99.0, "14:25": 99.5})
    assert sig.side == OrderSide.BUY  # morning move was up (+1%), so buy into the close
    assert sig.stop_price == pytest.approx(99.5 - 2.0 * s.atr.value) and sig.target_price is None
    confirmed = create_strategy("late_day_momentum", {"confirm_with_12th": True}, mnq, 5)
    for _ in range(15):
        confirmed.atr.update(102.0, 98.0, 100.0)
    _session(confirmed, DAY - timedelta(days=1), {"15:00": 100.0})
    assert _session(confirmed, DAY, {"09:00": 101.0, "14:00": 100.0, "14:25": 99.5}) is None  # afternoon disagrees


def test_late_day_momentum_needs_a_previous_close_and_valid_times(mnq):
    s = create_strategy("late_day_momentum", {"min_move_pct": 0.5}, mnq, 5)
    for _ in range(15):
        s.atr.update(102.0, 98.0, 100.0)
    assert _session(s, DAY, {"09:00": 101.0, "14:25": 101.0}) is None  # first day: no previous close
    assert _session(s, DAY + timedelta(days=1), {"09:00": 101.2, "14:25": 101.0}) is None  # +0.2% < 0.5%
    with pytest.raises(ValueError, match="not the close of a 15-minute bar"):
        create_strategy("late_day_momentum", {}, mnq, 15)  # 14:25 isn't a 15-minute bar close
    assert create_strategy("late_day_momentum", {"entry_time": "14:15"}, mnq, 15)


# ------------------------------------------------- noise breakout checkpoint exits

def test_noise_checkpoint_mode_keeps_safety_stop_and_never_trails(mnq):
    s = create_strategy("noise_breakout", {}, mnq, 5)
    assert s.p["exit_mode"] == "checkpoint"  # the paper's design is the default
    assert s.trailing_stop(bar(ct(2026, 3, 3, 10, 0), 1, 1, 1, 1), ctx(ct(2026, 3, 3, 10, 5), position=1)) is None
    trail = create_strategy("noise_breakout", {"exit_mode": "trail"}, mnq, 5)
    assert trail.p["exit_mode"] == "trail"
    with pytest.raises(ValueError, match="exit_mode"):
        create_strategy("noise_breakout", {"exit_mode": "sometimes"}, mnq, 5)


# ------------------------------------------------------------ min_stop_atr floor

def test_min_stop_atr_widens_tight_stops_and_shrinks_size(mnq):
    async def go(min_stop_atr):
        cfg = BotConfig.model_validate({"risk": {"min_stop_atr": min_stop_atr, "risk_per_trade": 300,
                                                 "personal_daily_loss_limit": 1000}})
        now = [ct(2026, 3, 3, 9, 0)]
        broker = PaperBroker(mnq, 50_000, slippage_ticks=0, live=True)
        core = build_core(cfg, mnq, broker, clock=lambda: now[0], account_label="t")
        core.balance = 50_000
        await core.begin_day(core.schedule.trading_day(now[0]), 50_000)
        for _ in range(20):
            core.atr.update(110.0, 100.0, 105.0)  # ATR = 10 points
        await broker.on_price(now[0], 100.0)
        from topstep_bot.models import Signal

        await core._handle_entry(Signal("long", 98.0, None, "t"), bar(now[0], 100, 100, 100, 100), core.context(bar(now[0], 100, 100, 100, 100)))
        return core.orders.trade

    tight = run(go(None))
    wide = run(go(1.0))
    assert tight.stop_price == 98.0
    assert wide.stop_price == 90.0 and wide.size < tight.size  # 1 ATR away, fewer contracts for the same risk


# ------------------------------------------------------------------- training

def _run(name, daily, trades_per_day=1):
    r = Run(Candidate(name, ()))
    for i, p in enumerate(daily):
        d = date(2025, 1, 1) + timedelta(days=i)
        r.days.append(d)
        r.pnl[d] = p
        r.worst[d] = min(0.0, p)
        r.trades[d] = [(p / trades_per_day, p / 100)] * trades_per_day if p else []
    return r


def test_make_folds_rolls_forward_without_overlap():
    days = [date(2025, 1, 1) + timedelta(days=i) for i in range(140)]
    folds = make_folds(days, folds=4)
    assert len(folds) == 4
    for f in folds:
        assert f.train[1] < f.test[0]  # always judged on later, unseen days
        assert (f.train[1] - f.train[0]).days + 1 == 3 * ((f.test[1] - f.test[0]).days + 1)
    assert all(a.test[1] < b.test[0] for a, b in zip(folds, folds[1:], strict=False))
    assert folds[-1].test[1] == days[-1]
    with pytest.raises(ValueError, match="Not enough history"):
        make_folds(days[:40], folds=4)


def test_walk_forward_scores_only_unseen_days():
    # "lucky" is great early (train) and bad later; "steady" is modestly positive throughout.
    n = 140
    lucky = _run("lucky", [50.0 if i < 105 else -60.0 for i in range(n)])
    steady = _run("steady", [10.0 + (5 if i % 2 else -5) for i in range(n)])
    folds = [Fold((lucky.days[0], lucky.days[104]), (lucky.days[105], lucky.days[139]))]
    results, recommended, _ = walk_forward([lucky, steady], folds)
    by = {r.strategy: r for r in results}
    assert by["lucky"].oos.net < 0 and not by["lucky"].eligible and by["lucky"].reason == "lost money"
    assert recommended is by["steady"]


def test_stats_combine_pass_rate_uses_real_mll():
    from topstep_bot.risk.topstep import PLANS

    r = _run("x", [400.0] * 20)
    s = stats_for([(r, r.days[0], r.days[-1])], PLANS["50K"])
    assert s.net == 8000 and s.combine_pass_rate == 1.0 and s.max_drawdown == 0


def test_candidates_skip_settings_invalid_for_the_timeframe():
    cfg5 = BotConfig.model_validate({"instrument": {"timeframe_minutes": 5}})
    cfg15 = BotConfig.model_validate({"instrument": {"timeframe_minutes": 15}, "strategy": {"name": "noise_breakout"}})
    five = [c for c in candidates(cfg5, ["orb_momentum"])]
    fifteen = [c for c in candidates(cfg15, ["orb_momentum"])]
    assert any(dict(c.params).get("range_minutes", 5) == 5 for c in five)
    assert all(dict(c.params).get("range_minutes", 5) == 15 for c in fifteen)  # 5-min range impossible on 15m bars
    assert len(set(five)) == len(five)
    with pytest.raises(ValueError, match="Unknown strategy"):
        candidates(cfg5, ["nope"])


def test_train_end_to_end_on_synthetic_data(monkeypatch):
    monkeypatch.setitem(__import__("topstep_bot.training").training.GRIDS, "orb_momentum",
                        [{"target_r": 3}, {"stop_mode": "range", "target_r": 10}])
    cfg = BotConfig.model_validate({"strategy": {"name": "orb"}})
    bars = synthetic_bars("MNQ", days=100, seed=3)
    report = train(cfg, bars, offline_contract("MNQ"), strategies=["orb_momentum"], folds=2, workers=1)
    assert len(report.folds) == 2 and report.candidates == 4  # defaults + 2 grid points + the current config
    assert {r.strategy for r in report.results} == {"orb_momentum", "orb"}  # current config is the baseline
    assert report.current is not None and report.current_label == "orb (defaults)"
    assert report.to_dict()["folds"][0]["train"][0] <= report.to_dict()["folds"][0]["test"][0]


def test_tuning_report_html_and_cli_save(tmp_path, monkeypatch, restore_logging):
    from topstep_bot import cli
    from topstep_bot.backtest.train_report import build_training_report

    monkeypatch.setitem(__import__("topstep_bot.training").training.GRIDS, "orb_momentum", [{"target_r": 3}])
    cfg = BotConfig.model_validate({"strategy": {"name": "orb"}})
    bars = synthetic_bars("MNQ", days=100, seed=3)
    report = train(cfg, bars, offline_contract("MNQ"), strategies=["orb_momentum"], folds=2, workers=1)
    page = build_training_report(report, "MNQ", 5)
    assert page.startswith("<!doctype html>") and "orb_momentum" in page and "Settings chosen in each window" in page
    assert "{" not in page.split("<script>")[0].split("</style>")[1]  # no unformatted template fields

    # `tune` saves the recommendation (when there is one) into config.yaml, keeping a backup.
    (tmp_path / "config.yaml").write_text(CONFIG, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    rec = report.results[0]
    rec.final = Candidate("orb_momentum", (("target_r", 0),))
    report.recommended = rec
    monkeypatch.setattr("topstep_bot.training.train", lambda *a, **k: report)
    monkeypatch.setattr(cli, "_history", lambda cfg, args, allow_synthetic=True: (bars, False))
    assert cli.main(["-c", "config.yaml", "tune", "--save", "--no-open", "--workers", "1"]) == 0
    assert load_config(tmp_path / "config.yaml").strategy.name == "orb_momentum"
    assert (tmp_path / "config.yaml.bak").exists() and list((tmp_path / "reports").glob("training_*.html"))


# ---------------------------------------------------------------- save settings

CONFIG = """# my settings
mode: paper

instrument:
  symbol: MNQ   # micro nasdaq

strategy:
  name: orb                # comment that goes away
  params: {}

risk:
  risk_per_trade: 120          # keep me
"""


def test_save_strategy_rewrites_only_the_strategy_block(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(CONFIG, encoding="utf-8")
    backup = save_strategy(path, "orb_momentum", {"stop_mode": "atr", "atr_stop_frac": 0.1}, note="Tuned today")
    text = path.read_text(encoding="utf-8")
    assert "# my settings" in text and "# micro nasdaq" in text and "risk_per_trade: 120          # keep me" in text
    assert "# Tuned today" in text
    cfg = load_config(path)
    assert cfg.strategy.name == "orb_momentum" and cfg.strategy.params == {"stop_mode": "atr", "atr_stop_frac": 0.1}
    assert cfg.risk.risk_per_trade == 120
    assert backup.read_text(encoding="utf-8") == CONFIG
    save_strategy(path, "noise_breakout", {}, note="Tuned today")  # again: replaces block and old note
    text = path.read_text(encoding="utf-8")
    assert text.count("strategy:") == 1 and text.count("# Tuned") == 1
    assert yaml.safe_load(text)["strategy"] == {"name": "noise_breakout", "params": {}}


def test_save_strategy_appends_when_missing_and_restores_on_error(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("mode: paper\n", encoding="utf-8")
    save_strategy(path, "orb", {"target_r": 3.0})
    assert load_config(path).strategy.params == {"target_r": 3.0}
    before = path.read_text(encoding="utf-8")
    with pytest.raises(ValueError):
        save_strategy(path, "orb", {"not_a_param": 1})
    assert path.read_text(encoding="utf-8") == before  # bad result rolled back
