"""Reading the chart's trend: is price going up, down or sideways, how strongly, and since when.

The bot watches the same chart you do (its own bars, 5 minutes by default) and reads it four ways,
each a number from -1 (down) to +1 (up):

* **Slope** - the straight line that fits the last ``WINDOW`` closes (two hours of 5-minute bars),
  measured in ATRs and weighted by how straight the climb or fall was (R squared): a steady move
  counts fully, a choppy one much less.
* **Averages** - price against its 20-bar average, and the 20-bar average against the 50-bar one.
  Both above = +1, both below = -1, mixed = 0.
* **Swings** - the last two swing highs and swing lows (a bar whose high or low is the most extreme
  of the ``SWING`` bars either side). Higher highs and higher lows = +1, lower highs and lower lows
  = -1, one of each = 0.
* **Today** - price against today's regular-hours VWAP, in ATRs.

The **trend score** is 35% slope + 25% averages + 25% swings + 15% today. Its direction is *up*
from +0.35, *down* from -0.35 and *sideways* in between (a direction holds until the score falls
back past +/-0.20, so it doesn't flip on every bar). **Strength** is the 14-bar ADX: below 20 the
market ranges, from 25 it trends. A trend is **strong** when the score is beyond +/-0.6 and the ADX
is 25 or more.

What the bot does with the reading:

* Every signal, trade and idea saves it in its market snapshot (``tscore``, ``tadx``, ``tstretch``;
  market_context.py), so the bot learns how each strategy does with the trend and against it. The
  Knowledge tab's condition hints, the decision records and the trend test all read those.
* The AI trading assistant sees it with every decision, the Ask answers can report it, the day
  plan (day_plan.py) builds the morning route around it and the chart draws it.
* The trend filter (``trend.filter``) skips a signal against a strong trend - only once the trend
  test (trend_lab.py) has shown, on replayed history and with 95% confidence, that skipping those
  made more than taking them (``auto``, the default), or when you switch it on yourself.

How good the reading is gets measured too (``TrendCalls``): every call it makes during regular
hours is checked an hour later. The share of calls price followed is shown next to the reading, so
you can see whether the chart reading has been worth anything on this market. A trend is a
description of the past hours; it does not promise where price goes next.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from topstep_bot.indicators import ADX, ATR, EMA
from topstep_bot.models import Bar

WINDOW = 24  # bars the slope is fitted over (2 hours of 5-minute bars)
SWING = 3  # bars either side that make a swing high or low
FAST, SLOW = 20, 50  # the two averages
WEIGHTS = {"slope": 0.35, "averages": 0.25, "swings": 0.25, "today": 0.15}
ENTER, LEAVE = 0.35, 0.20  # direction thresholds (score), with hysteresis
STRONG_SCORE, STRONG_ADX = 0.6, 25.0
RANGING_ADX = 20.0
STRETCHED_ATR = 2.5  # price this many ATRs from its 20-bar average, with the trend: extended
CALL_BARS = 12  # a call is checked this many bars later (an hour of 5-minute bars)
CALLS_KEPT = 3000
DIRECTIONS = ("up", "down", "sideways")


def _clamp(x: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def _sign(x: float, eps: float = 0.0) -> int:
    return 1 if x > eps else -1 if x < -eps else 0


def fit_line(values: list[float]) -> tuple[float, float, float]:
    """Least squares through ``values`` at x = 0..n-1: (slope per step, value at the last step, R squared)."""
    n = len(values)
    if n < 2:
        return 0.0, values[-1] if values else 0.0, 0.0
    mx = (n - 1) / 2
    my = sum(values) / n
    sxx = sum((i - mx) ** 2 for i in range(n))
    sxy = sum((i - mx) * (v - my) for i, v in enumerate(values))
    slope = sxy / sxx
    end = my + slope * (n - 1 - mx)
    ss_tot = sum((v - my) ** 2 for v in values)
    ss_res = sum((v - (my + slope * (i - mx))) ** 2 for i, v in enumerate(values))
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0
    return slope, end, max(0.0, r2)


@dataclass
class Swing:
    ts: datetime
    price: float
    kind: str  # high | low


@dataclass
class Reading:
    """The trend as of the last closed bar."""

    direction: str  # up | down | sideways
    score: float  # -1 .. +1
    strength: float | None  # ADX (0-100)
    strong: bool
    parts: dict[str, float]  # each piece, -1 .. +1
    phase: str  # what the trend is doing now, in words ("" when nothing stands out)
    since: datetime | None  # when the current direction began (the bar it was first read)
    at: datetime  # the bar close this reading is from
    stretch: float | None  # price against its 20-bar average, in ATRs (signed)
    swings: list[Swing] = field(default_factory=list)

    @property
    def sign(self) -> int:
        return 1 if self.direction == "up" else -1 if self.direction == "down" else 0

    @property
    def label(self) -> str:
        if self.direction == "sideways":
            return "Ranging" if self.strength is not None and self.strength < RANGING_ADX else "Sideways"
        word = "uptrend" if self.direction == "up" else "downtrend"
        return f"Strong {word}" if self.strong else word.capitalize()

    def against(self, side: str, min_score: float) -> bool:
        """Is a LONG / SHORT trade against this trend, with the score at least ``min_score`` the other way?"""
        return against(self.score, side, min_score)

    def words(self) -> list[str]:
        """The reading in plain sentences."""
        out = []
        p = self.parts
        strength = f", ADX {self.strength:.0f}" if self.strength is not None else ""
        out.append(f"{self.label} (trend score {self.score:+.2f}{strength}).")
        bits = []
        if abs(p.get("slope", 0)) >= 0.2:
            bits.append(f"the last two hours slope {'up' if p['slope'] > 0 else 'down'}")
        avg = p.get("averages", 0)
        if avg >= 0.5:
            bits.append("price is above its 20- and 50-bar averages")
        elif avg <= -0.5:
            bits.append("price is below its 20- and 50-bar averages")
        sw = p.get("swings", 0)
        if sw >= 0.5:
            bits.append("higher highs and higher lows")
        elif sw <= -0.5:
            bits.append("lower highs and lower lows")
        today = p.get("today", 0)
        if abs(today) >= 0.25:
            bits.append(f"price is {'above' if today > 0 else 'below'} today's VWAP")
        if bits:
            out.append(bits[0][0].upper() + bits[0][1:] + ("; " + "; ".join(bits[1:]) if bits[1:] else "") + ".")
        if self.phase:
            out.append(self.phase[0].upper() + self.phase[1:] + ".")
        return out

    def to_dict(self, tz=None) -> dict[str, Any]:
        def local(ts: datetime | None) -> str | None:
            if ts is None:
                return None
            return (ts.astimezone(tz) if tz else ts).strftime("%H:%M")

        return {"direction": self.direction, "label": self.label, "score": round(self.score, 2),
                "strength": round(self.strength, 1) if self.strength is not None else None, "strong": self.strong,
                "parts": {k: round(v, 2) for k, v in self.parts.items()}, "phase": self.phase,
                "since": self.since.isoformat() if self.since else None, "since_local": local(self.since),
                "at": self.at.isoformat(), "at_local": local(self.at),
                "stretch": round(self.stretch, 2) if self.stretch is not None else None, "words": self.words()}


def against(score: float | None, side: str, min_score: float) -> bool:
    """A LONG with the score at or below ``-min_score``, or a SHORT at or above ``+min_score``."""
    if score is None:
        return False
    return score <= -min_score if side == "LONG" else score >= min_score


def with_trend(ctx: dict | None, side: str, min_score: float = ENTER) -> str:
    """'with', 'against' or 'none': a LONG / SHORT signal against the trend reading saved in its snapshot."""
    score = (ctx or {}).get("tscore")
    if score is None:
        return "unknown"
    if against(score, side, min_score):
        return "against"
    if against(-score, side, min_score):
        return "with"
    return "none"


class TrendCalls:
    """How the reading's calls turned out: each up / down call during regular hours, checked ``CALL_BARS`` later.

    A call is "followed" when price was further in the called direction an hour later. Calls whose hour runs
    past the end of regular hours are dropped (overnight moves aren't the chart the call was about)."""

    def __init__(self, horizon: int = CALL_BARS, kept: int = CALLS_KEPT):
        self.horizon = horizon
        self.pending: deque[tuple[int, date, str, bool, float, float]] = deque()
        self.done: deque[tuple[str, bool, float]] = deque(maxlen=kept)  # (direction, strong, move with the call in ATRs)
        self._i = 0
        self._day: date | None = None

    def update(self, close: float, day: date, rth: bool, reading: Reading | None, atr: float | None) -> None:
        self._i += 1
        if day != self._day:
            self.pending.clear()
            self._day = day
        if not rth:
            self.pending.clear()
            return
        while self.pending and self._i - self.pending[0][0] >= self.horizon:
            _, _, direction, strong, price, unit = self.pending.popleft()
            move = (close - price) * (1 if direction == "up" else -1)
            self.done.append((direction, strong, move / unit))
        if reading is not None and reading.direction != "sideways" and atr:
            self.pending.append((self._i, day, reading.direction, reading.strong, close, atr))

    def summary(self) -> dict[str, Any]:
        rows = list(self.done)
        out: dict[str, Any] = {"calls": len(rows), "horizon_bars": self.horizon}

        def part(sel: list[tuple[str, bool, float]]) -> dict[str, Any] | None:
            if not sel:
                return None
            return {"n": len(sel), "followed": round(sum(m > 0 for _, _, m in sel) / len(sel), 3),
                    "avg_atr": round(sum(m for _, _, m in sel) / len(sel), 3)}

        out["all"] = part(rows)
        out["strong"] = part([r for r in rows if r[1]])
        out["up"] = part([r for r in rows if r[0] == "up"])
        out["down"] = part([r for r in rows if r[0] == "down"])
        return out


def calls_text(s: dict[str, Any] | None, minutes: int) -> str:
    """'Its calls: price followed 56% of 812 up/down calls an hour later (strong trends: 61% of 240).'"""
    if not s or not s.get("all"):
        return "Too few up/down calls checked yet to say how often price followed them."
    a = s["all"]
    text = (f"Price kept going the called way {a['followed']:.0%} of the time over {a['n']} up/down calls, checked "
            f"{minutes} minutes later (a coin flip would be about 50%)")
    st = s.get("strong")
    if st and st["n"] >= 20:
        text += f"; after strong-trend calls {st['followed']:.0%} of {st['n']}"
    return text + "."


class TrendReader:
    """Streams closed bars and reads the trend (see the module docstring)."""

    def __init__(self, window: int = WINDOW, swing: int = SWING):
        self.window = window
        self.swing = swing
        self.closes: deque[float] = deque(maxlen=window)
        self.times: deque[datetime] = deque(maxlen=window)
        self.fast, self.slow = EMA(FAST), EMA(SLOW)
        self.atr = ATR(14)
        self.adx = ADX(14)
        self._adx_hist: deque[float] = deque(maxlen=7)
        self._bars: deque[Bar] = deque(maxlen=2 * swing + 1)
        self.highs: deque[Swing] = deque(maxlen=4)
        self.lows: deque[Swing] = deque(maxlen=4)
        self.day: date | None = None
        self._pv = self._v = 0.0
        self.vwap: float | None = None
        self.direction = "sideways"
        self.since: datetime | None = None
        self.last: Reading | None = None
        self.calls = TrendCalls()
        self._closes_recent: deque[float] = deque(maxlen=4)

    @property
    def ready(self) -> bool:
        return len(self.closes) >= self.window and self.atr.value is not None and self.fast.value is not None

    def update(self, bar: Bar, close_time: datetime, day: date, rth: bool) -> Reading | None:
        """Feed one closed bar (``close_time``: when it closed; ``rth``: inside regular hours)."""
        if day != self.day:
            self.day = day
            self._pv = self._v = 0.0
            self.vwap = None
        if rth:
            vol = bar.volume if bar.volume > 0 else 1.0
            self._pv += (bar.high + bar.low + bar.close) / 3 * vol
            self._v += vol
            self.vwap = self._pv / self._v
        self.closes.append(bar.close)
        self.times.append(bar.ts)  # bar start times, as the chart's candles are keyed
        self._closes_recent.append(bar.close)
        self.fast.update(bar.close)
        self.slow.update(bar.close)
        self.atr.update(bar.high, bar.low, bar.close)
        adx = self.adx.update(bar.high, bar.low, bar.close)
        if adx is not None:
            self._adx_hist.append(adx)
        self._find_swings(bar)
        self.last = self._read(close_time, rth) if self.ready else None
        self.calls.update(bar.close, day, rth, self.last, self.atr.value)
        return self.last

    def _find_swings(self, bar: Bar) -> None:
        self._bars.append(bar)
        if len(self._bars) < self._bars.maxlen:
            return
        bars = list(self._bars)
        mid = bars[self.swing]
        left, right = bars[:self.swing], bars[self.swing + 1:]
        ts = mid.ts
        if (all(mid.high > b.high for b in left) and all(mid.high >= b.high for b in right)
                and (not self.highs or self.highs[-1].ts != ts)):
            self.highs.append(Swing(ts, mid.high, "high"))
        if (all(mid.low < b.low for b in left) and all(mid.low <= b.low for b in right)
                and (not self.lows or self.lows[-1].ts != ts)):
            self.lows.append(Swing(ts, mid.low, "low"))

    def _read(self, at: datetime, rth: bool) -> Reading:
        atr = self.atr.value or 0.0
        close = self.closes[-1]
        slope, _, r2 = fit_line(list(self.closes))
        move_atr = slope * (len(self.closes) - 1) / atr if atr > 0 else 0.0
        parts = {"slope": math.tanh(move_atr / 2.0) * r2}
        fast, slow = self.fast.value, self.slow.value
        a = _sign(close - fast) if fast is not None else 0
        b = _sign(fast - slow) if fast is not None and slow is not None else 0
        parts["averages"] = (a + b) / 2
        if len(self.highs) >= 2 and len(self.lows) >= 2:
            parts["swings"] = (_sign(self.highs[-1].price - self.highs[-2].price)
                               + _sign(self.lows[-1].price - self.lows[-2].price)) / 2
        else:
            parts["swings"] = 0.0
        parts["today"] = math.tanh((close - self.vwap) / (2 * atr)) if rth and self.vwap is not None and atr > 0 else 0.0
        score = _clamp(sum(WEIGHTS[k] * v for k, v in parts.items()))
        adx = self.adx.value
        # Direction with hysteresis: it takes +/-ENTER to start a trend and falling inside +/-LEAVE to end it.
        prev = self.direction
        if prev == "up" and score < LEAVE:
            new = "down" if score <= -ENTER else "sideways"
        elif prev == "down" and score > -LEAVE:
            new = "up" if score >= ENTER else "sideways"
        elif prev == "sideways":
            new = "up" if score >= ENTER else "down" if score <= -ENTER else "sideways"
        else:
            new = prev
        if new != prev or self.since is None:
            self.since = at
        self.direction = new
        strong = new != "sideways" and abs(score) >= STRONG_SCORE and adx is not None and adx >= STRONG_ADX
        stretch = (close - fast) / atr if fast is not None and atr > 0 else None
        reading = Reading(new, score, adx, strong, parts, "", self.since, at, stretch,
                          [*self.highs, *self.lows])
        reading.phase = self._phase(reading)
        return reading

    def _phase(self, r: Reading) -> str:
        if r.direction == "sideways":
            if r.strength is not None and r.strength < RANGING_ADX:
                return "no clear direction: the market is ranging, so breakouts fail more often"
            return ""
        sign = r.sign
        if r.stretch is not None and r.stretch * sign >= STRETCHED_ATR:
            return f"stretched: price is {abs(r.stretch):.1f} ATRs from its 20-bar average, where pullbacks are common"
        recent = list(self._closes_recent)
        if len(recent) >= 4 and all((recent[i + 1] - recent[i]) * sign < 0 for i in range(len(recent) - 1)):
            return f"pulling back against the {'uptrend' if sign > 0 else 'downtrend'} for the last few bars"
        hist = list(self._adx_hist)
        if len(hist) >= 7 and hist[-1] >= STRONG_ADX and hist[-1] < hist[0] - 3:
            return "losing steam: the ADX has been falling"
        if self.since is not None and len(self.times) >= 6 and self.since >= self.times[-6]:
            return "just started"
        return ""

    # ------------------------------------------------------------------ outputs

    def features(self, price: float | None = None) -> dict[str, float]:
        """The reading for a market snapshot (market_context.py): tscore, tadx and tstretch at ``price``."""
        r = self.last
        if r is None:
            return {}
        out = {"tscore": round(r.score, 3)}
        if r.strength is not None:
            out["tadx"] = round(r.strength, 1)
        atr, fast = self.atr.value, self.fast.value
        ref = price if price is not None else self.closes[-1]
        if atr and fast is not None:
            out["tstretch"] = round((ref - fast) / atr, 2)
        return out

    def chart(self) -> dict[str, Any] | None:
        """The fitted line over the last ``window`` bars (with its spread) and the swing points, for the chart."""
        if self.last is None or len(self.closes) < 2:
            return None
        values = list(self.closes)
        slope, end, _ = fit_line(values)
        start = end - slope * (len(values) - 1)
        spread = math.sqrt(sum((v - (start + slope * i)) ** 2 for i, v in enumerate(values)) / len(values))
        times = list(self.times)
        return {"from": [int(times[0].timestamp()), start], "to": [int(times[-1].timestamp()), end],
                "band": spread, "direction": self.last.direction, "label": self.last.label,
                "swings": [[int(s.ts.timestamp()), s.price, s.kind] for s in self.last.swings]}


def view_text(view: dict[str, Any] | None, symbol: str = "") -> str:
    """The trend view (TradingCore.trend_view) in plain text, for Telegram and the command line."""
    if not view:
        return "No trend reading (the bot isn't running)."
    r = view.get("reading")
    head = f"📈 Trend{' on ' + symbol if symbol else ''}: "
    if r is None:
        lines = [head + "not enough bars yet to read the chart (it needs about two hours of bars)."]
    else:
        since = f" since {r['since_local']} CT" if r.get("since_local") and r["direction"] != "sideways" else ""
        lines = [head + r["label"] + since + "."] + r["words"][1:]
    lines.append(view.get("calls_text") or "")
    h = view.get("history") or {}
    w, a = h.get("with") or {}, h.get("against") or {}
    if w.get("n") and a.get("n"):
        lines.append(f"The bot's recent signals with the trend averaged {w['avg_r']:+.2f}R ({w['n']}), against it "
                     f"{a['avg_r']:+.2f}R ({a['n']}), after costs.")
    elif h.get("signals"):
        lines.append(f"{h.get('with_reading', 0)} of the bot's {h['signals']} recent signals carry a trend reading so far: "
                     "the comparison with and against the trend fills in as it learns.")
    f = view.get("filter") or {}
    lines.append(f"Trend filter: {'ON - signals against a strong trend are skipped' if f.get('on') else 'off'} "
                 f"({f.get('why', '')}).")
    lines.append("A trend describes the last hours; it does not promise where price goes next.")
    return "\n".join(x for x in lines if x)
