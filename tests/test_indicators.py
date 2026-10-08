import math

import pytest

from topstep_bot.bars import resample
from topstep_bot.indicators import ATR, EMA, RSI, SMA, RollingExtremes, SessionVWAP
from topstep_bot.models import Bar

from .conftest import ct


def test_ema_seeds_with_sma_then_smooths():
    ema = EMA(3)
    assert ema.update(1) is None
    assert ema.update(2) is None
    assert ema.update(3) == pytest.approx(2.0)
    assert ema.update(4) == pytest.approx(0.5 * 4 + 0.5 * 2.0)


def test_sma_window():
    sma = SMA(2)
    sma.update(1)
    assert sma.value is None
    assert sma.update(3) == 2
    assert sma.update(5) == 4


def test_atr_wilder():
    atr = ATR(2)
    atr.update(10, 8, 9)  # TR 2
    assert atr.update(11, 9, 10) == pytest.approx(2.0)  # TR max(2, 2, 0)=2
    assert atr.update(14, 10, 13) == pytest.approx((2 * 1 + 4) / 2)  # TR 4


def test_rsi_extremes():
    up = RSI(3)
    for x in range(1, 8):
        up.update(x)
    assert up.value == 100.0
    down = RSI(3)
    for x in range(8, 0, -1):
        down.update(x)
    assert down.value == pytest.approx(0.0)


def test_vwap_and_bands():
    vwap = SessionVWAP()
    vwap.update(10, 10, 10, 100)
    vwap.update(20, 20, 20, 100)
    assert vwap.value == pytest.approx(15)
    assert vwap.stdev == pytest.approx(5)
    assert vwap.band(1) == (pytest.approx(10), pytest.approx(20))
    vwap.reset()
    assert vwap.value is None


def test_rolling_extremes():
    r = RollingExtremes(2)
    r.update(5, 1)
    r.update(7, 3)
    r.update(6, 4)
    assert (r.highest, r.lowest) == (7, 3)


def test_resample_to_five_minutes():
    start = ct(2026, 3, 2, 8, 30)
    from datetime import timedelta

    ones = [Bar(start + timedelta(minutes=i), 100 + i, 101 + i, 99 + i, 100.5 + i, 10) for i in range(10)]
    fives = list(resample(ones, 5))
    assert len(fives) == 2
    assert fives[0].open == 100 and fives[0].close == 104.5
    assert fives[0].high == 105 and fives[0].low == 99
    assert fives[0].volume == 50
    assert math.isclose((fives[1].ts - fives[0].ts).total_seconds(), 300)
