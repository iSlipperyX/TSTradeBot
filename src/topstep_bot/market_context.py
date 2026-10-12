"""What the market looked like when a signal fired: a small snapshot kept with every observation.

The knowledge base decides with only two facts about the market (time-of-day slot and calm /
volatile). To find out later which conditions a strategy really works in, the bot also saves a
handful of plain measurements with every signal, trade and idea - taken or not. They change no
trading decision; they are the raw material for the "What the bot learned" report (insights.py)
and for future, evidence-tested filters.

Distances are measured in *average day ranges* (the mean regular-hours high-low of the last
10 days), so the numbers mean the same thing on a quiet day and a wild one, and on any symbol.

Only regular-hours bars build the day's open, range and VWAP: the overnight session is thin
and would make every morning look the same.
"""

from __future__ import annotations

from collections import deque
from datetime import date, datetime, time

from topstep_bot.indicators import EMA
from topstep_bot.models import Bar

# key -> (name, what it measures). Keys are short because thousands of snapshots are stored.
FEATURES: dict[str, tuple[str, str]] = {
    "vol": ("Volatility", "the 14-bar range against its normal level (1.0 = normal, 1.2 or more = volatile)"),
    "min_open": ("Time since the open", "minutes since regular hours opened"),
    "gap": ("Opening gap", "today's open against yesterday's close, in average day ranges"),
    "move": ("Move since the open", "price against today's open, in average day ranges"),
    "range_used": ("Range used", "today's high-low so far against an average day's range"),
    "range_pos": ("Place in today's range", "0 = at the day's low, 1 = at its high"),
    "vwap": ("Distance from VWAP", "price against today's VWAP, in average day ranges"),
    "trend": ("Trend", "price against its 50-bar average, in average day ranges"),
    "volume": ("Volume", "the bar's volume against its normal level"),
    "dow": ("Day of week", "0 = Monday ... 4 = Friday"),
}
# Features with a direction: the report turns them to "with the trade" (a long above VWAP and a
# short below it are the same situation), see orient().
SIGNED = frozenset({"gap", "move", "vwap", "trend"})
MIN_DAYS = 3  # completed days needed before day-range units mean anything


def orient(ctx: dict | None, side: str) -> dict[str, float]:
    """A snapshot seen from the trade's side (LONG / SHORT): "with the trade" is positive for longs and shorts alike."""
    if not ctx:
        return {}
    if side != "SHORT":
        return dict(ctx)
    out = {}
    for key, v in ctx.items():
        if key in SIGNED:
            v = -v
        elif key == "range_pos":
            v = round(1.0 - v, 3)
        out[key] = v
    return out


def describe(ctx: dict | None, side: str | None = None) -> list[str]:
    """The snapshot in plain words, e.g. ["volatile (1.4x a normal range)", "gapped up ...", ...].

    With ``side`` the direction words say whether the market was moving with or against the trade."""
    if not ctx:
        return []
    out: list[str] = []
    vol = ctx.get("vol")
    if vol is not None:
        out.append(f"{'volatile' if vol >= 1.2 else 'quiet' if vol < 0.8 else 'normal volatility'} ({vol:.1f}x a normal range)")
    minutes = ctx.get("min_open")
    if minutes is not None:
        out.append(f"{_minutes(minutes)} after the open" if minutes >= 0 else f"{_minutes(-minutes)} before the open")
    gap = ctx.get("gap")
    if gap is not None and abs(gap) >= 0.1:
        out.append(f"opened with a gap {'up' if gap > 0 else 'down'} of {abs(gap):.1f} of an average day's range")
    move = ctx.get("move")
    if move is not None:
        out.append(f"price {'up' if move > 0 else 'down'} {abs(move):.2f} day-ranges since the open" if abs(move) >= 0.05
                   else "price about where it opened")
    vwap = ctx.get("vwap")
    if vwap is not None:
        out.append("price at VWAP" if abs(vwap) < 0.03 else f"price {'above' if vwap > 0 else 'below'} VWAP")
    trend = ctx.get("trend")
    if trend is not None and abs(trend) >= 0.05:
        out.append(f"{'above' if trend > 0 else 'below'} its 50-bar average ({'uptrend' if trend > 0 else 'downtrend'})")
    pos = ctx.get("range_pos")
    if pos is not None:
        out.append("near the day's high" if pos >= 0.8 else "near the day's low" if pos <= 0.2 else "mid-range for the day")
    used = ctx.get("range_used")
    if used is not None:
        out.append(f"{round(100 * used)}% of an average day's range already used")
    volume = ctx.get("volume")
    if volume is not None:
        out.append(f"{'heavy' if volume >= 1.5 else 'light' if volume < 0.6 else 'normal'} volume ({volume:.1f}x)")
    if side in ("LONG", "SHORT") and trend is not None and abs(trend) >= 0.05:
        with_trend = (trend > 0) == (side == "LONG")
        out.append(f"a {side.lower()} {'with' if with_trend else 'against'} the trend")
    return out


def _minutes(m: float) -> str:
    m = int(round(m))
    return f"{m} min" if m < 60 else f"{m // 60}h {m % 60:02d}m"


class MarketContext:
    """Streams closed bars and answers "what does the market look like right now?"."""

    def __init__(self, lookback_days: int = 10, trend_bars: int = 50, volume_bars: int = 300):
        self.ranges: deque[float] = deque(maxlen=lookback_days)
        self.trend = EMA(trend_bars)
        self.volume_avg = EMA(volume_bars)
        self.day: date | None = None
        self.day_open: float | None = None
        self.high: float | None = None
        self.low: float | None = None
        self._pv = 0.0
        self._v = 0.0
        self.prev_close: float | None = None  # yesterday's last regular-hours close
        self._last_rth_close: float | None = None
        self.gap: float | None = None
        self.last_volume: float | None = None

    def update(self, bar: Bar, day: date, rth: bool) -> None:
        if day != self.day:
            if self.high is not None and self.low is not None:
                self.ranges.append(self.high - self.low)
            if self._last_rth_close is not None:
                self.prev_close = self._last_rth_close
            self.day = day
            self.day_open = self.high = self.low = self.gap = None
            self._pv = self._v = 0.0
        self.trend.update(bar.close)
        if not rth:
            return
        if self.day_open is None:
            self.day_open = bar.open
            if self.prev_close is not None:
                self.gap = bar.open - self.prev_close
        self.high = bar.high if self.high is None else max(self.high, bar.high)
        self.low = bar.low if self.low is None else min(self.low, bar.low)
        if bar.volume > 0:
            self._pv += (bar.high + bar.low + bar.close) / 3 * bar.volume
            self._v += bar.volume
            self.volume_avg.update(bar.volume)
            self.last_volume = bar.volume
        self._last_rth_close = bar.close

    @property
    def avg_range(self) -> float | None:
        if len(self.ranges) < MIN_DAYS:
            return None
        avg = sum(self.ranges) / len(self.ranges)
        return avg if avg > 0 else None

    @property
    def vwap(self) -> float | None:
        return self._pv / self._v if self._v else None

    def snapshot(self, price: float, local: datetime, vol_ratio: float | None, rth_open: time) -> dict[str, float]:
        """The measurements at ``price`` and Chicago time ``local``; ones not known yet are left out."""
        avg = self.avg_range
        out: dict[str, float] = {}

        def put(key: str, value: float | None, digits: int = 3) -> None:
            if value is not None:
                out[key] = round(value, digits)

        def units(points: float | None) -> float | None:
            return None if points is None or avg is None else points / avg

        put("vol", vol_ratio, 2)
        opened = local.replace(hour=rth_open.hour, minute=rth_open.minute, second=0, microsecond=0)
        put("min_open", (local - opened).total_seconds() / 60, 0)
        put("gap", units(self.gap))
        if self.day_open is not None:
            put("move", units(price - self.day_open))
        if self.high is not None and self.low is not None:
            put("range_used", units(self.high - self.low))
            if self.high > self.low:
                put("range_pos", min(1.0, max(0.0, (price - self.low) / (self.high - self.low))))
        if self.vwap is not None:
            put("vwap", units(price - self.vwap))
        if self.trend.value is not None:
            put("trend", units(price - self.trend.value))
        if self.last_volume is not None and self.volume_avg.value:
            put("volume", self.last_volume / self.volume_avg.value, 2)
        if self.day is not None:
            out["dow"] = self.day.weekday()
        return out
