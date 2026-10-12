"""Price history for every market Topstep permits, so the Simulation Lab and the market pick can look at
all of them.

Where bars come from, best first:

  TopstepX   the contract the bot would really trade (needs your API key; about 60 days of intraday
             history). Used when you download from the dashboard or the command line with a working login.
  Yahoo      Yahoo Finance's continuous front-month future (``ES=F``, ``6E=F``...), 60 days of 5-minute
             bars, no login needed. It only fills gaps: it never overwrites a bar TopstepX delivered.

Everything lands in the bot's market library (``data/market_library.sqlite``, memory.py) at the bot's bar
timeframe. The library only grows, so each refresh adds the newest days and the history gets longer over
time. Ten years of daily bars per market go to ``data/research`` for long-term context (how today's
volatility compares with the past).

Micro and mini contracts of one market share prices (MNQ and NQ move tick for tick), so each market is
stored under its own symbol from the same feed. Information only: nothing here trades.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import statistics
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from topstep_bot.models import Bar

if TYPE_CHECKING:
    from topstep_bot.api.rest import ProjectXClient
    from topstep_bot.config import BotConfig

log = logging.getLogger("topstep_bot.market_data")
UTC = timezone.utc

YAHOO_INTRADAY = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval={interval}&range={range}"
YAHOO_DAILY = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range=10y&interval=1d"
YAHOO_PAUSE = 0.35  # seconds between Yahoo requests: one market after another, politely
REFRESH_HOURS = 12  # the controller's background refresh: at most this often
TOPSTEPX_DAYS = 90  # how far back a TopstepX download reaches (it keeps about 60 days of intraday bars)


def data_root(symbol: str) -> str:
    """The market a symbol's prices come from (MNQ -> NQ, M6E -> 6E): micros share their mini's feed."""
    from topstep_bot.instruments import SPECS

    spec = SPECS.get(symbol.upper())
    return (spec.mini or spec.root) if spec else symbol.upper()


# Yahoo keeps only a day of daily bars for the micro crypto futures: their full-size twins have years.
YAHOO_DAILY_SYMBOL = {"MBT": "BTC=F", "MET": "ETH=F"}


def yahoo_symbol(symbol: str, daily: bool = False) -> str:
    root = data_root(symbol)
    return YAHOO_DAILY_SYMBOL.get(root, f"{root}=F") if daily else f"{root}=F"


def _interval(tf: int) -> tuple[str, str]:
    """Yahoo's finest bars that build the bot's timeframe, and how far back Yahoo keeps them."""
    if tf % 5 == 0:
        return "5m", "60d"
    if tf % 2 == 0:
        return "2m", "60d"
    return "1m", "7d"


def parse_yahoo_bars(data: dict, minutes: int, now: datetime) -> list[Bar]:
    """Closed bars from Yahoo's chart API (rows with a missing price and the bar still forming are skipped)."""
    try:
        res = data["chart"]["result"][0]
        ts, q = res.get("timestamp") or [], res["indicators"]["quote"][0]
    except (KeyError, IndexError, TypeError):
        return []
    out: list[Bar] = []
    step = timedelta(minutes=minutes)
    for i, t in enumerate(ts):
        vals = [(q.get(k) or [None] * len(ts))[i] if i < len(q.get(k) or []) else None for k in ("open", "high", "low", "close")]
        if None in vals:
            continue
        start = datetime.fromtimestamp(int(t), UTC)
        if start.second or start + step > now:  # an odd stamp is the live quote, not a bar
            continue
        o, h, lo, c = (float(v) for v in vals)
        vol = (q.get("volume") or [0] * len(ts))[i] if i < len(q.get("volume") or []) else 0
        out.append(Bar(start, o, max(h, o, c), min(lo, o, c), c, float(vol or 0)))
    return out


@dataclass
class Fetched:
    symbol: str
    source: str = ""  # TopstepX | Yahoo | "" (failed)
    new_bars: int = 0
    error: str = ""

    def line(self) -> str:
        if self.error and not self.source:
            return f"{self.symbol}: {self.error}"
        return f"{self.symbol}: {self.new_bars:,} new bars from {self.source}" + (f" ({self.error})" if self.error else "")


class MarketData:
    """Downloads and describes the history of every market (see the module docstring)."""

    def __init__(self, cfg: BotConfig, *, transport: httpx.AsyncBaseTransport | None = None):
        self.cfg = cfg
        self.transport = transport
        self.running = False
        self.stage = ""
        self.progress: tuple[int, int] = (0, 0)
        self.last_run: datetime | None = None
        self.last_results: list[Fetched] = []
        self.last_error: str | None = None

    @property
    def tf(self) -> int:
        return self.cfg.instrument.timeframe_minutes

    def _http(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=25, transport=self.transport, follow_redirects=True,
                                 headers={"User-Agent": "Mozilla/5.0 topstep-bot market-data"})

    # ---------------------------------------------------------------- downloading

    async def _yahoo(self, http: httpx.AsyncClient, symbol: str, library, now: datetime) -> int:
        from topstep_bot.backtest.runner import prepare_bars

        interval, span = _interval(self.tf)
        resp = await http.get(YAHOO_INTRADAY.format(symbol=yahoo_symbol(symbol), interval=interval, range=span))
        resp.raise_for_status()
        bars = parse_yahoo_bars(resp.json(), int(interval[:-1]), now)
        if not bars:
            raise ValueError("Yahoo returned no bars")
        bars = prepare_bars(bars, self.tf)
        cutoff = now - timedelta(minutes=self.tf)
        bars = [b for b in bars if b.ts <= cutoff]  # an aggregated bar must be complete
        return library.add(symbol, self.tf, bars, f"yahoo:{yahoo_symbol(symbol)}", replace=False)

    async def _topstepx(self, client: ProjectXClient, symbol: str, library, now: datetime) -> int:
        from topstep_bot.memory import LongRunMemory

        contract = await client.resolve_contract(symbol)
        return await LongRunMemory(library, None, symbol, self.tf).backfill(
            client, contract, TOPSTEPX_DAYS, now, live=self.cfg.data.live_market_data)

    async def _daily(self, http: httpx.AsyncClient, symbol: str) -> None:
        from topstep_bot.research import ResearchLibrary, parse_yahoo_daily

        root = data_root(symbol)
        lib = ResearchLibrary(self.research_folder)
        existing = lib.daily_path(root)
        with contextlib.suppress(OSError):
            if datetime.now(UTC).timestamp() - existing.stat().st_mtime < 20 * 3600:
                return  # fetched today already (by this or by the research collector)
        resp = await http.get(YAHOO_DAILY.format(symbol=yahoo_symbol(symbol, daily=True)))
        resp.raise_for_status()
        bars = parse_yahoo_daily(resp.json())
        if bars:
            lib.save_daily(root, bars)

    @property
    def research_folder(self) -> Path:
        return Path(self.cfg.data_dir) / "research"

    async def refresh(self, symbols: list[str] | None = None, *, client: ProjectXClient | None = None,
                      daily: bool = True, now: datetime | None = None,
                      progress: Callable[[int, int, str], None] | None = None) -> list[Fetched]:
        """Bring every market's history up to date. ``client``: a logged-in TopstepX client (optional)."""
        from topstep_bot.instruments import markets
        from topstep_bot.memory import MarketLibrary

        if self.running:
            raise RuntimeError("Market data is already being downloaded")
        syms = [s.upper() for s in (symbols or markets())]
        self.running, self.last_error, self.progress = True, None, (0, len(syms))
        library = MarketLibrary(self.cfg.library_path)
        results: list[Fetched] = []
        try:
            async with self._http() as http:
                for i, symbol in enumerate(syms):
                    self.stage, self.progress = f"downloading {symbol}", (i, len(syms))
                    if progress:
                        progress(i, len(syms), self.stage)
                    at = now or datetime.now(UTC)
                    got = Fetched(symbol)
                    if client is not None:
                        try:
                            got.new_bars, got.source = await self._topstepx(client, symbol, library, at), "TopstepX"
                        except Exception as exc:  # noqa: BLE001 - fall back to Yahoo for this market
                            got.error = f"TopstepX: {exc}"[:120]
                    if not got.source or got.new_bars == 0:
                        try:
                            n = await self._yahoo(http, symbol, library, at)
                            if got.source:
                                got.new_bars += n
                            else:
                                got.new_bars, got.source = n, "Yahoo"
                        except Exception as exc:  # noqa: BLE001 - one market failing never stops the rest
                            if not got.source:
                                got.error = (got.error + "; " if got.error else "") + f"Yahoo: {exc}"[:120]
                        await asyncio.sleep(YAHOO_PAUSE)
                    if daily:
                        try:
                            await self._daily(http, symbol)
                        except Exception as exc:  # noqa: BLE001 - daily context is a bonus
                            log.info("Daily history for %s failed: %s", symbol, exc)
                    results.append(got)
            self.last_results, self.last_run = results, datetime.now(UTC)
            if progress:
                progress(len(syms), len(syms), "done")
            return results
        except Exception as exc:
            self.last_error = str(exc)
            raise
        finally:
            library.close()
            self.running, self.stage = False, ""

    def due(self, now: datetime | None = None) -> bool:
        now = now or datetime.now(UTC)
        return self.last_run is None or now - self.last_run >= timedelta(hours=REFRESH_HOURS)

    async def run(self, check_every: float = 600.0) -> None:
        """The controller's background loop: refresh every market from Yahoo about twice a day."""
        await asyncio.sleep(90)  # let the bots start first
        while True:
            if self.due() and not self.running:
                try:
                    results = await self.refresh()
                    log.info("Market data refreshed: %s new bars across %d markets",
                             f"{sum(r.new_bars for r in results):,}", sum(1 for r in results if r.source))
                except Exception as exc:  # noqa: BLE001
                    log.info("Market data refresh failed: %s", exc)
                    self.last_run = datetime.now(UTC)  # try again after the usual wait, not in a tight loop
            await asyncio.sleep(check_every)

    # ---------------------------------------------------------------- describing

    def coverage(self) -> list[dict[str, Any]]:
        """One row per permitted market: what history the bot has and whether the account may trade it."""
        return coverage(self.cfg)

    def status(self) -> dict[str, Any]:
        done, total = self.progress
        return {"running": self.running, "stage": self.stage, "done": done, "total": total, "error": self.last_error,
                "last_run": self.last_run.isoformat(timespec="minutes") if self.last_run else None,
                "results": [r.line() for r in self.last_results if r.error or not r.source][:20]}


def coverage(cfg: BotConfig) -> list[dict[str, Any]]:
    """Every market Topstep permits, with the history on this PC and what the account may trade."""
    import sqlite3

    from topstep_bot.instruments import CLASSES, SPECS
    from topstep_bot.memory import MarketLibrary
    from topstep_bot.research import ResearchLibrary
    from topstep_bot.risk.topstep import PLANS, product_limit

    tf = cfg.instrument.timeframe_minutes
    summary: dict[str, dict[str, Any]] = {}
    if cfg.library_path.exists():
        lib = MarketLibrary(cfg.library_path)
        try:
            summary = lib.summary(tf)
        except sqlite3.Error as exc:
            log.warning("Could not read the market library: %s", exc)
        finally:
            lib.close()
    csvs = {p.name.split("_", 1)[0].upper() for p in cfg.data_path.glob("*_1m.csv")}
    research = ResearchLibrary(Path(cfg.data_dir) / "research")
    plan = PLANS[cfg.account.plan]
    rows = []
    for root, spec in SPECS.items():
        info = summary.get(root, {})
        cap = product_limit(root, plan)
        daily = research.load_daily(data_root(root))
        rows.append({
            "symbol": root, "description": spec.description, "class": spec.asset_class, "class_label": CLASSES[spec.asset_class],
            "exchange": spec.exchange, "tick_size": spec.tick_size, "tick_value": spec.tick_value, "fees": spec.fees_round_turn,
            "hours": f"{spec.rth_open}-{spec.market_close or spec.rth_close} CT", "early_close": spec.market_close,
            "bars": info.get("bars", 0), "days": info.get("days", 0), "first": info.get("first"), "last": info.get("last"),
            "sources": info.get("sources", []) + (["CSV file"] if root in csvs else []),
            "allowed": cap != 0, "cap": cap, "daily_years": round(len(daily) / 252, 1) if daily else 0,
            "context": daily_context(daily),
        })
    return rows


def daily_context(bars: list) -> dict[str, Any] | None:
    """Long-term context from daily bars: the last close, the 20-day trend, and how busy the market is now
    compared with its own past (the 20-day average range as a percentile of every 20-day stretch)."""
    if len(bars) < 60:
        return None
    ranges = [(b.high - b.low) / b.close for b in bars if b.close > 0]
    if len(ranges) < 60:
        return None
    window = 20
    rolling = [statistics.fmean(ranges[i - window:i]) for i in range(window, len(ranges) + 1)]
    now = rolling[-1]
    pctl = sum(1 for r in rolling if r < now) / len(rolling)
    last, before = bars[-1].close, bars[-1 - window].close if len(bars) > window else bars[0].close
    return {"close": bars[-1].close, "day": bars[-1].day, "trend_20d": round((last - before) / before, 4) if before else None,
            "range_pct": round(100 * now, 2), "range_percentile": round(pctl, 2)}
