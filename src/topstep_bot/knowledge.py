"""The bot's knowledge base: what each strategy's signals have been worth, by time of day and regime.

The bot learns while it runs. Every recommendation (recommendations.py) - a trade the bot took,
a signal it skipped, or an idea from a shadow strategy - is followed to its outcome on real bars
and recorded here as an *observation*: strategy, time-of-day slot, volatility regime, result in R.
The adaptive strategy (strategies/adaptive.py) asks this base before taking a signal, so the bot
trades what has been working at this time of day in this kind of market and stays out of what
hasn't. It learns from signals it did not trade too, so it never has to "try" a bad idea.

Four sources of observations are kept apart:
  train   replayed from recent history by ``topstep-bot train`` (and automatically at startup
          when the base is stale - normally once a day after the close)
  shadow  hypothetical outcomes observed while running (stop / target / session end on real bars)
  real    the bot's own closed trades, which count double
  manual  trades you opened yourself from the dashboard's trade ticket (strategy "manual"). They
          never switch a strategy on or off; the ticket uses them to show your own record by time
          of day and regime next to what the strategies have done.

Observations fade with age (half-life ``half_life_days``), so the base keeps adapting as the
market changes. Retraining replaces the "train" layer and drops shadow observations the new
training already covers, so nothing is counted twice. Real and manual trades are never dropped.

Time slots (Chicago time): open 08:30-10:00, midday 10:00-13:00, close 13:00-15:10.
Regime: "volatile" when the 14-bar ATR (regular hours) is 20% or more above its multi-day average, else "calm".

Each observation also keeps what the decisions don't use yet: a market snapshot at signal time
(``ctx``, market_context.py), how far it went for and against it (MFE / MAE in R), how long it
lasted, its costs in R and, for real fills, the slippage. insights.py turns these into the
"What the bot learned" report. Files from before these fields existed still load.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field, fields
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from topstep_bot.indicators import ATR, EMA

if TYPE_CHECKING:
    from topstep_bot.config import BotConfig
    from topstep_bot.models import Bar, Contract

log = logging.getLogger("topstep_bot.knowledge")
UTC = timezone.utc

SLOTS: tuple[tuple[str, time, time], ...] = (
    ("open", time(8, 30), time(10, 0)),
    ("midday", time(10, 0), time(13, 0)),
    ("close", time(13, 0), time(15, 10)),
)
SLOT_NAMES = tuple(name for name, _, _ in SLOTS)
REGIMES = ("calm", "volatile")
SOURCES = ("train", "shadow", "real", "manual")
KEPT_SOURCES = ("real", "manual")  # actual fills: never pruned or replaced by retraining
MANUAL = "manual"  # strategy name of trades opened from the dashboard's trade ticket
VOLATILE_RATIO = 1.2
UNINFORMATIVE_EXITS = ("bot shutdown", "halted", "flatten requested", "daily maintenance", "expired", "entry not filled")


def slot_for(local_time: time) -> str:
    """Time-of-day slot for a Chicago wall-clock time ('off' outside regular hours)."""
    for name, start, end in SLOTS:
        if start <= local_time < end:
            return name
    return "off"


class RegimeTracker:
    """Classifies volatility: the 14-bar ATR against a slow average of the true range.

    Only regular-hours bars count, so the quiet overnight session doesn't make every day look
    volatile: the question is "is today busier than a normal trading day?".
    """

    def __init__(self, fast: int = 14, slow: int = 300):
        self.fast = ATR(fast)
        self.slow = EMA(slow)
        self._prev_close: float | None = None

    def update(self, high: float, low: float, close: float, rth: bool = True) -> str:
        tr = high - low if self._prev_close is None else max(high - low, abs(high - self._prev_close), abs(low - self._prev_close))
        self._prev_close = close
        if rth:
            self.fast.update(high, low, close)
            self.slow.update(tr)
        return self.value

    @property
    def ratio(self) -> float | None:
        if self.fast.value is None or not self.slow.value:
            return None
        return self.fast.value / self.slow.value

    @property
    def value(self) -> str:
        ratio = self.ratio
        return "volatile" if ratio is not None and ratio >= VOLATILE_RATIO else "calm"


@dataclass
class Observation:
    day: str  # trading day, ISO
    time: str  # signal time, HH:MM Chicago
    strategy: str
    side: str  # LONG | SHORT
    slot: str
    regime: str
    r: float  # result in R (risk multiples)
    usd: float | None
    source: str  # train | shadow | real | manual
    why: str = ""  # how it ended (stop hit, target hit, session end, ...)
    ctx: dict | None = None  # market snapshot at signal time (market_context.FEATURES)
    mfe_r: float | None = None  # furthest it went in its favour, in R (>= 0)
    mae_r: float | None = None  # furthest it went against it, in R (<= 0)
    bars: int | None = None  # bars it lasted
    cost_r: float | None = None  # costs not already in ``r``, in R: fees (+ assumed slippage for ideas)
    slip_in: float | None = None  # real fills: entry slippage in ticks (positive = worse than expected)
    slip_out: float | None = None  # real fills: exit slippage in ticks

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Observation:
        """Build from a saved record, ignoring fields this version doesn't know (a newer file)."""
        return cls(**{k: v for k, v in d.items() if k in _OBS_FIELDS})

    @property
    def net_r(self) -> float:
        """Result after the costs not already in ``r``."""
        return self.r - (self.cost_r or 0.0)


_OBS_FIELDS = frozenset(f.name for f in fields(Observation))
_OPTIONAL = frozenset({"ctx", "mfe_r", "mae_r", "bars", "cost_r", "slip_in", "slip_out"})


@dataclass
class Stats:
    n: int = 0
    n_eff: float = 0.0  # recency- and source-weighted sample count
    wins: int = 0
    sum_wr: float = 0.0  # weighted sum of R
    usd: float = 0.0
    last_day: str = ""

    def add(self, o: Observation, w: float) -> None:
        self.n += 1
        self.n_eff += w
        self.wins += o.r > 0
        self.sum_wr += w * o.r
        self.usd += o.usd or 0.0
        if o.day > self.last_day:
            self.last_day = o.day

    @property
    def mean_r(self) -> float:
        return self.sum_wr / self.n_eff if self.n_eff else 0.0

    def score(self, min_samples: int) -> float:
        """Expectancy in R, shrunk toward zero while the sample is small."""
        return self.mean_r * self.n_eff / (self.n_eff + min_samples) if self.n_eff else 0.0

    def to_dict(self, min_samples: int) -> dict[str, Any]:
        return {"n": self.n, "n_eff": round(self.n_eff, 1), "wins": self.wins, "mean_r": round(self.mean_r, 2),
                "score": round(self.score(min_samples), 3), "usd": round(self.usd, 2), "last_day": self.last_day}


@dataclass
class Verdict:
    allowed: bool
    level: str  # cell | slot | strategy | unproven
    score: float
    why: str
    stats: Stats = field(default_factory=Stats)


class KnowledgeBase:
    """Observations plus the rules that turn them into a trade / don't-trade decision."""

    def __init__(
        self,
        path: Path | None,
        *,
        half_life_days: int = 20,
        min_samples: int = 8,
        min_edge_r: float = 0.05,
        real_weight: float = 2.0,
        max_observations: int = 6000,
        source: str | None = None,
    ):
        self.path = Path(path) if path else None
        self.half_life_days = max(1, half_life_days)
        self.min_samples = max(1, min_samples)
        self.min_edge_r = min_edge_r
        self.real_weight = real_weight
        self.max_observations = max_observations
        self.source = source  # force every recorded observation to this source (used while training)
        self.obs: list[Observation] = []
        self.trained: dict[str, Any] | None = None
        self.updated: int = 0  # bumps on every change
        if self.path:
            self.load()

    @classmethod
    def from_config(cls, cfg: BotConfig, path: Path | None) -> KnowledgeBase:
        k = cfg.knowledge
        return cls(path, half_life_days=k.half_life_days, min_samples=k.min_samples, min_edge_r=k.min_edge_r,
                   real_weight=k.real_trade_weight)

    # ------------------------------------------------------------ persistence

    def load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        try:
            self.obs = [Observation.from_dict(o) for o in raw.get("observations", [])]
            self.trained = raw.get("trained")
        except (TypeError, AttributeError):
            log.warning("Knowledge file %s has an unexpected format - starting fresh", self.path)
            self.obs, self.trained = [], None

    def save(self) -> None:
        if not self.path:
            return
        data = {"version": 2, "trained": self.trained, "observations": [_compact(o) for o in self.obs]}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError as exc:
            log.warning("Could not save the knowledge base: %s", exc)

    # ------------------------------------------------------------ learning

    def record(self, o: Observation, save: bool = True) -> None:
        if self.source:
            o.source = self.source
        self.obs.append(o)
        if len(self.obs) > self.max_observations:  # drop the oldest hypothetical observations first
            keep_real = [x for x in self.obs if x.source in KEPT_SOURCES]
            others = [x for x in self.obs if x.source not in KEPT_SOURCES]
            self.obs = sorted(others[-(self.max_observations - len(keep_real)):] + keep_real, key=lambda x: (x.day, x.time))
        self.updated += 1
        if save:
            self.save()

    def replace_training(self, observations: Iterable[Observation], *, first_day: date, last_day: date, days: int,
                         bars: int, symbol: str, timeframe: int, at: datetime | None = None) -> int:
        """Swap in a fresh training layer; shadow observations the training covers are dropped."""
        last = last_day.isoformat()
        kept = [o for o in self.obs if o.source in KEPT_SOURCES or (o.source == "shadow" and o.day > last)]
        new = [Observation(**{**asdict(o), "source": "train"}) for o in observations]
        self.obs = sorted(kept + new, key=lambda x: (x.day, x.time))
        self.trained = {"at": (at or datetime.now(UTC)).isoformat(timespec="seconds"), "from": first_day.isoformat(),
                        "to": last, "days": days, "bars": bars, "symbol": symbol, "timeframe": timeframe,
                        "observations": len(new)}
        self.updated += 1
        self.save()
        return len(new)

    def training_due(self, now: datetime, retrain_hours: float) -> bool:
        if not self.trained:
            return True
        try:
            at = datetime.fromisoformat(self.trained["at"])
        except (KeyError, ValueError):
            return True
        if at.tzinfo is None:
            at = at.replace(tzinfo=UTC)
        return (now - at).total_seconds() > retrain_hours * 3600

    # ------------------------------------------------------------ querying

    def weight(self, o: Observation, today: date) -> float:
        try:
            age = max(0, (today - date.fromisoformat(o.day)).days)
        except ValueError:
            age = 0
        return (0.5 ** (age / self.half_life_days)) * (self.real_weight if o.source in KEPT_SOURCES else 1.0)

    def stats(self, strategy: str, slot: str | None = None, regime: str | None = None, today: date | None = None) -> Stats:
        today = today or datetime.now(UTC).date()
        s = Stats()
        for o in self.obs:
            if o.strategy == strategy and (slot is None or o.slot == slot) and (regime is None or o.regime == regime):
                s.add(o, self.weight(o, today))
        return s

    def side_stats(self, side: str, slot: str, regime: str, today: date | None = None) -> Stats:
        """Every strategy's signals on one side (LONG / SHORT) at this slot and regime - your manual trades excluded."""
        today = today or datetime.now(UTC).date()
        s = Stats()
        for o in self.obs:
            if o.side == side and o.slot == slot and o.regime == regime and o.strategy != MANUAL:
                s.add(o, self.weight(o, today))
        return s

    def verdict(self, strategy: str, slot: str, regime: str, today: date | None = None) -> Verdict:
        """May ``strategy`` trade now? Uses the most specific evidence that has enough samples."""
        for level, st in (("cell", self.stats(strategy, slot, regime, today)),
                          ("slot", self.stats(strategy, slot, None, today)),
                          ("strategy", self.stats(strategy, None, None, today))):
            if st.n_eff >= self.min_samples:
                score = st.score(self.min_samples)
                where = {"cell": f"{slot}/{regime}", "slot": f"{slot} (any regime)", "strategy": "all day"}[level]
                if score >= self.min_edge_r:
                    return Verdict(True, level, score, f"{st.mean_r:+.2f}R avg over {st.n} at {where}", st)
                return Verdict(False, level, score, f"{st.mean_r:+.2f}R avg over {st.n} at {where} - not working", st)
        st = self.stats(strategy, None, None, today)
        return Verdict(False, "unproven", 0.0, f"unproven: only {st.n} observation(s), need {self.min_samples}", st)

    def allowed_now(self, strategies: Iterable[str], slot: str, regime: str, today: date | None = None) -> list[str]:
        return [s for s in strategies if self.verdict(s, slot, regime, today).allowed]

    def counts(self) -> dict[str, int]:
        return {src: sum(1 for o in self.obs if o.source == src) for src in SOURCES}

    def summary(self, strategies: list[tuple[str, str]], *, today: date | None = None, slot: str = "",
                regime: str = "") -> dict[str, Any]:
        """Everything the dashboard shows: per strategy, per slot x regime, plus training info."""
        today = today or datetime.now(UTC).date()
        rows = []
        for name, title in strategies:
            cells = {}
            for sl in SLOT_NAMES:
                for rg in REGIMES:
                    v = self.verdict(name, sl, rg, today)
                    cells[f"{sl}|{rg}"] = {**v.stats.to_dict(self.min_samples), "allowed": v.allowed, "level": v.level,
                                          "why": v.why, "cell_n": self.stats(name, sl, rg, today).n}
            rows.append({"name": name, "title": title, "overall": self.stats(name, None, None, today).to_dict(self.min_samples),
                         "slots": {sl: self.stats(name, sl, None, today).to_dict(self.min_samples) for sl in SLOT_NAMES},
                         "cells": cells, "allowed_now": cells[f"{slot}|{regime}"]["allowed"] if slot in SLOT_NAMES else False})
        return {
            "trained": self.trained, "counts": self.counts(), "total": len(self.obs), "manual": self.manual_summary(today),
            "now": {"slot": slot, "regime": regime}, "slots": list(SLOT_NAMES), "regimes": list(REGIMES),
            "params": {"min_samples": self.min_samples, "min_edge_r": self.min_edge_r, "half_life_days": self.half_life_days},
            "strategies": rows,
        }

    def manual_summary(self, today: date | None = None, recent: int = 8) -> dict[str, Any]:
        """Your manual trades: overall, per slot x regime, and the latest few."""
        today = today or datetime.now(UTC).date()
        mine = [o for o in self.obs if o.strategy == MANUAL]
        cells = {f"{sl}|{rg}": self.stats(MANUAL, sl, rg, today).to_dict(self.min_samples) for sl in SLOT_NAMES for rg in REGIMES}
        return {"overall": self.stats(MANUAL, None, None, today).to_dict(self.min_samples), "cells": cells,
                "recent": [asdict(o) for o in mine[-recent:][::-1]]}

    def text(self, strategies: list[tuple[str, str]], *, today: date | None = None, slot: str = "", regime: str = "") -> str:
        """Plain-text summary for Telegram."""
        s = self.summary(strategies, today=today, slot=slot, regime=regime)
        c = s["counts"]
        lines = [f"Knowledge base: {s['total']} observations (train {c['train']}, live ideas {c['shadow']}, "
                 f"real trades {c['real']}, your manual trades {c['manual']})."]
        if s["trained"]:
            t = s["trained"]
            lines.append(f"Trained {t['at'][:16].replace('T', ' ')} on {t['days']} days ({t['from']} to {t['to']}).")
        else:
            lines.append("Not trained yet - send /train or run 'topstep-bot train'.")
        if slot:
            lines.append(f"Now: {slot} / {regime}.")
        for row in s["strategies"]:
            parts = []
            for sl in SLOT_NAMES:
                marks = []
                for rg in REGIMES:
                    cell = row["cells"][f"{sl}|{rg}"]
                    mark = "✅" if cell["allowed"] else ("❌" if cell["level"] != "unproven" else "❔")
                    marks.append(f"{rg[0]}{mark}")
                parts.append(f"{sl} {''.join(marks)}")
            o = row["overall"]
            lines.append(f"{row['title']}: {o['mean_r']:+.2f}R avg over {o['n']} | " + ", ".join(parts))
        m = s["manual"]["overall"]
        if m["n"]:
            lines.append(f"Your manual trades: {m['n']}, {m['wins']} won, {m['mean_r']:+.2f}R avg.")
        lines.append("c = calm, v = volatile. ✅ trades now, ❌ switched off (losing), ❔ not enough evidence yet.")
        return "\n".join(lines)


def _compact(o: Observation) -> dict[str, Any]:
    """An observation as saved: the optional learning fields are left out while empty (smaller file)."""
    return {k: v for k, v in asdict(o).items() if v is not None or k not in _OPTIONAL}


# --------------------------------------------------------------------------- training

async def train_from_bars(
    cfg: BotConfig,
    contract: Contract,
    bars: list[Bar],
    kb: KnowledgeBase,
    *,
    strategies: list[str] | None = None,
    progress: Callable[[float], None] | None = None,
    at: datetime | None = None,
) -> dict[str, Any]:
    """Replay history through every strategy in shadow mode and store the outcomes as training.

    This is exactly what the running bot does with live bars (RecommendationBook), so the
    training layer and the live observations measure the same thing. Returns a summary.
    """
    from topstep_bot.backtest.runner import prepare_bars
    from topstep_bot.broker.paper import PaperBroker
    from topstep_bot.factory import build_core, fees_for, starting_balance
    from topstep_bot.recommendations import RecommendationBook

    trial = cfg.model_copy(deep=True)
    trial.strategy.name, trial.strategy.params = "adaptive", {}
    tf = trial.instrument.timeframe_minutes
    bars = prepare_bars(bars, tf)
    now = [bars[0].ts]
    balance0 = starting_balance(trial)
    broker = PaperBroker(contract, balance0, slippage_ticks=trial.risk.slippage_ticks, fees_round_turn=fees_for(trial, contract))
    core = build_core(trial, contract, broker, clock=lambda: now[0], account_label="training")
    core.balance = balance0
    collector = KnowledgeBase(None, half_life_days=kb.half_life_days, min_samples=kb.min_samples, source="train")
    core.knowledge = collector
    book = RecommendationBook(core, strategies, quiet=True)
    core.recommender = book
    if not book.shadows:
        raise ValueError("no strategy can be trained on this timeframe")
    warmup = max(s.warmup_days for s in book.shadows)
    days = sorted({core.schedule.trading_day(b.ts) for b in bars})
    if len(days) <= warmup + 5:
        raise ValueError(f"Need more than {warmup + 5} trading days of data to train (have {len(days)})")
    start = days[warmup]
    total = len(bars)
    for i, bar in enumerate(bars):
        now[0] = bar.ts
        if core.schedule.trading_day(bar.ts) < start:
            core.warmup_bar(bar)
            continue
        await core.roll_day_if_needed(bar.ts)
        core.observe_bar(bar)
        now[0] = bar.ts + core.tf
        core.last_price = bar.close
        book.on_bar(bar)
        if i % 500 == 0:
            if progress:
                progress(i / total)
            await asyncio.sleep(0)  # let the live bot keep serving its clock while it trains
    added = kb.replace_training(collector.obs, first_day=start, last_day=days[-1], days=len(days) - warmup, bars=total,
                                symbol=contract.root or trial.instrument.symbol, timeframe=tf, at=at)
    if progress:
        progress(1.0)
    per_strategy = {s.name: sum(1 for o in collector.obs if o.strategy == s.name) for s in book.shadows}
    return {"observations": added, "days": len(days) - warmup, "from": start.isoformat(), "to": days[-1].isoformat(),
            "bars": total, "per_strategy": per_strategy}
