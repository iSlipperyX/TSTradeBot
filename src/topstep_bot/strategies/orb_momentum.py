from __future__ import annotations

from collections import deque
from datetime import date, datetime, timedelta

from topstep_bot.models import Bar, Signal
from topstep_bot.strategies.base import Setup, Strategy, StrategyContext, parse_hhmm


class OpeningRangeMomentum(Strategy):
    """Adapted from Zarattini & Aziz (2023), "Can Day Trading Really Be Profitable? Evidence of
    Sustainable Long-term Profits from Opening Range Breakout (ORB) Day Trading Strategy vs. Benchmark
    in the US Stock Market" (SSRN 4416622), which traded QQQ.

    The first few minutes after the open reveal who is in control. If the opening candle closes up,
    buy right away (short if it closes down; skip a doji). By default the stop is 10% of the 14-day
    average daily range and the trade is held to the session's end, as in the authors' 2024
    follow-up ("A Profitable Day Trading Strategy for The U.S. Equity Market"); the 2023 paper's
    version (stop at the other end of the candle, 10R target) is ``stop_mode: range, target_r: 10``.
    Differences from the paper: futures instead of QQQ, fixed-dollar risk sizing, and Topstep's
    flat-by-15:10 rule (the bot exits at session.flatten_at).
    """

    name = "orb_momentum"
    title = "Opening Range Momentum (5-min ORB)"
    description = (
        "Research-based (Zarattini & Aziz 2023/2024). Trades the direction of the first 5-minute candle after "
        "the 8:30 CT open: up candle -> long, down candle -> short, doji -> no trade. Stop at 10% of the "
        "average daily range, held until the session flatten time. One trade a day. Strong on Nasdaq "
        "futures since 2018 in testing, but it lost money on S&P futures and is sensitive to slippage."
    )
    defaults = {
        "range_minutes": 5,
        "stop_mode": "atr",  # atr (the 2024 follow-up) | range (the 2023 paper: the other end of the candle)
        "atr_days": 14,
        "atr_stop_frac": 0.10,  # stop_mode atr: stop distance = this x the average daily range
        "target_r": 0,  # 0 = no target: hold until the session flatten time (the 2023 paper used 10)
        "min_body_ticks": 1,  # opening candles with a smaller body are dojis: no trade
        "buffer_ticks": 0,
        "direction": "both",
    }

    def setup(self) -> None:
        if self.p["range_minutes"] % self.tf:
            raise ValueError("orb_momentum: range_minutes must be a multiple of the bar timeframe")
        if self.p["stop_mode"] not in ("range", "atr"):
            raise ValueError("orb_momentum: stop_mode must be range or atr")
        if not 0 < self.p["atr_stop_frac"] <= 1:
            raise ValueError("orb_momentum: atr_stop_frac must be between 0 and 1")
        self.daily_ranges: deque[float] = deque(maxlen=self.p["atr_days"])
        self.prev_close: float | None = None
        self.day_high = self.day_low = self.day_close = None
        self.on_new_day(date.min)

    @property
    def warmup_days(self) -> int:
        return self.p["atr_days"] + 1 if self.p["stop_mode"] == "atr" else 1

    @property
    def daily_atr(self) -> float | None:
        if len(self.daily_ranges) < self.p["atr_days"]:
            return None
        return sum(self.daily_ranges) / len(self.daily_ranges)

    def on_new_day(self, day: date) -> None:
        if getattr(self, "day_high", None) is not None:  # close out yesterday's regular session
            tr = self.day_high - self.day_low
            if self.prev_close is not None:
                tr = max(tr, abs(self.day_high - self.prev_close), abs(self.day_low - self.prev_close))
            self.daily_ranges.append(tr)
            self.prev_close = self.day_close
        self.day_high = self.day_low = self.day_close = None
        self.range_open: float | None = None
        self.range_high: float | None = None
        self.range_low: float | None = None
        self.decided = False

    def on_bar(self, bar: Bar, ctx: StrategyContext) -> Signal | None:
        if not self.is_rth_bar(ctx):
            return None
        self.day_high = bar.high if self.day_high is None else max(self.day_high, bar.high)
        self.day_low = bar.low if self.day_low is None else min(self.day_low, bar.low)
        self.day_close = bar.close
        if self.decided:
            return None
        open_dt = ctx.local_close.replace(hour=self.rth_open.hour, minute=self.rth_open.minute, second=0, microsecond=0)
        range_end = open_dt + timedelta(minutes=self.p["range_minutes"])
        if ctx.local_close > range_end:
            self.decided = True  # started late (e.g. restart after the open): no trade today
            return None
        if self.range_open is None:
            self.range_open = bar.open
        self.range_high = bar.high if self.range_high is None else max(self.range_high, bar.high)
        self.range_low = bar.low if self.range_low is None else min(self.range_low, bar.low)
        if ctx.local_close < range_end:
            return None

        self.decided = True
        if ctx.position != 0:
            return None
        body = bar.close - self.range_open
        if abs(body) < self.tick(self.p["min_body_ticks"]):
            return None
        long = body > 0
        if not self.allows("long" if long else "short"):
            return None
        buf = self.tick(self.p["buffer_ticks"])
        if self.p["stop_mode"] == "range":
            stop = self.range_low - buf if long else self.range_high + buf
        else:
            atr = self.daily_atr
            if atr is None:
                return None
            distance = self.p["atr_stop_frac"] * atr
            stop = bar.close - distance if long else bar.close + distance
        risk = abs(bar.close - stop)
        target = None
        if self.p["target_r"] > 0:
            target = bar.close + self.p["target_r"] * risk if long else bar.close - self.p["target_r"] * risk
        candle = "up" if long else "down"
        return Signal("long" if long else "short", stop, target,
                      f"opening {self.p['range_minutes']}-min candle closed {candle} ({self.range_open} -> {bar.close})")

    def setups(self, price: float | None, now: datetime) -> list[Setup]:
        minutes = self.rth_minutes(now)
        end = self.p["range_minutes"]
        if self.decided or minutes < -60 or minutes >= end:
            return []
        decide_at = self.rth_time(end)
        out = []
        for side in ("long", "short"):
            if not self.allows(side):
                continue
            long = side == "long"
            started = self.range_open is not None and price is not None
            moving = started and (price > self.range_open if long else price < self.range_open)
            conds = [(f"Regular hours open ({self.rth_open:%H:%M} CT)", minutes >= 0),
                     (f"Opening {end}-min candle closes {'up' if long else 'down'}"
                      + (f" (opened {self.fmt(self.range_open)})" if self.range_open is not None else ""), moving)]
            stop = None
            ref = price
            if ref is not None:
                if self.p["stop_mode"] == "range" and self.range_low is not None:
                    stop = self.range_low if long else self.range_high
                elif self.p["stop_mode"] == "atr" and self.daily_atr is not None:
                    d = self.p["atr_stop_frac"] * self.daily_atr
                    stop = ref - d if long else ref + d
            target = None
            if stop is not None and ref is not None and self.p["target_r"] > 0:
                risk = abs(ref - stop)
                target = ref + self.p["target_r"] * risk if long else ref - self.p["target_r"] * risk
            out.append(Setup(side, conds, None, stop, target, f"decides at the {decide_at} CT close", at=parse_hhmm(decide_at)))
        return out

    def state(self) -> dict:
        return {"opening_high": self.range_high, "opening_low": self.range_low, "daily_atr": self.daily_atr}
