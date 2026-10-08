from __future__ import annotations

from datetime import date

from topstep_bot.indicators import ATR, RSI, SessionVWAP
from topstep_bot.models import Bar, Signal
from topstep_bot.strategies.base import Strategy, StrategyContext, parse_hhmm


class VwapReversion(Strategy):
    name = "vwap_reversion"
    title = "VWAP Mean Reversion"
    description = (
        "Fades stretched moves: buys when price closes well below the session VWAP band with an "
        "oversold RSI and a bullish reversal bar (and the mirror image for shorts). Targets a return "
        "to VWAP with a stop just beyond the extreme."
    )
    defaults = {
        "band_k": 2.0,
        "rsi_period": 14,
        "rsi_low": 30,
        "rsi_high": 70,
        "atr_period": 14,
        "stop_atr_mult": 1.0,
        "min_minutes_after_open": 30,
        "entry_cutoff": "13:30",
        "min_target_ticks": 8,
        "direction": "both",
    }

    def setup(self) -> None:
        self.vwap = SessionVWAP()
        self.rsi = RSI(self.p["rsi_period"])
        self.atr = ATR(self.p["atr_period"])
        self.cutoff = parse_hhmm(self.p["entry_cutoff"])

    def on_new_day(self, day: date) -> None:
        self.vwap.reset()

    def on_bar(self, bar: Bar, ctx: StrategyContext) -> Signal | None:
        rsi = self.rsi.update(bar.close)
        atr = self.atr.update(bar.high, bar.low, bar.close)
        if not self.is_rth_bar(ctx):
            return None
        vwap = self.vwap.update(bar.high, bar.low, bar.close, bar.volume)
        if rsi is None or atr is None or self.minutes_since_open(ctx) < self.p["min_minutes_after_open"]:
            return None
        lower, upper = self.vwap.band(self.p["band_k"])
        min_target = self.tick(self.p["min_target_ticks"])

        if ctx.position == 0 and ctx.local_close.time() <= self.cutoff:
            bullish_bar = bar.close > bar.open
            bearish_bar = bar.close < bar.open
            if bar.close < lower and rsi < self.p["rsi_low"] and bullish_bar and self.allows("long"):
                if vwap - bar.close >= min_target:
                    stop = bar.low - self.p["stop_atr_mult"] * atr
                    return Signal("long", stop, vwap, f"stretched {bar.close:.2f} below VWAP band, RSI {rsi:.0f}")
            if bar.close > upper and rsi > self.p["rsi_high"] and bearish_bar and self.allows("short"):
                if bar.close - vwap >= min_target:
                    stop = bar.high + self.p["stop_atr_mult"] * atr
                    return Signal("short", stop, vwap, f"stretched {bar.close:.2f} above VWAP band, RSI {rsi:.0f}")
        elif ctx.position > 0 and bar.close >= vwap:
            return Signal("exit", reason="back at VWAP")
        elif ctx.position < 0 and bar.close <= vwap:
            return Signal("exit", reason="back at VWAP")
        return None

    def state(self) -> dict:
        return {"vwap": self.vwap.value, "rsi": self.rsi.value}
