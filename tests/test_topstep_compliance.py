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
