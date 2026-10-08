from datetime import date, time

import pytest

from topstep_bot.config import BotConfig, SessionConfig
from topstep_bot.risk.topstep import PLANS, LossLimitTracker, consistency, max_minis_allowed
from topstep_bot.sessions import SessionSchedule

from .conftest import ct


# ---- Maximum Loss Limit (examples from Topstep's help center)

def test_combine_mll_trails_end_of_day_high_and_never_drops():
    t = LossLimitTracker(50_000, 2_000)
    assert t.floor == 48_000
    t.end_of_day(50_500)
    assert t.floor == 48_500
    t.end_of_day(50_000)  # losing day: floor stays
    assert t.floor == 48_500


def test_combine_mll_locks_at_starting_balance():
    t = LossLimitTracker(50_000, 2_000)
    t.end_of_day(53_000)
    assert t.floor == 50_000
    t.end_of_day(56_000)
    assert t.floor == 50_000


def test_express_mll_starts_negative_and_locks_at_zero():
    t = LossLimitTracker(0, 2_000)
    assert t.floor == -2_000
    t.end_of_day(500)
    assert t.floor == -1_500
    t.end_of_day(2_000)
    assert t.floor == 0
    t.end_of_day(10_000)
    assert t.floor == 0


def test_mll_breach_is_inclusive_and_uses_equity():
    t = LossLimitTracker(50_000, 2_000)
    assert t.breached(48_000)
    assert not t.breached(48_000.01)
    assert t.room(49_000) == 1_000


def test_consistency_rule():
    assert consistency([500, 400, 300]).ok  # best 500 < 50% of 1200
    status = consistency([1_600, 200, 200])
    assert not status.ok
    assert status.required_total == 3_200


def test_contract_caps():
    assert max_minis_allowed(PLANS["50K"], "combine", 50_000) == 5
    assert max_minis_allowed(PLANS["150K"], "combine", 150_000) == 15
    assert max_minis_allowed(PLANS["50K"], "express", 0) == 2
    assert max_minis_allowed(PLANS["50K"], "express", 1_600) == 3
    assert max_minis_allowed(PLANS["50K"], "express", 2_500) == 5


# ---- sessions

@pytest.fixture
def schedule():
    return SessionSchedule(SessionConfig(no_trade_dates=[date(2026, 12, 25)]))


def test_trading_day_rolls_at_5pm_and_skips_weekend(schedule):
    assert schedule.trading_day(ct(2026, 3, 3, 16, 59)) == date(2026, 3, 3)
    assert schedule.trading_day(ct(2026, 3, 3, 17, 0)) == date(2026, 3, 4)
    assert schedule.trading_day(ct(2026, 3, 6, 18, 0)) == date(2026, 3, 9)  # Friday evening -> Monday
    assert schedule.trading_day(ct(2026, 3, 8, 17, 30)) == date(2026, 3, 9)  # Sunday open -> Monday


def test_entry_window_and_flatten(schedule):
    assert schedule.entry_block_reason(ct(2026, 3, 3, 8, 0)) is not None
    assert schedule.entry_block_reason(ct(2026, 3, 3, 9, 0)) is None
    assert schedule.entry_block_reason(ct(2026, 3, 3, 14, 45)) is not None
    assert not schedule.must_be_flat(ct(2026, 3, 3, 14, 59))
    assert schedule.must_be_flat(ct(2026, 3, 3, 15, 0))
    assert schedule.must_be_flat(ct(2026, 3, 3, 16, 30))
    assert not schedule.must_be_flat(ct(2026, 3, 3, 17, 5))


def test_holidays_and_blackouts():
    cfg = SessionConfig(
        no_trade_dates=[date(2026, 12, 24)],
        blackout_windows=[{"start": "07:25", "end": "07:35", "label": "CPI"}],
        trade_start=time(7, 0),
    )
    s = SessionSchedule(cfg)
    assert "not a configured trading day" in s.entry_block_reason(ct(2026, 12, 24, 9, 0))
    assert s.entry_block_reason(ct(2026, 3, 3, 7, 30)) == "CPI"
    assert s.entry_block_reason(ct(2026, 3, 3, 7, 40)) is None


def test_flatten_time_after_topstep_deadline_is_rejected():
    with pytest.raises(ValueError):
        BotConfig.model_validate({"session": {"flatten_at": "15:09"}})


def test_unknown_config_keys_are_rejected():
    with pytest.raises(ValueError):
        BotConfig.model_validate({"risk": {"risk_per_trad": 100}})
