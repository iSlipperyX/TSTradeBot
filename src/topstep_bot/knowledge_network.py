"""The knowledge network: what the bot has learned on every market, shared between related markets.

Each market the bot trades keeps its own knowledge base (``knowledge_<SYMBOL>_<bars>m.json``): every
signal of every strategy and how it ended. The probability calculator (probability.py) reads the
knowledge bases of the other markets of the same asset class too, at a fraction of the weight, so a
strategy that has never traded this market starts from what it did on related ones.

The network grows in three ways:

* **Trading.** Every market the bot trades (one or several at once) records every signal it sees.
* **Learning related markets.** Outside trading hours, while flat, the bot replays the price history
  already saved on this PC (TopstepX downloads, the Simulation Lab's "Download every market", the
  market pick) for related markets it doesn't trade, a few a day (``probability.grow_network``).
  This is the same training replay the traded markets get, so the outcomes are like-for-like.
  Nothing is traded; it only adds evidence.
* **Other accounts.** Every account's knowledge bases are read (one per market).

``status`` describes the network for the dashboard's Knowledge tab and the Ask answers.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from topstep_bot.instruments import CLASSES, SPECS, class_of
from topstep_bot.probability import knowledge_folders, network_paths

if TYPE_CHECKING:
    from topstep_bot.config import BotConfig

log = logging.getLogger("topstep_bot.knowledge_network")
UTC = timezone.utc

MIN_DAYS = 25  # trading days of saved history a related market needs before the bot learns it
STALE = timedelta(hours=20)  # a related market's knowledge older than this is relearned (when its turn comes)
GROWN = "knowledge network"  # marks the training origin of a market learned only for the network

_cache: dict[Path, tuple[tuple[int, int], dict[str, Any]]] = {}


def related(symbol: str) -> list[str]:
    """Every other market of this market's asset class."""
    cls = class_of(symbol)
    return [s for s, spec in SPECS.items() if spec.asset_class == cls and s != symbol.upper()]


def _file_stats(path: Path) -> dict[str, Any] | None:
    """What one knowledge base holds (cached until the file changes)."""
    try:
        st = path.stat()
    except OSError:
        return None
    stamp = (st.st_mtime_ns, st.st_size)
    hit = _cache.get(path)
    if hit and hit[0] == stamp:
        return hit[1]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    rows = data.get("observations") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        return None
    sources: dict[str, int] = {}
    wins = 0
    net = 0.0
    days = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        src = str(row.get("source") or "?")
        sources[src] = sources.get(src, 0) + 1
        r = float(row.get("r") or 0.0) - float(row.get("cost_r") or 0.0)
        wins += r > 0
        net += r
        days.add(row.get("day"))
    n = sum(sources.values())
    trained = data.get("trained") if isinstance(data.get("trained"), dict) else {}
    out = {"n": n, "sources": sources, "win_rate": round(wins / n, 3) if n else None,
           "avg_r": round(net / n, 3) if n else None, "days": len(days), "trained_at": trained.get("at"),
           "grown": GROWN in str(trained.get("origin") or ""),
           "updated": datetime.fromtimestamp(st.st_mtime, UTC).isoformat(timespec="minutes")}
    _cache[path] = (stamp, out)
    return out


def status(cfg: BotConfig, symbol: str, *, traded: list[str] | None = None) -> dict[str, Any]:
    """The network one market learns from: every related market's knowledge, and what could still be learned."""
    symbol = symbol.upper()
    tf = cfg.instrument.timeframe_minutes
    markets = []
    for path in network_paths(cfg, symbol):
        stats = _file_stats(path)
        if not stats or not stats["n"]:
            continue
        root = path.stem.split("_")[1]
        spec = SPECS.get(root)
        markets.append({"symbol": root, "name": spec.description if spec else root,
                        "account": path.parent.name if path.parent != cfg.data_path else "",
                        "traded": root in (traded or []), **stats})
    markets.sort(key=lambda m: -m["n"])
    known = {m["symbol"] for m in markets}
    learnable = []
    try:
        history = _history(cfg, tf)
    except Exception as exc:  # noqa: BLE001 - the library is optional here
        log.debug("Market library unavailable: %s", exc)
        history = {}
    for s in related(symbol):
        if s not in known and history.get(s, {}).get("days", 0) >= MIN_DAYS:
            learnable.append(s)
    own = _file_stats(cfg.for_market(symbol).knowledge_path)
    cls = class_of(symbol)
    return {"symbol": symbol, "asset_class": CLASSES.get(cls, cls), "markets": markets,
            "outcomes": sum(m["n"] for m in markets), "own": own, "learnable": learnable,
            "related": related(symbol), "enabled": cfg.probability.network, "grows": cfg.probability.grow_network}


def status_text(st: dict[str, Any]) -> str:
    if not st["enabled"]:
        return "The knowledge network is turned off (probability.network): each market learns only from itself."
    own = st.get("own") or {}
    lines = [f"Knowledge network for {st['symbol']} ({st['asset_class']}): {own.get('n', 0):,} outcomes on "
             f"{st['symbol']} itself, {st['outcomes']:,} on {len(st['markets'])} related market(s)."]
    for m in st["markets"][:10]:
        how = "learned from saved history" if m["grown"] else ("traded" if m["sources"].get("real") else "watched")
        where = f", account {m['account']}" if m["account"] else ""
        lines.append(f"  {m['symbol']} ({m['name']}): {m['n']:,} outcomes over {m['days']} days, "
                     f"{(m['win_rate'] or 0):.0%} won, {(m['avg_r'] or 0):+.2f}R a signal after costs ({how}{where})")
    if st["learnable"]:
        lines.append(f"Saved history the bot can still learn from: {', '.join(st['learnable'])}"
                     + (" (a few each night)." if st["grows"] else " (probability.grow_network is off)."))
    elif not st["markets"]:
        lines.append("No related market has a knowledge base yet: trade one, or download history on the Knowledge tab "
                     "(Simulations > Download every market) and the bot learns them outside trading hours.")
    lines.append("Related markets count at 30% of the weight of this market's own outcomes, and only until this "
                 "market has enough evidence of its own.")
    return "\n".join(lines)


def _history(cfg: BotConfig, tf: int) -> dict[str, dict[str, Any]]:
    from topstep_bot.memory import MarketLibrary

    if not cfg.library_path.exists():
        return {}
    library = MarketLibrary(cfg.library_path)
    try:
        return library.summary(tf)
    finally:
        library.close()


def _fresh(cfg: BotConfig, symbol: str, now: datetime) -> bool:
    """Some account already has recent knowledge of this market (traded, or learned for the network)."""
    name = f"knowledge_{symbol.upper()}_{cfg.instrument.timeframe_minutes}m.json"
    for folder in knowledge_folders(cfg):
        stats = _file_stats(folder / name)
        if not stats or not stats["trained_at"]:
            continue
        try:
            at = datetime.fromisoformat(stats["trained_at"])
        except ValueError:
            continue
        if at.tzinfo is None:
            at = at.replace(tzinfo=UTC)
        if now - at < STALE:
            return True
    return False


def candidates(cfg: BotConfig, traded: list[str], now: datetime, history: dict[str, dict[str, Any]]) -> list[str]:
    """Related markets worth learning now: enough saved history, no fresh knowledge anywhere, most history first."""
    wanted: list[str] = []
    for sym in traded:
        wanted += [s for s in related(sym) if s not in traded and s not in wanted]
    ok = [s for s in wanted if history.get(s, {}).get("days", 0) >= MIN_DAYS and not _fresh(cfg, s, now)]
    return sorted(ok, key=lambda s: -history[s]["days"])


async def grow(cfg: BotConfig, traded: list[str], now: datetime, *, limit: int | None = None,
               progress: Callable[[str], None] | None = None, news_archive: Any = None) -> list[dict[str, Any]]:
    """Learn up to ``limit`` related markets from the saved price history. Returns what was learned."""
    from topstep_bot.instruments import offline_contract
    from topstep_bot.knowledge import KnowledgeBase, train_from_bars
    from topstep_bot.memory import MarketLibrary

    limit = cfg.probability.grow_per_day if limit is None else limit
    if limit <= 0 or not cfg.library_path.exists():
        return []
    tf = cfg.instrument.timeframe_minutes
    library = MarketLibrary(cfg.library_path)
    learned: list[dict[str, Any]] = []
    try:
        history = library.summary(tf)
        for symbol in candidates(cfg, [t.upper() for t in traded], now, history):
            if len(learned) >= limit:
                break
            if progress:
                progress(symbol)
            bars = library.bars(symbol, tf, now - timedelta(days=cfg.knowledge.history_days), now)
            mcfg = cfg.for_market(symbol)
            kb = KnowledgeBase.from_config(mcfg, mcfg.knowledge_path)
            try:
                result = await train_from_bars(mcfg, offline_contract(symbol), bars, kb, at=now,
                                               origin=f"saved history, learned for the {GROWN}", news_archive=news_archive)
            except ValueError as exc:  # too little history after all
                log.info("Knowledge network: skipped %s (%s)", symbol, exc)
                continue
            learned.append({"symbol": symbol, "observations": result["observations"], "days": result["days"],
                            "from": result["from"], "to": result["to"]})
            log.info("Knowledge network: learned %s from %d days of saved history (%d outcomes)", symbol,
                     result["days"], result["observations"])
    finally:
        library.close()
    return learned
