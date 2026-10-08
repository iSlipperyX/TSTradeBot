from __future__ import annotations

from collections import deque
from datetime import date

from topstep_bot.indicators import SessionVWAP
from topstep_bot.models import Bar, Signal
from topstep_bot.strategies.base import Strategy, StrategyContext


class NoiseAreaMomentum(Strategy):
    """Adapted from Zarattini, Aziz & Barbon (2024), "Beat the Market: An Effective Intraday
    Momentum Strategy for S&P500 ETF (SPY)", Swiss Finance Institute Research Paper 24-97.

    The 'noise area' around today's open is the average absolute move from the open at each
    time of day over the last ``lookback_days`` sessions. Moves inside it are treated as noise;
    a close outside it at a checkpoint signals a supply/demand imbalance worth following.
    Differences from the paper: futures instead of SPY, a hard broker stop (the paper checks
    exits only at checkpoints), and fixed-risk sizing instead of volatility targeting.
    """

    name = "noise_breakout"
    title = "Intraday Momentum (Noise Area)"
    description = (
        "Research-based trend strategy (Zarattini, Aziz & Barbon 2024). Builds a 'noise band' around "
        "the day's open from the average move at each time of day over the past 14 sessions. When "
        "price closes outside the band at a half-hour checkpoint it follows the move, trails the stop "
        "at the band or VWAP, and is always flat by the session flatten time."
    )
    defaults = {
        "lookback_days": 14,
        "band_mult": 1.0,
        "check_every_minutes": 30,
        "first_check_minutes": 30,
        "trail_with_vwap": True,
        "stop_buffer_ticks": 4,
        "max_trades_per_day": 3,
        "direction": "both",
    }

    def setup(self) -> None:
        if self.p["check_every_minutes"] % self.tf:
            raise ValueError("noise_breakout: check_every_minutes must be a multiple of the bar timeframe")
        self.history: dict[int, deque[float]] = {}
        self.today: dict[int, float] = {}
        self.vwap = SessionVWAP()
        self.prev_close: float | None = None
        self.last_close: float | None = None
        self.day_open: float | None = None
        self.upper: float | None = None
        self.lower: float | None = None
        self.trades = 0

    @property
    def warmup_days(self) -> int:
        return self.p["lookback_days"] + 1

    def on_new_day(self, day: date) -> None:
        lookback = self.p["lookback_days"]
        for minute, move in self.today.items():
            self.history.setdefault(minute, deque(maxlen=lookback)).append(move)
        if self.last_close is not None:
            self.prev_close = self.last_close
        self.today = {}
        self.vwap.reset()
        self.day_open = None
        self.upper = self.lower = None
        self.trades = 0

    def _sigma(self, minute: int) -> float | None:
        moves = self.history.get(minute)
        if not moves or len(moves) < self.p["lookback_days"]:
            return None
        return sum(moves) / len(moves)

    def on_bar(self, bar: Bar, ctx: StrategyContext) -> Signal | None:
        if not self.is_rth_bar(ctx):
            return None
        if self.day_open is None:
            self.day_open = bar.open
        self.vwap.update(bar.high, bar.low, bar.close, bar.volume)
        self.last_close = bar.close
        minute = self.minutes_since_open(ctx)
        self.today[minute] = abs(bar.close / self.day_open - 1.0)

        sigma = self._sigma(minute)
        if sigma is None:
            self.upper = self.lower = None
            return None
        ref_prev = self.prev_close if self.prev_close is not None else self.day_open
        mult = self.p["band_mult"]
        self.upper = max(self.day_open, ref_prev) * (1 + mult * sigma)
        self.lower = min(self.day_open, ref_prev) * (1 - mult * sigma)

        checkpoint = minute >= self.p["first_check_minutes"] and minute % self.p["check_every_minutes"] == 0
        if not checkpoint:
            return None
        if ctx.position == 0 and self.trades < self.p["max_trades_per_day"]:
            if bar.close > self.upper and self.allows("long"):
                self.trades += 1
                stop = self._trail_level(long=True)
                return Signal("long", stop, None, f"close {bar.close:.2f} above noise band {self.upper:.2f}")
            if bar.close < self.lower and self.allows("short"):
                self.trades += 1
                stop = self._trail_level(long=False)
                return Signal("short", stop, None, f"close {bar.close:.2f} below noise band {self.lower:.2f}")
        elif ctx.position > 0 and bar.close < self.lower:
            return Signal("exit", reason="price fell back below the lower noise band")
        elif ctx.position < 0 and bar.close > self.upper:
            return Signal("exit", reason="price rose back above the upper noise band")
        return None

    def _trail_level(self, long: bool) -> float:
        buf = self.tick(self.p["stop_buffer_ticks"])
        use_vwap = self.p["trail_with_vwap"] and self.vwap.value is not None
        if long:
            level = max(self.upper, self.vwap.value) if use_vwap else self.upper
            return level - buf
        level = min(self.lower, self.vwap.value) if use_vwap else self.lower
        return level + buf

    def trailing_stop(self, bar: Bar, ctx: StrategyContext) -> float | None:
        if self.upper is None or ctx.position == 0:
            return None
        return self._trail_level(long=ctx.position > 0)

    def state(self) -> dict:
        return {"upper_band": self.upper, "lower_band": self.lower, "vwap": self.vwap.value}
