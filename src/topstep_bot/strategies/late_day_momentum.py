from __future__ import annotations

from datetime import date, time

from topstep_bot.indicators import ATR
from topstep_bot.models import Bar, Signal
from topstep_bot.strategies.base import Strategy, StrategyContext, parse_hhmm


class LateDayMomentum(Strategy):
    """Adapted from Gao, Han, Li & Zhou (2018), "Market intraday momentum", Journal of Financial
    Economics 129(2). On S&P 500 ETFs, the return from the previous close to the end of the first
    half hour predicts the return of the last half hour - a pattern linked to late-informed traders
    and to hedging flows into the close.

    Here: the "morning move" runs from the previous regular-session close to ``signal_end``
    (default 9:00 CT, the end of the first half hour). Near the end of the day (``entry_time``,
    default 14:25 CT) the bot enters in that direction - optionally only if the move since 14:00
    agrees, as the paper's 12th half-hour also predicts the last one - with an ATR safety stop, and
    exits at the session flatten time (15:00 CT by default, the paper's close).
    """

    name = "late_day_momentum"
    title = "Late-Day Momentum (first & last half hour)"
    description = (
        "Research-based (Gao, Han, Li & Zhou 2018, Journal of Financial Economics). The move from "
        "yesterday's close to 9:00 CT tends to continue in the session's last half hour. Enters around "
        "14:25 CT in the direction of that morning move, with an ATR safety stop, and exits at the "
        "session flatten time. At most one trade a day. Did NOT hold up on Nasdaq futures 2018-2025 in "
        "testing (the effect was found on the SPY ETF) - train it on your data before using it."
    )
    defaults = {
        "signal_end": "09:00",  # morning move = previous regular-session close -> this time (CT)
        "entry_time": "14:25",  # decide at the close of the bar ending here (before session.last_entry)
        "confirm_with_12th": False,  # also require the move since 14:00 CT to point the same way
        "min_move_pct": 0.0,  # ignore morning moves smaller than this many percent
        "stop_atr": 2.0,  # safety stop distance in ATRs (14 bars of the bar timeframe)
        "direction": "both",
    }

    def setup(self) -> None:
        self.signal_end = parse_hhmm(self.p["signal_end"])
        self.entry_time = parse_hhmm(self.p["entry_time"])
        for label, t in (("signal_end", self.signal_end), ("entry_time", self.entry_time)):
            if (t.hour * 60 + t.minute) % self.tf:
                raise ValueError(f"late_day_momentum: {label} {t:%H:%M} is not the close of a {self.tf}-minute bar")
        if not self.signal_end < self.entry_time:
            raise ValueError("late_day_momentum: signal_end must be before entry_time")
        if self.p["stop_atr"] <= 0:
            raise ValueError("late_day_momentum: stop_atr must be positive")
        self.atr = ATR(14)
        self.prev_close: float | None = None
        self.last_rth_close: float | None = None
        self.on_new_day(date.min)

    def on_new_day(self, day: date) -> None:
        if self.last_rth_close is not None:
            self.prev_close = self.last_rth_close
        self.last_rth_close = None
        self.morning_move: float | None = None
        self.close_1400: float | None = None
        self.done = False

    def on_bar(self, bar: Bar, ctx: StrategyContext) -> Signal | None:
        self.atr.update(bar.high, bar.low, bar.close)
        if not self.is_rth_bar(ctx):
            return None
        self.last_rth_close = bar.close
        t = ctx.local_close.time().replace(second=0, microsecond=0)
        if t == self.signal_end and self.prev_close:
            self.morning_move = bar.close / self.prev_close - 1.0
        if t == time(14, 0):
            self.close_1400 = bar.close
        if t != self.entry_time or self.done:
            return None
        self.done = True
        move = self.morning_move
        if move is None or ctx.position != 0 or self.atr.value is None:
            return None
        if abs(move) * 100 < self.p["min_move_pct"] or move == 0:
            return None
        long = move > 0
        if self.p["confirm_with_12th"] and (
            self.close_1400 is None or (bar.close > self.close_1400) != long or bar.close == self.close_1400
        ):
            return None
        if not self.allows("long" if long else "short"):
            return None
        distance = self.p["stop_atr"] * self.atr.value
        stop = bar.close - distance if long else bar.close + distance
        return Signal("long" if long else "short", stop, None,
                      f"morning move {move * 100:+.2f}% (previous close -> {self.signal_end:%H:%M})")

    def state(self) -> dict:
        return {"previous_close": self.prev_close,
                "morning_move_pct": None if self.morning_move is None else round(self.morning_move * 100, 3)}
