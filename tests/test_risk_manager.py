from datetime import date, timedelta

import pytest

from topstep_bot.config import AccountConfig, RiskConfig, SessionConfig
from topstep_bot.risk.manager import RiskManager
from topstep_bot.risk.topstep import PLANS, LossLimitTracker
from topstep_bot.sessions import SessionSchedule

from .conftest import ct

DAY = date(2026, 3, 3)
OPEN = ct(2026, 3, 3, 9, 0)


def make(contract, stage="combine", balance=50_000.0, **risk):
    cfg = RiskConfig(**risk)
    plan = PLANS["50K"]
    start = 0.0 if stage == "express" else 50_000.0
    rm = RiskManager(
        cfg, AccountConfig(stage=stage), plan, contract, SessionSchedule(SessionConfig()),
        LossLimitTracker(start, plan.max_loss_limit), fees_round_turn=1.22,
    )
    rm.start_day(DAY, balance)
    return rm


def test_position_size_from_risk_budget(mnq):
    rm = make(mnq, risk_per_trade=150, slippage_ticks=1)
    # 39 ticks stop + 1 slippage = 40 ticks * $0.50 = $20 + $1.22 fees = $21.22/contract
    assert rm.position_size(20_000.0, 20_000.0 - 39 * 0.25, 50_000) == 7


def test_size_capped_by_topstep_and_user_caps(mnq, es):
    assert make(mnq, risk_per_trade=10_000, personal_daily_loss_limit=10_000).position_size(100, 99, 50_000) == 50
    assert make(es, risk_per_trade=10_000, personal_daily_loss_limit=10_000).position_size(100, 99, 50_000) == 5
    assert make(mnq, risk_per_trade=10_000, personal_daily_loss_limit=10_000, max_contracts=3).position_size(100, 99, 50_000) == 3


def test_size_shrinks_near_daily_limit_and_mll(mnq):
    rm = make(mnq, risk_per_trade=150, personal_daily_loss_limit=500)
    # Down $460 today: only $40 of daily budget left -> 1 contract at $21.22
    assert rm.position_size(100, 100 - 39 * 0.25, 49_540) == 1
    rm2 = make(mnq, risk_per_trade=150, personal_daily_loss_limit=5_000, mll_buffer=200)
    rm2.tracker.floor = 49_800  # $200 room - $200 buffer = nothing left
    assert rm2.position_size(100, 99, 50_000) == 0


def test_entry_blocks(mnq):
    rm = make(mnq, max_trades_per_day=2, max_consecutive_losses=2, cooldown_minutes_after_loss=10)
    assert rm.entry_block_reason(OPEN, 50_000) is None
    assert "before trade_start" in rm.entry_block_reason(ct(2026, 3, 3, 8, 0), 50_000)
    rm.record_trade(-50, OPEN)
    assert "cooling down" in rm.entry_block_reason(OPEN + timedelta(minutes=5), 49_950)
    assert rm.entry_block_reason(OPEN + timedelta(minutes=11), 49_950) is None
    rm.record_trade(-50, OPEN + timedelta(minutes=20))
    assert "max trades" in rm.entry_block_reason(OPEN + timedelta(minutes=40), 49_900)


def test_combine_daily_profit_cap_defaults_to_40pct_of_target(mnq):
    rm = make(mnq)
    assert rm.daily_profit_target == pytest.approx(1_200)
    assert "profit target" in rm.entry_block_reason(OPEN, 51_250)
    assert rm.lock_reason is not None  # locked for the rest of the day
    assert make(mnq, stage="express").daily_profit_target is None


def test_open_risk_triggers_flatten(mnq):
    rm = make(mnq, personal_daily_loss_limit=500, mll_buffer=200)
    assert rm.check_open_risk(50_000, -300) is None
    assert "daily loss" in rm.check_open_risk(50_000, -500)
    rm2 = make(mnq, personal_daily_loss_limit=5_000, mll_buffer=200)
    assert "MLL" in rm2.check_open_risk(48_150, -60)  # equity 48,090 is $90 above the 48,000 floor
