from datetime import date, timedelta

import pytest
import yaml

from topstep_bot.backtest.data import load_csv, save_csv, synthetic_bars
from topstep_bot.backtest.metrics import combine_statistics, compute_metrics, simulate_combine
from topstep_bot.backtest.report import build_report
from topstep_bot.backtest.runner import run_backtest
from topstep_bot.config import BotConfig
from topstep_bot.engine import DayRecord
from topstep_bot.instruments import offline_contract
from topstep_bot.models import Bar
from topstep_bot.risk.topstep import PLANS
from topstep_bot.sessions import SessionSchedule
from topstep_bot.strategies import STRATEGIES, StrategyContext, create_strategy
from topstep_bot.wizard import render_config

from .conftest import CT, ct, run

BARS = synthetic_bars("MNQ", days=45, seed=3)


@pytest.mark.parametrize("name", list(STRATEGIES))
def test_backtest_accounting_and_session_rules(name):
    cfg = BotConfig.model_validate({"strategy": {"name": name}})
    res = run(run_backtest(cfg, BARS, offline_contract("MNQ")))
    # Balance change equals the sum of trade results (nothing leaks or double counts).
    assert res.final_balance - res.starting_balance == pytest.approx(sum(t.net_pnl for t in res.trades), abs=0.01)
    schedule = SessionSchedule(cfg.session)
    for t in res.trades:
        local_open = schedule.local(t.opened_at)
        local_close = schedule.local(t.closed_at)
        assert local_open.time() >= cfg.session.trade_start
        assert schedule.trading_day(t.opened_at) == schedule.trading_day(t.closed_at)  # never held overnight
        assert local_close.hour < 15 or (local_close.hour == 15 and local_close.minute <= 10)
        assert t.filled_size <= 50  # Topstep 50K cap: 5 minis = 50 micros
    assert all(d.trades <= cfg.risk.max_trades_per_day for d in res.days)


def test_backtest_is_deterministic():
    cfg = BotConfig.model_validate({"strategy": {"name": "orb"}})
    a = run(run_backtest(cfg, BARS, offline_contract("MNQ")))
    b = run(run_backtest(cfg, BARS, offline_contract("MNQ")))
    assert [t.net_pnl for t in a.trades] == [t.net_pnl for t in b.trades]


def test_report_renders():
    cfg = BotConfig.model_validate({"strategy": {"name": "orb"}})
    res = run(run_backtest(cfg, BARS, offline_contract("MNQ")))
    html = build_report(res)
    assert "Opening Range Breakout" in html and "const DATA" in html


def test_metrics_and_combine_simulation():
    d = date(2026, 3, 2)
    days = [DayRecord(d + timedelta(days=i), 50_000 + 1_000 * i, 50_000 + 1_000 * (i + 1), 50_000 + 1_000 * i, 1) for i in range(4)]
    out = simulate_combine(days, PLANS["50K"])
    assert out.status == "passed" and out.days_used == 3
    one_big_day = [DayRecord(d, 50_000, 53_100, 50_000, 1)]
    assert simulate_combine(one_big_day, PLANS["50K"]).status == "incomplete"  # consistency rule
    crash = [DayRecord(d, 50_000, 49_900, 47_900, 1)]
    assert simulate_combine(crash, PLANS["50K"]).status == "failed"  # intraday MLL touch
    stats = combine_statistics(days, PLANS["50K"])
    assert stats["attempts"] >= 1
    m = compute_metrics([], days, 50_000)
    assert m["trades"] == 0 and m["days"] == 4


def test_orb_signals_long_on_range_breakout(mnq):
    strat = create_strategy("orb", {"range_minutes": 15}, mnq, 5)
    strat.on_new_day(date(2026, 3, 3))

    def feed(hh, mm, o, h, l, c):  # noqa: E741
        start = ct(2026, 3, 3, hh, mm)
        close = start + timedelta(minutes=5)
        ctx = StrategyContext(close, close.astimezone(CT), date(2026, 3, 3), 0, None, None)
        return strat.on_bar(Bar(start, o, h, l, c, 100), ctx)

    assert feed(8, 30, 100, 105, 99, 104) is None
    assert feed(8, 35, 104, 106, 101, 102) is None
    assert feed(8, 40, 102, 104, 98, 100) is None  # range 98-106 complete
    sig = feed(8, 45, 100, 108, 100, 107.5)
    assert sig is not None and sig.action == "long"
    assert sig.stop_price == pytest.approx(102.0)  # middle of range
    assert sig.target_price == pytest.approx(107.5 + 2 * 5.5)
    assert feed(8, 50, 107, 109, 106, 108.5) is None  # one long per day


def test_strategy_rejects_unknown_params(mnq):
    with pytest.raises(ValueError, match="Unknown parameter"):
        create_strategy("orb", {"rang_minutes": 15}, mnq, 5)


def test_csv_roundtrip(tmp_path):
    path = tmp_path / "bars.csv"
    save_csv(BARS[:100], path)
    loaded = load_csv(path)
    assert len(loaded) == 100 and loaded[0].ts == BARS[0].ts and loaded[-1].close == BARS[99].close


def test_csv_naive_times_use_given_timezone(tmp_path):
    path = tmp_path / "ct.csv"
    path.write_text("Date,Open,High,Low,Close,Volume\n2026-03-03 08:30:00,1,2,0.5,1.5,10\n", encoding="utf-8")
    bar = load_csv(path, naive_tz="America/Chicago")[0]
    assert bar.ts == ct(2026, 3, 3, 8, 30)


def test_generated_config_templates_parse():
    for text in (render_config(), render_config(mode="live", plan="150K", stage="express", account_id=5, strategy="noise_breakout")):
        cfg = BotConfig.model_validate(yaml.safe_load(text))
        assert cfg.session.flatten_at.hour == 15
    cfg = BotConfig.model_validate(yaml.safe_load(render_config(account_id=42, risk_per_trade=99)))
    assert cfg.account.account_id == 42 and cfg.risk.risk_per_trade == 99


def test_example_config_file_parses():
    from pathlib import Path

    cfg = BotConfig.model_validate(yaml.safe_load(Path("config.example.yaml").read_text(encoding="utf-8")))
    assert cfg.mode == "paper"
