"""The probability calculator: how likely a signal is to win, what it wins and loses, and what it is worth.

For every signal (the bot's own and every shadow strategy's idea) the calculator answers four
questions from everything the bot has seen, after fees and slippage:

* **P(win)** - the chance the trade ends in profit.
* **Average win / average loss** - in R (multiples of the risk at the stop).
* **Expected value** - P(win) x average win - (1 - P(win)) x average loss, in R per trade.
* **How sure** - an 80% range for P(win) and the expected value, from how much evidence there is.

How it is worked out (hierarchical, "partial pooling"):

    the knowledge network (every market of the same asset class, at a fraction of the weight)
      -> this market, this strategy, this side (long or short)
        -> ... at this time of day
          -> ... in this kind of market (calm or volatile)

Each level starts from what the levels around it say (everything outside it, so no outcome is
counted twice) and moves away from that only as far as its own number of outcomes justifies
(``STRENGTH`` pseudo-observations). Eight lucky trades at one time of day can't outvote two hundred
elsewhere, and a strategy that has never traded this market starts from what it did on related
markets instead of from nothing. Recent outcomes count more (the knowledge base's half-life), the
bot's own fills count double, and the long-run memory joins at a small weight.

What it changes, and what it doesn't:

* ``probability.veto`` - a signal the calculator is at least 90% sure loses money after costs is
  skipped (only with enough evidence: an unknown signal is never vetoed for being unknown).
* ``probability.sizing`` - in aggressive mode, the size of the next trade follows a quarter of the
  Kelly fraction (bigger when the edge is large and well measured, smaller when it is thin), within
  ``aggressive.max_conviction`` and every hard limit: MLL, daily loss limits, Topstep's caps.
* Its predictions are saved with every outcome, so ``Calibration`` can show how well they held up:
  when it said 60%, did about 60% win? That record is shown on the Knowledge tab. Until it has a few
  hundred outcomes, treat its numbers as rough.

Honest limits: it only knows what the strategies' past signals did, and markets change. A P(win)
of 55% is a long-run frequency, not a promise about the next trade; nothing here can guarantee a
profit or a passed Combine.
"""

from __future__ import annotations

import json
import logging
import math
import time
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from topstep_bot.knowledge import KEPT_SOURCES, NOT_STRATEGIES

if TYPE_CHECKING:
    from topstep_bot.config import BotConfig
    from topstep_bot.engine import TradingCore
    from topstep_bot.knowledge import Observation

log = logging.getLogger("topstep_bot.probability")
UTC = timezone.utc

STRENGTH = 12.0  # pseudo-observations a level's own outcomes must outweigh to move away from the level above
ROOT_STRENGTH = 4.0  # how firmly the very top (no evidence at all) holds P(win) at 45% and 1R either way
ROOT_P, ROOT_WIN_R, ROOT_LOSS_R = 0.45, 1.0, 1.0
NETWORK_WEIGHT = 0.3  # an outcome on a related market counts this much compared with one on this market
PRIOR_FLOOR = 0.05  # long-run memory outcomes (older than the recent window): this weight each
Z80 = 1.2816  # half-width of an 80% range, in standard deviations
KELLY_SHARE = 0.25  # quarter Kelly: a big edge measured on a few weeks is often smaller than it looks
KELLY_FULL = 0.04  # a quarter-Kelly fraction this big earns the full risk per trade
MIN_CONVICTION = 0.25
NETWORK_REFRESH_SECONDS = 3600


@dataclass(frozen=True)
class Estimate:
    p_win: float
    win_r: float  # average net R of a winner
    loss_r: float  # average net R lost by a loser (positive)
    ev_r: float  # expected net R per trade
    p_lo: float
    p_hi: float
    ev_lo: float
    ev_hi: float
    n: int  # recent outcomes of this strategy and side on this market
    n_eff: float  # their recency- and source-weighted count
    network_n: int  # outcomes on related markets that informed it
    level: str  # the most specific level with real evidence: cell | slot | side | network | none
    kelly: float  # quarter-Kelly fraction (0 = no edge)
    memory_n: int = 0  # long-run memory outcomes of this strategy and side that informed it

    @property
    def payoff(self) -> float:
        return self.win_r / self.loss_r if self.loss_r > 0 else 0.0

    def confident_loser(self, min_samples: int) -> bool:
        """At least 90% sure this signal loses money after costs, on enough of its own evidence."""
        return self.n_eff >= min_samples and self.ev_hi < 0

    def conviction(self, max_conviction: float) -> float:
        """Size multiplier for the aggressive mode: quarter Kelly against a reference edge, within bounds."""
        if self.ev_r <= 0 or self.kelly <= 0:
            return MIN_CONVICTION
        return round(max(MIN_CONVICTION, min(max_conviction, self.kelly / KELLY_FULL)), 2)

    def to_dict(self) -> dict[str, Any]:
        d = {k: (round(v, 3) if isinstance(v, float) else v) for k, v in asdict(self).items()}
        d["payoff"] = round(self.payoff, 2)
        return d

    def text(self) -> str:
        where = {"cell": "this time of day and market mood", "slot": "this time of day", "side": "all day",
                 "network": "related markets", "none": "no history yet"}[self.level]
        return (f"P(win) {self.p_win:.0%} (80% range {self.p_lo:.0%}-{self.p_hi:.0%}), wins {self.win_r:+.2f}R / "
                f"losses -{self.loss_r:.2f}R, expected {self.ev_r:+.2f}R per trade after costs "
                f"({self.n} outcomes here{f', {self.memory_n} in the long-run memory' if self.memory_n else ''}"
                f"{f', {self.network_n} on related markets' if self.network_n else ''}; "
                f"most specific evidence: {where})")


@dataclass
class _Agg:
    w: float = 0.0
    wins: float = 0.0  # weighted count of winners
    win_r: float = 0.0  # weighted sum of net R of winners
    win_r2: float = 0.0  # ... and of its square (for the spread)
    loss_r: float = 0.0  # weighted sum of |net R| of losers
    loss_r2: float = 0.0
    n: int = 0  # recent outcomes (not the long-run memory's)
    own_w: float = 0.0  # ... and their weight
    memory_n: int = 0

    def add(self, net: float, w: float, *, memory: bool = False) -> None:
        if memory:
            self.memory_n += 1
        else:
            self.n += 1
            self.own_w += w
        self.w += w
        if net > 0:
            self.wins += w
            self.win_r += w * net
            self.win_r2 += w * net * net
        else:
            self.loss_r += w * -net
            self.loss_r2 += w * net * net

    @property
    def losses(self) -> float:
        return max(0.0, self.w - self.wins)

    def merge(self, o: _Agg) -> None:
        for f in ("w", "wins", "win_r", "win_r2", "loss_r", "loss_r2", "n", "own_w", "memory_n"):
            setattr(self, f, getattr(self, f) + getattr(o, f))

    def minus(self, o: _Agg) -> _Agg:
        """The outcomes in this group that are not in ``o`` (a subset of it)."""
        out = _Agg()
        for f in ("w", "wins", "win_r", "win_r2", "loss_r", "loss_r2", "own_w"):
            setattr(out, f, max(0.0, getattr(self, f) - getattr(o, f)))
        out.n, out.memory_n = max(0, self.n - o.n), max(0, self.memory_n - o.memory_n)
        return out


def _pool(total: float, weight: float, parent: float, strength: float) -> float:
    """A weighted mean (``total`` / ``weight``) pulled toward ``parent`` by ``strength`` pseudo-observations."""
    whole = weight + strength
    return (total + parent * strength) / whole if whole > 0 else parent


def _spread(sum_x: float, sum_x2: float, weight: float, fallback: float) -> float:
    if weight < 2:
        return fallback
    mean = sum_x / weight
    return math.sqrt(max(0.0, sum_x2 / weight - mean * mean)) or fallback


class ProbabilityModel:
    """Weighted outcome totals by strategy, side, time of day and regime: this market, the long-run memory
    (at a floor weight) and the knowledge network (related markets, at ``NETWORK_WEIGHT``)."""

    def __init__(self, recent: Iterable[Observation], *, today: date, half_life_days: int = 20,
                 real_weight: float = 2.0, prior: Iterable[Observation] = (), network: Iterable[Observation] = ()):
        self.today = today
        self.cells: dict[tuple[str, str, str, str], _Agg] = {}
        self.net: dict[tuple[str, str], _Agg] = {}
        hl = max(1, half_life_days)

        def recency(o: Observation) -> float:
            try:
                age = max(0, (today - date.fromisoformat(o.day)).days)
            except ValueError:
                age = 0
            return 0.5 ** (age / hl)

        def source(o: Observation) -> float:
            return real_weight if o.source in KEPT_SOURCES else 1.0

        for o in recent:
            if o.strategy not in NOT_STRATEGIES:
                self.cells.setdefault((o.strategy, o.side, o.slot, o.regime), _Agg()).add(o.net_r, recency(o) * source(o))
        for o in prior:  # the long-run memory: a steady background that recent outcomes can overrule
            if o.strategy not in NOT_STRATEGIES:
                self.cells.setdefault((o.strategy, o.side, o.slot, o.regime), _Agg()).add(
                    o.net_r, PRIOR_FLOOR * source(o), memory=True)
        for o in network:
            if o.strategy not in NOT_STRATEGIES:
                w = NETWORK_WEIGHT * max(PRIOR_FLOOR, recency(o)) * source(o)
                self.net.setdefault((o.strategy, o.side), _Agg()).add(o.net_r, w)

    def _here(self, strategy: str, side: str, slot: str | None = None, regime: str | None = None) -> _Agg:
        out = _Agg()
        for (s, sd, sl, rg), agg in self.cells.items():
            if s == strategy and sd == side and (slot is None or sl == slot) and (regime is None or rg == regime):
                out.merge(agg)
        return out

    def estimate(self, strategy: str, side: str, slot: str, regime: str) -> Estimate:
        side = side.upper()
        network = self.net.get((strategy, side), _Agg())
        here = self._here(strategy, side)
        in_slot = self._here(strategy, side, slot)
        cell = self._here(strategy, side, slot, regime)
        # Rings, from the outside in: related markets, the rest of the day, the other market mood at this
        # time of day. Each ring sets the starting point for the next one in, as far as its evidence goes
        # (never more than STRENGTH outcomes' worth), and the cell itself has the last word. Every outcome
        # sits in exactly one ring, so nothing is counted twice and the range isn't narrower than it is.
        rings = (network, here.minus(in_slot), in_slot.minus(cell))
        p, win_r, loss_r, strength = ROOT_P, ROOT_WIN_R, ROOT_LOSS_R, ROOT_STRENGTH
        for ring in (*rings, cell):
            p, win_r, loss_r = (_pool(ring.wins, ring.w, p, strength),
                                _pool(ring.win_r, ring.wins, win_r, strength * p),
                                _pool(ring.loss_r, ring.losses, loss_r, strength * (1 - p)))
            if ring is not cell:
                strength = min(STRENGTH, ring.w + strength)
        info = cell.w + strength  # outcomes' worth of evidence the estimate stands on
        wins_n, losses_n = cell.wins + strength * p, cell.losses + strength * (1 - p)
        level, sd_win, sd_loss = "none", 0.5, 0.3  # typical spread of a win / a loss in R until measured
        both = _Agg()
        both.merge(network)
        both.merge(here)
        for name, agg in (("network", both), ("side", here), ("slot", in_slot), ("cell", cell)):
            sd_win = _spread(agg.win_r, agg.win_r2, agg.wins, sd_win)
            sd_loss = _spread(agg.loss_r, agg.loss_r2, agg.losses, sd_loss)
            if agg.w >= STRENGTH / 2:
                level = name
        ev = p * win_r - (1 - p) * loss_r
        var_p = p * (1 - p) / (info + 1)
        var_ev = ((win_r + loss_r) ** 2 * var_p + p * p * sd_win ** 2 / (wins_n + 1)
                  + (1 - p) ** 2 * sd_loss ** 2 / (losses_n + 1))
        sd_p, sd_ev = math.sqrt(var_p), math.sqrt(var_ev)
        b = win_r / loss_r if loss_r > 0 else 0.0
        kelly = max(0.0, (p - (1 - p) / b) * KELLY_SHARE) if b > 0 else 0.0
        return Estimate(
            p_win=p, win_r=win_r, loss_r=loss_r, ev_r=ev,
            p_lo=max(0.0, p - Z80 * sd_p), p_hi=min(1.0, p + Z80 * sd_p),
            ev_lo=ev - Z80 * sd_ev, ev_hi=ev + Z80 * sd_ev,
            n=here.n, n_eff=here.own_w, network_n=network.n, level=level, kelly=kelly, memory_n=here.memory_n,
        )


# ------------------------------------------------------------------------------ the knowledge network

def knowledge_folders(cfg: BotConfig) -> list[Path]:
    """Where knowledge bases live: this account's folder first, then every other account's, then the data folder."""
    folders = [cfg.account_dir]
    accounts = cfg.data_path / "accounts"
    if accounts.is_dir():
        folders += [p for p in sorted(accounts.iterdir()) if p.is_dir() and p != cfg.account_dir]
    if cfg.account_dir != cfg.data_path:
        folders.append(cfg.data_path)
    return folders


def network_paths(cfg: BotConfig, symbol: str) -> list[Path]:
    """The knowledge bases of every other market in this market's asset class (this account's, then every
    account's), at the same bar size: one per market."""
    from topstep_bot.instruments import SPECS, class_of

    cls = class_of(symbol)
    tf = cfg.instrument.timeframe_minutes
    others = [s for s, spec in SPECS.items() if spec.asset_class == cls and s != symbol.upper()]
    out, seen = [], set()
    for folder in knowledge_folders(cfg):
        for s in others:
            path = folder / f"knowledge_{s}_{tf}m.json"
            if s not in seen and path.exists():  # one knowledge base per market is enough
                seen.add(s)
                out.append(path)
    return out


def load_network(paths: Iterable[Path]) -> tuple[list[Observation], list[str]]:
    """Observations from related markets' knowledge base files (read-only, never written)."""
    from topstep_bot.knowledge import Observation

    obs: list[Observation] = []
    markets: list[str] = []
    for path in paths:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        rows = data.get("observations") if isinstance(data, dict) else data
        if not isinstance(rows, list):
            continue
        added = 0
        for row in rows:
            try:
                obs.append(Observation.from_dict(row))
                added += 1
            except (TypeError, ValueError):
                continue
        if added:
            markets.append(path.stem.split("_")[1])
    return obs, markets


# ------------------------------------------------------------------------------ the core's calculator

class Calculator:
    """The probability calculator one trading core asks (cached: rebuilt when what it knows changes)."""

    def __init__(self, core: TradingCore):
        self.core = core
        self.daily_only = False  # training: one model per replayed day (walk-forward, and fast)
        self.network: list[Observation] = []
        self.network_markets: list[str] = []
        self._network_at = 0.0
        self._model: tuple[tuple, ProbabilityModel] | None = None

    def refresh_network(self, force: bool = False) -> None:
        cfg = self.core.cfg
        if not cfg.probability.network or self.daily_only:
            return
        now = time.monotonic()
        if not force and self._network_at and now - self._network_at < NETWORK_REFRESH_SECONDS:
            return
        self._network_at = now
        root = (self.core.contract.root or self.core.contract.name).upper()
        try:
            self.network, self.network_markets = load_network(network_paths(cfg, root))
        except Exception:  # noqa: BLE001 - the network is a bonus; this market's own evidence still counts
            log.exception("Could not read the knowledge network")

    def model(self, today: date) -> ProbabilityModel | None:
        kb = self.core.knowledge
        if kb is None:
            return None
        memory = self.core.memory.knowledge if self.core.memory is not None else None
        if self.daily_only:
            key: tuple = (today, id(kb))
        else:
            self.refresh_network()
            key = (today, id(kb), kb.updated, len(kb.obs), id(memory), memory.updated if memory else -1,
                   self._network_at)
        if self._model is None or self._model[0] != key:
            from topstep_bot.evidence import prior_observations

            prior = prior_observations(kb, memory) if memory is not None else []
            # training: only what was known before the replayed day (walk-forward, like live)
            recent = [o for o in kb.obs if o.day < today.isoformat()] if self.daily_only else kb.obs
            self._model = (key, ProbabilityModel(recent, today=today, half_life_days=kb.half_life_days,
                                                 real_weight=kb.real_weight, prior=prior, network=self.network))
        return self._model[1]

    def estimate(self, strategy: str, side: str, slot: str | None = None, regime: str | None = None,
                 when: datetime | None = None) -> Estimate | None:
        if not self.core.cfg.probability.enabled or strategy in NOT_STRATEGIES:
            return None
        when = when or self.core.clock()
        model = self.model(self.core.schedule.trading_day(when))
        if model is None:
            return None
        return model.estimate(strategy, side, slot or self.core.slot(when), regime or self.core.regime.value)


# ------------------------------------------------------------------------------ the outlook

def outlook(calc_or_model: Calculator | ProbabilityModel, strategies: Iterable[str], *, slot: str | None = None,
            regime: str | None = None) -> list[dict[str, Any]]:
    """Every strategy and side as the calculator sees it now (or all day, without a slot), best expected value first."""
    rows = []
    for name in strategies:
        if name in NOT_STRATEGIES:
            continue
        for side in ("LONG", "SHORT"):
            if isinstance(calc_or_model, Calculator):
                est = calc_or_model.estimate(name, side, slot, regime)
            else:  # without a live market: the whole day (no time-of-day or mood cell matches "-")
                est = calc_or_model.estimate(name, side, slot or "-", regime or "-")
            if est is not None:
                rows.append({"strategy": name, "side": side, **est.to_dict(), "text": est.text()})
    rows.sort(key=lambda r: -r["ev_r"])
    return rows


def outlook_text(rows: list[dict[str, Any]], titles: dict[str, str], *, where: str, top: int = 8) -> str:
    if not rows:
        return "The probability calculator has nothing to go on yet: it needs the knowledge base (trained or watched signals)."
    lines = [f"The probability calculator, {where} (after fees and slippage; 80% ranges):"]
    for r in rows[:top]:
        sure = "edge" if r["ev_lo"] > 0 else ("losing" if r["ev_hi"] < 0 else "unclear")
        lines.append(f"• {titles.get(r['strategy'], r['strategy'])} {r['side'].lower()}: P(win) {r['p_win']:.0%} "
                     f"({r['p_lo']:.0%}-{r['p_hi']:.0%}), {r['ev_r']:+.2f}R a trade, {r['n']} outcomes here"
                     + (f" + {r['network_n']} on related markets" if r["network_n"] else "") + f" - {sure}")
    lines.append("Edge: at least 90% sure it makes money after costs. Losing: at least 90% sure it doesn't (the bot "
                 "skips those). A probability is a long-run frequency, never a promise about the next trade.")
    return "\n".join(lines)


# ------------------------------------------------------------------------------ how well it predicts

BUCKETS = ((0.0, 0.35), (0.35, 0.45), (0.45, 0.55), (0.55, 0.65), (0.65, 1.01))


def calibration(observations: Iterable[Observation], *, sources: tuple[str, ...] | None = None) -> dict[str, Any]:
    """How the calculator's predictions held up: for outcomes it predicted before they happened.

    Brier score (lower is better; always guessing the overall win rate scores ``base``), skill (above 0 =
    better than that guess), and a reliability table: predicted vs actual win rate per band."""
    rows = [o for o in observations if o.p_win is not None and o.strategy not in NOT_STRATEGIES
            and (sources is None or o.source in sources)]
    out: dict[str, Any] = {"n": len(rows), "buckets": [], "brier": None, "base": None, "skill": None,
                           "ev_predicted": None, "ev_actual": None, "by_source": {}}
    if not rows:
        return out
    ys = [1.0 if o.net_r > 0 else 0.0 for o in rows]
    rate = sum(ys) / len(ys)
    brier = sum((o.p_win - y) ** 2 for o, y in zip(rows, ys, strict=True)) / len(rows)
    base = sum((rate - y) ** 2 for y in ys) / len(ys)
    out.update(brier=round(brier, 4), base=round(base, 4), win_rate=round(rate, 3),
               skill=round(1 - brier / base, 3) if base > 0 else None)
    with_ev = [o for o in rows if o.ev_r is not None]
    if with_ev:
        out["ev_predicted"] = round(sum(o.ev_r for o in with_ev) / len(with_ev), 3)
        out["ev_actual"] = round(sum(o.net_r for o in with_ev) / len(with_ev), 3)
    for lo, hi in BUCKETS:
        sel = [(o, y) for o, y in zip(rows, ys, strict=True) if lo <= o.p_win < hi]
        if sel:
            out["buckets"].append({"band": f"{lo:.0%}-{min(hi, 1.0):.0%}", "n": len(sel),
                                   "predicted": round(sum(o.p_win for o, _ in sel) / len(sel), 3),
                                   "actual": round(sum(y for _, y in sel) / len(sel), 3)})
    for src in sorted({o.source for o in rows}):
        sel = [o for o in rows if o.source == src]
        out["by_source"][src] = {"n": len(sel), "actual": round(sum(o.net_r > 0 for o in sel) / len(sel), 3),
                                 "predicted": round(sum(o.p_win for o in sel) / len(sel), 3)}
    return out


def calibration_text(cal: dict[str, Any]) -> str:
    if not cal.get("n"):
        return ("The probability calculator has no scored predictions yet: they build up as signals finish "
                "(training replays add them too).")
    lines = [f"Probability calculator, {cal['n']:,} predictions scored: Brier {cal['brier']:.3f} "
             f"(guessing the average win rate scores {cal['base']:.3f}; skill {cal['skill']:+.1%})."
             if cal.get("skill") is not None else f"Probability calculator, {cal['n']:,} predictions scored."]
    for b in cal["buckets"]:
        lines.append(f"  said {b['band']}: {b['n']} signals, predicted {b['predicted']:.0%}, actually won {b['actual']:.0%}")
    if cal.get("ev_predicted") is not None:
        lines.append(f"  expected {cal['ev_predicted']:+.2f}R per signal on average, got {cal['ev_actual']:+.2f}R")
    return "\n".join(lines)
