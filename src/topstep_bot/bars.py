"""Bar utilities: clock alignment and resampling to larger timeframes."""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from datetime import datetime, timezone

from topstep_bot.models import Bar

UTC = timezone.utc


def floor_time(ts: datetime, seconds: int) -> datetime:
    """Round a timestamp down to a multiple of ``seconds`` (e.g. the start of its 5-minute bar)."""
    epoch = int(ts.timestamp())
    return datetime.fromtimestamp(epoch - epoch % seconds, tz=UTC)


def resample(bars: Iterable[Bar], minutes: int) -> Iterator[Bar]:
    """Combine sorted bars (e.g. 1-minute) into N-minute bars aligned to the clock."""
    seconds = minutes * 60
    cur: Bar | None = None
    for b in bars:
        start = floor_time(b.ts, seconds)
        if cur is not None and start != cur.ts:
            yield cur
            cur = None
        if cur is None:
            cur = Bar(start, b.open, b.high, b.low, b.close, b.volume)
        else:
            cur.high = max(cur.high, b.high)
            cur.low = min(cur.low, b.low)
            cur.close = b.close
            cur.volume += b.volume
    if cur is not None:
        yield cur
