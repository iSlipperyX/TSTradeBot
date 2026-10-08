"""Walk-forward tuning (`topstep-bot tune`): find the strategy and settings that held up on data they had never seen.

Tuning settings until a backtest looks perfect is the classic way to build a strategy that fails
live. Training here works the honest way:

1. Every candidate (a strategy plus one combination of its settings) is backtested once over the
   whole history with the bot's real trading code.
2. The history is cut into rolling windows: settings are *chosen* on a train window using only
   that window's results, then *scored* on the test window that follows, which the choice never saw.
3. Only those out-of-sample (OOS) test results count as evidence. A strategy is recommended only if
   its OOS results are positive, and the settings it recommends are the ones chosen on the most
   recent train window.

Past results - even out-of-sample - are no guarantee. Training says which approach has held up,
not what will happen next.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import math
import re
import statistics
from collections.abc import Callable
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

import yaml

from topstep_bot.config import BotConfig, load_config
from topstep_bot.models import Bar, Contract


def _grid(**axes: list) -> list[dict]:
    keys = list(axes)
    return [dict(zip(keys, values, strict=True)) for values in itertools.product(*axes.values())]


# Candidate settings per strategy. Kept deliberately small: the more combinations are tried, the
# more likely the "best" one only looks good by luck.
GRIDS: dict[str, list[dict]] = {
    "orb_momentum": (
        _grid(range_minutes=[5, 15], stop_mode=["range"], target_r=[0, 3, 10])
        + _grid(range_minutes=[5, 15], stop_mode=["atr"], atr_stop_frac=[0.05, 0.1, 0.2], target_r=[0, 3, 10])
    ),
    "noise_breakout": (
        _grid(exit_mode=["checkpoint"], band_mult=[0.75, 1.0, 1.25], check_every_minutes=[30, 60],
              trail_with_vwap=[True, False], stop_atr=[1.5, 2.0, 3.0])
        + _grid(exit_mode=["trail"], band_mult=[1.0], check_every_minutes=[30], trail_with_vwap=[True])
    ),
    "late_day_momentum": _grid(confirm_with_12th=[False, True], stop_atr=[1.5, 2.0, 3.0], min_move_pct=[0.0, 0.25]),
    "orb": _grid(range_minutes=[15, 30, 60], stop_mode=["middle", "opposite"], target_r=[1.0, 2.0, 4.0],
                 entry_cutoff=["11:00", "13:00"]),
    "ema_trend": [dict(fast=f, slow=s, trend=t, atr_stop_mult=a, target_r=r)
                  for (f, s), t, a, r in itertools.product([(9, 21), (20, 50)], [50, 200], [1.5, 3.0], [2.0, 4.0])],
    "vwap_reversion": [dict(band_k=k, stop_atr_mult=a, rsi_low=lo, rsi_high=hi)
                       for k, a, (lo, hi) in itertools.product([2.0, 2.5, 3.0], [1.0, 2.0], [(30, 70), (20, 80)])],
    "vwap_pullback": [],  # tested with its defaults only
}

MIN_HISTORY_DAYS = 80
TRADING_DAYS_PER_YEAR = 252


@dataclass(frozen=True)
class Candidate:
    strategy: str
    params: tuple[tuple[str, Any], ...]  # only the settings that differ from the strategy's defaults

    @property
    def params_dict(self) -> dict[str, Any]:
        return dict(self.params)

    @property
    def label(self) -> str:
        if not self.params:
            return f"{self.strategy} (defaults)"
        return f"{self.strategy} " + " ".join(f"{k}={v}" for k, v in self.params)


@dataclass
class Run:
    """One candidate backtested over the whole history: results per trading day."""

    candidate: Candidate
    days: list[date] = field(default_factory=list)
    pnl: dict[date, float] = field(default_factory=dict)
    worst: dict[date, float] = field(default_factory=dict)  # lowest intraday equity vs the day's start
    trades: dict[date, list[tuple[float, float]]] = field(default_factory=dict)  # (net P&L, R) per trade
    error: str | None = None


@dataclass(frozen=True)
class Fold:
    train: tuple[date, date]
    test: tuple[date, date]


@dataclass
class Stats:
    days: int = 0
    trades: int = 0
    net: float = 0.0
    profit_factor: float = 0.0
    win_rate: float = 0.0
    avg_r: float = 0.0
    sharpe: float = 0.0
    max_drawdown: float = 0.0
    combine_pass_rate: float | None = None
    combine_attempts: int = 0

    def to_dict(self) -> dict:
        d = dict(self.__dict__)
        if math.isinf(d["profit_factor"]):
            d["profit_factor"] = "inf"
        return d


@dataclass
class StrategyResult:
    strategy: str
    oos: Stats
    choices: list[tuple[Fold, Candidate, float]]  # per fold: the settings chosen on its train window
    final: Candidate | None  # chosen on the most recent train window: what to trade now
    final_train: Stats | None
    stability: float  # share of folds that chose the same settings as `final`
    n_candidates: int = 1  # settings tried for this strategy (1 = no selection happened)
    eligible: bool = False
    reason: str = ""
    oos_daily: list[tuple[date, float]] = field(default_factory=list)  # the test-window days, in order


@dataclass
class TrainingReport:
    first_day: date
    last_day: date
    folds: list[Fold]
    results: list[StrategyResult]
    recommended: StrategyResult | None
    current: Stats | None  # the configuration in config.yaml, over the same test windows
    current_label: str
    candidates: int
    errors: list[str]

    def to_dict(self) -> dict:
        def fold(f: Fold) -> dict:
            return {"train": [f.train[0].isoformat(), f.train[1].isoformat()],
                    "test": [f.test[0].isoformat(), f.test[1].isoformat()]}

        return {
            "generated": datetime.now().isoformat(timespec="seconds"),
            "history": [self.first_day.isoformat(), self.last_day.isoformat()],
            "folds": [fold(f) for f in self.folds],
            "candidates": self.candidates,
            "current": {"label": self.current_label, "oos": self.current.to_dict() if self.current else None},
            "recommended": self.recommended.strategy if self.recommended else None,
            "strategies": [
                {
                    "strategy": r.strategy, "eligible": r.eligible, "reason": r.reason, "oos": r.oos.to_dict(),
                    "final": {"strategy": r.final.strategy, "params": r.final.params_dict} if r.final else None,
                    "stability": round(r.stability, 2),
                    "choices": [{"test": fold(f)["test"], "chosen": c.label, "train_sharpe": round(s, 2)}
                                for f, c, s in r.choices],
                }
                for r in self.results
            ],
            "errors": self.errors,
        }


# ------------------------------------------------------------------ candidates

def _normalize(strategy: str, params: dict) -> tuple[tuple[str, Any], ...]:
    from topstep_bot.strategies import STRATEGIES

    defaults = STRATEGIES[strategy].defaults
    return tuple(sorted((k, v) for k, v in params.items() if defaults.get(k) != v))


def candidates(cfg: BotConfig, strategies: list[str] | None = None) -> list[Candidate]:
    """Every grid combination that is valid for the configured symbol and bar timeframe."""
    from topstep_bot.instruments import SPECS, offline_contract
    from topstep_bot.strategies import STRATEGIES, create_strategy

    names = strategies or list(GRIDS)
    unknown = [n for n in names if n not in STRATEGIES]
    if unknown:
        raise ValueError(f"Unknown strategy: {', '.join(unknown)}. Available: {', '.join(STRATEGIES)}")
    symbol = cfg.instrument.symbol
    contract = offline_contract(symbol) if symbol in SPECS else Contract("CHECK", symbol, 0.25, 1.0, root=symbol)
    out: list[Candidate] = []
    for name in names:
        for params in [{}] + GRIDS.get(name, []):
            try:
                create_strategy(name, params, contract, cfg.instrument.timeframe_minutes)
            except ValueError:
                continue  # e.g. a 5-minute range on 15-minute bars
            cand = Candidate(name, _normalize(name, params))
            if cand not in out:
                out.append(cand)
    return out


def current_candidate(cfg: BotConfig) -> Candidate:
    return Candidate(cfg.strategy.name, _normalize(cfg.strategy.name, cfg.strategy.params))


# --------------------------------------------------------------------- running

_BARS: list[Bar] = []
_CONTRACT: Contract | None = None
_CFG: dict = {}
_START: date | None = None


def _init_worker(cfg: dict, bars: list[Bar], contract: Contract, start: date) -> None:
    import logging

    global _BARS, _CONTRACT, _CFG, _START
    logging.disable(logging.WARNING)  # thousands of simulated orders would flood the console
    _BARS, _CONTRACT, _CFG, _START = bars, contract, cfg, start


def _run_one(cand: Candidate) -> Run:
    from topstep_bot.backtest.runner import run_backtest
    from topstep_bot.sessions import SessionSchedule

    run = Run(cand)
    raw = json.loads(json.dumps(_CFG))
    raw["strategy"] = {"name": cand.strategy, "params": cand.params_dict}
    try:
        cfg = BotConfig.model_validate(raw)
        res = asyncio.run(run_backtest(cfg, _BARS, _CONTRACT, start=_START, enforce_mll=False))
    except Exception as exc:  # noqa: BLE001 - one bad candidate must not stop training
        run.error = f"{cand.label}: {exc}"
        return run
    schedule = SessionSchedule(cfg.session)
    for d in res.days:
        run.days.append(d.day)
        run.pnl[d.day] = d.pnl
        run.worst[d.day] = d.min_equity - d.start_balance
    for t in res.trades:
        day = schedule.trading_day(t.closed_at or t.created_at)
        run.trades.setdefault(day, []).append((t.net_pnl, t.r_multiple() or 0.0))
    return run


def common_start(cfg: BotConfig, bars: list[Bar], cands: list[Candidate], contract: Contract) -> date:
    """First day every candidate can trade (after the longest warm-up), so all are judged on the same days."""
    from topstep_bot.sessions import SessionSchedule
    from topstep_bot.strategies import create_strategy

    warmup = max(create_strategy(c.strategy, c.params_dict, contract, cfg.instrument.timeframe_minutes).warmup_days
                 for c in cands)
    schedule = SessionSchedule(cfg.session)
    days = sorted({schedule.trading_day(b.ts) for b in bars})
    if len(days) <= warmup + MIN_HISTORY_DAYS:
        raise ValueError(f"Training needs at least {warmup + MIN_HISTORY_DAYS + 1} trading days of history "
                         f"(have {len(days)}). Download more with: topstep-bot download --days 365")
    return days[warmup]


def run_candidates(
    cfg: BotConfig,
    bars: list[Bar],
    contract: Contract,
    cands: list[Candidate],
    *,
    workers: int = 1,
    progress: Callable[[int, int], None] | None = None,
) -> tuple[list[Run], date]:
    from topstep_bot.backtest.runner import prepare_bars

    bars = prepare_bars(bars, cfg.instrument.timeframe_minutes)
    start = common_start(cfg, bars, cands, contract)
    raw = cfg.model_dump(mode="json")
    raw["news"]["enabled"] = False  # history has no news calendar; live trading still pauses for news
    runs: list[Run] = []
    if workers > 1:
        try:
            with ProcessPoolExecutor(workers, initializer=_init_worker, initargs=(raw, bars, contract, start)) as ex:
                for run in ex.map(_run_one, cands):
                    runs.append(run)
                    if progress:
                        progress(len(runs), len(cands))
            return runs, start
        except (OSError, RuntimeError):  # no multiprocessing available: fall back to one process
            runs = []
    _init_worker(raw, bars, contract, start)
    import logging

    try:
        for cand in cands:
            runs.append(_run_one(cand))
            if progress:
                progress(len(runs), len(cands))
    finally:
        logging.disable(logging.NOTSET)
    return runs, start


# --------------------------------------------------------------- walk-forward

def make_folds(days: list[date], folds: int = 4, train_multiple: int = 3) -> list[Fold]:
    """Rolling windows: each train window is ``train_multiple`` x as long as the test window after it."""
    n = len(days)
    test = n // (folds + train_multiple)
    if test < 10:
        raise ValueError(f"Not enough history for {folds} walk-forward windows ({n} trading days). "
                         "Download more, or use fewer --folds.")
    train = train_multiple * test
    out = []
    for k in range(folds):
        i = n - (folds - k) * test  # test windows end at the last day
        out.append(Fold((days[i - train], days[i - 1]), (days[i], days[min(i + test, n) - 1])))
    return out


def _window(run: Run, first: date, last: date) -> list[date]:
    return [d for d in run.days if first <= d <= last]


def sharpe(daily: list[float]) -> float:
    if len(daily) < 2:
        return -math.inf
    sd = statistics.pstdev(daily)
    return statistics.mean(daily) / sd * math.sqrt(TRADING_DAYS_PER_YEAR) if sd > 0 else -math.inf


def stats_for(runs_and_windows: list[tuple[Run, date, date]], plan=None) -> Stats:
    from topstep_bot.backtest.metrics import simulate_combine
    from topstep_bot.engine import DayRecord

    days: list[tuple[date, float, float, int]] = []
    trades: list[tuple[float, float]] = []
    for run, first, last in runs_and_windows:
        for d in _window(run, first, last):
            day_trades = run.trades.get(d, [])
            days.append((d, run.pnl[d], run.worst[d], len(day_trades)))
            trades.extend(day_trades)
    if not days:
        return Stats()
    pnls = [p for p, _ in trades]
    won, lost = sum(p for p in pnls if p > 0), -sum(p for p in pnls if p <= 0)
    eq = peak = dd = 0.0
    for _, p, _, _ in days:
        eq += p
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    ratio = sharpe([p for _, p, _, _ in days])
    s = Stats(
        days=len(days), trades=len(pnls), net=round(sum(p for _, p, _, _ in days), 2),
        profit_factor=round(won / lost, 2) if lost > 0 else (math.inf if won > 0 else 0.0),
        win_rate=round(sum(1 for p in pnls if p > 0) / len(pnls), 3) if pnls else 0.0,
        avg_r=round(statistics.mean(r for _, r in trades), 3) if trades else 0.0,
        sharpe=round(ratio, 2) if math.isfinite(ratio) else 0.0,
        max_drawdown=round(dd, 2),
    )
    if plan is not None:
        records = [DayRecord(d, 0.0, p, worst, n) for d, p, worst, n in days]
        outcomes = [simulate_combine(records, plan, i) for i in range(len(records))]
        decided = [o for o in outcomes if o.status != "incomplete"]
        s.combine_attempts = len(decided)
        if decided:
            s.combine_pass_rate = round(sum(o.status == "passed" for o in decided) / len(decided), 3)
    return s


def _choose(runs: list[Run], first: date, last: date, min_trades: int) -> tuple[Run, float] | None:
    best: tuple[float, int, Run] | None = None
    for run in runs:
        days = _window(run, first, last)
        n = sum(len(run.trades.get(d, [])) for d in days)
        if n < min_trades:
            continue
        score = sharpe([run.pnl[d] for d in days])
        key = (score, n)
        if best is None or key > best[:2]:
            best = (score, n, run)
    return (best[2], best[0]) if best else None


def walk_forward(
    runs: list[Run],
    folds: list[Fold],
    *,
    current: Candidate | None = None,
    plan=None,
    min_trades_per_100_days: int = 15,
) -> tuple[list[StrategyResult], StrategyResult | None, Stats | None]:
    def min_trades(n_days: int) -> int:
        return max(10, n_days * min_trades_per_100_days // 100)

    ok = [r for r in runs if r.error is None and r.days]
    results: list[StrategyResult] = []
    for strategy in dict.fromkeys(r.candidate.strategy for r in ok):
        mine = [r for r in ok if r.candidate.strategy == strategy]
        choices, oos_windows = [], []
        for f in folds:
            pick = _choose(mine, *f.train, min_trades(len(_window(mine[0], *f.train))))
            if pick is None:
                continue
            run, score = pick
            choices.append((f, run.candidate, score))
            oos_windows.append((run, *f.test))
        oos = stats_for(oos_windows, plan)
        oos_daily = [(d, run.pnl[d]) for run, first, last in oos_windows for d in _window(run, first, last)]
        # What to trade from now on: the settings chosen on the most recent train-length window.
        n_train = len(_window(mine[0], *folds[-1].train))
        recent = mine[0].days[-n_train:]
        final_pick = _choose(mine, recent[0], recent[-1], min_trades(n_train))
        final = final_pick[0].candidate if final_pick else None
        final_train = stats_for([(final_pick[0], recent[0], recent[-1])]) if final_pick else None
        stability = sum(1 for _, c, _ in choices if c == final) / len(choices) if choices else 0.0
        res = StrategyResult(strategy, oos, choices, final, final_train, stability, n_candidates=len(mine),
                             oos_daily=oos_daily)
        min_oos = max(15, oos.days * min_trades_per_100_days // 200)
        if len(choices) < len(folds) or oos.trades < min_oos:
            res.reason = "too few trades"
        elif oos.net <= 0:
            res.reason = "lost money"
        elif oos.profit_factor < 1.05:
            res.reason = "edge too thin"
        else:
            res.eligible = True
            res.reason = "held up"
        results.append(res)
    eligible = [r for r in results if r.eligible]
    recommended = max(eligible, key=lambda r: (r.oos.sharpe, r.oos.net)) if eligible else None
    current_stats = None
    if current is not None:
        run = next((r for r in ok if r.candidate == current), None)
        if run is not None:
            current_stats = stats_for([(run, *f.test) for f in folds], plan)
    return results, recommended, current_stats


def train(
    cfg: BotConfig,
    bars: list[Bar],
    contract: Contract,
    *,
    strategies: list[str] | None = None,
    folds: int = 4,
    workers: int = 1,
    progress: Callable[[int, int], None] | None = None,
) -> TrainingReport:
    from topstep_bot.risk.topstep import PLANS

    cands = candidates(cfg, strategies)
    cur = current_candidate(cfg)
    if cur not in cands:
        cands.append(cur)
    runs, start = run_candidates(cfg, bars, contract, cands, workers=workers, progress=progress)
    good = [r for r in runs if r.error is None and r.days]
    if not good:
        raise ValueError("No candidate could be backtested: " + "; ".join(r.error or "" for r in runs[:3]))
    days = sorted({d for r in good for d in r.days})
    fold_list = make_folds(days, folds)
    plan = PLANS[cfg.account.plan] if cfg.account.stage == "combine" else None
    results, recommended, current_stats = walk_forward(good, fold_list, current=cur, plan=plan)
    return TrainingReport(
        first_day=start, last_day=days[-1], folds=fold_list, results=results, recommended=recommended,
        current=current_stats, current_label=cur.label, candidates=len(cands),
        errors=[r.error for r in runs if r.error],
    )


# ------------------------------------------------------------- save settings

_TOP_LEVEL_KEY = re.compile(r"^[A-Za-z_][\w-]*\s*:")


def save_strategy(config_path: Path, strategy: str, params: dict, note: str = "") -> Path:
    """Write the strategy block into config.yaml, keeping every other line (and comment) as it was.

    A backup is saved as config.yaml.bak; if the result doesn't load, the backup is restored."""
    path = Path(config_path)
    original = path.read_text(encoding="utf-8") if path.exists() else ""
    lines = original.splitlines(keepends=True)
    block = yaml.safe_dump({"strategy": {"name": strategy, "params": params}}, sort_keys=False,
                           default_flow_style=None, width=1000)
    if note:
        block = f"# {note}\n{block}"
    start = next((i for i, line in enumerate(lines) if re.match(r"^strategy\s*:", line)), None)
    if start is None:
        new = original + ("" if original.endswith("\n") or not original else "\n") + "\n" + block
    else:
        end = start + 1
        while end < len(lines) and not _TOP_LEVEL_KEY.match(lines[end]):
            end += 1
        while end > start + 1 and lines[end - 1].strip() == "":
            end -= 1  # keep the blank line before the next section
        while start > 0 and lines[start - 1].startswith(("# Tuned ", "# Trained ")):
            start -= 1  # replace an earlier tuning note too
        new = "".join(lines[:start]) + block + "".join(lines[end:])
    backup = path.with_name(path.name + ".bak")
    if original:
        backup.write_text(original, encoding="utf-8")
    path.write_text(new, encoding="utf-8")
    try:
        cfg = load_config(path)
    except Exception:
        if original:
            path.write_text(original, encoding="utf-8")
        raise
    if cfg.strategy.name != strategy:
        path.write_text(original, encoding="utf-8")
        raise ValueError("Could not update the strategy in config.yaml - edit it by hand.")
    return backup
