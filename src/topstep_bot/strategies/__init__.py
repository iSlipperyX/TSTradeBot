"""Bundled strategies. Add your own by subclassing Strategy and registering it in STRATEGIES."""

from __future__ import annotations

from typing import Any

from topstep_bot.instruments import get_spec
from topstep_bot.models import Contract
from topstep_bot.strategies.adaptive import AdaptiveAllDay
from topstep_bot.strategies.base import Strategy, StrategyContext, parse_hhmm
from topstep_bot.strategies.ema_trend import EmaTrend
from topstep_bot.strategies.late_day_momentum import LateDayMomentum
from topstep_bot.strategies.noise_breakout import NoiseAreaMomentum
from topstep_bot.strategies.orb import OpeningRangeBreakout
from topstep_bot.strategies.orb_momentum import OpeningRangeMomentum
from topstep_bot.strategies.vwap_pullback import VwapPullback
from topstep_bot.strategies.vwap_reversion import VwapReversion

# The adaptive strategy runs all the others, so it is listed first and they are listed in the
# order of the trading day they are built for: open, all day, midday, afternoon.
STRATEGIES: dict[str, type[Strategy]] = {
    cls.name: cls
    for cls in (
        AdaptiveAllDay,
        OpeningRangeBreakout,
        OpeningRangeMomentum,
        NoiseAreaMomentum,
        EmaTrend,
        VwapReversion,
        VwapPullback,
        LateDayMomentum,
    )
}
BASE_STRATEGIES = tuple(n for n in STRATEGIES if n != AdaptiveAllDay.name)


def create_strategy(name: str, params: dict[str, Any], contract: Contract, timeframe_minutes: int) -> Strategy:
    try:
        cls = STRATEGIES[name]
    except KeyError:
        raise ValueError(f"Unknown strategy '{name}'. Available: {', '.join(STRATEGIES)}") from None
    try:
        spec = get_spec(contract.root)
        rth_open, rth_close = parse_hhmm(spec.rth_open), parse_hhmm(spec.rth_close)
    except KeyError:
        rth_open, rth_close = parse_hhmm("08:30"), parse_hhmm("15:00")
    return cls(contract, timeframe_minutes, rth_open, rth_close, params)


__all__ = ["BASE_STRATEGIES", "STRATEGIES", "Strategy", "StrategyContext", "create_strategy"]
