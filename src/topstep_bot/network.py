"""The market network: every market Topstep permits, read together, so the bot sees the whole board.

The bot trades one market per account, but markets move together: the index futures follow each
other, bonds and gold rise when stocks are feared, the dollar moves every currency future, crude
leads the energy complex. This module reads everything the bot already collects into one picture:

  * every market's long-term state from ten years of daily prices (research.py): trend over 5 and
    20 days, how volatile it is now against its own decade (the volatility percentile), and how
    unusual the last day's move was (a z-score against the last 60 days);
  * the links between markets: 60-day correlations of daily moves, each market's closest peers,
    and the moves that ran against their usual peers (divergences worth knowing about);
  * the board's mood: a risk appetite reading from equities, bonds, the yen and gold, and how
    stressed the equity market is (risk-on / risk-off / mixed, calm to stressed);
  * the day ahead: the calendar events that matter for each market in the next 24 hours, and how
    busy its headline flow has been (the research library's archive);
  * where the bot's own evidence points: the Simulation Lab's best playbooks and pockets;
  * jump risk: how far each market can move against a stop between two prices (from its 5-minute
    history), which the risk manager adds to every worst-case loss so a sudden move can't carry a
    trade through the Maximum Loss Limit or the daily loss limits.

It is saved to ``data/network.json`` (the controller refreshes it every hour; ``topstep-bot
network`` builds it on demand) and read by every account's bot. It shows on the dashboard's
Market tab, in Telegram (/network), to the Ask answers and to the AI coach.

What it may change: only protection. Jump risk makes trades smaller; in the Daily profit mode a
stressed market halves the risk per trade. Nothing here makes the bot trade bigger or more often,
and nothing here picks a trade's direction: correlations and moods change, and reading them as
signals would be curve-fitting the past.
"""

from __future__ import annotations

import contextlib
import json
import logging
import math
import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from topstep_bot.instruments import CLASSES, SPECS, class_of

if TYPE_CHECKING:
    from topstep_bot.config import BotConfig
    from topstep_bot.models import Bar

log = logging.getLogger("topstep_bot.network")
UTC = timezone.utc
CT = ZoneInfo("America/Chicago")

FILE = "network.json"
VERSION = 1
MAX_AGE = timedelta(hours=36)  # an older picture is not used to size trades (the class defaults are)
CORR_DAYS = 60
MIN_OVERLAP = 40  # days two markets must share before their correlation counts
PEER_MIN = 0.5  # |correlation| for a market to count as a peer
DIVERGENCE_Z = 1.5  # a move this many standard deviations away from what its peers implied
STRESSED = 0.90  # volatility percentile above which a market counts as stressed
BUSY = 0.75
CALM = 0.25

# Worst-case jump against a stop, in ticks, when the history doesn't show more (the floors) and the
# most the history may raise it to (the caps). Stops are orders working at the exchange, so the risk
# is the gap between two trades in a fast market: news spikes in indexes and energy, limit-locked
# grains and livestock that can't be exited at all for a while.
JUMP_FLOOR_TICKS = {"equity": 8, "crypto": 10, "fx": 4, "energy": 10, "metals": 8, "rates": 3, "ags": 12}
JUMP_CAP_MULTIPLE = 4
JUMP_QUANTILE = 0.995  # the bar-to-bar gap this rare (one 5-minute bar in 200) sets the estimate
JUMP_RANGE_SHARE = 0.10  # ...or this share of a once-in-100-bars 5-minute range, whichever is larger

# Risk appetite: markets whose 20-day trend says "risk on" when up (+1) or "risk off" when up (-1).
APPETITE = {"ES": 1.0, "NQ": 1.0, "RTY": 1.0, "YM": 0.5, "ZN": -1.0, "6J": -1.0, "GC": -0.5, "6A": 0.5}


# ------------------------------------------------------------------------------------------- numbers

def _quantile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    xs = sorted(values)
    pos = q * (len(xs) - 1)
    lo, hi = math.floor(pos), math.ceil(pos)
    return xs[lo] + (xs[hi] - xs[lo]) * (pos - lo)


def _returns(closes: list[float]) -> list[float]:
    return [math.log(b / a) for a, b in zip(closes, closes[1:], strict=False) if a > 0 and b > 0]


def _corr(a: list[float], b: list[float]) -> float | None:
    if len(a) < MIN_OVERLAP or len(a) != len(b):
        return None
    try:
        return statistics.correlation(a, b)
    except statistics.StatisticsError:  # one side never moved
        return None


def vol_state(pctile: float | None) -> str:
    if pctile is None:
        return "unknown"
    if pctile >= STRESSED:
        return "stressed"
    if pctile >= BUSY:
        return "busy"
    if pctile <= CALM:
        return "calm"
    return "normal"


def daily_view(bars: list) -> dict[str, Any] | None:
    """One market's long-term state from its daily bars (needs about a year)."""
    if len(bars) < 120:
        return None
    closes = [b.close for b in bars]
    rets = _returns(closes)
    if len(rets) < 100:
        return None
    vols = [statistics.pstdev(rets[i - 20:i]) for i in range(20, len(rets) + 1)]
    vol_now = vols[-1]
    pctile = sum(1 for v in vols if v < vol_now) / len(vols)
    recent = rets[-60:]
    sd = statistics.pstdev(recent[:-1]) if len(recent) > 2 else 0.0
    last = closes[-1]

    def change(days: int) -> float | None:
        return round(last / closes[-1 - days] - 1, 4) if len(closes) > days and closes[-1 - days] > 0 else None

    sma20 = statistics.fmean(closes[-20:])
    sma50 = statistics.fmean(closes[-50:])
    if last > sma20 > sma50:
        trend = "up"
    elif last < sma20 < sma50:
        trend = "down"
    else:
        trend = "sideways"
    atr = statistics.fmean(b.high - b.low for b in bars[-14:])
    return {"day": bars[-1].day, "close": last, "chg_1d": change(1), "chg_5d": change(5), "chg_20d": change(20),
            "trend": trend, "vol_annual": round(vol_now * math.sqrt(252), 4), "vol_pctile": round(pctile, 2),
            "state": vol_state(pctile), "move_z": round(rets[-1] / sd, 2) if sd > 0 else None,
            "atr_points": round(atr, 6), "years": round(len(bars) / 252, 1)}


def jump_ticks(bars: list[Bar], tick_size: float, tf_minutes: int, asset_class: str) -> dict[str, Any]:
    """How far price can jump against a stop, in ticks, from 5-minute history (with a floor and a cap)."""
    floor = JUMP_FLOOR_TICKS.get(asset_class, 8)
    gaps: list[float] = []
    ranges: list[float] = []
    step = timedelta(minutes=tf_minutes)
    for prev, bar in zip(bars, bars[1:], strict=False):
        ranges.append((bar.high - bar.low) / tick_size)
        if bar.ts - prev.ts == step:  # consecutive bars only: no session breaks, no contract rolls
            gaps.append(abs(bar.open - prev.close) / tick_size)
    measured = None
    if len(gaps) >= 500:
        measured = max(_quantile(gaps, JUMP_QUANTILE), JUMP_RANGE_SHARE * _quantile(ranges, 0.99))
    ticks = floor if measured is None else min(max(floor, measured), floor * JUMP_CAP_MULTIPLE)
    return {"ticks": round(ticks, 1), "measured": None if measured is None else round(measured, 1), "floor": floor,
            "bars": len(bars), "basis": "history" if measured is not None and measured > floor else "floor"}


def default_jump_ticks(root: str) -> float:
    return float(JUMP_FLOOR_TICKS.get(class_of(root), 8))


# ------------------------------------------------------------------------------------------- building

@dataclass
class _Series:
    root: str
    days: list[str]
    closes: list[float]


def _aligned(a: _Series, b: _Series, window: int) -> tuple[list[float], list[float]]:
    """Daily returns of two markets on the days both traded, over the last ``window`` shared days."""
    common = sorted(set(a.days) & set(b.days))[-(window + 1):]
    ia, ib = dict(zip(a.days, a.closes, strict=False)), dict(zip(b.days, b.closes, strict=False))
    return _returns([ia[d] for d in common]), _returns([ib[d] for d in common])


def build(cfg: BotConfig, now: datetime | None = None) -> dict[str, Any]:
    """Read everything on this PC into the network picture (a few seconds; runs in a worker thread)."""
    from topstep_bot.market_data import data_root
    from topstep_bot.research import ResearchLibrary

    now = now or datetime.now(UTC)
    research = ResearchLibrary(Path(cfg.data_dir) / "research")
    markets: dict[str, dict[str, Any]] = {}
    series: dict[str, _Series] = {}
    for root, spec in SPECS.items():
        feed = data_root(root)
        bars = research.load_daily(feed)
        view = daily_view(bars) if bars else None
        markets[root] = {"symbol": root, "description": spec.description, "class": spec.asset_class,
                         "class_label": CLASSES[spec.asset_class], "feed": feed, "daily": view,
                         "jump": {"ticks": default_jump_ticks(root), "measured": None, "floor": default_jump_ticks(root),
                                  "bars": 0, "basis": "floor"},
                         "peers": [], "events": [], "headlines_24h": 0}
        if view and feed not in series:
            series[feed] = _Series(feed, [b.day for b in bars], [b.close for b in bars])
    _links(markets, series)
    _jumps(cfg, markets)
    _calendar(research, markets, now)
    _headlines(research, markets, now)
    board = _board(markets, series)
    return {"version": VERSION, "built": now.isoformat(timespec="seconds"), "board": board, "markets": markets,
            "lab": _lab(cfg), "coverage": {"daily": sum(1 for m in markets.values() if m["daily"]),
                                           "intraday": sum(1 for m in markets.values() if m["jump"]["bars"]),
                                           "markets": len(markets)}}


def _links(markets: dict[str, dict], series: dict[str, _Series]) -> None:
    """Each market's closest peers by 60-day correlation, and moves that went against them."""
    feeds = sorted(series)
    corr: dict[tuple[str, str], float] = {}
    rets: dict[tuple[str, str], tuple[list[float], list[float]]] = {}
    for i, a in enumerate(feeds):
        for b in feeds[i + 1:]:
            ra, rb = _aligned(series[a], series[b], CORR_DAYS)
            c = _corr(ra, rb)
            if c is not None:
                corr[(a, b)] = corr[(b, a)] = c
                rets[(a, b)], rets[(b, a)] = (ra, rb), (rb, ra)
    for m in markets.values():
        feed = m["feed"]
        if feed not in series:
            continue
        peers = sorted(((other, c) for (x, other), c in corr.items() if x == feed and abs(c) >= PEER_MIN),
                       key=lambda t: -abs(t[1]))[:4]
        m["peers"] = [{"symbol": p, "corr": round(c, 2)} for p, c in peers]
        # divergence: today's move against what the closest peer's move implied (beta x peer move)
        if peers:
            other, c = peers[0]
            mine, theirs = rets[(feed, other)]
            if len(mine) > 20 and statistics.pstdev(theirs) > 0:
                beta = c * statistics.pstdev(mine) / statistics.pstdev(theirs)
                resid = [x - beta * y for x, y in zip(mine, theirs, strict=False)]
                sd = statistics.pstdev(resid[:-1])
                if sd > 0:
                    z = resid[-1] / sd
                    if abs(z) >= DIVERGENCE_Z:
                        m["divergence"] = {"peer": other, "z": round(z, 2),
                                           "text": f"moved {'up' if z > 0 else 'down'} {abs(z):.1f} standard deviations away "
                                                   f"from what {other} implied"}


def _jumps(cfg: BotConfig, markets: dict[str, dict]) -> None:
    """Jump risk for every market with 5-minute history in the market library."""
    import sqlite3

    from topstep_bot.memory import MarketLibrary

    if not cfg.library_path.exists():
        return
    tf = cfg.instrument.timeframe_minutes
    start = datetime.now(UTC) - timedelta(days=120)
    lib = MarketLibrary(cfg.library_path)
    try:
        have = set(lib.symbols(tf))
        for root, m in markets.items():
            if root in have:
                bars = lib.bars(root, tf, start)
                if bars:
                    m["jump"] = jump_ticks(bars, SPECS[root].tick_size, tf, SPECS[root].asset_class)
    except sqlite3.Error as exc:
        log.warning("Network: could not read the market library: %s", exc)
    finally:
        lib.close()


def _calendar(research, markets: dict[str, dict], now: datetime) -> None:
    from topstep_bot.market_knowledge import event_matters
    from topstep_bot.news import parse_feed

    raw = research.load_calendar().get("events", {})
    items = list(raw.values()) if isinstance(raw, dict) else list(raw)
    soon = [e for e in parse_feed(items) if now <= e.time <= now + timedelta(hours=24)]
    for root, m in markets.items():
        hits = [e for e in soon if (e.impact == "High" and e.country == "USD") or event_matters(e.title, root)]
        m["events"] = [{"title": e.label, "impact": e.impact, "time": e.time.astimezone(CT).strftime("%a %H:%M CT")}
                       for e in hits[:6]]


def _headlines(research, markets: dict[str, dict], now: datetime) -> None:
    from topstep_bot.market_knowledge import product_for

    since = (now - timedelta(hours=24)).isoformat(timespec="minutes")
    for root, m in markets.items():
        product = product_for(root)
        path = research.headlines_path(product.root if product else m["feed"])
        n = 0
        with contextlib.suppress(OSError):
            for line in path.read_text(encoding="utf-8").splitlines()[-400:]:
                with contextlib.suppress(ValueError):
                    if str(json.loads(line).get("seen", "")) >= since:
                        n += 1
        m["headlines_24h"] = n


def _board(markets: dict[str, dict], series: dict[str, _Series]) -> dict[str, Any]:
    """The whole board in a few words: risk appetite, equity stress, what moved, which classes are busy."""
    score, weight, parts = 0.0, 0.0, []
    for root, w in APPETITE.items():
        view = (markets.get(root) or {}).get("daily")
        if not view or view.get("chg_20d") is None:
            continue
        direction = 1 if view["chg_20d"] > 0 else -1 if view["chg_20d"] < 0 else 0
        score += w * direction
        weight += abs(w)
        parts.append(f"{root} {view['chg_20d']:+.1%}")
    appetite = score / weight if weight else None
    eq = [markets[r]["daily"]["vol_pctile"] for r in ("ES", "NQ", "RTY") if markets.get(r, {}).get("daily")]
    eq_stress = statistics.fmean(eq) if eq else None
    if appetite is None:
        mood = "unknown"
    elif eq_stress is not None and eq_stress >= STRESSED and appetite < 0:
        mood = "risk-off, stressed"
    elif appetite >= 0.3:
        mood = "risk-on"
    elif appetite <= -0.3:
        mood = "risk-off"
    else:
        mood = "mixed"
    movers = sorted((m for m in markets.values() if m["daily"] and m["daily"].get("move_z") is not None
                     and m["symbol"] == m["feed"]), key=lambda m: -abs(m["daily"]["move_z"]))[:5]
    classes = {}
    for key, label in CLASSES.items():
        views = [m["daily"] for m in markets.values() if m["class"] == key and m["daily"] and m["symbol"] == m["feed"]]
        if views:
            classes[key] = {"label": label, "markets": len(views),
                            "vol_pctile": round(statistics.fmean(v["vol_pctile"] for v in views), 2),
                            "up": sum(v["trend"] == "up" for v in views), "down": sum(v["trend"] == "down" for v in views)}
    for c in classes.values():
        c["state"] = vol_state(c["vol_pctile"])
    return {"mood": mood, "appetite": None if appetite is None else round(appetite, 2), "appetite_basis": parts,
            "equity_vol_pctile": None if eq_stress is None else round(eq_stress, 2), "equity_state": vol_state(eq_stress),
            "movers": [{"symbol": m["symbol"], "z": m["daily"]["move_z"], "chg_1d": m["daily"]["chg_1d"]} for m in movers],
            "classes": classes, "links": len(series)}


def _lab(cfg: BotConfig) -> dict[str, Any] | None:
    """The Simulation Lab's best playbooks and pockets, when it has run."""
    try:
        from topstep_bot.simlab import load_report

        report = load_report(cfg)
    except Exception:  # noqa: BLE001 - the lab is a bonus here
        return None
    if not report:
        return None
    plays = [{"symbol": p.get("symbol"), "strategy": p.get("strategy"), "verdict": p.get("verdict"),
              "net": p.get("net"), "trades": p.get("trades")} for p in (report.get("playbooks") or [])[:5]]
    opp = report.get("opportunities") or {}
    pockets = opp.get("items") if isinstance(opp, dict) else opp
    pockets = [{"label": o.get("label"), "strength": o.get("strength"), "net": o.get("net"), "trades": o.get("trades")}
               for o in (pockets or [])[:5] if isinstance(o, dict)]
    return {"generated": report.get("generated"), "playbooks": plays, "pockets": pockets,
            "pockets_text": opp.get("text") if isinstance(opp, dict) else None}


# ------------------------------------------------------------------------------------------- reading

def path_for(cfg: BotConfig) -> Path:
    return Path(cfg.data_dir) / FILE


def save(cfg: BotConfig, picture: dict[str, Any]) -> None:
    path = path_for(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(picture, default=str), encoding="utf-8")
    tmp.replace(path)


def load(cfg: BotConfig) -> dict[str, Any] | None:
    try:
        data = json.loads(path_for(cfg).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("version") == VERSION else None


def refresh(cfg: BotConfig) -> dict[str, Any]:
    picture = build(cfg)
    save(cfg, picture)
    return picture


def fresh(picture: dict[str, Any] | None, now: datetime | None = None) -> bool:
    if not picture:
        return False
    try:
        built = datetime.fromisoformat(picture["built"])
    except (KeyError, ValueError):
        return False
    return (now or datetime.now(UTC)) - built <= MAX_AGE


@dataclass(frozen=True)
class MarketRisk:
    """What the network tells the risk manager about one market (protection only)."""

    jump_ticks: float
    stressed: bool
    why: str


def market_risk(cfg: BotConfig, root: str, picture: dict[str, Any] | None = None) -> MarketRisk:
    """Jump allowance and stress for ``root``: from a fresh network picture, else the class defaults."""
    root = root.upper()
    picture = picture if picture is not None else load(cfg)
    m = (picture or {}).get("markets", {}).get(root) if fresh(picture) else None
    if not m:
        return MarketRisk(default_jump_ticks(root), False, "default jump allowance (no network picture yet)")
    jump = float(m["jump"]["ticks"])
    view = m.get("daily") or {}
    stressed = view.get("state") == "stressed"
    why = f"jump allowance {jump:g} ticks ({'from its 5-minute history' if m['jump']['basis'] == 'history' else 'class floor'})"
    if stressed:
        why += f"; volatility in the top {100 - round(100 * view['vol_pctile'])}% of its last {view.get('years', 10):g} years"
    return MarketRisk(jump, stressed, why)


# ------------------------------------------------------------------------------------------- words

def _pct(x: float | None) -> str:
    return "–" if x is None else f"{x:+.1%}"


def market_line(m: dict[str, Any]) -> str:
    v = m.get("daily")
    if not v:
        return f"{m['symbol']}: no daily history yet"
    line = (f"{m['symbol']}: {v['trend']}, {_pct(v['chg_1d'])} day / {_pct(v['chg_20d'])} 20 days, volatility {v['state']} "
            f"({round(100 * v['vol_pctile'])}th percentile)")
    if m.get("peers"):
        line += "; moves with " + ", ".join(f"{p['symbol']} ({p['corr']:+.2f})" for p in m["peers"][:3])
    if m.get("divergence"):
        line += f"; {m['divergence']['text']}"
    return line


def focus_for(symbol: str | None) -> list[str]:
    """The network rows for a traded symbol: its own (MNQ) and, for a micro, the market it follows (NQ)."""
    from topstep_bot.market_knowledge import product_for

    out: list[str] = []
    sym = (symbol or "").strip().upper()
    if sym in SPECS:
        out.append(sym)
    p = product_for(sym) if sym else None
    if p and p.root not in out:
        out.append(p.root)
    return out


def text(picture: dict[str, Any] | None, focus: list[str] | None = None) -> str:
    """The network in plain words (Telegram /network, the Ask answers, the coach)."""
    if not picture:
        return ("No market network yet. It builds from the daily price history the bot downloads (Simulations tab > "
                "Download history, or wait for the background refresh), then refreshes every hour.")
    b = picture["board"]
    built = picture.get("built", "")[:16].replace("T", " ")
    lines = [f"Market network ({picture['coverage']['daily']} of {picture['coverage']['markets']} markets with daily history, "
             f"built {built} UTC)"]
    mood = b["mood"]
    if b.get("appetite") is not None:
        mood += f" (appetite {b['appetite']:+.2f} from {', '.join(b['appetite_basis'][:6])})"
    lines.append(f"Board: {mood}. Equity volatility: {b['equity_state']}.")
    if b.get("movers"):
        lines.append("Biggest moves vs normal: " + ", ".join(f"{x['symbol']} {_pct(x['chg_1d'])} ({x['z']:+.1f} sd)"
                                                             for x in b["movers"]))
    busy = [c["label"] for c in b.get("classes", {}).values() if c["state"] in ("busy", "stressed")]
    if busy:
        lines.append("Busy asset classes: " + ", ".join(busy) + ".")
    markets = picture["markets"]
    for root in focus or []:
        m = markets.get(root.upper())
        if m:
            lines.append(market_line(m))
            if m.get("events"):
                lines.append(f"  next 24h for {m['symbol']}: " + "; ".join(f"{e['time']} {e['title']}" for e in m["events"][:4]))
            lines.append(f"  jump allowance {m['jump']['ticks']:g} ticks ({m['jump']['basis']}), "
                         f"{m['headlines_24h']} headlines in 24h")
    diverging = [m for m in markets.values() if m.get("divergence") and m["symbol"] == m["feed"]][:4]
    if diverging:
        lines.append("Moving against their peers: " + "; ".join(f"{m['symbol']} {m['divergence']['text']}" for m in diverging))
    lab = picture.get("lab")
    if lab and lab.get("playbooks"):
        lines.append("Simulation Lab's best playbooks: " + ", ".join(f"{p['symbol']} {p['strategy']}" for p in lab["playbooks"][:3]))
    lines.append("Information only: the network sizes trades down for jump risk and stress, never up, and never picks a direction.")
    return "\n".join(lines)


def view(picture: dict[str, Any] | None, focus: list[str] | None = None) -> dict[str, Any] | None:
    """What the dashboard's Market network panel shows."""
    if not picture:
        return None
    markets = picture["markets"]
    rows = []
    for root, m in markets.items():
        v = m.get("daily") or {}
        rows.append({"symbol": root, "feed": m["feed"], "class": m["class"], "class_label": m["class_label"], "trend": v.get("trend"),
                     "chg_1d": v.get("chg_1d"), "chg_20d": v.get("chg_20d"), "state": v.get("state", "unknown"),
                     "vol_pctile": v.get("vol_pctile"), "move_z": v.get("move_z"),
                     "peers": [p["symbol"] for p in m.get("peers", [])[:3]], "divergence": (m.get("divergence") or {}).get("text"),
                     "events": len(m.get("events", [])), "headlines": m.get("headlines_24h", 0),
                     "jump": m["jump"]["ticks"], "focus": root in (focus or [])})
    return {"built": picture.get("built"), "fresh": fresh(picture), "board": picture["board"], "rows": rows,
            "coverage": picture["coverage"], "lab": picture.get("lab"), "text": text(picture, focus)}
