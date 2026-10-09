from __future__ import annotations

from datetime import date, datetime

from topstep_bot.indicators import ATR, RSI, SessionVWAP
from topstep_bot.models import Bar, Signal
from topstep_bot.strategies.base import Setup, Strategy, StrategyContext, parse_hhmm


class VwapReversion(Strategy):
    name = "vwap_reversion"
    title = "VWAP Mean Reversion"
    description = (
        "Fades stretched moves: buys when price closes well below the session VWAP band with an "
        "oversold RSI and a bullish reversal bar (and the mirror image for shorts). Targets a return "
        "to VWAP with a stop just beyond the extreme. Lost money in every year of testing on Nasdaq "
        "futures 2018-2025 - not recommended."
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
            stretched_low = bar.close < lower and rsi < self.p["rsi_low"] and vwap - bar.close >= min_target
            stretched_high = bar.close > upper and rsi > self.p["rsi_high"] and bar.close - vwap >= min_target
            if stretched_low and bullish_bar and self.allows("long"):
                stop = bar.low - self.p["stop_atr_mult"] * atr
                return Signal("long", stop, vwap, f"stretched {bar.close:.2f} below VWAP band, RSI {rsi:.0f}")
            if stretched_high and bearish_bar and self.allows("short"):
                stop = bar.high + self.p["stop_atr_mult"] * atr
                return Signal("short", stop, vwap, f"stretched {bar.close:.2f} above VWAP band, RSI {rsi:.0f}")
        elif ctx.position > 0 and bar.close >= vwap or ctx.position < 0 and bar.close <= vwap:
            return Signal("exit", reason="back at VWAP")
        return None

    def setups(self, price: float | None, now: datetime) -> list[Setup]:
        rsi, atr, vwap = self.rsi.value, self.atr.value, self.vwap.value
        if None in (rsi, atr, vwap) or price is None or not self.in_rth(now) or now.time() > self.cutoff:
            return []
        lower, upper = self.vwap.band(self.p["band_k"])
        min_target = self.tick(self.p["min_target_ticks"])
        early = self.rth_minutes(now) < self.p["min_minutes_after_open"]
        out = []
        for side in ("long", "short"):
            if not self.allows(side):
                continue
            long = side == "long"
            band = lower if long else upper
            beyond = price < band if long else price > band
            ref = price if beyond else band  # where it would enter
            conds = [(f"Price {'below the lower' if long else 'above the upper'} VWAP band ({self.fmt(band)})", beyond),
                     (f"RSI {'below ' + str(self.p['rsi_low']) if long else 'above ' + str(self.p['rsi_high'])} (now {rsi:.0f})",
                      rsi < self.p["rsi_low"] if long else rsi > self.p["rsi_high"]),
                     (f"At least {self.p['min_target_ticks']} ticks from VWAP ({self.fmt(vwap)})", (vwap - price if long else price - vwap) >= min_target),
                     (f"A {'bullish' if long else 'bearish'} reversal bar", False)]
            if early:
                conds.append((f"{self.p['min_minutes_after_open']} minutes after the open", False))
            if band == vwap:
                continue  # no spread around VWAP yet: the bands aren't meaningful
            stop = ref - self.p["stop_atr_mult"] * atr if long else ref + self.p["stop_atr_mult"] * atr
            out.append(Setup(side, conds, ref, stop, vwap, "targets a return to VWAP"))
        return out

    def state(self) -> dict:
        return {"vwap": self.vwap.value, "rsi": self.rsi.value}
