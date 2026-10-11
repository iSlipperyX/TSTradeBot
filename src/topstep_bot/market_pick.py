"""The bot picks its own market (``instrument.auto_pick: true``).

When the bot starts (and so every trading day, after the daily restart), each candidate market is
replayed through the account's strategy on its recent history, exactly as training does it. The
replayed days are split in two: what the bot learns on the earlier two thirds decides which signals
it would have taken on the last third, and only that held-back part is scored. That keeps the pick
honest: a market is chosen for what worked on days the bot hadn't seen, not for what it was fitted to.

Scoring, per candidate:
  * R per day on the held-back days, after costs, from the trades it would have taken (at most
    ``risk.max_trades_per_day`` a day, and only signals that fit this account's risk: a stop too
    wide for even 1 contract can't be traded)
  * a candidate needs at least ``MIN_TEST_TRADES`` such trades and a result above zero to be picked
  * the current market is kept unless another beats it by ``SWITCH_MARGIN_R`` per day, so the bot
    doesn't hop between markets on noise

If no candidate qualifies, the bot trades ``instrument.symbol`` (your set market). An open position
on the account keeps its market. The pick is saved in the account's folder (market_pick.json) and
reused for ``VALID_HOURS``, so a restart during the day doesn't pick again.

Honest limits: TopstepX keeps about 60 days of intraday history, so the held-back part is two to
four weeks until the long-run memory has collected more. A short sample is noisy; the margin and
the minimum trade count only reduce, not remove, the chance that luck decides the pick. It never
makes the bot trade bigger or more often: every risk setting and Topstep rule applies as before.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from topstep_bot.config import BotConfig
    from topstep_bot.knowledge import Observation
    from topstep_bot.models import Bar, Contract

log = logging.getLogger(__name__)

DEFAULT_CANDIDATES = ("MNQ", "MES", "M2K", "MYM", "MGC", "MCL")
PICK_FILE = "market_pick.json"
MIN_TEST_TRADES = 6
SWITCH_MARGIN_R = 0.05
VALID_HOURS = 20
TEST_SHARE = 1 / 3
PICK_DAYS = 120  # history scored per market, as far as the market library reaches (TopstepX keeps about 60 days)
MIN_SIZED = 0.5  # at least half the signals must fit the account's risk at 1 contract or more


@dataclass
class Candidate:
    symbol: str
    days: int = 0  # trading days replayed
    test_days: int = 0  # held-back days that were scored
    trades: int = 0  # trades it would have taken on them
    r_per_day: float | None = None  # after costs
    usd_per_day: float | None = None
    win_rate: float | None = None
    sized: float | None = None  # share of signals that fit the account's risk
    note: str = ""  # why it can't be picked (no data, too little evidence, too big for the risk...)

    @property
    def eligible(self) -> bool:
        return (not self.note and self.trades >= MIN_TEST_TRADES and (self.r_per_day or 0) > 0
                and (self.sized or 0) >= MIN_SIZED)

    def line(self) -> str:
        if self.r_per_day is None:
            return f"{self.symbol}: {self.note or 'not scored'}"
        text = (f"{self.symbol}: {self.r_per_day:+.2f}R/day (${self.usd_per_day:+,.0f}/day) over {self.test_days} "
                f"held-back days, {self.trades} trades")
        if self.win_rate is not None:
            text += f", {self.win_rate:.0%} won"
        return text + (f" - {self.note}" if self.note else "")


@dataclass
class Pick:
    symbol: str
    reason: str
    at: str  # ISO time it was made
    fallback: str  # instrument.symbol
    fingerprint: str
    candidates: list[Candidate] = field(default_factory=list)
    kept: bool = False  # the previous pick (or an open position's market) was kept

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["candidates"] = [{**asdict(c), "eligible": c.eligible, "line": c.line()} for c in self.candidates]
        return d

    @classmethod
    def from_dict(cls, d: dict) -> Pick:
        known = {f for f in Candidate.__dataclass_fields__}
        cands = [Candidate(**{k: v for k, v in c.items() if k in known}) for c in d.get("candidates") or []]
        return cls(symbol=d["symbol"], reason=d.get("reason", ""), at=d.get("at", ""), fallback=d.get("fallback", ""),
                   fingerprint=d.get("fingerprint", ""), candidates=cands, kept=bool(d.get("kept")))

    def text(self) -> str:
        lines = [f"Market picked: {self.symbol} - {self.reason}"]
        lines += [f"  {c.line()}" for c in sorted(self.candidates, key=lambda c: -(c.r_per_day or -99))]
        return "\n".join(lines)


def candidates_for(cfg: BotConfig) -> list[str]:
    """The markets auto_pick may choose from: the configured list, or the micros, minus any the plan doesn't allow."""
    from topstep_bot.instruments import SPECS
    from topstep_bot.risk.topstep import PLANS, product_limit

    plan = PLANS[cfg.account.plan]
    wanted = cfg.instrument.candidates or list(DEFAULT_CANDIDATES)
    return [s for s in wanted if s in SPECS and product_limit(s, plan) != 0]


def fingerprint(cfg: BotConfig) -> str:
    """What the pick depends on: a change in any of these makes the bot pick again."""
    return "|".join([",".join(candidates_for(cfg)), cfg.strategy.name, cfg.account.plan, cfg.account.stage,
                     f"{cfg.risk.risk_per_trade:g}", str(cfg.instrument.timeframe_minutes), cfg.instrument.symbol])


def pick_path(cfg: BotConfig) -> Path:
    return cfg.account_dir / PICK_FILE


def load_pick(cfg: BotConfig) -> Pick | None:
    try:
        return Pick.from_dict(json.loads(pick_path(cfg).read_text(encoding="utf-8")))
    except (OSError, ValueError, KeyError, TypeError):
        return None


def save_pick(cfg: BotConfig, pick: Pick) -> None:
    path = pick_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(pick.to_dict(), indent=1), encoding="utf-8")


def current_symbol(cfg: BotConfig) -> str:
    """The market an account trades: its last pick with auto_pick on, else instrument.symbol."""
    if cfg.instrument.auto_pick:
        pick = load_pick(cfg)
        if pick and pick.symbol in candidates_for(cfg) + [cfg.instrument.symbol]:
            return pick.symbol
    return cfg.instrument.symbol


def fresh(pick: Pick | None, cfg: BotConfig, now: datetime) -> bool:
    if pick is None or pick.fingerprint != fingerprint(cfg):
        return False
    try:
        return now - datetime.fromisoformat(pick.at) < timedelta(hours=VALID_HOURS)
    except ValueError:
        return False


# ------------------------------------------------------------------------------ scoring

def for_symbol(cfg: BotConfig, symbol: str) -> BotConfig:
    trial = cfg.model_copy(deep=True)
    trial.instrument.symbol, trial.instrument.contract_id, trial.instrument.auto_pick = symbol, None, False
    return trial


def score_observations(cfg: BotConfig, symbol: str, observations: list[Observation], trading_days: list[str]) -> Candidate:
    """Score one market from its replayed signals: learn on the earlier days, count what it would have taken later."""
    from topstep_bot.knowledge import KnowledgeBase

    cand = Candidate(symbol, days=len(trading_days))
    if len(trading_days) < 9:
        cand.note = f"only {len(trading_days)} days of history"
        return cand
    split = trading_days[int(len(trading_days) * (1 - TEST_SHARE))]
    test_days = [d for d in trading_days if d >= split]
    cand.test_days = len(test_days)
    k = cfg.knowledge
    adaptive = cfg.strategy.name == "adaptive"
    learned = KnowledgeBase(None, half_life_days=k.half_life_days, min_samples=k.min_samples, min_edge_r=k.min_edge_r,
                            source="train")
    learned.obs = [o for o in observations if o.day < split]
    learned._reindex()
    signals = [o for o in observations if o.day >= split and (adaptive or o.strategy == cfg.strategy.name)]
    if signals:
        cand.sized = round(sum(1 for o in signals if o.usd is not None) / len(signals), 2)
    taken: list[Observation] = []
    per_day: dict[str, int] = {}
    for o in sorted(signals, key=lambda o: (o.day, o.time)):
        if o.usd is None or per_day.get(o.day, 0) >= cfg.risk.max_trades_per_day:
            continue
        if adaptive and not learned.verdict(o.strategy, o.slot, o.regime, date.fromisoformat(o.day)).allowed:
            continue
        per_day[o.day] = per_day.get(o.day, 0) + 1
        taken.append(o)
    cand.trades = len(taken)
    cand.r_per_day = round(sum(o.net_r for o in taken) / len(test_days), 3)
    cand.usd_per_day = round(sum(o.usd or 0.0 for o in taken) / len(test_days), 2)
    cand.win_rate = round(sum(1 for o in taken if o.net_r > 0) / len(taken), 2) if taken else None
    if signals and (cand.sized or 0) < MIN_SIZED:
        cand.note = f"too big for ${cfg.risk.risk_per_trade:,.0f} per trade: most stops are too wide for even 1 contract"
    elif cand.trades < MIN_TEST_TRADES:
        cand.note = f"too few trades to judge (need {MIN_TEST_TRADES})"
    elif (cand.r_per_day or 0) <= 0:
        cand.note = "no edge on the held-back days"
    return cand


async def score_market(cfg: BotConfig, symbol: str, contract: Contract, bars: list[Bar]) -> Candidate:
    """Replay ``bars`` through the account's strategy (every strategy for adaptive) and score the result."""
    from topstep_bot.knowledge import KnowledgeBase, train_from_bars

    trial = for_symbol(cfg, symbol)
    kb = KnowledgeBase(None, half_life_days=cfg.knowledge.half_life_days, min_samples=cfg.knowledge.min_samples,
                       min_edge_r=cfg.knowledge.min_edge_r)
    strategies = None if cfg.strategy.name == "adaptive" else [cfg.strategy.name]
    try:
        result = await train_from_bars(trial, contract, bars, kb, strategies=strategies)
    except ValueError as exc:  # too little history for the strategies' warm-up
        return Candidate(symbol, note=str(exc))
    # every replayed trading day counts, also the ones without a signal
    days = _trading_days(trial, bars, date.fromisoformat(result["from"]))
    return score_observations(cfg, symbol, kb.obs, days)


def _trading_days(cfg: BotConfig, bars: list[Bar], first: date) -> list[str]:
    from topstep_bot.sessions import SessionSchedule

    schedule = SessionSchedule(cfg.session)
    return sorted({d.isoformat() for d in (schedule.trading_day(b.ts) for b in bars) if d >= first
                   and schedule.is_trade_day(d)})


def choose(cands: list[Candidate], current: str | None, fallback: str) -> tuple[str, str, bool]:
    """(symbol, reason, kept) from scored candidates."""
    eligible = sorted((c for c in cands if c.eligible), key=lambda c: -(c.r_per_day or 0))
    if not eligible:
        return fallback, (f"no market showed an edge on the days held back from training, so the bot trades "
                          f"{fallback}, your set market"), False
    best = eligible[0]
    cur = next((c for c in eligible if c.symbol == current), None)
    if cur is not None and cur is not best and (best.r_per_day or 0) - (cur.r_per_day or 0) < SWITCH_MARGIN_R:
        return cur.symbol, (f"kept {cur.symbol}: {best.symbol} did only slightly better "
                            f"({best.r_per_day:+.2f} vs {cur.r_per_day:+.2f}R/day), not enough to switch"), True
    return best.symbol, (f"best result on the held-back days: {best.r_per_day:+.2f}R per day after costs "
                         f"({best.trades} trades over {best.test_days} days)"), False


async def pick_market(
    cfg: BotConfig,
    load: Callable[[str], Awaitable[tuple[Contract, list[Bar]]]],
    *,
    now: datetime | None = None,
    held: str | None = None,
    progress: Callable[[str], None] | None = None,
) -> Pick:
    """Score every candidate and choose. ``load(symbol)`` returns its contract and recent bars.

    ``held``: the market of an open position on the account - kept, whatever the scores say."""
    now = now or datetime.now(UTC)
    fallback = cfg.instrument.symbol
    previous = load_pick(cfg)
    current = previous.symbol if previous else None
    if held:
        pick = Pick(held, f"kept {held}: the account has an open position in it", now.isoformat(timespec="minutes"),
                    fallback, fingerprint(cfg), previous.candidates if previous else [], kept=True)
        save_pick(cfg, pick)
        return pick
    cands = []
    for symbol in candidates_for(cfg):
        if progress:
            progress(symbol)
        try:
            contract, bars = await load(symbol)
            cands.append(await score_market(cfg, symbol, contract, bars) if bars else Candidate(symbol, note="no history"))
        except Exception as exc:  # noqa: BLE001 - one market without data mustn't stop the others
            log.info("Market pick: %s could not be scored: %s", symbol, exc)
            cands.append(Candidate(symbol, note=f"no data: {exc}"[:120]))
    symbol, reason, kept = choose(cands, current, fallback)
    pick = Pick(symbol, reason, now.isoformat(timespec="minutes"), fallback, fingerprint(cfg), cands, kept)
    save_pick(cfg, pick)
    return pick
