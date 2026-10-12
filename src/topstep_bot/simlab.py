"""Simulation Lab: run the real bot through history as if it were trading live, many times over.

Every simulation is a fresh Topstep Combine started on a different day. It is played bar by bar through
the bot's own trading core (the same code that trades live): its risk manager, Topstep's rules (the
trailing Maximum Loss Limit, the daily limits, the 15:10 CT flatten), simulated fills with slippage and
fees. A simulation ends when the Combine is passed or failed, or when the history runs out.

Markets: by default every market Topstep permits that has history on this PC (``lab.markets: all``):
equity indexes, crypto, currencies, energy, metals, interest rates and agriculture. market_data.py
downloads that history (TopstepX when logged in, else Yahoo). Markets your plan can't trade right now are
still simulated, so you can see what they would have done, but they are never recommended.

A *playbook* is a market plus a strategy (MNQ + orb_momentum, say). For each playbook the lab reports:

  live-like runs     the Combines started on different days, as described above
  resampled paths    thousands of Combines built by drawing whole trading days at random from the
                     playbook's own simulated days (Monte Carlo): a rough pass / bust probability
                     for a Combine longer than the history the bot has
  halves             the first half of the history against the second half: an edge that shows up in
                     only one half is probably luck
  held-out check     the playbook that looks best on the first half alone, scored on the second half
                     it never saw. With many playbooks tested, the overall winner looks better than it
                     is; this check says how much of its result survives.

Opportunities: every playbook's trades are also split by time of day (open, midday, close), so an edge
that exists only in one part of the day is not hidden by the rest. A pocket is listed when it made money
in both halves of the history; its strength says how far it stands out from chance, and the list says how
many pockets were tested, because some always look good by luck.

Adaptive playbooks start each simulation with what the bot would know on that morning: its knowledge
base is trained on the history before the start day only (no peeking), then keeps learning as the
simulation plays, exactly as it does live.

"Teaching" the bot (``teach``): every market's history is added to the long-run memory and replayed
through every strategy, so the knowledge reports cover all the markets the bot could trade, and the
report is saved where the dashboard and the AI coach read it. The lab never changes a setting or places
a trade by itself: a playbook becomes the bot's only when you apply it.

Past results are no promise. A few months of history is a small sample, and every result here says so.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import random
import statistics
import time
from collections.abc import Callable
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from topstep_bot.config import BotConfig
from topstep_bot.models import Bar

log = logging.getLogger("topstep_bot.simlab")
UTC = timezone.utc

LAB_DIR = "simlab"
REPORT_FILE = "report.json"
RUNS_FILE = "runs.csv"
HISTORY_KEPT = 20
MIN_RUNWAY_DAYS = 8  # a live-like run needs at least this many trading days ahead of its start
MIN_TRADES = 10  # fewer trades than this over the whole history: too few to say anything
BALANCE = 0.25  # an edge: the weaker half of the history made at least this share of the stronger half
VERDICT_ORDER = {"edge": 0, "mixed": 1, "losing": 2, "thin": 3}
MC_PATHS = 2000
MC_MAX_DAYS = 60  # a resampled Combine that hasn't passed or failed after this many days counts as open
DEFAULT_STARTS = 4  # live-like runs per playbook (with every market: about 50 x 8 x 4 = 1,600 simulations)
MC_PATHS_WEAK = 400  # fewer resampled paths for playbooks that lost money or traded too little
MIN_POCKET_TRADES = 8  # an opportunity (market x strategy x time of day) needs at least this many trades
POCKETS_KEPT = 60
CHANCE_PROMISING = 0.067  # share of edgeless pockets whose t-score clears 1.5 by luck (one-sided normal)


@dataclass(frozen=True)
class SimSpec:
    symbol: str
    strategy: str
    start: str  # ISO trading day the simulated Combine starts on ("" = the first day the strategy can trade)
    full: bool = False  # the whole history with the MLL off (for halves and resampling), not a Combine attempt
    params: str = ""  # strategy settings that differ from its defaults, as JSON ("" = the defaults)


@dataclass
class SimRun:
    symbol: str
    strategy: str
    start: str
    status: str  # passed | failed | open | error
    days: int = 0
    profit: float = 0.0
    trades: int = 0
    wins: int = 0
    best_day: float = 0.0
    worst_day: float = 0.0
    max_drawdown: float = 0.0
    end: str = ""
    detail: str = ""
    seconds: float = 0.0
    # (day, P&L, worst intraday equity vs the day's start, trades) - kept for the full-history runs only
    daily: list[tuple[str, float, float, int]] = field(default_factory=list)
    r_values: list[float] = field(default_factory=list)
    trade_pnls: list[float] = field(default_factory=list)  # each closed trade's net P&L (full-history runs)
    # (trading day, time-of-day slot, R, net P&L) per closed trade (full-history runs): the opportunity finder
    trade_slots: list[tuple[str, str, float | None, float]] = field(default_factory=list)


# ------------------------------------------------------------------------------------------ history


def history_sources(cfg: BotConfig) -> dict[str, list[Path]]:
    """Price history on this PC for each market: the downloaded 1-minute CSVs in the data folder."""
    out: dict[str, list[Path]] = {}
    for path in sorted(cfg.data_path.glob("*_1m.csv")):
        symbol = path.name.split("_", 1)[0].upper()
        out.setdefault(symbol, []).append(path)
    return out


def available_markets(cfg: BotConfig) -> list[str]:
    """Every permitted market with history on this PC: the CSVs plus the market library at the bot's timeframe."""
    import sqlite3

    from topstep_bot.instruments import SPECS
    from topstep_bot.memory import MarketLibrary

    have = set(history_sources(cfg))
    if cfg.library_path.exists():
        lib = MarketLibrary(cfg.library_path)
        try:
            have.update(lib.symbols(cfg.instrument.timeframe_minutes))
        except sqlite3.Error as exc:
            log.warning("Could not read the market library: %s", exc)
        finally:
            lib.close()
    return [s for s in SPECS if s in have]


def load_history(cfg: BotConfig, symbol: str) -> list[Bar]:
    """Every bar the bot has for ``symbol`` at its timeframe: the CSVs plus the long-run memory's library."""
    from topstep_bot.backtest.data import load_csv
    from topstep_bot.backtest.runner import prepare_bars
    from topstep_bot.memory import MarketLibrary

    tf = cfg.instrument.timeframe_minutes
    merged: dict[datetime, Bar] = {}
    for path in history_sources(cfg).get(symbol, []):
        try:
            for b in prepare_bars(load_csv(path), tf):
                merged[b.ts] = b
        except (OSError, ValueError) as exc:
            log.warning("Could not read %s: %s", path, exc)
    if cfg.library_path.exists():
        lib = MarketLibrary(cfg.library_path)
        try:
            for b in lib.bars(symbol, tf):
                merged.setdefault(b.ts, b)
        except Exception as exc:  # noqa: BLE001 - the library is a bonus
            log.warning("Could not read the market library: %s", exc)
        finally:
            lib.close()
    return [merged[k] for k in sorted(merged)]


def lab_symbols(cfg: BotConfig, available: list[str], scope: str | None = None) -> list[str]:
    """Markets to simulate: every market with history here (``lab.markets: all``), or only the ones this
    account may auto-pick plus its configured market (``tradable``)."""
    from topstep_bot.market_pick import candidates_for

    if (scope or cfg.lab.markets) == "all":
        return list(available)
    wanted = list(dict.fromkeys([*candidates_for(cfg), cfg.instrument.symbol]))
    return [s for s in wanted if s in available]


def lab_strategies(cfg: BotConfig) -> list[str]:
    from topstep_bot.instruments import offline_contract
    from topstep_bot.strategies import STRATEGIES, create_strategy

    out = []
    for name in STRATEGIES:
        try:
            create_strategy(name, {}, offline_contract("MNQ"), cfg.instrument.timeframe_minutes)
        except ValueError:
            continue  # this strategy can't run on the configured bar timeframe
        out.append(name)
    return out


def sim_config(cfg: BotConfig, symbol: str, strategy: str, params: dict[str, Any] | None = None) -> BotConfig:
    """The account's settings for one simulation: its plan, risk and rules, with this market and strategy."""
    sim = cfg.model_copy(deep=True)
    sim.mode = "paper"
    sim.instrument.symbol = symbol
    sim.instrument.auto_pick = False
    sim.instrument.contract_id = None
    sim.strategy.name, sim.strategy.params = strategy, dict(params or {})
    sim.news.enabled = False  # history has no live calendar; the live bot still steps aside for news
    sim.first_trade.enabled = False
    sim.account.stage = "combine"
    sim.account.mll_floor_override = None
    sim.account.starting_balance = None
    sim.risk.ramp_up_days = 0  # every simulated day is a normal day
    return sim


def plan_runs(trading_days: dict[str, list[date]], symbols: list[str], strategies: list[str], warmup: dict[str, int],
              starts: int, params: dict[str, dict[str, Any]] | None = None) -> list[SimSpec]:
    """Spread ``starts`` Combine start days evenly over each market's history, after each strategy's warm-up."""
    specs: list[SimSpec] = []
    for symbol in symbols:
        days = trading_days[symbol]
        for strategy in strategies:
            extra = json.dumps((params or {}).get(strategy) or {}, sort_keys=True)
            extra = "" if extra == "{}" else extra
            specs.append(SimSpec(symbol, strategy, "", full=True, params=extra))
            first = warmup.get(strategy, 0)
            last = len(days) - MIN_RUNWAY_DAYS
            if last <= first:
                continue
            n = max(1, min(starts, last - first + 1))
            picks = sorted({first + round(i * (last - first) / max(1, n - 1)) for i in range(n)}) if n > 1 else [first]
            specs.extend(SimSpec(symbol, strategy, days[i].isoformat(), params=extra) for i in picks)
    return specs


# ------------------------------------------------------------------------------------- one simulation

_BARS: dict[str, list[Bar]] = {}
_CFG: dict = {}


def _init_worker(cfg: dict, bars: dict[str, list[Bar]] | None) -> None:
    """``bars``: the history, or None to have each worker process read it from disk when first needed
    (with every market, sending it all to every process would cost more than the reading)."""
    global _BARS, _CFG
    logging.disable(logging.WARNING)  # thousands of simulated orders would flood the log
    _BARS, _CFG = (bars if bars is not None else {}), cfg


def _bars_for(symbol: str) -> list[Bar]:
    if symbol not in _BARS:
        _BARS[symbol] = load_history(BotConfig.model_validate(_CFG), symbol)
    return _BARS[symbol]


def _run_spec(spec: SimSpec) -> SimRun:
    return asyncio.run(_simulate(spec))


async def _simulate(spec: SimSpec) -> SimRun:
    from topstep_bot.backtest.metrics import simulate_combine
    from topstep_bot.backtest.runner import run_backtest
    from topstep_bot.instruments import offline_contract
    from topstep_bot.risk.topstep import PLANS

    t0 = time.perf_counter()
    run = SimRun(spec.symbol, spec.strategy, spec.start, "error")
    try:
        cfg = sim_config(BotConfig.model_validate(_CFG), spec.symbol, spec.strategy, json.loads(spec.params or "{}"))
        bars = _bars_for(spec.symbol)
        contract = offline_contract(spec.symbol)
        start = date.fromisoformat(spec.start) if spec.start else None
        prepare = None
        if spec.strategy == "adaptive" and start is not None:
            prepare = await _morning_knowledge(cfg, contract, bars, start)
        res = await run_backtest(cfg, bars, contract, start=start, enforce_mll=not spec.full, prepare=prepare)
    except Exception as exc:  # noqa: BLE001 - one broken simulation must not stop the lab
        run.detail = f"{type(exc).__name__}: {exc}"
        run.seconds = round(time.perf_counter() - t0, 2)
        return run
    days = res.days
    run.days = len(days)
    run.trades = len(res.trades)
    run.wins = sum(1 for t in res.trades if t.net_pnl > 0)
    run.r_values = [round(r, 3) for t in res.trades if (r := t.r_multiple()) is not None]
    run.trade_pnls = [round(t.net_pnl, 2) for t in res.trades]
    pnls = [d.pnl for d in days]
    run.best_day = round(max(pnls, default=0.0), 2)
    run.worst_day = round(min(pnls, default=0.0), 2)
    eq = peak = dd = 0.0
    for p in pnls:
        eq += p
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    run.max_drawdown = round(dd, 2)
    run.start = days[0].day.isoformat() if days else spec.start
    run.end = days[-1].day.isoformat() if days else ""
    if spec.full:
        run.status = "full"
        run.profit = round(sum(pnls), 2)
        run.trade_slots = _trade_slots(cfg, res.trades)
        run.daily = [(d.day.isoformat(), round(d.pnl, 2), round(d.min_equity - d.start_balance, 2), d.trades) for d in days]
    else:
        outcome = simulate_combine(days, PLANS[cfg.account.plan], 0) if days else None
        if outcome is None:
            run.status, run.detail = "open", "no trading days"
        else:
            run.status = "open" if outcome.status == "incomplete" else outcome.status
            run.days = outcome.days_used
            run.profit = round(outcome.profit, 2)
            run.detail = outcome.detail
    run.seconds = round(time.perf_counter() - t0, 2)
    return run


def _trade_slots(cfg: BotConfig, trades: list) -> list[tuple[str, str, float | None, float]]:
    from zoneinfo import ZoneInfo

    from topstep_bot.knowledge import slot_for

    tz = ZoneInfo(cfg.session.timezone)
    out = []
    for t in trades:
        when = (t.opened_at or t.created_at).astimezone(tz)
        r = t.r_multiple()
        out.append((when.date().isoformat(), slot_for(when.time()), round(r, 3) if r is not None else None, round(t.net_pnl, 2)))
    return out


async def _morning_knowledge(cfg: BotConfig, contract, bars: list[Bar], start: date):
    """What the adaptive strategy would know on the morning of ``start``: training on the days before it only."""
    from topstep_bot.knowledge import KnowledgeBase, train_from_bars
    from topstep_bot.sessions import SessionSchedule

    schedule = SessionSchedule(cfg.session)
    before = [b for b in bars if schedule.trading_day(b.ts) < start]
    kb = KnowledgeBase.from_config(cfg, None)
    try:
        await train_from_bars(cfg, contract, before, kb, at=before[-1].ts if before else None)
    except ValueError:
        return None  # too little history before this day: it starts knowing nothing, as the backtest does

    def attach(core) -> None:
        core.attach_knowledge(kb)

    return attach


# ----------------------------------------------------------------------------------------- the lab


def _resample(daily: list[tuple[str, float, float, int]], plan, rng: random.Random, paths: int) -> dict[str, Any]:
    """Monte Carlo Combines: random sequences of the playbook's own simulated days."""
    from topstep_bot.backtest.metrics import simulate_combine
    from topstep_bot.engine import DayRecord

    if not daily:
        return {"paths": 0}
    # Quiet days (no trade) are kept in proportion: they cost time, not money.
    pool = [DayRecord(date(2000, 1, 1), 0.0, p, worst, n) for _, p, worst, n in daily]
    passed = failed = 0
    pass_days: list[int] = []
    for _ in range(paths):
        seq = [pool[rng.randrange(len(pool))] for _ in range(MC_MAX_DAYS)]
        out = simulate_combine(seq, plan, 0)
        if out.status == "passed":
            passed += 1
            pass_days.append(out.days_used)
        elif out.status == "failed":
            failed += 1
    return {"paths": paths, "pass": round(passed / paths, 3), "fail": round(failed / paths, 3),
            "open": round((paths - passed - failed) / paths, 3),
            "median_days_to_pass": statistics.median(pass_days) if pass_days else None}


def _half_net(daily: list[tuple[str, float, float, int]]) -> tuple[float, float]:
    mid = len(daily) // 2
    return round(sum(p for _, p, _, _ in daily[:mid]), 2), round(sum(p for _, p, _, _ in daily[mid:]), 2)


def _verdict(pb: dict[str, Any]) -> tuple[str, str]:
    if pb["trades"] < MIN_TRADES:
        return "thin", f"only {pb['trades']} trades in the history: too few to judge"
    first, second = pb["halves"]
    if pb["net"] <= 0:
        return "losing", "lost money over the history after fees and slippage"
    if first <= 0 or second <= 0:
        return "mixed", "made money overall, but lost in one half of the history: likely luck"
    if min(first, second) < BALANCE * max(first, second):
        return "mixed", "made money in both halves, but nearly all of it in one: the other half was close to flat"
    if pb["profit_factor"] < 1.1:
        return "mixed", "made money in both halves, but the edge is very thin (profit factor under 1.1)"
    return "edge", "made money in both halves of the history after fees and slippage"


def summarize_playbooks(runs: list[SimRun], plan, seed: int = 7) -> list[dict[str, Any]]:
    from topstep_bot.instruments import CLASSES, SPECS
    from topstep_bot.risk.topstep import product_limit

    rng = random.Random(seed)
    by_key: dict[tuple[str, str], list[SimRun]] = {}
    for r in runs:
        by_key.setdefault((r.symbol, r.strategy), []).append(r)
    out = []
    for (symbol, strategy), group in by_key.items():
        full = next((r for r in group if r.status == "full"), None)
        combines = [r for r in group if r.status in ("passed", "failed", "open")]
        errors = [r for r in group if r.status == "error"]
        daily = full.daily if full else []
        trades = full.trades if full else 0
        wins = full.wins if full else 0
        rs = full.r_values if full else []
        tp = full.trade_pnls if full else []
        won = sum(x for x in tp if x > 0)
        lost = -sum(x for x in tp if x <= 0)
        pnl = [p for _, p, _, _ in daily]
        net = round(sum(pnl), 2)
        spec = SPECS.get(symbol)
        cls = spec.asset_class if spec else "equity"
        strong = net > 0 and trades >= MIN_TRADES
        pb: dict[str, Any] = {
            "symbol": symbol, "strategy": strategy, "label": f"{symbol} · {strategy}",
            "class": cls, "class_label": CLASSES.get(cls, cls), "description": spec.description if spec else symbol,
            "allowed": product_limit(symbol, plan) != 0,
            "days": len(daily), "trades": trades, "win_rate": round(wins / trades, 3) if trades else None,
            "net": net, "per_day": round(net / len(daily), 2) if daily else 0.0,
            "avg_r": round(statistics.mean(rs), 3) if rs else None,
            "profit_factor": round(won / lost, 2) if lost > 0 else (99.0 if won > 0 else 0.0),
            "max_drawdown": full.max_drawdown if full else 0.0,
            "worst_day": full.worst_day if full else 0.0, "best_day": full.best_day if full else 0.0,
            "halves": list(_half_net(daily)),
            "runs": {"total": len(combines), "passed": sum(r.status == "passed" for r in combines),
                     "failed": sum(r.status == "failed" for r in combines), "open": sum(r.status == "open" for r in combines),
                     "avg_profit": round(statistics.mean(r.profit for r in combines), 2) if combines else None},
            "resampled": _resample(daily, plan, rng, MC_PATHS if strong else MC_PATHS_WEAK) if daily else {"paths": 0},
            "errors": [e.detail for e in errors][:3],
            "history": [daily[0][0], daily[-1][0]] if daily else None,
            "equity": _equity(pnl),
        }
        pb["verdict"], pb["why"] = _verdict(pb)
        mc = pb["resampled"]
        pb["score"] = round((mc.get("pass", 0) - mc.get("fail", 0)) if mc.get("paths") else -1.0, 3)
        out.append(pb)
    out.sort(key=lambda p: (VERDICT_ORDER[p["verdict"]], -p["score"], -p["per_day"]))
    return out


def _equity(pnl: list[float], points: int = 60) -> list[float]:
    """The cumulative P&L, thinned to at most ``points`` values for a sparkline."""
    eq, out = 0.0, []
    for p in pnl:
        eq += p
        out.append(round(eq, 2))
    if len(out) <= points:
        return out
    step = len(out) / points
    return [out[min(len(out) - 1, math.floor(i * step))] for i in range(points)] + [out[-1]]


def find_opportunities(runs: list[SimRun], playbooks: list[dict[str, Any]]) -> dict[str, Any]:
    """Every market x strategy x time of day that made money in both halves of its history, strongest first.

    Strength is a t-like score: the average trade divided by its spread, times the square root of the
    number of trades. With hundreds of pockets tested some clear 2 by luck alone, which is why the
    report says how many were tested; only "strong" ones that also held up in both halves stand out."""
    by_pb = {(p["symbol"], p["strategy"]): p for p in playbooks}
    tested = 0
    found: list[dict[str, Any]] = []
    for r in runs:
        if r.status != "full" or not r.trade_slots:
            continue
        pb = by_pb.get((r.symbol, r.strategy))
        days = [d for d, *_ in r.daily]
        mid = days[len(days) // 2] if days else ""
        groups: dict[str, list[tuple[str, str, float | None, float]]] = {}
        for row in r.trade_slots:
            groups.setdefault(row[1], []).append(row)
        for slot, rows in groups.items():
            if slot == "off" or len(rows) < MIN_POCKET_TRADES:
                continue
            tested += 1
            pnls = [p for *_, p in rows]
            net = sum(pnls)
            first = sum(p for d, _, _, p in rows if d < mid)
            second = net - first
            if net <= 0 or first <= 0 or second <= 0:
                continue
            rs = [x for _, _, x, _ in rows if x is not None]
            sd = statistics.pstdev(pnls) if len(pnls) > 1 else 0.0
            t = (statistics.fmean(pnls) / sd * math.sqrt(len(pnls))) if sd > 0 else 0.0
            found.append({
                "symbol": r.symbol, "strategy": r.strategy, "slot": slot, "trades": len(rows),
                "label": f"{r.symbol} · {r.strategy} · {slot}",
                "class": pb["class"] if pb else "", "allowed": pb["allowed"] if pb else True,
                "net": round(net, 2), "per_trade": round(net / len(rows), 2),
                "avg_r": round(statistics.fmean(rs), 3) if rs else None,
                "win_rate": round(sum(1 for p in pnls if p > 0) / len(pnls), 3),
                "halves": [round(first, 2), round(second, 2)], "score": round(t, 2),
                "strength": "strong" if t >= 2.5 else "promising" if t >= 1.5 else "early",
                "playbook_verdict": pb["verdict"] if pb else None,
            })
    found.sort(key=lambda o: -o["score"])
    standout = sum(1 for o in found if o["strength"] != "early")
    by_chance = round(CHANCE_PROMISING * tested)
    if standout > by_chance:
        verdict = "more than luck alone would give: worth confirming in paper mode, strongest first."
    else:
        verdict = "no more than luck alone would give, so none of them is evidence of an edge yet: watch them, don't trade them."
    return {"tested": tested, "found": len(found), "standout": standout, "by_chance": by_chance, "items": found[:POCKETS_KEPT],
            "text": (f"{tested} pockets (market x strategy x time of day with at least {MIN_POCKET_TRADES} trades) were "
                     f"tested; {len(found)} made money in both halves of the history and {standout} of them clear the "
                     f"'promising' bar. If no pocket had a real edge, about {by_chance} would clear it by luck: {standout} is "
                     + verdict)}


def holdout_check(playbooks: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Pick on the first half of the history only, then look at the second half (which the pick never saw)."""
    eligible = [p for p in playbooks if p["trades"] >= MIN_TRADES and p["days"] >= 2 * MIN_RUNWAY_DAYS
                and p.get("allowed", True)]
    if not eligible:
        return None
    pick = max(eligible, key=lambda p: p["halves"][0])
    first, second = pick["halves"]
    held = second > 0
    return {"label": pick["label"], "symbol": pick["symbol"], "strategy": pick["strategy"], "first_half": first,
            "second_half": second, "held_up": held,
            "text": (f"{pick['label']} was the best playbook on the first half of the history (${first:,.0f}). "
                     + (f"On the second half, which that choice never saw, it made ${second:,.0f}: it held up."
                        if held else f"On the second half, which that choice never saw, it {'lost' if second < 0 else 'made'} "
                        f"${abs(second):,.0f}: the first-half winner did not hold up, so treat every ranking here with caution."))}


def recommendation(playbooks: list[dict[str, Any]], holdout: dict[str, Any] | None, cfg: BotConfig) -> dict[str, Any]:
    """What the lab suggests, in plain words. Never applied automatically."""
    playbooks = [p for p in playbooks if p.get("allowed", True)]  # never suggest a market the plan can't trade
    edges = [p for p in playbooks if p["verdict"] == "edge"]
    current = next((p for p in playbooks if p["symbol"] == cfg.instrument.symbol and p["strategy"] == cfg.strategy.name), None)
    now = ""
    if current is not None:
        now = (f" The bot's current playbook, {current['label']}, {'made' if current['net'] > 0 else 'lost'} "
               f"${abs(current['net']):,.0f} over {current['days']} days ({current['trades']} trades): {current['why']}.")
    if not edges:
        lead = next((p for p in playbooks if p["verdict"] == "mixed" and p["net"] > 0), None)
        hint = (f" The closest is {lead['label']} (${lead['net']:,.0f}; halves ${lead['halves'][0]:,.0f} / "
                f"${lead['halves'][1]:,.0f}): worth watching in paper mode, not yet worth trading." if lead else "")
        return {"kind": "none", "playbook": {"symbol": lead["symbol"], "strategy": lead["strategy"], "label": lead["label"]} if lead else None,
                "current": current["label"] if current else None, "same": bool(lead and current and lead["label"] == current["label"]),
                "text": "No playbook made steady money across both halves of the history after costs, so the bot has not "
                        "found a dependable edge in this data. Keep it in paper mode, or trade the smallest size, until one appears."
                        + hint + now}
    best = edges[0]
    mc = best["resampled"]
    caution = "" if holdout and holdout["held_up"] else " The held-out check did not confirm the ranking, so treat it as a lead to watch in paper mode."
    same = current is not None and current["label"] == best["label"]
    text = (f"{best['label']} has the best record: ${best['net']:,.0f} over {best['days']} days "
            f"({best['trades']} trades, both halves positive). Resampled Combines: "
            f"{round(100 * mc.get('pass', 0))}% pass, {round(100 * mc.get('fail', 0))}% fail within {MC_MAX_DAYS} days."
            + (" It is already what the bot trades." if same else now) + caution)
    return {"kind": "playbook", "playbook": {"symbol": best["symbol"], "strategy": best["strategy"], "label": best["label"]},
            "current": current["label"] if current else None, "same": same, "text": text}


async def run_lab(cfg: BotConfig, *, starts: int = DEFAULT_STARTS, symbols: list[str] | None = None,
                  strategies: list[str] | None = None, workers: int | None = None,
                  params: dict[str, dict[str, Any]] | None = None, risk_per_trade: float | None = None,
                  scope: str | None = None, classes: list[str] | None = None,
                  save: bool = True, progress: Callable[[int, int, str], None] | None = None) -> dict[str, Any]:
    """Run the simulations and return the report (saved as the lab's latest unless ``save`` is False).

    ``params``: strategy settings to try instead of the defaults, per strategy. ``risk_per_trade``:
    simulate a different risk per trade than the account's. Both are checked like config.yaml. ``scope``:
    "all" or "tradable" markets (default ``lab.markets``); ``classes``: only these asset classes (fx, metals...)."""
    from topstep_bot.instruments import offline_contract
    from topstep_bot.risk.topstep import PLANS
    from topstep_bot.sessions import SessionSchedule
    from topstep_bot.strategies import create_strategy

    t0 = time.perf_counter()
    if progress:
        progress(0, 1, "loading price history")
    from topstep_bot.instruments import SPECS, class_of

    available = available_markets(cfg)
    syms = [s.upper() for s in symbols] if symbols else lab_symbols(cfg, available, scope)
    if classes:
        wanted = {c.lower() for c in classes}
        syms = [s for s in syms if class_of(s) in wanted]
    bars = {}
    for i, s in enumerate(syms):
        if progress:
            progress(0, 1, f"loading price history ({s}, {i + 1} of {len(syms)})")
        if b := load_history(cfg, s):
            bars[s] = b
    if not bars:
        raise ValueError("No price history on this PC yet. Download it first: the Simulations tab's 'Download every "
                         "market' button, or topstep-bot markets --download.")
    strats = strategies or lab_strategies(cfg)
    unknown = [s for s in strats if s not in lab_strategies(cfg)]
    if unknown:
        raise ValueError(f"Unknown strategy: {', '.join(unknown)}. Available: {', '.join(lab_strategies(cfg))}")
    if params or risk_per_trade is not None:  # the same checks config.yaml gets, before any worker starts
        cfg = cfg.model_copy(deep=True)
        if risk_per_trade is not None:
            cfg.risk.risk_per_trade = float(risk_per_trade)
        for name, p in (params or {}).items():
            create_strategy(name, p, offline_contract("MNQ"), cfg.instrument.timeframe_minutes)
        BotConfig.model_validate(cfg.model_dump(mode="json"))
    schedule = SessionSchedule(cfg.session)
    trading_days = {s: sorted({schedule.trading_day(b.ts) for b in bs}) for s, bs in bars.items()}
    tf = cfg.instrument.timeframe_minutes
    warmup = {name: create_strategy(name, (params or {}).get(name) or {}, offline_contract("MNQ"), tf).warmup_days
              for name in strats}
    specs = plan_runs(trading_days, list(bars), strats, warmup, starts, params)
    raw = cfg.model_dump(mode="json")
    raw["accounts"] = []  # the shared settings: one account's plan and risk
    runs: list[SimRun] = []
    total = len(specs)
    n_workers = workers if workers is not None else max(1, min(8, (os.cpu_count() or 2) - 1))
    many = len(bars) > 6  # with many markets each worker reads its history itself (see _init_worker)

    def step() -> None:
        if progress:
            progress(len(runs), total, f"simulated {len(runs)} of {total}")

    loop = asyncio.get_running_loop()
    if n_workers > 1:
        try:
            with ProcessPoolExecutor(n_workers, initializer=_init_worker, initargs=(raw, None if many else bars)) as ex:
                futures = [loop.run_in_executor(ex, _run_spec, s) for s in specs]
                for fut in asyncio.as_completed(futures):
                    runs.append(await fut)
                    step()
        except (OSError, RuntimeError) as exc:  # no worker processes available: one process does it all
            log.info("Simulating in one process (%s)", exc)
            runs = []
    if not runs:
        _init_worker(raw, bars)
        try:
            for s in specs:
                runs.append(await _simulate(s))
                step()
                await asyncio.sleep(0)
        finally:
            logging.disable(logging.NOTSET)
    plan = PLANS[cfg.account.plan]
    if progress:
        progress(total, total, "scoring playbooks and opportunities")
    playbooks = summarize_playbooks(runs, plan)
    holdout = holdout_check(playbooks)
    opportunities = find_opportunities(runs, playbooks)
    missing = [s for s in SPECS if s not in bars]
    combines = [r for r in runs if r.status in ("passed", "failed", "open")]
    report = {
        "generated": datetime.now(UTC).isoformat(timespec="seconds"),
        "seconds": round(time.perf_counter() - t0, 1),
        "plan": plan.name, "profit_target": plan.profit_target, "max_loss_limit": plan.max_loss_limit,
        "risk_per_trade": cfg.risk.risk_per_trade, "timeframe": tf, "params": params or {},
        "markets": {s: {"bars": len(b), "days": len(trading_days[s]), "from": trading_days[s][0].isoformat(),
                        "to": trading_days[s][-1].isoformat(), "class": class_of(s)} for s, b in bars.items()},
        "coverage": {"simulated": len(bars), "permitted": len(SPECS), "missing": missing},
        "classes": _class_summary(playbooks),
        "opportunities": opportunities,
        "strategies": strats,
        "totals": {"simulations": len(combines), "full_histories": sum(r.status == "full" for r in runs),
                   "passed": sum(r.status == "passed" for r in combines), "failed": sum(r.status == "failed" for r in combines),
                   "open": sum(r.status == "open" for r in combines), "errors": sum(r.status == "error" for r in runs),
                   "trades": sum(r.trades for r in runs if r.status == "full"),
                   "resampled_paths": sum(p["resampled"].get("paths", 0) for p in playbooks)},
        "playbooks": playbooks,
        "holdout": holdout,
        "recommendation": recommendation(playbooks, holdout, cfg),
        "caveats": [
            f"{max((len(d) for d in trading_days.values()), default=0)} trading days of history is a small sample: results swing a lot with a few trades.",
            f"{len(playbooks)} playbooks were compared, so the best one looks better than it really is (the held-out check tests that).",
            "News days are not skipped in history; the live bot steps aside for scheduled news.",
            "Fills assume the configured slippage; real fills can be worse in fast markets.",
            "History from Yahoo is the continuous front-month future: around a contract roll it can differ from the "
            "contract TopstepX trades for a day or two.",
            "Each market uses its own contract specs and fees but your account's settings (risk per trade, trading window): "
            "a full-size contract whose stops are too wide for your risk simply takes no trade.",
        ],
        "runs": [_run_row(r) for r in runs if r.status != "full"],
    }
    if save:
        save_report(cfg, report)
    if progress:
        progress(total, total, "done")
    return report


def _class_summary(playbooks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Per asset class: how many playbooks, how many with an edge, and the best one."""
    from topstep_bot.instruments import CLASSES

    out = []
    for key, label in CLASSES.items():
        group = [p for p in playbooks if p.get("class") == key]
        if not group:
            continue
        best = group[0]  # playbooks are already ranked
        out.append({"class": key, "label": label, "markets": len({p["symbol"] for p in group}), "playbooks": len(group),
                    "edges": sum(p["verdict"] == "edge" for p in group), "net": round(sum(p["net"] for p in group), 2),
                    "best": {"label": best["label"], "verdict": best["verdict"], "net": best["net"]}})
    return out


def _run_row(r: SimRun) -> dict[str, Any]:
    d = asdict(r)
    d.pop("daily", None)
    d.pop("trade_slots", None)
    d.pop("r_values", None)
    d.pop("trade_pnls", None)
    return d


# ------------------------------------------------------------------------------------------ files


def lab_dir(cfg: BotConfig) -> Path:
    return cfg.data_path / LAB_DIR


def save_report(cfg: BotConfig, report: dict[str, Any]) -> Path:
    folder = lab_dir(cfg)
    (folder / "history").mkdir(parents=True, exist_ok=True)
    path = folder / REPORT_FILE
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(report, indent=1, default=str), encoding="utf-8")
    os.replace(tmp, path)
    stamp = report["generated"].replace(":", "").replace("-", "")[:15]
    slim = {k: v for k, v in report.items() if k != "runs"}
    (folder / "history" / f"report_{stamp}.json").write_text(json.dumps(slim, default=str), encoding="utf-8")
    old = sorted((folder / "history").glob("report_*.json"))[:-HISTORY_KEPT]
    for p in old:
        p.unlink(missing_ok=True)
    _write_runs_csv(folder / RUNS_FILE, report["runs"])
    return path


def _write_runs_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    import csv

    cols = ["symbol", "strategy", "start", "end", "status", "days", "profit", "trades", "wins", "best_day", "worst_day",
            "max_drawdown", "detail"]
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def load_report(cfg: BotConfig) -> dict[str, Any] | None:
    path = lab_dir(cfg) / REPORT_FILE
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def report_view(report: dict[str, Any] | None, runs: int = 0) -> dict[str, Any] | None:
    """The report without the per-run rows (the dashboard asks for those separately)."""
    if report is None:
        return None
    view = {k: v for k, v in report.items() if k != "runs"}
    if runs:
        view["runs"] = report.get("runs", [])[:runs]
    return view


def report_text(report: dict[str, Any] | None, top: int = 6) -> str:
    """A short plain-text version (Telegram, the console, and the AI coach)."""
    if not report:
        return "No simulations yet. Run them on the dashboard's Simulations tab or with: topstep-bot simulate"
    t = report["totals"]
    lines = [f"Simulation Lab ({report['generated'][:16].replace('T', ' ')} UTC): {t['simulations']} live-like Combines "
             f"on {len(report['markets'])} markets - {t['passed']} passed, {t['failed']} failed, {t['open']} still open "
             f"when the history ran out. {t['resampled_paths']:,} resampled Combine paths.",
             f"Plan {report['plan']}: target ${report['profit_target']:,.0f}, MLL ${report['max_loss_limit']:,.0f}, "
             f"risk ${report['risk_per_trade']:,.0f}/trade."]
    lines.append("Best playbooks:")
    for p in report["playbooks"][:top]:
        mc = p["resampled"]
        lines.append(f"  {p['label']}: {p['verdict']} - net ${p['net']:,.0f} over {p['days']}d, {p['trades']} trades, "
                     f"halves ${p['halves'][0]:,.0f} / ${p['halves'][1]:,.0f}, PF {p['profit_factor']}, "
                     f"runs {p['runs']['passed']}P/{p['runs']['failed']}F/{p['runs']['open']}O"
                     + (f", resampled pass {round(100 * mc['pass'])}% fail {round(100 * mc['fail'])}%" if mc.get("paths") else ""))
    opp = report.get("opportunities") or {}
    if opp.get("items"):
        lines.append("Opportunities (time-of-day pockets that made money in both halves):")
        for o in opp["items"][:top]:
            lines.append(f"  {o['label']}: {o['strength']} - ${o['net']:,.0f} over {o['trades']} trades, "
                         f"halves ${o['halves'][0]:,.0f} / ${o['halves'][1]:,.0f}"
                         + ("" if o.get("allowed", True) else " (your plan can't trade this market now)"))
        lines.append(f"  {opp['text']}")
    cov = report.get("coverage")
    if cov and cov.get("missing"):
        lines.append(f"No history yet for {len(cov['missing'])} of {cov['permitted']} markets: download it to include them.")
    if report.get("holdout"):
        lines.append("Held-out check: " + report["holdout"]["text"])
    lines.append("Suggestion: " + report["recommendation"]["text"])
    return "\n".join(lines)


# ------------------------------------------------------------------------------------- teaching


async def teach(cfg: BotConfig, symbols: list[str] | None = None,
                progress: Callable[[int, int, str], None] | None = None) -> list[dict[str, Any]]:
    """Add every market's history to the long-run memory and replay it through every strategy.

    The long-run reports then cover each market the bot could trade. The trading knowledge base the
    bot decides with is retrained by the bot itself from fresh history when it starts."""
    from topstep_bot.instruments import offline_contract
    from topstep_bot.memory import LongRunMemory, import_csv

    sources = history_sources(cfg)
    syms = symbols or lab_symbols(cfg, available_markets(cfg))
    out = []
    for i, symbol in enumerate(syms):
        if progress:
            progress(i, len(syms), f"teaching {symbol}")
        scfg = cfg.model_copy(deep=True)
        scfg.instrument.symbol = symbol
        added = 0
        for path in sources.get(symbol, []):
            try:
                added += import_csv(scfg, path)[1]
            except (OSError, ValueError) as exc:
                log.warning("Could not import %s: %s", path, exc)
        memory = LongRunMemory.open(scfg)
        try:
            bars = memory.library.bars(symbol, scfg.instrument.timeframe_minutes)
            if not bars:
                continue
            res = await memory.replay(scfg, offline_contract(symbol), at=bars[-1].ts)
            out.append({"symbol": symbol, "new_bars": added, "observations": res["observations"], "days": res["days"],
                        "from": res["from"], "to": res["to"]})
        except ValueError as exc:
            out.append({"symbol": symbol, "error": str(exc)})
        finally:
            memory.close()
    if progress:
        progress(len(syms), len(syms), "done")
    return out
