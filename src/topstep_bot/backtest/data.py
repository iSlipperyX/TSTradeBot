"""Historical data: CSV import/export and a synthetic market generator for demos and tests."""

from __future__ import annotations

import csv
import math
import random
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from topstep_bot.models import Bar

UTC = timezone.utc
CT = ZoneInfo("America/Chicago")

_TS_COLUMNS = ("timestamp", "datetime", "time", "date", "ts", "t")


def _parse_time(value: str, naive_tz: ZoneInfo) -> datetime:
    value = value.strip()
    if value.replace(".", "", 1).isdigit():  # epoch seconds or milliseconds
        num = float(value)
        return datetime.fromtimestamp(num / 1000 if num > 1e11 else num, tz=UTC)
    ts = datetime.fromisoformat(value.replace("Z", "+00:00").replace("/", "-"))
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=naive_tz)
    return ts.astimezone(UTC)


def load_csv(path: Path | str, naive_tz: str = "UTC") -> list[Bar]:
    """Load OHLCV bars. Needs a time column (timestamp/datetime/time/date) plus open, high, low, close.

    Timestamps without a timezone are interpreted in ``naive_tz`` (e.g. 'America/Chicago').
    Each row's time must be the bar's OPEN time.
    """
    tz = ZoneInfo(naive_tz)
    bars: list[Bar] = []
    with open(path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        if reader.fieldnames is None:
            raise ValueError(f"{path} is empty")
        cols = {name.strip().lower(): name for name in reader.fieldnames}
        ts_col = next((cols[c] for c in _TS_COLUMNS if c in cols), None)
        missing = [c for c in ("open", "high", "low", "close") if c not in cols]
        if ts_col is None or missing:
            raise ValueError(f"{path}: need a time column and open/high/low/close columns (found {reader.fieldnames})")
        vol_col = cols.get("volume") or cols.get("vol") or cols.get("v")
        for row in reader:
            bars.append(
                Bar(
                    ts=_parse_time(row[ts_col], tz),
                    open=float(row[cols["open"]]),
                    high=float(row[cols["high"]]),
                    low=float(row[cols["low"]]),
                    close=float(row[cols["close"]]),
                    volume=float(row[vol_col]) if vol_col and row.get(vol_col) not in (None, "") else 0.0,
                )
            )
    bars.sort(key=lambda b: b.ts)
    return bars


def save_csv(bars: list[Bar], path: Path | str) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["timestamp", "open", "high", "low", "close", "volume"])
        for b in bars:
            writer.writerow([b.ts.isoformat(), b.open, b.high, b.low, b.close, b.volume])


BASE_PRICES = {"ES": 5800, "MES": 5800, "NQ": 20500, "MNQ": 20500, "YM": 42000, "MYM": 42000,
               "RTY": 2200, "M2K": 2200, "CL": 70, "MCL": 70, "GC": 2400, "MGC": 2400}


def _intraday_vol(local: datetime) -> float:
    """Relative volatility by time of day: quiet overnight, busy at the open and close."""
    t = local.hour + local.minute / 60
    if 8.5 <= t < 9.0:
        return 2.6
    if 9.0 <= t < 10.5:
        return 1.7
    if 10.5 <= t < 13.0:
        return 1.0
    if 13.0 <= t < 15.0:
        return 1.3
    if 7.5 <= t < 8.5:
        return 1.1
    return 0.45


def synthetic_bars(
    symbol: str = "MNQ",
    days: int = 60,
    tick_size: float = 0.25,
    seed: int = 7,
    end: date | None = None,
) -> list[Bar]:
    """Random-walk 1-minute bars across full CME sessions (17:00-16:00 CT), weekdays only.

    About a third of days get a directional drift (trend days) so trend and breakout logic
    has something to find. This is for demos and tests - never for judging a strategy.
    """
    rng = random.Random(seed)
    price = float(BASE_PRICES.get(symbol.upper(), 5000))
    daily_vol = 0.011
    minute_vol = daily_vol / math.sqrt(1380)
    end = end or datetime.now(CT).date()
    trading_days: list[date] = []
    d = end
    while len(trading_days) < days:
        if d.weekday() < 5:
            trading_days.append(d)
        d -= timedelta(days=1)
    trading_days.reverse()

    def snap(p: float) -> float:
        return round(round(p / tick_size) * tick_size, 6)

    bars: list[Bar] = []
    for day in trading_days:
        drift = 0.0
        roll = rng.random()
        if roll < 0.18:
            drift = minute_vol * 0.08
        elif roll < 0.36:
            drift = -minute_vol * 0.08
        start = datetime.combine(day - timedelta(days=1), time(17, 0), tzinfo=CT)
        for i in range(23 * 60):
            local = start + timedelta(minutes=i)
            vol_mult = _intraday_vol(local)
            in_rth = time(8, 30) <= local.time() < time(15, 0)
            step_drift = drift if in_rth else 0.0
            ret = step_drift + rng.gauss(0, minute_vol * vol_mult)
            open_ = price
            close = max(price * (1 + ret), tick_size)
            wiggle = abs(rng.gauss(0, minute_vol * vol_mult * 0.6)) * price
            high = max(open_, close) + wiggle * rng.random()
            low = min(open_, close) - wiggle * rng.random()
            o, h, lo, c = snap(open_), snap(high), snap(low), snap(close)
            h, lo = max(h, o, c), min(lo, o, c)
            volume = max(1, int(rng.gauss(400, 120) * vol_mult))
            bars.append(Bar(local.astimezone(UTC), o, h, lo, c, float(volume)))
            price = c
    return bars
