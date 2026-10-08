from __future__ import annotations

from datetime import date, datetime, timedelta

from topstep_bot.indicators import ATR
from topstep_bot.models import Bar, Signal
from topstep_bot.strategies.base import Strategy, StrategyContext, parse_hhmm


class OpeningRangeBreakout(Strategy):
    name = "orb"
    title = "Opening Range Breakout"
    description = (
        "Marks the high and low of the first minutes after the 8:30 CT open, then trades the first "
        "bar that closes beyond that range. Stop at the middle (or other side) of the range, target "
        "at a multiple of the risk. One trade a day by default (max_trades_per_day), never twice in the "
        "same direction."
    )
    defaults = {
        "range_minutes": 15,
        "entry_cutoff": "11:00",
        "stop_mode": "middle",  # middle | opposite | atr
        "atr_period": 14,
        "atr_stop_mult": 1.0,
        "target_r": 2.0,
        "buffer_ticks": 2,
        "min_range_ticks": 8,
        "max_range_ticks": 400,
        "max_trades_per_day": 1,
        "direction": "both",
    }

    def setup(self) -> None:
        if self.p["range_minutes"] % self.tf:
            raise ValueError("orb: range_minutes must be a multiple of the bar timeframe")
        if self.p["stop_mode"] not in ("middle", "opposite", "atr"):
            raise ValueError("orb: stop_mode must be middle, opposite or atr")
        self.cutoff = parse_hhmm(self.p["entry_cutoff"])
        self.atr = ATR(self.p["atr_period"])
        self.on_new_day(date.min)

    def on_new_day(self, day: date) -> None:
        self.high: float | None = None
        self.low: float | None = None
        self.range_ready = False
        self.range_valid = False
        self.trades = 0
        self.traded: set[str] = set()

    def _range_end(self, ctx: StrategyContext) -> datetime:
        open_dt = ctx.local_close.replace(hour=self.rth_open.hour, minute=self.rth_open.minute, second=0, microsecond=0)
        return open_dt + timedelta(minutes=self.p["range_minutes"])

    def on_bar(self, bar: Bar, ctx: StrategyContext) -> Signal | None:
        self.atr.update(bar.high, bar.low, bar.close)
        if not self.is_rth_bar(ctx):
            return None
        range_end = self._range_end(ctx)
        if ctx.local_close <= range_end and not self.range_ready:
            self.high = bar.high if self.high is None else max(self.high, bar.high)
            self.low = bar.low if self.low is None else min(self.low, bar.low)
            if ctx.local_close == range_end:
                self.range_ready = True
                width = self.contract.ticks(self.high - self.low)
                self.range_valid = self.p["min_range_ticks"] <= width <= self.p["max_range_ticks"]
            return None
        if not (self.range_ready and self.range_valid) or ctx.position != 0:
            return None
        if self.trades >= self.p["max_trades_per_day"] or ctx.local_close.time() > self.cutoff:
            return None

        buf = self.tick(self.p["buffer_ticks"])
        mid = (self.high + self.low) / 2
        if bar.close > self.high + buf and self.allows("long") and "long" not in self.traded:
            stop = {"middle": mid, "opposite": self.low - buf}.get(self.p["stop_mode"])
            if stop is None:
                if self.atr.value is None:
                    return None
                stop = bar.close - self.p["atr_stop_mult"] * self.atr.value
            target = bar.close + self.p["target_r"] * (bar.close - stop)
            self.trades += 1
            self.traded.add("long")
            return Signal("long", stop, target, f"close {bar.close} broke opening-range high {self.high}")
        if bar.close < self.low - buf and self.allows("short") and "short" not in self.traded:
            stop = {"middle": mid, "opposite": self.high + buf}.get(self.p["stop_mode"])
            if stop is None:
                if self.atr.value is None:
                    return None
                stop = bar.close + self.p["atr_stop_mult"] * self.atr.value
            target = bar.close - self.p["target_r"] * (stop - bar.close)
            self.trades += 1
            self.traded.add("short")
            return Signal("short", stop, target, f"close {bar.close} broke opening-range low {self.low}")
        return None

    def state(self) -> dict:
        return {"range_high": self.high, "range_low": self.low, "range_ready": self.range_ready}
