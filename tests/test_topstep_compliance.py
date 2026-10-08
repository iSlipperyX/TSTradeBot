"""Every Topstep rule the bot enforces, checked against Topstep's own published numbers."""

from datetime import date, timedelta
from pathlib import Path

import pytest

from topstep_bot.config import AccountConfig, BotConfig, RiskConfig, SessionConfig
from topstep_bot.instruments import offline_contract
from topstep_bot.models import Account
from topstep_bot.news import NewsCalendar, NewsEvent
from topstep_bot.risk.guards import OrderGuard, api_trading_block, hosting_warning
from topstep_bot.risk.manager import RiskManager
from topstep_bot.risk.topstep import (
    PLANS,
    LossLimitTracker,
    combine_progress,
    max_contracts_allowed,
    product_limit,
    xfa_payout_progress,
)
from topstep_bot.sessions import SessionSchedule

from .conftest import ct

DAY = date(2026, 3, 3)
OPEN = ct(2026, 3, 3, 9, 0)


def make(contract, stage="combine", plan="50K", balance=None, account=None, **risk):
    spec = PLANS[plan]
    start = 0.0 if stage == "express" else spec.account_size
    rm = RiskManager(
        RiskConfig(**risk), AccountConfig(stage=stage, plan=plan, **(account or {})), spec, contract,
        SessionSchedule(SessionConfig()), LossLimitTracker(start, spec.max_loss_limit), fees_round_turn=1.22,
    )
    rm.start_day(DAY, start if balance is None else balance)
    return rm


def calendar(*events):
    cal = NewsCalendar("http://unused", Path("unused.json"), ["High"], ["USD"], 5, 10)
    cal.events = list(events)
    cal.fetched_at = OPEN
    return cal


# ------------------------------------------------------------- Consistency Target (Combine)

def test_consistency_examples_from_topstep_help_center():
    plan = PLANS["50K"]
    # $1,600 best day on $3,000 of profit is 53%: passes.
    assert combine_progress(plan, 3_000, 1_600).passed
    # A $1,800 best day raises the target to $1,800 / 0.55 = $3,273.
    p = combine_progress(plan, 3_000, 1_800)
    assert p.profit_target == pytest.approx(3_272.73, abs=0.01)
    assert p.target_raised and not p.passed
    # A $2,200 best day makes it $4,000.
    assert combine_progress(plan, 3_000, 2_200).profit_target == pytest.approx(4_000)
    # 55% is a hard line with no rounding: exactly 55% is fine.
    assert not combine_progress(plan, 3_000, 1_650).target_raised


def test_daily_cap_and_consistency_guard_stay_under_55_percent(mnq):
    for name, spec in PLANS.items():
        rm = make(mnq, plan=name)
        assert rm.daily_profit_target < spec.consistency_day_limit
    rm = make(mnq)
    assert rm.check_open_risk(50_000, 1_400) is None
    reason = rm.check_open_risk(50_000, 1_500)  # 50% of the $3,000 target: close before 55%
    assert reason and "Consistency Target" in reason
    assert rm.entry_block_reason(OPEN, 50_000) is not None  # done for the day


def test_consistency_guard_can_be_turned_off_and_is_combine_only(mnq):
    assert make(mnq, consistency_guard=False).check_open_risk(50_000, 1_600) is None
    assert make(mnq, stage="express").check_open_risk(0, 1_600) is None


def test_stop_at_profit_target_in_live_trading(mnq):
    rm = make(mnq, balance=52_600)
    rm.live_guards = True
    rm.best_prior_day = 1_000
    assert rm.entry_block_reason(OPEN, 52_600) is None
    reason = rm.check_open_risk(52_600, 450)  # total $3,050 with best day $1,000
    assert reason and "profit target reached" in reason
    # Backtests replay many Combine starts from one run, so they keep trading.
    bt = make(mnq, balance=52_600)
    bt.best_prior_day = 1_000
    assert bt.check_open_risk(52_600, 450) is None


def test_raised_target_must_be_reached_before_stopping(mnq):
    rm = make(mnq, balance=52_000, consistency_guard=False)
    rm.live_guards = True
    rm.best_prior_day = 2_200  # target is now $4,000
    assert rm.check_open_risk(52_000, 1_100) is None
    assert "profit target reached" in rm.check_open_risk(52_000, 2_000)


# ------------------------------------------------------------------- position limits

def test_plan_caps_with_10_to_1_micros():
    for name, minis in (("50K", 5), ("100K", 10), ("150K", 15)):
        plan = PLANS[name]
        assert max_contracts_allowed(plan, "combine", plan.account_size, "ES", False) == minis
        assert max_contracts_allowed(plan, "combine", plan.account_size, "MNQ", True) == minis * 10


@pytest.mark.parametrize("plan,balance,minis", [
    ("50K", 0, 2), ("50K", 1_499, 2), ("50K", 1_500, 3), ("50K", 2_000, 5), ("50K", 9_000, 5),
    ("100K", 0, 3), ("100K", 1_500, 4), ("100K", 2_000, 5), ("100K", 3_000, 10),
    ("150K", 0, 3), ("150K", 2_999, 5), ("150K", 3_000, 10), ("150K", 4_500, 15),
])
def test_xfa_scaling_plan(plan, balance, minis):
    assert max_contracts_allowed(PLANS[plan], "express", balance, "NQ", False) == minis


def test_metals_and_energy_product_caps():
    assert max_contracts_allowed(PLANS["50K"], "combine", 50_000, "GC", False) == 3
    assert max_contracts_allowed(PLANS["150K"], "combine", 150_000, "MCL", True) == 90
    assert max_contracts_allowed(PLANS["100K"], "combine", 100_000, "CL", False) == 6
    # The XFA scaling plan still applies when it is lower than the product cap.
    assert max_contracts_allowed(PLANS["150K"], "express", 0, "GC", False) == 3
    assert max_contracts_allowed(PLANS["50K"], "express", 0, "MGC", True) == 20
    assert product_limit("SI", PLANS["50K"]) == 0
    assert product_limit("ES", PLANS["50K"]) is None


def test_untradable_product_is_refused_at_load_time():
    with pytest.raises(ValueError, match="does not currently allow trading SI"):
        BotConfig.model_validate({"instrument": {"symbol": "SI"}, "strategy": {"name": "orb"}})


def test_risk_manager_uses_product_cap():
    gc = offline_contract("GC")
    rm = make(gc, risk_per_trade=10_000, personal_daily_loss_limit=1_900)
    assert rm.max_contracts_topstep() == 3
    assert rm.position_size(2_000.0, 1_999.0, 50_000) <= 3


# ------------------------------------------------------------------- Daily Loss Limit

def test_topstep_dll_true_uses_the_plan_amount():
    assert AccountConfig(plan="50K", topstep_daily_loss_limit=True).topstep_daily_loss_limit == 1_000
    assert AccountConfig(plan="100K", topstep_daily_loss_limit=True).topstep_daily_loss_limit == 2_000
    assert AccountConfig(plan="150K", topstep_daily_loss_limit=True).topstep_daily_loss_limit == 3_000
    assert AccountConfig(plan="150K", topstep_daily_loss_limit=False).topstep_daily_loss_limit is None
    assert AccountConfig(plan="50K", topstep_daily_loss_limit=800).topstep_daily_loss_limit == 800


def test_personal_limit_must_sit_below_topstep_limits():
    with pytest.raises(ValueError, match="below your Topstep Daily Loss Limit"):
        BotConfig.model_validate({"account": {"topstep_daily_loss_limit": True},
                                  "risk": {"personal_daily_loss_limit": 1_000}, "strategy": {"name": "orb"}})
    with pytest.raises(ValueError, match="below the 50K Maximum Loss Limit"):
        BotConfig.model_validate({"risk": {"personal_daily_loss_limit": 2_000}, "strategy": {"name": "orb"}})
    with pytest.raises(ValueError, match="risk_per_trade"):
        BotConfig.model_validate({"risk": {"risk_per_trade": 600, "personal_daily_loss_limit": 500},
                                  "strategy": {"name": "orb"}})


def test_topstep_dll_stops_entries_and_flattens(mnq):
    rm = make(mnq, personal_daily_loss_limit=1_900, account={"topstep_daily_loss_limit": True})
    assert rm.topstep_dll == 1_000
    assert "Topstep daily loss limit" in rm.entry_block_reason(OPEN, 49_090)
    rm2 = make(mnq, personal_daily_loss_limit=1_900, account={"topstep_daily_loss_limit": True})
    assert "Topstep daily loss limit" in rm2.check_open_risk(50_000, -960)
    # Sizing never risks more than what is left before 90% of the DLL.
    rm3 = make(mnq, risk_per_trade=1_000, personal_daily_loss_limit=1_900, account={"topstep_daily_loss_limit": True})
    rpc = rm3.risk_per_contract(100, 90)
    assert rm3.position_size(100, 90, 49_500) * rpc <= 400


# ----------------------------------------------------------------------- MLL

def test_mll_guard_flattens_before_the_floor(mnq):
    rm = make(mnq, personal_daily_loss_limit=1_900, mll_buffer=200)
    assert rm.check_open_risk(50_000, -1_850) is None  # $150 above the floor, outside half the buffer
    rm2 = make(mnq, personal_daily_loss_limit=1_990, mll_buffer=200)
    assert "MLL" in rm2.check_open_risk(48_500, -401)


def test_xfa_mll_resets_to_zero_floor():
    t = LossLimitTracker(0, 2_000, floor=0)  # after the first payout the MLL is $0
    assert t.breached(0)
    assert t.room(500) == 500


# ----------------------------------------------------------------------- news

def test_news_size_cap_near_a_release(mnq):
    rm = make(mnq, risk_per_trade=10_000, personal_daily_loss_limit=1_900)
    rm.live_guards = True
    rm.schedule.news = calendar(NewsEvent("CPI m/m", "USD", "High", OPEN + timedelta(minutes=20)))
    assert rm.max_contracts(OPEN) == 25  # half of 50 micros
    assert rm.max_contracts(OPEN + timedelta(minutes=30)) == 50  # after the release
    assert rm.max_contracts(OPEN - timedelta(hours=1)) == 50  # far from it


def test_news_size_cap_when_calendar_is_unavailable(mnq):
    rm = make(mnq)
    rm.live_guards = True
    rm.schedule.news = None
    assert rm.max_contracts(OPEN) == 25
    rm.live_guards = False  # backtests: no calendar, no cap
    assert rm.max_contracts(OPEN) == 50


def test_max_size_position_is_closed_before_news(mnq):
    rm = make(mnq)
    rm.schedule.news = calendar(NewsEvent("Non-Farm Employment Change", "USD", "High", OPEN + timedelta(minutes=3)))
    assert "maximum position size" in rm.news_flatten_reason(OPEN, 50)
    assert rm.news_flatten_reason(OPEN, -50) is not None
    assert rm.news_flatten_reason(OPEN, 49) is None
    assert rm.news_flatten_reason(OPEN - timedelta(minutes=30), 50) is None


# ------------------------------------------------------------------ order guard

def test_order_guard_never_exceeds_the_cap():
    guard = OrderGuard(lambda: 5)
    assert guard.check_entry(5, 0, OPEN) is None
    assert "exceed" in guard.check_entry(1, 5, OPEN)
    assert "exceed" in guard.check_entry(3, -3, OPEN)
    assert guard.check_entry(0, 0, OPEN) is not None


def test_order_rate_breaker_trips_and_blocks_entries():
    guard = OrderGuard(lambda: 5, max_actions_per_minute=3)
    for i in range(3):
        assert guard.record_action(OPEN + timedelta(seconds=i)) is None
    assert "high-frequency" in guard.record_action(OPEN + timedelta(seconds=4))
    assert guard.record_action(OPEN + timedelta(seconds=5)) is None  # reported once
    assert "tripped" in guard.check_entry(1, 0, OPEN)
    guard.reset()
    assert guard.check_entry(1, 0, OPEN) is None


def test_order_rate_window_slides():
    guard = OrderGuard(lambda: 5, max_actions_per_minute=3)
    for i in range(10):
        assert guard.record_action(OPEN + timedelta(seconds=30 * i)) is None


def test_daily_entry_safety_limit():
    guard = OrderGuard(lambda: 5, max_entries_per_day=2)
    guard.record_entry(OPEN)
    guard.record_entry(OPEN)
    assert "entries already sent today" in guard.check_entry(1, 0, OPEN)
    assert guard.check_entry(1, 0, OPEN + timedelta(days=1)) is None


# ---------------------------------------------------------- account & hosting

def test_live_funded_accounts_are_refused():
    assert api_trading_block(Account(1, "50KTC-V2-1-1", 50_000, simulated=True)) is None
    assert "Live" in api_trading_block(Account(2, "LFA-1", 10_000, simulated=False))


def test_hosting_warning_detects_cloud_servers(tmp_path):
    (tmp_path / "sys_vendor").write_text("Amazon EC2\n")
    assert "cloud" in hosting_warning(tmp_path, env={})
    (tmp_path / "sys_vendor").write_text("Dell Inc.\n")
    assert hosting_warning(tmp_path, env={}) is None
    assert "Remote Desktop" in hosting_warning(tmp_path, env={"SESSIONNAME": "RDP-Tcp#3"})


# ---------------------------------------------------------------- XFA payouts

def test_xfa_payout_progress():
    assert xfa_payout_progress([200, 150, -50, 300, 160, 175]).eligible
    assert not xfa_payout_progress([200, 149, 300]).eligible
    assert xfa_payout_progress([400, 300, 300], "consistency").eligible  # best day 40%
    assert not xfa_payout_progress([450, 300, 250], "consistency").eligible  # 45%
    assert not xfa_payout_progress([300, 300], "consistency").eligible  # only 2 days


def test_session_must_end_before_topstep_flat_time():
    with pytest.raises(ValueError, match="15:10"):
        SessionConfig(flatten_at="15:09")


# ------------------------------------------------------------------ preflight

def _preflight(tmp_path, accounts=None, **cfg):
    import httpx

    from topstep_bot.api.rest import ProjectXClient
    from topstep_bot.config import Secrets
    from topstep_bot.preflight import run_preflight

    from .conftest import run
    from .test_live_runner import FakeTopstepX

    fake = FakeTopstepX()
    original = fake.handler

    def handler(request):
        if accounts is not None and request.url.path == "/api/Account/search":
            return httpx.Response(200, json={"success": True, "accounts": accounts})
        return original(request)

    config = BotConfig.model_validate({"data_dir": str(tmp_path), "news": {"enabled": False}, **cfg})
    client = ProjectXClient("u", "k", transport=httpx.MockTransport(handler))
    try:
        report = run(run_preflight(config, Secrets(username="u", api_key="k"), client=client, run_backtests=False))
    finally:
        run(client.close())
    return report, {c.name: c for c in report.checks}


def test_preflight_refuses_live_funded_accounts(tmp_path):
    report, checks = _preflight(tmp_path, [{"id": 7, "name": "LFA-50K-1", "balance": 10_000.0, "canTrade": True,
                                             "simulated": False}])
    assert report.verdict == "NOT READY"
    assert "Live" in checks["Account type"].detail


def test_preflight_reports_topstep_rules(tmp_path):
    report, checks = _preflight(tmp_path, account={"topstep_daily_loss_limit": True})
    assert "$1,000.00" in checks["Topstep Daily Loss Limit"].detail
    assert "$1,650.00" in checks["Consistency Target"].detail
    assert "50 MNQ" in checks["Position limit"].detail
    assert "Your computer" in checks


# ------------------------------------------------------ guards wired into the bot

def _core(mnq, **cfg):
    from topstep_bot.broker.paper import PaperBroker
    from topstep_bot.factory import build_core

    now = [OPEN]
    broker = PaperBroker(mnq, 50_000, slippage_ticks=0, live=True)
    core = build_core(BotConfig.model_validate(cfg), mnq, broker, clock=lambda: now[0], account_label="t")
    return core, broker, now


def test_engine_closes_a_max_size_position_before_news(mnq):
    from topstep_bot.models import OrderSide

    from .conftest import run

    core, broker, now = _core(mnq, risk={"risk_per_trade": 400, "personal_daily_loss_limit": 1_000})
    core.schedule.news = calendar(NewsEvent("CPI m/m", "USD", "High", OPEN + timedelta(minutes=2)))

    async def go():
        await core.begin_day(DAY, 50_000)
        await broker.on_price(now[0], 100.0)
        await core.orders.enter(OrderSide.BUY, 50, 99.0, None, "t", ref_price=100.0)
        await broker.drain()
        assert core.orders.position == 50
        await core.on_clock(now[0])
        await broker.drain()
        assert broker.position == 0
    run(go())


def test_engine_keeps_a_smaller_position_through_news(mnq):
    from topstep_bot.models import OrderSide

    from .conftest import run

    core, broker, now = _core(mnq)
    core.schedule.news = calendar(NewsEvent("CPI m/m", "USD", "High", OPEN + timedelta(minutes=2)))

    async def go():
        await core.begin_day(DAY, 50_000)
        await broker.on_price(now[0], 100.0)
        await core.orders.enter(OrderSide.BUY, 2, 90.0, None, "t", ref_price=100.0)
        await broker.drain()
        await core.on_clock(now[0])
        await broker.drain()
        assert broker.position == 2
    run(go())


def test_order_manager_refuses_an_entry_over_the_cap(mnq):
    from topstep_bot.models import OrderSide

    from .conftest import run

    core, broker, now = _core(mnq)

    async def go():
        await core.begin_day(DAY, 50_000)
        await broker.on_price(now[0], 100.0)
        assert await core.orders.enter(OrderSide.BUY, 51, 90.0, None, "too big", ref_price=100.0) is None
        await broker.drain()
        assert broker.position == 0 and not await broker.open_orders()
        assert any("Entry refused" in e["message"] for e in core.events)
    run(go())


def test_runaway_order_loop_halts_the_bot(mnq):
    from .conftest import run

    core, broker, now = _core(mnq)
    core.orders.guard.max_actions_per_minute = 5

    async def go():
        await core.begin_day(DAY, 50_000)
        for _ in range(6):
            core.orders._count_action()
        await core.on_clock(now[0])
        assert core.halted and "high-frequency" in core.halted
        assert core.risk.entry_block_reason(now[0], 50_000) is not None
    run(go())


def test_live_runner_refuses_a_live_funded_account(tmp_path):
    import httpx

    from topstep_bot.api.rest import ProjectXClient
    from topstep_bot.config import Secrets
    from topstep_bot.live import Controls, LiveRunner, SetupError

    from .conftest import run
    from .test_live_runner import FakeTopstepX

    fake = FakeTopstepX()
    original = fake.handler

    def handler(request):
        if request.url.path == "/api/Account/search":
            return httpx.Response(200, json={"success": True, "accounts": [
                {"id": 7, "name": "LFA-1", "balance": 10_000.0, "canTrade": True, "simulated": False}]})
        return original(request)

    cfg = BotConfig.model_validate({"mode": "live", "data_dir": str(tmp_path), "log_dir": str(tmp_path),
                                    "news": {"enabled": False}, "dashboard": {"enabled": False}})
    runner = LiveRunner(cfg, Secrets(username="u", api_key="k"), Controls())
    runner.client = ProjectXClient("u", "k", transport=httpx.MockTransport(handler))
    with pytest.raises(SetupError, match="Live Funded Accounts"):
        run(runner.prepare())
    assert not any(p == "/api/Order/place" for p, _ in fake.calls)
    run(runner.client.close())
    runner.journal.close()


def test_best_prior_day_starts_over_after_a_reset(tmp_path):
    from topstep_bot.config import Secrets
    from topstep_bot.live import LiveRunner

    from .conftest import run

    cfg = BotConfig.model_validate({"data_dir": str(tmp_path), "log_dir": str(tmp_path)})
    runner = LiveRunner(cfg, Secrets(username="u", api_key="k"))
    j = runner.journal
    j.record_day(date(2026, 3, 2), "A", 50_000, 51_400, 2, 48_000)  # big day, then the account was reset
    j.record_day(date(2026, 3, 3), "A", 50_000, 50_300, 1, 48_000)
    j.record_day(date(2026, 3, 4), "A", 50_300, 50_900, 1, 48_300)
    assert runner._best_prior_day("A", date(2026, 3, 5), 50_000) == pytest.approx(600)
    assert runner._best_prior_day("A", date(2026, 3, 3), 50_000) == pytest.approx(1_400)
    j.close()
    run(runner.client.close())


def test_engine_tracks_the_best_day(mnq):
    from .conftest import run

    core, broker, now = _core(mnq)

    async def go():
        await core.begin_day(DAY, 50_000)
        core.risk.trades_today = 1
        core.balance = 50_700
        await core.end_day()
        assert core.risk.best_prior_day == pytest.approx(700)
    run(go())


def test_setup_wizard_config_records_the_topstep_dll_and_payout_path():
    import yaml

    from topstep_bot.wizard import render_config

    cfg = BotConfig.model_validate(yaml.safe_load(render_config(plan="100K", topstep_dll=True, daily_loss=750)))
    assert cfg.account.topstep_daily_loss_limit == 2_000
    assert cfg.risk.consistency_guard and cfg.risk.stop_at_profit_target
    cfg = BotConfig.model_validate(yaml.safe_load(render_config(stage="express", payout_path="consistency")))
    assert cfg.account.topstep_daily_loss_limit is None and cfg.account.payout_path == "consistency"


def test_rules_summary_for_each_account_type():
    from topstep_bot.risk.summary import rule_rows

    combine = {r[0]: r for r in rule_rows(BotConfig.model_validate({"account": {"topstep_daily_loss_limit": True}}))}
    assert "$1,650" in combine["Consistency Target"][1] and "$1,500" in combine["Consistency Target"][2]
    assert "$1,000" in combine["Daily Loss Limit"][1] and "$900" in combine["Daily Loss Limit"][2]
    assert "50 MNQ micros" in combine["Position size"][1]
    express = {r[0]: r for r in rule_rows(BotConfig.model_validate(
        {"account": {"stage": "express", "plan": "150K"}, "instrument": {"symbol": "GC"}, "strategy": {"name": "orb"}}))}
    assert "Consistency Target" not in express and "Payouts" in express
    assert "3 GC contracts" in express["Position size"][1]


def test_rules_command_prints(tmp_path, monkeypatch, capsys):
    from topstep_bot.cli import main

    monkeypatch.chdir(tmp_path)
    assert main(["rules"]) == 0
