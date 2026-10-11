"""The evidence behind a decision: everything the bot's memory says about one signal.

The knowledge base (knowledge.py) keeps the outcome of every signal every strategy gave. This
module reads those outcomes three ways, and the bot's two decision rules (``knowledge.policy``)
use them differently:

* **Classic** (version 1.2): the first of cell (strategy x time of day x calm/volatile), slot or
  whole strategy with enough recent samples decides, on the average result before costs, shrunk
  toward zero while the sample is small. ``KnowledgeBase.verdict`` does this.
* **Evidence** (new in 1.3): one estimate from *all* levels at once, after fees and slippage.
  Each level is judged against what the rest of the strategy says (partial pooling): the cell
  (this time of day and market) starts from the strategy's other cells at that time of day,
  which start from its record at other times of day, which starts from zero - and a level's
  own results move the estimate away from where it starts only as far as their number
  justifies. How far that is comes from the spread of results (``TAU``): with outcomes that
  swing by about 1R, it takes some forty signals to move an estimate halfway. So eight lucky
  trades in one cell can no longer outvote two hundred elsewhere. The long-run memory (years of
  replayed history, when the bot has it) joins at a small weight per signal, so a strategy with
  a long record isn't judged on six weeks only, but recent evidence can still overrule it.
  ``EvidenceModel`` + ``evidence_verdict`` do this. The decision lab (lab.py) replays both rules
  on the same history, and ``policy: auto`` only uses this one once it has won there.

* **Similar situations** (both rules, for explanations): the past signals of the same strategy
  whose market snapshot (market_context.py) looked most like now - gap, trend, VWAP, volatility,
  time since the open - and how they turned out. ``similar()``. It explains a decision; it never
  makes one, because with this many measurements a lucky match is easy to find.

Nothing in here places a trade: it only answers "how good is this signal, and how sure are we?".
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date
from typing import TYPE_CHECKING, Any

from topstep_bot.knowledge import KEPT_SOURCES, NOT_STRATEGIES, Verdict
from topstep_bot.market_context import orient

if TYPE_CHECKING:
    from topstep_bot.knowledge import KnowledgeBase, Observation

PRIOR_FLOOR = 0.05  # weight an observation from the long-run memory keeps however old it is (recent ones decay to it)
TAU = 0.15  # how far apart, in R, the true results of one strategy at different times / markets typically are
MAX_STRENGTH = 20  # x min_samples: the most evidence a level's own results must outweigh
SIMILAR_FEATURES = ("vol", "min_open", "gap", "move", "range_used", "range_pos", "vwap", "trend", "volume")
SIMILAR_K = 30  # nearest past signals compared with the current one
SIMILAR_MIN_SHARED = 4  # measurements two snapshots must both have to be compared at all
SIMILAR_MIN_POOL = 15  # past signals with a snapshot needed before "similar situations" means anything


# --------------------------------------------------------------------------- aggregates

@dataclass
class Agg:
    """Weighted totals for a group of observations (net R = after fees and slippage)."""

    n: int = 0
    w: float = 0.0  # effective sample size (recency- and source-weighted)
    w_net: float = 0.0  # weighted sum of net R
    w_net2: float = 0.0  # weighted sum of squared net R (for the spread)
    wins: int = 0
    recent_n: int = 0  # how many came from the recent knowledge base (the rest: the long-run memory)

    def add(self, net: float, weight: float, recent: bool) -> None:
        self.n += 1
        self.w += weight
        self.w_net += weight * net
        self.w_net2 += weight * net * net
        self.wins += net > 0
        self.recent_n += recent

    def merge(self, other: Agg) -> None:
        self.n += other.n
        self.w += other.w
        self.w_net += other.w_net
        self.w_net2 += other.w_net2
        self.wins += other.wins
        self.recent_n += other.recent_n

    def minus(self, other: Agg) -> Agg:
        """These totals without ``other`` (a part of them)."""
        return Agg(self.n - other.n, max(0.0, self.w - other.w), self.w_net - other.w_net, self.w_net2 - other.w_net2,
                   self.wins - other.wins, self.recent_n - other.recent_n)

    @property
    def mean(self) -> float:
        return self.w_net / self.w if self.w else 0.0

    @property
    def sd(self) -> float:
        if self.w <= 0:
            return 0.0
        return math.sqrt(max(0.0, self.w_net2 / self.w - self.mean ** 2))

    def to_dict(self) -> dict[str, Any]:
        return {"n": self.n, "n_eff": round(self.w, 1), "mean_net_r": round(self.mean, 3),
                "win_rate": round(self.wins / self.n, 3) if self.n else None, "recent_n": self.recent_n}


def shrink(mean: float, weight: float, prior: float, strength: float) -> float:
    """A sample mean pulled toward ``prior``: the smaller the sample, the closer to the prior."""
    total = weight + strength
    return (mean * weight + prior * strength) / total if total > 0 else prior


class EvidenceModel:
    """Totals per strategy x time of day x regime over the recent knowledge base plus the long-run memory.

    Recent observations fade with age like the knowledge base's own (half-life), but never below
    ``floor``; long-run ones (older than the recent window) all count at ``floor``. So the long-run
    memory is a steady background that recent evidence can overrule, not the other way round.
    """

    def __init__(self, recent: Iterable[Observation], prior: Iterable[Observation] = (), *, today: date,
                 half_life_days: int = 20, real_weight: float = 2.0, floor: float = PRIOR_FLOOR):
        self.today = today
        self.cells: dict[tuple[str, str, str], Agg] = {}
        hl = max(1, half_life_days)
        for o, is_recent in [*((o, True) for o in recent), *((o, False) for o in prior)]:
            if o.strategy in NOT_STRATEGIES:
                continue
            try:
                age = max(0, (today - date.fromisoformat(o.day)).days)
            except ValueError:
                age = 0
            w = max(floor, 0.5 ** (age / hl)) if is_recent else floor
            if o.source in KEPT_SOURCES:
                w *= real_weight
            self.cells.setdefault((o.strategy, o.slot, o.regime), Agg()).add(o.net_r, w, is_recent)

    def strength(self, min_samples: int) -> float:
        """How much evidence a level's own results must outweigh: the outcomes' variance over TAU squared.

        Outcomes that swing widely (stops and targets) say less per signal, so they need more of them."""
        total = Agg()
        for agg in self.cells.values():
            total.merge(agg)
        k = max(1, min_samples)
        return min(MAX_STRENGTH * k, max(k, total.sd ** 2 / TAU ** 2)) if total.w else k

    def level(self, strategy: str, slot: str | None = None, regime: str | None = None) -> Agg:
        out = Agg()
        for (s, sl, rg), agg in self.cells.items():
            if s == strategy and (slot is None or sl == slot) and (regime is None or rg == regime):
                out.merge(agg)
        return out


def evidence_verdict(model: EvidenceModel, strategy: str, slot: str, regime: str, *, min_samples: int,
                     min_edge_r: float) -> Verdict:
    """The 1.3 decision rule: the net R a trade is expected to make here, pooled from every level."""
    whole = model.level(strategy)
    if whole.w < max(1, min_samples):
        return Verdict(False, "unproven", 0.0, f"unproven: only {whole.n} observation(s), need about {min_samples}",
                       _as_stats(whole))
    k = model.strength(min_samples)
    in_slot = model.level(strategy, slot)
    cell = model.level(strategy, slot, regime)
    elsewhere = whole.minus(in_slot)  # other times of day
    beside = in_slot.minus(cell)  # this time of day, the other kind of market
    start = shrink(elsewhere.mean, elsewhere.w, 0.0, k)
    start = shrink(beside.mean, beside.w, start, k)
    est = shrink(cell.mean, cell.w, start, k)
    detail = (f"{est:+.2f}R expected after costs (here {cell.mean:+.2f}R over {cell.n}, {slot} {in_slot.mean:+.2f}R over "
              f"{in_slot.n}, all day {whole.mean:+.2f}R over {whole.n})")
    level = "cell" if cell.w >= k else "slot" if in_slot.w >= k else "strategy"
    if est >= min_edge_r:
        return Verdict(True, level, est, detail, _as_stats(cell))
    return Verdict(False, level, est, detail + " - not enough edge", _as_stats(cell))


def _as_stats(agg: Agg):
    from topstep_bot.knowledge import Stats

    return Stats(n=agg.n, n_eff=agg.w, wins=agg.wins, sum_wr=agg.w_net)


# --------------------------------------------------------------------------- similar situations

@dataclass
class Similar:
    n: int  # past signals compared
    mean_net_r: float
    win_rate: float
    baseline_net_r: float  # every past signal of the strategy with a snapshot, for comparison
    baseline_n: int
    closeness: float  # 0..1: how alike the nearest ones were (1 = identical)
    examples: list[dict] = field(default_factory=list)  # the three closest

    def to_dict(self) -> dict[str, Any]:
        return {"n": self.n, "mean_net_r": round(self.mean_net_r, 3), "win_rate": round(self.win_rate, 3),
                "baseline_net_r": round(self.baseline_net_r, 3), "baseline_n": self.baseline_n,
                "closeness": round(self.closeness, 2), "examples": self.examples}


def _spread(values: list[float]) -> float:
    """A robust scale: the interquartile range (or the plain spread for very few values), never zero."""
    s = sorted(values)
    if len(s) >= 8:
        q = s[(3 * len(s)) // 4] - s[len(s) // 4]
        if q > 0:
            return q
    return (s[-1] - s[0]) or 1.0 if s else 1.0


def similar(pool: Iterable[Observation], strategy: str, side: str, ctx: dict | None, *, k: int = SIMILAR_K,
            exclude_day: str | None = None) -> Similar | None:
    """How the past signals of ``strategy`` that looked most like ``ctx`` (seen from ``side``) turned out.

    Both sides count: snapshots are turned to "with the trade", so a long above VWAP and a short
    below it are the same situation. ``exclude_day`` leaves out one day (the trade being explained)."""
    here = {key: v for key, v in orient(ctx, side).items() if key in SIMILAR_FEATURES}
    if len(here) < SIMILAR_MIN_SHARED:
        return None
    past = []
    for o in pool:
        if o.strategy != strategy or not o.ctx or o.day == exclude_day:
            continue
        past.append((o, {key: v for key, v in orient(o.ctx, o.side).items() if key in SIMILAR_FEATURES}))
    if len(past) < SIMILAR_MIN_POOL:
        return None
    scale = {key: _spread([f[key] for _, f in past if key in f]) for key in here}
    scored = []
    for o, f in past:
        shared = [key for key in here if key in f]
        if len(shared) < SIMILAR_MIN_SHARED:
            continue
        d = math.sqrt(sum(((here[key] - f[key]) / scale[key]) ** 2 for key in shared) / len(shared))
        scored.append((d, o))
    if len(scored) < SIMILAR_MIN_POOL:
        return None
    scored.sort(key=lambda t: t[0])
    near = scored[:k]
    nets = [o.net_r for _, o in near]
    avg_d = sum(d for d, _ in near) / len(near)
    examples = [{"day": o.day, "time": o.time, "side": o.side, "net_r": round(o.net_r, 2), "why": o.why,
                 "source": o.source} for _, o in near[:3]]
    return Similar(n=len(near), mean_net_r=sum(nets) / len(nets), win_rate=sum(v > 0 for v in nets) / len(nets),
                   baseline_net_r=sum(o.net_r for _, o in scored) / len(scored), baseline_n=len(scored),
                   closeness=1.0 / (1.0 + avg_d), examples=examples)


# --------------------------------------------------------------------------- one signal, every angle

def signal_evidence(kb: KnowledgeBase, model: EvidenceModel, prior: list[Observation], strategy: str, side: str,
                    slot: str, regime: str, ctx: dict | None, today: date) -> dict[str, Any]:
    """Everything the bot's memory says about one signal, for a decision record (reasoning.py).

    ``model``: the evidence model over ``kb`` and ``prior`` (the long-run memory's older observations)."""
    levels = {}
    for name, (sl, rg) in (("cell", (slot, regime)), ("slot", (slot, None)), ("strategy", (None, None))):
        levels[name] = kb.stats(strategy, sl, rg, today).to_dict(kb.min_samples)
    out: dict[str, Any] = {"recent": levels}
    classic = kb.verdict(strategy, slot, regime, today)
    out["classic"] = {"allowed": classic.allowed, "level": classic.level, "score": round(classic.score, 3), "why": classic.why}
    v = evidence_verdict(model, strategy, slot, regime, min_samples=kb.min_samples, min_edge_r=kb.min_edge_r)
    out["estimate"] = {"allowed": v.allowed, "level": v.level, "net_r": round(v.score, 3), "why": v.why}
    if prior:
        whole = Agg()
        for o in prior:
            if o.strategy == strategy:
                whole.add(o.net_r, 1.0, False)
        out["longrun"] = {**whole.to_dict(), "from": prior[0].day, "to": prior[-1].day} if whole.n else None
    sim = similar([*kb.obs, *prior], strategy, side, ctx, exclude_day=today.isoformat())
    if sim is not None:
        out["similar"] = sim.to_dict()
    return out


def prior_observations(kb: KnowledgeBase, prior: KnowledgeBase | None) -> list[Observation]:
    """The long-run memory's observations from before the recent knowledge base starts (no double counting)."""
    if prior is None or not prior.obs:
        return []
    start = (kb.trained or {}).get("from") or min((o.day for o in kb.obs), default="9999")
    return sorted((o for o in prior.obs if o.day < start), key=lambda o: (o.day, o.time))
