from __future__ import annotations

from collections import deque
from datetime import date, datetime

from topstep_bot.indicators import ATR, SessionVWAP
from topstep_bot.models import Bar, Signal
from topstep_bot.strategies.base import Setup, Strategy, StrategyContext, parse_hhmm


class NoiseAreaMomentum(Strategy):
    """Adapted from Zarattini, Aziz & Barbon (2024), "Beat the Market: An Effective Intraday
    Momentum Strategy for S&P500 ETF (SPY)", Swiss Finance Institute Research Paper 24-97.

    The 'noise area' around today's open is the average absolute move from the open at each
    time of day over the last ``lookback_days`` sessions. Moves inside it are treated as noise;
    a close outside it at a checkpoint signals a supply/demand imbalance worth following.
    Differences from the paper: futures instead of SPY, a hard broker stop (the paper checks
    exits only at checkpoints), and fixed-risk sizing instead of volatility targeting.

    ``exit_mode``:
      * "checkpoint" (default) - as in the paper: the trailing exit is only judged at the half-hour
        checkpoints, while a wider safety stop (``stop_atr`` x ATR) stays at the broker the whole
        time, because Topstep accounts must never be left unprotected.
      * "trail" - the broker stop trails the band/VWAP on every bar (tight; frequent shake-outs).
    """

    name = "noise_breakout"
    title = "Intraday Momentum (Noise Area)"
    description = (
        "Research-based trend strategy (Zarattini, Aziz & Barbon 2024). Builds a 'noise band' around "
        "the day's open from the average move at each time of day over the past 14 sessions. When "
        "price closes outside the band at a half-hour checkpoint it follows the move; it exits at a later "
        "checkpoint if price closes back through the band or VWAP, and is always flat by the session "
        "flatten time. The most robust strategy in testing (Nasdaq and S&P futures, 2015-2025)."
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
        "exit_mode": "checkpoint",  # checkpoint (as in the paper) | trail
        "stop_atr": 2.0,  # exit_mode checkpoint: safety stop distance in ATRs of the bar timeframe
    }

    def setup(self) -> None:
        if self.p["check_every_minutes"] % self.tf:
            raise ValueError("noise_breakout: check_every_minutes must be a multiple of the bar timeframe")
        if self.p["exit_mode"] not in ("trail", "checkpoint"):
            raise ValueError("noise_breakout: exit_mode must be trail or checkpoint")
        self.atr = ATR(14)
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
        self.atr.update(bar.high, bar.low, bar.close)
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
        checkpoint_mode = self.p["exit_mode"] == "checkpoint"
        if ctx.position == 0 and self.trades < self.p["max_trades_per_day"]:
            for long, beyond, label in ((True, bar.close > self.upper, "above"), (False, bar.close < self.lower, "below")):
                if not beyond or not self.allows("long" if long else "short"):
                    continue
                stop = self._entry_stop(bar, long) if checkpoint_mode else self._trail_level(long=long)
                if stop is None:
                    return None
                self.trades += 1
                band = self.upper if long else self.lower
                return Signal("long" if long else "short", stop, None,
                              f"close {bar.close:.2f} {label} noise band {band:.2f}")
        elif ctx.position > 0 and bar.close < self.lower:
            return Signal("exit", reason="price fell back below the lower noise band")
        elif ctx.position < 0 and bar.close > self.upper:
            return Signal("exit", reason="price rose back above the upper noise band")
        elif checkpoint_mode and ctx.position != 0:
            level = self._trail_level(long=ctx.position > 0)
            if (ctx.position > 0 and bar.close < level) or (ctx.position < 0 and bar.close > level):
                return Signal("exit", reason="checkpoint close beyond the band/VWAP trail")
        return None

    def _entry_stop(self, bar: Bar, long: bool) -> float | None:
        """Checkpoint mode's safety stop: at least ``stop_atr`` ATRs away, never inside the trail level."""
        if self.atr.value is None:
            return None
        distance = self.p["stop_atr"] * self.atr.value
        trail = self._trail_level(long=long)
        return min(trail, bar.close - distance) if long else max(trail, bar.close + distance)

    def _trail_level(self, long: bool) -> float:
        buf = self.tick(self.p["stop_buffer_ticks"])
        use_vwap = self.p["trail_with_vwap"] and self.vwap.value is not None
        if long:
            level = max(self.upper, self.vwap.value) if use_vwap else self.upper
            return level - buf
        level = min(self.lower, self.vwap.value) if use_vwap else self.lower
        return level + buf

    def trailing_stop(self, bar: Bar, ctx: StrategyContext) -> float | None:
        if self.upper is None or ctx.position == 0 or self.p["exit_mode"] == "checkpoint":
            return None  # checkpoint mode exits by signal at checkpoints; the safety stop stays put
        return self._trail_level(long=ctx.position > 0)

    def setups(self, price: float | None, now: datetime) -> list[Setup]:
        if not self.in_rth(now) or self.upper is None or self.trades >= self.p["max_trades_per_day"]:
            return []
        minutes, first, every = self.rth_minutes(now), self.p["first_check_minutes"], self.p["check_every_minutes"]
        nxt = max(first, (minutes // every + 1) * every)
        if nxt > self.rth_minutes(now.replace(hour=self.rth_close.hour, minute=self.rth_close.minute)):
            return []
        at = self.rth_time(nxt)
        left = self.p["max_trades_per_day"] - self.trades
        out = []
        for side in ("long", "short"):
            if not self.allows(side):
                continue
            long = side == "long"
            band = self.upper if long else self.lower
            beyond = price is not None and (price > band if long else price < band)
            conds = [(f"Price {'above the upper' if long else 'below the lower'} noise band ({self.fmt(band)})", beyond),
                     (f"Still there at a checkpoint close (next {at} CT)", False)]
            stop = None
            ref = price if beyond else band  # the checkpoint close it would enter at
            if self.p["exit_mode"] == "trail":
                stop = self._trail_level(long)
            elif self.atr.value is not None:
                distance = self.p["stop_atr"] * self.atr.value
                trail = self._trail_level(long)
                stop = min(trail, ref - distance) if long else max(trail, ref + distance)
            out.append(Setup(side, conds, ref, stop, None, f"decides at the {at} CT checkpoint; {left} of {self.p['max_trades_per_day']} trades left today",
                             at=parse_hhmm(at)))
        return out

    def state(self) -> dict:
        return {"upper_band": self.upper, "lower_band": self.lower, "vwap": self.vwap.value}
