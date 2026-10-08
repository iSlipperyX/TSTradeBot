from __future__ import annotations

from collections import deque
from datetime import date

from topstep_bot.indicators import ATR, EMA, SessionVWAP
from topstep_bot.models import Bar, Signal
from topstep_bot.strategies.base import Strategy, StrategyContext, parse_hhmm


class VwapPullback(Strategy):
    name = "vwap_pullback"
    title = "VWAP Trend Pullback"
    description = (
        "Trend continuation for the middle and the end of the day: when price is trending (above VWAP "
        "with a rising EMA), waits for a pullback into the VWAP band and buys the first bar that closes "
        "back above the previous bar's high (mirror image for shorts). Stop under the pullback low, "
        "fixed reward-to-risk target, and an exit if price closes through VWAP against the trade."
    )
    defaults = {
        "trend_ema": 50,
        "slope_bars": 6,  # the EMA must be higher than this many bars ago (a one-bar dip doesn't break the trend)
        "atr_period": 14,
        "band_k": 0.5,  # the pullback must reach within k std devs of VWAP
        "stop_atr_mult": 0.5,  # stop this far beyond the pullback extreme
        "target_r": 2.0,
        "min_minutes_after_open": 45,
        "entry_cutoff": "14:00",
        "max_trades_per_day": 2,
        "direction": "both",
    }

    def setup(self) -> None:
        self.vwap = SessionVWAP()
        self.ema = EMA(self.p["trend_ema"])
        self.atr = ATR(self.p["atr_period"])
        self.cutoff = parse_hhmm(self.p["entry_cutoff"])
        self.ema_hist: deque[float] = deque(maxlen=self.p["slope_bars"] + 1)
        self.prev_bar: Bar | None = None
        self.on_new_day(date.min)

    def on_new_day(self, day: date) -> None:
        self.vwap.reset()
        self.pullback_low: float | None = None  # armed long setup: lowest low of the pullback
        self.pullback_high: float | None = None  # armed short setup
        self.trades = 0
        self.prev_bar = None

    @property
    def warmup_days(self) -> int:
        return 3

    def on_bar(self, bar: Bar, ctx: StrategyContext) -> Signal | None:
        ema = self.ema.update(bar.close)
        atr = self.atr.update(bar.high, bar.low, bar.close)
        if ema is not None:
            self.ema_hist.append(ema)
        if not self.is_rth_bar(ctx):
            return None
        vwap = self.vwap.update(bar.high, bar.low, bar.close, bar.volume)
        prev_bar, self.prev_bar = self.prev_bar, bar
        if ema is None or atr is None or len(self.ema_hist) < self.ema_hist.maxlen:
            return None
        lower, upper = self.vwap.band(self.p["band_k"])
        rising, falling = ema > self.ema_hist[0], ema < self.ema_hist[0]

        if ctx.position > 0:
            return Signal("exit", reason="closed below VWAP") if bar.close < vwap else None
        if ctx.position < 0:
            return Signal("exit", reason="closed above VWAP") if bar.close > vwap else None

        # arm a setup when a pullback reaches the VWAP band while the trend holds; a close through the
        # far side of the band (or a turned EMA) cancels it
        if rising and bar.close >= lower:
            if bar.low <= upper:
                self.pullback_low = bar.low if self.pullback_low is None else min(self.pullback_low, bar.low)
        else:
            self.pullback_low = None
        if falling and bar.close <= upper:
            if bar.high >= lower:
                self.pullback_high = bar.high if self.pullback_high is None else max(self.pullback_high, bar.high)
        else:
            self.pullback_high = None

        if (self.trades >= self.p["max_trades_per_day"] or ctx.local_close.time() > self.cutoff
                or self.minutes_since_open(ctx) < self.p["min_minutes_after_open"] or prev_bar is None):
            return None
        if self.pullback_low is not None and bar.close > vwap and bar.close > prev_bar.high and self.allows("long"):
            stop = self.pullback_low - self.p["stop_atr_mult"] * atr
            target = bar.close + self.p["target_r"] * (bar.close - stop)
            self.trades += 1
            self.pullback_low = None
            return Signal("long", stop, target, f"pullback to VWAP {vwap:.2f} in an uptrend resumed at {bar.close:.2f}")
        if self.pullback_high is not None and bar.close < vwap and bar.close < prev_bar.low and self.allows("short"):
            stop = self.pullback_high + self.p["stop_atr_mult"] * atr
            target = bar.close - self.p["target_r"] * (stop - bar.close)
            self.trades += 1
            self.pullback_high = None
            return Signal("short", stop, target, f"pullback to VWAP {vwap:.2f} in a downtrend resumed at {bar.close:.2f}")
        return None

    def state(self) -> dict:
        return {"vwap": self.vwap.value, "ema_trend": self.ema.value,
                "setup": "long" if self.pullback_low is not None else ("short" if self.pullback_high is not None else None)}
