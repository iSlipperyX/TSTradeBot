from __future__ import annotations

from datetime import datetime

from topstep_bot.indicators import ATR, EMA
from topstep_bot.models import Bar, Signal
from topstep_bot.strategies.base import Setup, Strategy, StrategyContext


class EmaTrend(Strategy):
    name = "ema_trend"
    title = "EMA Trend Crossover"
    description = (
        "Classic trend-following: enters when a fast EMA crosses a slow EMA in the direction of a "
        "longer 'trend' EMA. ATR-based stop, fixed reward-to-risk target, and an exit on the "
        "opposite crossover."
    )
    defaults = {
        "fast": 9,
        "slow": 21,
        "trend": 50,
        "atr_period": 14,
        "atr_stop_mult": 1.5,
        "target_r": 2.0,
        "rth_only": True,
        "direction": "both",
    }

    def setup(self) -> None:
        if not self.p["fast"] < self.p["slow"]:
            raise ValueError("ema_trend: fast must be smaller than slow")
        self.fast = EMA(self.p["fast"])
        self.slow = EMA(self.p["slow"])
        self.trend = EMA(self.p["trend"])
        self.atr = ATR(self.p["atr_period"])
        self.prev_diff: float | None = None

    def on_bar(self, bar: Bar, ctx: StrategyContext) -> Signal | None:
        fast = self.fast.update(bar.close)
        slow = self.slow.update(bar.close)
        trend = self.trend.update(bar.close)
        atr = self.atr.update(bar.high, bar.low, bar.close)
        if fast is None or slow is None or trend is None or atr is None:
            return None
        diff = fast - slow
        prev, self.prev_diff = self.prev_diff, diff
        if prev is None:
            return None
        crossed_up = prev <= 0 < diff
        crossed_down = prev >= 0 > diff
        if self.p["rth_only"] and not self.is_rth_bar(ctx):
            return None

        if ctx.position == 0:
            if crossed_up and bar.close > trend and self.allows("long"):
                stop = bar.close - self.p["atr_stop_mult"] * atr
                target = bar.close + self.p["target_r"] * (bar.close - stop)
                return Signal("long", stop, target, f"EMA {self.p['fast']}/{self.p['slow']} bullish cross above trend")
            if crossed_down and bar.close < trend and self.allows("short"):
                stop = bar.close + self.p["atr_stop_mult"] * atr
                target = bar.close - self.p["target_r"] * (stop - bar.close)
                return Signal("short", stop, target, f"EMA {self.p['fast']}/{self.p['slow']} bearish cross below trend")
        elif ctx.position > 0 and crossed_down:
            return Signal("exit", reason="bearish EMA cross")
        elif ctx.position < 0 and crossed_up:
            return Signal("exit", reason="bullish EMA cross")
        return None

    def setups(self, price: float | None, now: datetime) -> list[Setup]:
        fast, slow, trend, atr = self.fast.value, self.slow.value, self.trend.value, self.atr.value
        if None in (fast, slow, trend, atr) or price is None or (self.p["rth_only"] and not self.in_rth(now)):
            return []
        out = []
        for side in ("long", "short"):
            long = side == "long"
            if not self.allows(side) or (fast > slow if long else fast < slow):
                continue  # already crossed this way: it has to cross back before it can signal again
            word = "above" if long else "below"
            conds = [(f"Price {word} the {self.p['trend']}-bar trend EMA ({self.fmt(trend)})", price > trend if long else price < trend),
                     (f"{self.p['fast']}-EMA crosses {word} the {self.p['slow']}-EMA ({abs(slow - fast):.2f} points apart)", False)]
            stop = price - self.p["atr_stop_mult"] * atr if long else price + self.p["atr_stop_mult"] * atr
            target = price + self.p["target_r"] * (price - stop) if long else price - self.p["target_r"] * (stop - price)
            out.append(Setup(side, conds, None, stop, target, "enters on the bar that completes the cross"))
        return out

    def state(self) -> dict:
        return {"ema_fast": self.fast.value, "ema_slow": self.slow.value, "ema_trend": self.trend.value}
