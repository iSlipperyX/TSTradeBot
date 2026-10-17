"""Streaming (incremental) indicators.

Each indicator is updated one bar at a time, so the exact same code runs in backtests and
live trading. ``value`` is None until enough data has been seen.
"""

from __future__ import annotations

import math
from collections import deque


class EMA:
    def __init__(self, period: int):
        if period < 1:
            raise ValueError("period must be >= 1")
        self.period = period
        self.alpha = 2.0 / (period + 1)
        self.value: float | None = None
        self._count = 0
        self._seed_sum = 0.0

    @property
    def ready(self) -> bool:
        return self._count >= self.period

    def update(self, x: float) -> float | None:
        self._count += 1
        if self._count < self.period:
            self._seed_sum += x
            return None
        if self._count == self.period:
            self.value = (self._seed_sum + x) / self.period  # seed with SMA
        else:
            self.value = self.alpha * x + (1 - self.alpha) * self.value
        return self.value


class SMA:
    def __init__(self, period: int):
        self.period = period
        self._window: deque[float] = deque(maxlen=period)
        self._sum = 0.0
        self.value: float | None = None

    @property
    def ready(self) -> bool:
        return len(self._window) == self.period

    def update(self, x: float) -> float | None:
        if len(self._window) == self.period:
            self._sum -= self._window[0]
        self._window.append(x)
        self._sum += x
        self.value = self._sum / self.period if self.ready else None
        return self.value


class ATR:
    """Average True Range with Wilder smoothing."""

    def __init__(self, period: int = 14):
        self.period = period
        self.value: float | None = None
        self._prev_close: float | None = None
        self._trs: list[float] = []

    @property
    def ready(self) -> bool:
        return self.value is not None

    def update(self, high: float, low: float, close: float) -> float | None:
        if self._prev_close is None:
            tr = high - low
        else:
            tr = max(high - low, abs(high - self._prev_close), abs(low - self._prev_close))
        self._prev_close = close
        if self.value is None:
            self._trs.append(tr)
            if len(self._trs) == self.period:
                self.value = sum(self._trs) / self.period
                self._trs.clear()
        else:
            self.value = (self.value * (self.period - 1) + tr) / self.period
        return self.value


class RSI:
    """Relative Strength Index with Wilder smoothing."""

    def __init__(self, period: int = 14):
        self.period = period
        self.value: float | None = None
        self._prev: float | None = None
        self._gains: list[float] = []
        self._losses: list[float] = []
        self._avg_gain: float | None = None
        self._avg_loss: float | None = None

    @property
    def ready(self) -> bool:
        return self.value is not None

    def update(self, close: float) -> float | None:
        if self._prev is None:
            self._prev = close
            return None
        change = close - self._prev
        self._prev = close
        gain, loss = max(change, 0.0), max(-change, 0.0)
        if self._avg_gain is None:
            self._gains.append(gain)
            self._losses.append(loss)
            if len(self._gains) < self.period:
                return None
            self._avg_gain = sum(self._gains) / self.period
            self._avg_loss = sum(self._losses) / self.period
        else:
            self._avg_gain = (self._avg_gain * (self.period - 1) + gain) / self.period
            self._avg_loss = (self._avg_loss * (self.period - 1) + loss) / self.period
        if self._avg_loss == 0:
            self.value = 100.0 if self._avg_gain > 0 else 50.0
        else:
            rs = self._avg_gain / self._avg_loss
            self.value = 100.0 - 100.0 / (1.0 + rs)
        return self.value


class SessionVWAP:
    """Volume-weighted average price that resets each session, with volume-weighted std dev bands."""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self._pv = 0.0
        self._v = 0.0
        self._p2v = 0.0
        self.value: float | None = None

    @property
    def ready(self) -> bool:
        return self.value is not None

    def update(self, high: float, low: float, close: float, volume: float) -> float | None:
        typical = (high + low + close) / 3.0
        vol = volume if volume > 0 else 1.0  # tolerate feeds without volume
        self._pv += typical * vol
        self._p2v += typical * typical * vol
        self._v += vol
        self.value = self._pv / self._v
        return self.value

    @property
    def stdev(self) -> float:
        if self._v == 0 or self.value is None:
            return 0.0
        var = self._p2v / self._v - self.value * self.value
        return math.sqrt(max(var, 0.0))

    def band(self, k: float) -> tuple[float, float] | None:
        if self.value is None:
            return None
        sd = self.stdev
        return self.value - k * sd, self.value + k * sd


class RollingExtremes:
    """Highest high / lowest low over the last N bars."""

    def __init__(self, period: int):
        self.period = period
        self._highs: deque[float] = deque(maxlen=period)
        self._lows: deque[float] = deque(maxlen=period)

    @property
    def ready(self) -> bool:
        return len(self._highs) == self.period

    def update(self, high: float, low: float) -> None:
        self._highs.append(high)
        self._lows.append(low)

    @property
    def highest(self) -> float | None:
        return max(self._highs) if self._highs else None

    @property
    def lowest(self) -> float | None:
        return min(self._lows) if self._lows else None


class ADX:
    """Average Directional Index with Wilder smoothing: how strongly price is trending, either way.

    ``value`` (0-100) is the strength; ``plus_di`` / ``minus_di`` say which way. Around 20 or below the
    market is ranging; 25 and up it trends. ``value`` is None until 2 x period bars have been seen."""

    def __init__(self, period: int = 14):
        self.period = period
        self.value: float | None = None
        self.plus_di: float | None = None
        self.minus_di: float | None = None
        self._prev: tuple[float, float, float] | None = None  # high, low, close
        self._seed: list[tuple[float, float, float]] = []  # tr, +dm, -dm until the first smoothing
        self._tr = self._pdm = self._mdm = 0.0
        self._dx: list[float] = []

    @property
    def ready(self) -> bool:
        return self.value is not None

    def update(self, high: float, low: float, close: float) -> float | None:
        if self._prev is None:
            self._prev = (high, low, close)
            return None
        ph, pl, pc = self._prev
        self._prev = (high, low, close)
        up, down = high - ph, pl - low
        pdm = up if up > down and up > 0 else 0.0
        mdm = down if down > up and down > 0 else 0.0
        tr = max(high - low, abs(high - pc), abs(low - pc))
        n = self.period
        if len(self._seed) < n:
            self._seed.append((tr, pdm, mdm))
            if len(self._seed) < n:
                return None
            self._tr = sum(s[0] for s in self._seed)
            self._pdm = sum(s[1] for s in self._seed)
            self._mdm = sum(s[2] for s in self._seed)
        else:
            self._tr = self._tr - self._tr / n + tr
            self._pdm = self._pdm - self._pdm / n + pdm
            self._mdm = self._mdm - self._mdm / n + mdm
        if self._tr <= 0:
            return self.value
        self.plus_di = 100.0 * self._pdm / self._tr
        self.minus_di = 100.0 * self._mdm / self._tr
        total = self.plus_di + self.minus_di
        dx = 100.0 * abs(self.plus_di - self.minus_di) / total if total > 0 else 0.0
        if self.value is None:
            self._dx.append(dx)
            if len(self._dx) == n:
                self.value = sum(self._dx) / n
                self._dx.clear()
        else:
            self.value = (self.value * (n - 1) + dx) / n
        return self.value
