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

Keeping it safe. What the bot learns live can't be downloaded again, so the base is saved three ways:
  file     ``knowledge_<SYMBOL>_<TF>m.json``, rewritten after every observation (write to a temporary
           file, then swap it in, so a crash or power cut never leaves half a file)
  ledger   ``knowledge_<SYMBOL>_<TF>m.ledger.jsonl``: every live observation (real, manual, idea) is
           also appended here as one line and never removed, even when the base trims old ideas
  backups  ``knowledge_backups/``: a copy of the file once a day, the last ``knowledge.backups_kept``
If the file is ever damaged it is set aside (``.damaged-<time>``), the newest good backup is loaded,
and anything from the ledger the backup is missing is added back, so no real or manual trade is lost.
The same observation is never counted twice (a restart that replays the morning, say).
"""

from __future__ import annotations

import asyncio
import functools
import hashlib
import json
import logging
import os
import shutil
import time as _time
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
BACKUP_DIR = "knowledge_backups"
_REPLACE_TRIES = 5  # Windows: antivirus or a sync tool can hold the file for a moment


@functools.cache
def training_fingerprint() -> str:
    """A short hash of the code that produces observations (strategies, idea tracking, this file).

    Stored with the training layer: after an update that changes how signals or outcomes are
    worked out, the next start retrains, so old and new results are never mixed."""
    root = Path(__file__).parent
    files = sorted((root / "strategies").glob("*.py")) + [root / "recommendations.py", root / "knowledge.py",
                                                         root / "market_context.py"]
    h = hashlib.sha1()
    for f in files:
        try:
            h.update(f.name.encode() + f.read_bytes().replace(b"\r\n", b"\n"))
        except OSError:
            continue
    return h.hexdigest()[:12]


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

    @property
    def key(self) -> tuple[str, str, str, str]:
        """The signal this observation is about: one per strategy, side and bar."""
        return (self.day, self.time, self.strategy, self.side)


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
        backups: int = 14,
        ledger: bool = True,
    ):
        self.path = Path(path) if path else None
        self.half_life_days = max(1, half_life_days)
        self.min_samples = max(1, min_samples)
        self.min_edge_r = min_edge_r
        self.real_weight = real_weight
        self.max_observations = max_observations
        self.source = source  # force every recorded observation to this source (used while training)
        self.backups = max(0, backups) if self.path else 0  # daily copies kept in knowledge_backups/
        self.ledger_path = self.path.with_name(self.path.stem + ".ledger.jsonl") if self.path and ledger else None
        self.obs: list[Observation] = []
        self.trained: dict[str, Any] | None = None
        self.updated: int = 0  # bumps on every change
        self.recovery: list[str] = []  # what loading had to repair, for the startup message
        self._index: dict[tuple[str, str, str, str], list[Observation]] = {}
        self._disk_stamp: tuple[int, int] | None = None  # the file as this base last read or wrote it
        self._backed_up: str = ""  # day of the last daily backup
        self._read_only: str | None = None  # why saving is off (a file that exists but can't be read)
        if self.path:
            self.load()

    @classmethod
    def from_config(cls, cfg: BotConfig, path: Path | None) -> KnowledgeBase:
        k = cfg.knowledge
        return cls(path, half_life_days=k.half_life_days, min_samples=k.min_samples, min_edge_r=k.min_edge_r,
                   real_weight=k.real_trade_weight, backups=k.backups_kept)

    # ------------------------------------------------------------ persistence

    def load(self) -> None:
        """Read the file; if it is damaged, set it aside and use the newest good backup. Then add back
        anything from the ledger the file is missing (after a crash, or when a backup was used)."""
        self.recovery = []
        try:
            data = _read_file(self.path)
        except OSError as exc:
            self._read_only = f"could not read {self.path.name}: {exc}"
            log.error("Knowledge base: %s - it will not be overwritten this session", self._read_only)
            self.recovery.append(f"{self._read_only}; nothing will be saved over it until the bot restarts")
            data = None
        except ValueError as exc:
            data = self._recover(str(exc))
        if data is not None:
            self.obs, self.trained = data
        self._reindex()
        self._disk_stamp = _stamp(self.path)
        restored = self._merge_ledger()
        if restored:
            self.recovery.append(f"added back {restored} observation(s) from the ledger")
            self.save()

    def _recover(self, problem: str) -> tuple[list[Observation], dict | None] | None:
        damaged = self.path.with_name(f"{self.path.name}.damaged-{datetime.now():%Y%m%d-%H%M%S}")
        try:
            os.replace(self.path, damaged)
        except OSError as exc:
            self._read_only = f"{self.path.name} is damaged and could not be moved aside ({exc})"
            log.error("Knowledge base: %s", self._read_only)
            self.recovery.append(self._read_only)
            return None
        log.error("Knowledge file %s was damaged (%s); kept as %s", self.path.name, problem, damaged.name)
        self.recovery.append(f"the knowledge file was damaged and was kept aside as {damaged.name}")
        for backup in reversed(self.backup_files()):
            try:
                data = _read_file(backup)
            except (OSError, ValueError):
                continue
            if data is not None:
                self.recovery.append(f"loaded the backup {backup.name}")
                return data
        self.recovery.append("no good backup was found")
        return None

    def _merge_ledger(self) -> int:
        """Add ledger observations the base is missing: every real and manual trade, and the ideas
        newer than the training layer (older ideas are covered by training)."""
        if not self.ledger_path or not self.ledger_path.exists():
            return 0
        cut = (self.trained or {}).get("to", "")
        added: list[Observation] = []
        try:
            with open(self.ledger_path, encoding="utf-8") as f:
                for line in f:
                    try:
                        o = Observation.from_dict(json.loads(line))
                    except (ValueError, TypeError, AttributeError):
                        continue  # a line cut short by a crash or power cut
                    if o.source not in KEPT_SOURCES and o.day <= cut:
                        continue
                    if not self._duplicate(o):
                        self._add(o)
                        added.append(o)
        except OSError as exc:
            log.warning("Could not read the knowledge ledger: %s", exc)
        if not added:
            return 0
        self.obs.sort(key=lambda x: (x.day, x.time))
        self._prune()
        kept = {id(o) for o in self.obs}
        return sum(1 for o in added if id(o) in kept)  # old ideas the base had trimmed don't come back

    def save(self) -> None:
        if not self.path:
            return
        if self._read_only:
            log.warning("Knowledge base not saved: %s", self._read_only)
            return
        try:
            self._merge_from_disk()
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._daily_backup()
            data = {"version": 2, "trained": self.trained, "observations": [_compact(o) for o in self.obs]}
            tmp = self.path.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f)
                f.flush()
                os.fsync(f.fileno())  # on disk before it replaces the old file: a power cut can't leave it empty
            _replace(tmp, self.path)
            self._disk_stamp = _stamp(self.path)
        except OSError as exc:
            log.warning("Could not save the knowledge base: %s", exc)

    def _merge_from_disk(self) -> None:
        """Another program saved the file since this base read it (``topstep-bot train`` while the bot
        runs, say): keep its newer training and its observations instead of overwriting them."""
        stamp = _stamp(self.path)
        if stamp is None or stamp == self._disk_stamp:
            return
        try:
            data = _read_file(self.path)
        except (OSError, ValueError):
            return  # unreadable: this base's own copy replaces it
        if data is None:
            return
        obs, trained = data
        if trained and (not self.trained or str(trained.get("at", "")) > str(self.trained.get("at", ""))):
            cut = trained.get("to", "")
            self.obs = [o for o in self.obs if o.source in KEPT_SOURCES or (o.source == "shadow" and o.day > cut)]
            self.obs += [o for o in obs if o.source == "train"]
            self.trained = trained
            self._reindex()
        cut = (self.trained or {}).get("to", "")
        for o in obs:
            if o.source == "train" or (o.source == "shadow" and o.day <= cut) or self._duplicate(o):
                continue
            self._add(o)
        self.obs.sort(key=lambda x: (x.day, x.time))
        self._prune()
        self.updated += 1
        log.info("Knowledge base: merged the changes another program saved to %s", self.path.name)

    def _daily_backup(self) -> None:
        today = datetime.now().date().isoformat()
        if not self.backups or self._backed_up == today or not self.path.exists():
            return
        folder = self.path.parent / BACKUP_DIR
        target = folder / f"{self.path.stem}-{today}.json"
        if not target.exists():
            folder.mkdir(parents=True, exist_ok=True)
            shutil.copy2(self.path, target)
            for old in self.backup_files()[:-self.backups]:
                old.unlink(missing_ok=True)
        self._backed_up = today

    def backup_files(self) -> list[Path]:
        """The daily backups of this file, oldest first."""
        if not self.path:
            return []
        return sorted((self.path.parent / BACKUP_DIR).glob(f"{self.path.stem}-????-??-??.json"))

    def _append_ledger(self, o: Observation) -> None:
        try:
            with open(self.ledger_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(_compact(o)) + "\n")
        except OSError as exc:
            log.warning("Could not add to the knowledge ledger: %s", exc)

    # ------------------------------------------------------------ learning

    def _add(self, o: Observation) -> None:
        self.obs.append(o)
        self._index.setdefault(o.key, []).append(o)

    def _reindex(self) -> None:
        self._index = {}
        for o in self.obs:
            self._index.setdefault(o.key, []).append(o)

    def _duplicate(self, o: Observation) -> bool:
        """Already known? A real or manual trade only matches an identical record of itself; an idea
        matches anything recorded for the same signal (its training replay, its real trade, or itself)."""
        for x in self._index.get(o.key, ()):
            if o.source in KEPT_SOURCES:
                if x == o:
                    return True
            elif o.source == "train":
                if x.source == "train":
                    return True
            else:
                return True
        return False

    def _prune(self) -> None:
        if len(self.obs) <= self.max_observations:
            return
        # Drop the oldest hypothetical observations first (live ideas stay in the ledger).
        keep_real = [x for x in self.obs if x.source in KEPT_SOURCES]
        others = [x for x in self.obs if x.source not in KEPT_SOURCES]
        room = max(0, self.max_observations - len(keep_real))
        self.obs = sorted((others[-room:] if room else []) + keep_real, key=lambda x: (x.day, x.time))
        self._reindex()

    def record(self, o: Observation, save: bool = True) -> bool:
        """Add an observation (False when it is already known, e.g. replayed after a restart)."""
        if self.source:
            o.source = self.source
        elif self.path and o.source not in KEPT_SOURCES and self._duplicate(o):
            return False  # an idea seen again: a restart replays the session so far (see LiveRunner._catch_up)
        self._add(o)
        if self.ledger_path and o.source != "train":
            self._append_ledger(o)
        self._prune()
        self.updated += 1
        if save:
            self.save()
        return True

    def replace_training(self, observations: Iterable[Observation], *, first_day: date, last_day: date, days: int,
                         bars: int, symbol: str, timeframe: int, at: datetime | None = None,
                         origin: str = "") -> int:
        """Swap in a fresh training layer; shadow observations the training covers are dropped."""
        last = last_day.isoformat()
        kept = [o for o in self.obs if o.source in KEPT_SOURCES or (o.source == "shadow" and o.day > last)]
        new = [Observation(**{**asdict(o), "source": "train"}) for o in observations]
        self.obs = sorted(kept + new, key=lambda x: (x.day, x.time))
        self._reindex()
        self.trained = {"at": (at or datetime.now(UTC)).isoformat(timespec="seconds"), "from": first_day.isoformat(),
                        "to": last, "days": days, "bars": bars, "symbol": symbol, "timeframe": timeframe,
                        "observations": len(new), "code": training_fingerprint()}
        if origin:
            self.trained["origin"] = origin
        self.updated += 1
        self.save()
        return len(new)

    def training_due(self, now: datetime, retrain_hours: float, *, session_end: datetime | None = None) -> bool:
        return self.training_due_reason(now, retrain_hours, session_end=session_end) is not None

    def training_due_reason(self, now: datetime, retrain_hours: float, *,
                            session_end: datetime | None = None) -> str | None:
        """Why the training layer needs refreshing (None: it is up to date).

        ``session_end``: when the last finished trading session ended. Training from before then
        is missing that session, so it is refreshed even if it is only a few hours old."""
        if not self.trained:
            return "it has not been trained yet"
        try:
            at = datetime.fromisoformat(self.trained["at"])
        except (KeyError, TypeError, ValueError):
            return "its training date is unreadable"
        if at.tzinfo is None:
            at = at.replace(tzinfo=UTC)
        if self.trained.get("code") != training_fingerprint():
            return "the strategy code changed since it was trained"
        if session_end is not None and at < session_end <= now:
            return "a trading session finished since it was trained"
        if (now - at).total_seconds() > retrain_hours * 3600:
            return f"its training is more than {retrain_hours:g} hours old"
        return None

    def learned_on(self, day: date) -> dict[str, int]:
        """What was learned live on one trading day, by source (training replays not counted)."""
        iso = day.isoformat()
        return {src: sum(1 for o in self.obs if o.day == iso and o.source == src) for src in SOURCES if src != "train"}

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
        if today is not None:
            got = self.learned_on(today)
            if sum(got.values()):
                lines.append(f"Learned today: {sum(got.values())} (live ideas {got['shadow']}, real trades {got['real']}, "
                             f"your manual trades {got['manual']}).")
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


def _stamp(path: Path | None) -> tuple[int, int] | None:
    try:
        st = path.stat()
    except (OSError, AttributeError):
        return None
    return st.st_mtime_ns, st.st_size


def _read_file(path: Path) -> tuple[list[Observation], dict | None] | None:
    """A saved knowledge file: (observations, training info), None if there is none.
    Raises ValueError when the file is there but damaged (half written, edited by hand, ...)."""
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8")
    try:
        raw = json.loads(text)
        if not isinstance(raw, dict) or not isinstance(raw.get("observations", []), list):
            raise ValueError("unexpected format")
        trained = raw.get("trained")
        if trained is not None and not isinstance(trained, dict):
            raise ValueError("unexpected training info")
        return [Observation.from_dict(o) for o in raw.get("observations", [])], trained
    except (TypeError, AttributeError, KeyError) as exc:
        raise ValueError(f"unexpected format ({exc})") from exc


def _replace(src: Path, dst: Path) -> None:
    """os.replace, retried briefly: on Windows a virus scanner or sync tool can hold the file for a moment."""
    for attempt in range(_REPLACE_TRIES):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == _REPLACE_TRIES - 1:
                raise
            _time.sleep(0.05 * (attempt + 1))


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
    origin: str = "",
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
                                symbol=contract.root or trial.instrument.symbol, timeframe=tf, at=at, origin=origin)
    if progress:
        progress(1.0)
    per_strategy = {s.name: sum(1 for o in collector.obs if o.strategy == s.name) for s in book.shadows}
    return {"observations": added, "days": len(days) - warmup, "from": start.isoformat(), "to": days[-1].isoformat(),
            "bars": total, "per_strategy": per_strategy}
