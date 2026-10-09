from datetime import date, time, timedelta

import pytest

from topstep_bot.config import BotConfig, SessionConfig
from topstep_bot.risk.topstep import PLANS, LossLimitTracker, max_minis_allowed
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


def test_mll_room_uses_equity():
    t = LossLimitTracker(50_000, 2_000)
    assert t.room(48_000) == 0  # touching the floor ends the account
    assert t.room(49_000) == 1_000


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


def test_session_rules_across_dst_changes(schedule):
    from datetime import datetime, timezone

    utc = timezone.utc
    # Clocks go forward on Sunday 2026-03-08 and back on Sunday 2026-11-01: the session still opens at
    # 17:00 CT, and the flatten deadline stays at 15:00 Chicago time (a different hour in UTC).
    for sunday, monday in ((date(2026, 3, 8), date(2026, 3, 9)), (date(2026, 11, 1), date(2026, 11, 2))):
        assert schedule.trading_day(ct(sunday.year, sunday.month, sunday.day, 17, 0)) == monday
        assert not schedule.market_open(ct(sunday.year, sunday.month, sunday.day, 16, 59))
        assert schedule.market_open(ct(sunday.year, sunday.month, sunday.day, 17, 0))
        assert schedule.trading_day(ct(monday.year, monday.month, monday.day, 17, 0)) == monday + timedelta(days=1)
        assert schedule.entry_block_reason(ct(monday.year, monday.month, monday.day, 8, 30)) is None
        assert schedule.entry_block_reason(ct(monday.year, monday.month, monday.day, 8, 29)) is not None
        assert schedule.must_be_flat(ct(monday.year, monday.month, monday.day, 15, 0))
        assert not schedule.must_be_flat(ct(monday.year, monday.month, monday.day, 14, 59))
    assert schedule.flatten_time(date(2026, 3, 6)) == datetime(2026, 3, 6, 21, 0, tzinfo=utc)  # CST
    assert schedule.flatten_time(date(2026, 3, 9)) == datetime(2026, 3, 9, 20, 0, tzinfo=utc)  # CDT
    assert schedule.flatten_time(date(2026, 10, 30)) == datetime(2026, 10, 30, 20, 0, tzinfo=utc)
    assert schedule.flatten_time(date(2026, 11, 2)) == datetime(2026, 11, 2, 21, 0, tzinfo=utc)


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


def test_market_clock_counts_down_to_the_next_open_and_close():
    from topstep_bot.config import SessionConfig
    from topstep_bot.sessions import SessionSchedule

    s = SessionSchedule(SessionConfig(no_trade_dates=["2026-11-26"]))

    def events(*t):
        c = s.clock(ct(*t))
        return c, {e["key"]: (e["label"], e["local"]) for e in c["events"]}

    c, e = events(2026, 10, 8, 9, 0)  # Thursday morning
    assert c["market_open"] and c["in_entry_window"] and c["holiday"] is None
    assert e["market"] == ("Market closes", "16:00 CT") and e["rth"] == ("Regular hours close", "15:00 CT")
    assert e["entries"] == ("Last new entry", "14:30 CT") and e["flatten"] == ("Bot closes all trades", "15:00 CT")
    assert e["topstep"] == ("Topstep flat-by", "15:10 CT")
    c, e = events(2026, 10, 8, 16, 30)  # the daily break
    assert not c["market_open"] and e["market"] == ("Market opens", "17:00 CT")
    assert e["entries"] == ("Bot starts trading", "Fri 08:30 CT")
    c, e = events(2026, 10, 9, 16, 30)  # Friday after the close: the weekend
    assert e["market"] == ("Market opens", "Sun 17:00 CT") and e["entries"][1] == "Mon 08:30 CT"
    c, e = events(2026, 11, 26, 10, 0)  # a no-trade date (holiday)
    assert c["holiday"] and not c["in_entry_window"] and e["entries"] == ("Bot starts trading", "Fri 08:30 CT")
    oil = {e["key"]: e for e in s.clock(ct(2026, 10, 8, 9, 0), (time(8, 0), time(13, 30)), "CL")["events"]}
    assert oil["rth"]["local"] == "13:30 CT" and "CL regular hours 08:00-13:30" in oil["rth"]["detail"]
    first = s.clock(ct(2026, 10, 8, 9, 0))["events"][0]["at"]
    assert first.endswith("+00:00") and first.startswith("2026-10-08T21:00")  # 16:00 CT in UTC
