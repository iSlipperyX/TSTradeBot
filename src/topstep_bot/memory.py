"""The bot's long-run memory: every price bar it has seen, and what every strategy did on all of it.

The knowledge base the bot trades with (knowledge.py) looks at the last couple of months, on
purpose: markets change, and old evidence fades out. That is a small window to learn from. This
module gives the bot a much bigger memory next to it:

* **Market library** - every bar the bot downloads (warm-up, training, live bars, backfill) is
  kept in a local SQLite file, ``data/market_library.sqlite``. Each day the bot backfills the
  history it is still missing, up to ``knowledge.deep_history_days`` back, asking TopstepX only
  for the ranges it doesn't have yet. Bars you import from a CSV (``topstep-bot learn --import``)
  go in too. The library only grows: nothing is thrown away.
* **Long-run knowledge** - once a day the whole library is replayed through every strategy, the
  same way the daily training replays the last 60 days, into its own file
  (``knowledge_<SYMBOL>_<TF>m_longrun.json``). That is every signal every strategy would have
  given on every day in the library, with its outcome, costs, price path and market snapshot.

What the long-run memory changes: the reports (what the bot knows, what it learned, the
next-trade forecast's background). What it does not change: trading. The adaptive strategy keeps
deciding from the recent knowledge base, and every risk rule and size stays as configured; the
long-run results are there to compare against and to test new ideas on.

Contract rolls: the library keeps one continuous series per symbol and timeframe, built from
whichever contract was the front month when it was downloaded. Prices jump at a roll; the
strategies are intraday and reset every day, so this matters only for multi-day measures
(the opening gap and the trend on the first day after a roll).
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from topstep_bot.models import Bar

if TYPE_CHECKING:
    from topstep_bot.api.rest import ProjectXClient
    from topstep_bot.config import BotConfig
    from topstep_bot.knowledge import KnowledgeBase
    from topstep_bot.models import Contract

log = logging.getLogger("topstep_bot.memory")
UTC = timezone.utc
DAY_SHIFT = 7 * 3600  # seconds that move 17:00 CT (the session open) past midnight UTC: one key per trading day
LONGRUN_MAX_OBSERVATIONS = 250_000

SCHEMA = """
CREATE TABLE IF NOT EXISTS bars (
    symbol TEXT NOT NULL, tf INTEGER NOT NULL, ts INTEGER NOT NULL,
    open REAL, high REAL, low REAL, close REAL, volume REAL, contract TEXT,
    PRIMARY KEY (symbol, tf, ts)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS spans (
    symbol TEXT NOT NULL, tf INTEGER NOT NULL, start INTEGER NOT NULL, end INTEGER NOT NULL
);
"""


def _epoch(ts: datetime) -> int:
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return int(ts.timestamp())


def _dt(epoch: int) -> datetime:
    return datetime.fromtimestamp(epoch, tz=UTC)


class MarketLibrary:
    """Every bar the bot has downloaded or imported, in one SQLite file.

    ``spans`` remembers which time ranges were already asked from TopstepX (including empty answers
    for weekends, holidays or before a contract existed), so a backfill never asks for them twice.
    """

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(self.path), check_same_thread=False)
        self._db.executescript(SCHEMA)
        self._db.commit()
        self._version = 0  # bumps on every write: stats() is cached between writes (the dashboard asks every 2 s)
        self._stats: dict[tuple[str, int], tuple[int, dict[str, Any]]] = {}

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # ------------------------------------------------------------- writing

    def add(self, symbol: str, tf: int, bars: Iterable[Bar], contract: str = "") -> int:
        """Store bars (a newer download of the same bar replaces the older one). Returns how many were new."""
        rows = [(symbol, tf, _epoch(b.ts), b.open, b.high, b.low, b.close, b.volume, contract) for b in bars]
        if not rows:
            return 0
        with self._lock:
            before = self._count(symbol, tf)
            self._db.executemany("INSERT OR REPLACE INTO bars VALUES (?,?,?,?,?,?,?,?,?)", rows)
            self._db.commit()
            self._version += 1
            return self._count(symbol, tf) - before

    def note_span(self, symbol: str, tf: int, start: datetime, end: datetime) -> None:
        """Remember that [start, end] was downloaded, merging it with the ranges it touches."""
        a, b = _epoch(start), _epoch(end)
        if b <= a:
            return
        with self._lock:
            touching = self._db.execute(
                "SELECT rowid, start, end FROM spans WHERE symbol=? AND tf=? AND start<=? AND end>=?", (symbol, tf, b, a)
            ).fetchall()
            for _, s, e in touching:
                a, b = min(a, s), max(b, e)
            self._db.executemany("DELETE FROM spans WHERE rowid=?", [(rowid,) for rowid, _, _ in touching])
            self._db.execute("INSERT INTO spans VALUES (?,?,?,?)", (symbol, tf, a, b))
            self._db.commit()

    # ------------------------------------------------------------- reading

    def _count(self, symbol: str, tf: int) -> int:
        return self._db.execute("SELECT COUNT(*) FROM bars WHERE symbol=? AND tf=?", (symbol, tf)).fetchone()[0]

    def missing(self, symbol: str, tf: int, start: datetime, end: datetime, min_gap: timedelta | None = None) -> list[tuple[datetime, datetime]]:
        """The parts of [start, end] never downloaded, oldest first (gaps shorter than ``min_gap`` are ignored)."""
        a, b = _epoch(start), _epoch(end)
        smallest = int((min_gap or timedelta(0)).total_seconds())
        with self._lock:
            spans = self._db.execute("SELECT start, end FROM spans WHERE symbol=? AND tf=? AND end>? AND start<? ORDER BY start",
                                     (symbol, tf, a, b)).fetchall()
        gaps, cursor = [], a
        for s, e in spans:
            if s > cursor:
                gaps.append((cursor, min(s, b)))
            cursor = max(cursor, e)
        if cursor < b:
            gaps.append((cursor, b))
        return [(_dt(s), _dt(e)) for s, e in gaps if e - s > smallest]

    def bars(self, symbol: str, tf: int, start: datetime | None = None, end: datetime | None = None) -> list[Bar]:
        lo = _epoch(start) if start else 0
        hi = _epoch(end) if end else 2**62
        with self._lock:
            rows = self._db.execute(
                "SELECT ts, open, high, low, close, volume FROM bars WHERE symbol=? AND tf=? AND ts>=? AND ts<? ORDER BY ts",
                (symbol, tf, lo, hi)).fetchall()
        return [Bar(ts=_dt(ts), open=o, high=h, low=lo_, close=c, volume=v or 0.0) for ts, o, h, lo_, c, v in rows]

    def stats(self, symbol: str, tf: int) -> dict[str, Any]:
        cached = self._stats.get((symbol, tf))
        if cached and cached[0] == self._version:
            return cached[1]
        with self._lock:
            n, first, last, days = self._db.execute(
                "SELECT COUNT(*), MIN(ts), MAX(ts), COUNT(DISTINCT (ts + ?) / 86400) FROM bars WHERE symbol=? AND tf=?",
                (DAY_SHIFT, symbol, tf)).fetchone()
        try:
            size = self.path.stat().st_size
        except OSError:
            size = 0
        out = {"bars": n, "first": _dt(first).date().isoformat() if first else None,
               "last": _dt(last).isoformat(timespec="minutes") if last else None, "days": days, "mb": round(size / 1e6, 1)}
        self._stats[(symbol, tf)] = (self._version, out)
        return out


@dataclass
class LearnResult:
    downloaded: int  # new bars from TopstepX
    bars: int  # bars in the library afterwards
    observations: int  # long-run observations after the replay
    days: int  # trading days replayed
    first: str = ""
    last: str = ""

    def text(self) -> str:
        got = f"{self.downloaded:,} new bars downloaded; " if self.downloaded else ""
        return (f"Long-run memory updated: {got}{self.bars:,} bars in the library, replayed {self.days:,} trading days "
                f"({self.first} to {self.last}) through every strategy: {self.observations:,} observations.")


class LongRunMemory:
    """The market library plus the knowledge learned from replaying all of it (see the module docstring)."""

    def __init__(self, library: MarketLibrary, knowledge: KnowledgeBase, symbol: str, tf: int):
        self.library = library
        self.knowledge = knowledge
        self.symbol = symbol
        self.tf = tf
        self.running = False
        self.progress: float | None = None  # 0..1 while learning
        self.stage = ""  # what it is doing right now
        self.last_error: str | None = None

    @classmethod
    def open(cls, cfg: BotConfig) -> LongRunMemory:
        from topstep_bot.knowledge import KnowledgeBase

        k = cfg.knowledge
        kb = KnowledgeBase(cfg.longrun_knowledge_path, half_life_days=k.half_life_days, min_samples=k.min_samples,
                           min_edge_r=k.min_edge_r, real_weight=k.real_trade_weight, max_observations=LONGRUN_MAX_OBSERVATIONS)
        return cls(MarketLibrary(cfg.library_path), kb, cfg.instrument.symbol, cfg.instrument.timeframe_minutes)

    def close(self) -> None:
        self.library.close()

    def remember(self, bars: Iterable[Bar], contract: str = "", span: tuple[datetime, datetime] | None = None) -> int:
        """Keep bars the bot downloaded anyway (warm-up, training, live). ``span`` = the range that was asked for."""
        try:
            added = self.library.add(self.symbol, self.tf, bars, contract)
            if span:
                self.library.note_span(self.symbol, self.tf, *span)
            return added
        except sqlite3.Error as exc:  # the library is a bonus: it must never disturb trading
            log.warning("Could not store bars in the market library: %s", exc)
            return 0

    def due(self, now: datetime, hours: float) -> bool:
        """True when the long-run replay is older than ``hours`` (or has never run)."""
        return self.knowledge.training_due(now, hours)

    async def backfill(self, client: ProjectXClient, contract: Contract, days: int, now: datetime, *, live: bool = False) -> int:
        """Download the history the library is missing, up to ``days`` back. Returns the number of new bars."""
        from topstep_bot.models import BarUnit

        tf = timedelta(minutes=self.tf)
        added = 0
        for start, end in self.library.missing(self.symbol, self.tf, now - timedelta(days=days), now, min_gap=2 * tf):
            self.stage = f"downloading {start:%Y-%m-%d} to {end:%Y-%m-%d}"
            bars = await client.retrieve_bars_range(contract.id, start, end, BarUnit.MINUTE, self.tf, live=live)
            closed = [b for b in bars if b.ts + tf <= now]
            added += self.library.add(self.symbol, self.tf, closed, contract.id)
            # Only the closed part counts as downloaded: the bar still forming is fetched next time.
            self.library.note_span(self.symbol, self.tf, start, min(end, now - tf))
        return added

    async def replay(self, cfg: BotConfig, contract: Contract, *, days: int | None = None, at: datetime | None = None,
                     progress: Callable[[float], None] | None = None) -> dict[str, Any]:
        """Replay the library (the last ``days``, default all of it) through every strategy into the long-run knowledge."""
        from topstep_bot.knowledge import train_from_bars

        start = (at or datetime.now(UTC)) - timedelta(days=days) if days else None
        bars = self.library.bars(self.symbol, self.tf, start)
        if not bars:
            raise ValueError("the market library is empty: nothing to learn from yet")
        self.stage = f"replaying {len(bars):,} bars through every strategy"
        return await train_from_bars(cfg, contract, bars, self.knowledge, progress=progress, at=at)

    async def learn(self, cfg: BotConfig, contract: Contract, client: ProjectXClient | None, now: datetime) -> LearnResult:
        """Backfill (when connected) and replay: the daily long-run learning run."""
        if self.running:
            raise RuntimeError("The bot is already learning from its long-run memory")
        self.running, self.progress, self.last_error = True, 0.0, None
        try:
            days = cfg.knowledge.deep_history_days
            downloaded = 0
            if client is not None:
                downloaded = await self.backfill(client, contract, days, now, live=cfg.data.live_market_data)
            await asyncio.sleep(0)

            def step(frac: float) -> None:
                self.progress = round(frac, 3)

            result = await self.replay(cfg, contract, days=days, at=now, progress=step)
            stats = self.library.stats(self.symbol, self.tf)
            return LearnResult(downloaded, stats["bars"], result["observations"], result["days"], result["from"], result["to"])
        except Exception as exc:
            self.last_error = str(exc)
            raise
        finally:
            self.running, self.progress, self.stage = False, None, ""

    def status(self) -> dict[str, Any]:
        """What the dashboard shows about the long-run memory."""
        kb = self.knowledge
        try:
            lib = self.library.stats(self.symbol, self.tf)
        except sqlite3.Error:
            lib = {"bars": 0, "first": None, "last": None, "days": 0, "mb": 0}
        return {"library": lib, "observations": len(kb.obs), "trained": kb.trained, "running": self.running,
                "progress": self.progress, "stage": self.stage, "error": self.last_error}


def import_csv(cfg: BotConfig, path: Path | str, naive_tz: str = "UTC") -> tuple[int, int]:
    """Add a CSV of bars (any timeframe at or below the bot's) to the market library. Returns (bars read, new bars)."""
    from topstep_bot.backtest.data import load_csv
    from topstep_bot.backtest.runner import prepare_bars

    bars = load_csv(path, naive_tz=naive_tz)
    if not bars:
        return 0, 0
    tf = cfg.instrument.timeframe_minutes
    prepared = prepare_bars(bars, tf)
    lib = MarketLibrary(cfg.library_path)
    try:
        added = lib.add(cfg.instrument.symbol, tf, prepared, f"csv:{Path(path).name}")
    finally:
        lib.close()
    return len(bars), added
