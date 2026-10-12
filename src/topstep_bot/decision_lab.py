"""The decision lab: replay the bot's decision rules side by side on the same history, without peeking.

The long-run memory (memory.py) knows the outcome of every signal every strategy gave on every
day in the market library. The lab walks through those days in order and, on each day, lets each
decision rule (``knowledge.policy``, see evidence.py) decide which signals to take - using only
what was known before that day, exactly as the live bot would have. The bot's own limits are
applied in R: one trade at a time, the entry window, ``max_trades_per_day``, the stop after
``max_consecutive_losses``, the cooldown after a loss and the personal daily loss limit.

Results are in R after fees and slippage (1R = the money a trade risks), next to what each
strategy would have made on its own. Because both rules trade the same days, the lab compares
them day by day and resamples those days (a bootstrap) to say how sure the difference is.

``knowledge.policy: auto`` switches to the 2.2 "evidence" rule only when the lab shows it ahead
with at least 95% confidence over at least ``MIN_DAYS`` trading days and ``MIN_TRADES`` trades
each - otherwise the bot keeps the 1.2 "classic" rule. The lab reruns after every long-run
learning run, so the choice follows the evidence as the memory grows.

It is a simulation: signals are replayed on bars with assumed slippage, the same way training
does. Real fills are usually a little worse. No result here promises future profit.
"""

from __future__ import annotations

import json
import logging
import random
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from topstep_bot.evidence import EvidenceModel, evidence_verdict
from topstep_bot.knowledge import NOT_STRATEGIES, KnowledgeBase, Observation

if TYPE_CHECKING:
    from topstep_bot.config import BotConfig

log = logging.getLogger("topstep_bot.decision_lab")
UTC = timezone.utc

POLICIES = ("classic", "evidence")
MIN_DAYS = 60  # test days before the lab may pick a rule
MIN_TRADES = 30  # trades each rule needs in the test
CONFIDENCE = 0.95  # how sure the lab must be that "evidence" is ahead
WARMUP_DAYS = 20  # trading days of history before the first test day
BOOTSTRAP = 2000


@dataclass
class Rules:
    """The bot's limits, in the units the lab works in."""

    history_days: int = 60
    half_life_days: int = 20
    min_samples: int = 8
    min_edge_r: float = 0.05
    real_weight: float = 2.0
    timeframe: int = 5
    trade_start: time = time(8, 30)
    last_entry: time = time(14, 30)
    max_trades: int = 4
    max_losses: int = 2
    cooldown_minutes: int = 10
    daily_loss_r: float = 3.33  # personal daily loss limit / risk per trade
    strategies: tuple[str, ...] = ()  # the adaptive strategy's sub-strategies (empty = every strategy seen)
    risk_usd: float = 150.0

    @classmethod
    def from_config(cls, cfg: BotConfig) -> Rules:
        from topstep_bot.strategies.base import parse_hhmm

        k, r, s = cfg.knowledge, cfg.risk, cfg.session
        subs = tuple(cfg.strategy.params.get("strategies") or ()) if cfg.strategy.name == "adaptive" else ()
        return cls(history_days=k.history_days, half_life_days=k.half_life_days, min_samples=k.min_samples,
                   min_edge_r=k.min_edge_r, real_weight=k.real_trade_weight, timeframe=cfg.instrument.timeframe_minutes,
                   trade_start=parse_hhmm(s.trade_start), last_entry=parse_hhmm(s.last_entry),
                   max_trades=r.max_trades_per_day, max_losses=r.max_consecutive_losses,
                   cooldown_minutes=r.cooldown_minutes_after_loss,
                   daily_loss_r=r.personal_daily_loss_limit / r.risk_per_trade, strategies=subs,
                   risk_usd=r.risk_per_trade)


@dataclass
class Book:
    """One rule's simulated trading: every trade it took and its result per day."""

    trades: list[dict] = field(default_factory=list)
    daily: dict[str, float] = field(default_factory=dict)

    def summary(self, risk_usd: float) -> dict[str, Any]:
        r = [t["net_r"] for t in self.trades]
        equity = peak = dd = 0.0
        for day in sorted(self.daily):
            equity += self.daily[day]
            peak = max(peak, equity)
            dd = max(dd, peak - equity)
        total = sum(r)
        wins = sum(v for v in r if v > 0)
        losses = -sum(v for v in r if v < 0)
        return {"trades": len(r), "total_r": round(total, 2), "avg_r": round(total / len(r), 3) if r else None,
                "win_rate": round(sum(v > 0 for v in r) / len(r), 3) if r else None,
                "profit_factor": round(wins / losses, 2) if losses else None, "max_drawdown_r": round(dd, 2),
                "days_traded": sum(1 for v in self.daily.values() if v), "approx_usd": round(total * risk_usd)}


def _minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")[:2]
    return int(h) * 60 + int(m)


def _slice(obs: list[Observation], start: str, end: str) -> list[Observation]:
    return [o for o in obs if start <= o.day < end]


class Lab:
    def __init__(self, observations: Iterable[Observation], rules: Rules):
        self.rules = rules
        wanted = set(rules.strategies)
        self.obs = sorted((o for o in observations if o.strategy not in NOT_STRATEGIES
                           and (not wanted or o.strategy in wanted)), key=lambda o: (o.day, o.time))
        self.days = sorted({o.day for o in self.obs})

    # ------------------------------------------------------------- deciding

    def _deciders(self, day: str) -> dict[str, Callable[[Observation], tuple[bool, float]]]:
        """Each rule's verdict function for one test day, built only from what was known before it."""
        r = self.rules
        today = date.fromisoformat(day)
        window = (today - timedelta(days=r.history_days)).isoformat()
        recent = _slice(self.obs, window, day)
        prior = _slice(self.obs, "", window)
        kb = KnowledgeBase(None, half_life_days=r.half_life_days, min_samples=r.min_samples, min_edge_r=r.min_edge_r,
                           real_weight=r.real_weight)
        kb.obs = recent
        model = EvidenceModel(recent, prior, today=today, half_life_days=r.half_life_days, real_weight=r.real_weight)
        cache: dict[tuple[str, str, str, str], tuple[bool, float]] = {}

        def classic(o: Observation) -> tuple[bool, float]:
            key = ("c", o.strategy, o.slot, o.regime)
            if key not in cache:
                v = kb.verdict(o.strategy, o.slot, o.regime, today)
                cache[key] = (v.allowed, v.score)
            return cache[key]

        def evidence(o: Observation) -> tuple[bool, float]:
            key = ("e", o.strategy, o.slot, o.regime)
            if key not in cache:
                v = evidence_verdict(model, o.strategy, o.slot, o.regime, min_samples=r.min_samples, min_edge_r=r.min_edge_r)
                cache[key] = (v.allowed, v.score)
            return cache[key]

        return {"classic": classic, "evidence": evidence}

    def _trade_day(self, signals: list[Observation], decide: Callable[[Observation], tuple[bool, float]],
                   book: Book, day: str) -> None:
        """Take the day's signals one bar at a time under the bot's limits."""
        r = self.rules
        lo, hi = r.trade_start.hour * 60 + r.trade_start.minute, r.last_entry.hour * 60 + r.last_entry.minute
        busy_until = -1
        cooldown_until = -1
        day_r, taken, losses_in_row = 0.0, 0, 0
        by_bar: dict[str, list[Observation]] = {}
        for o in signals:
            by_bar.setdefault(o.time, []).append(o)
        for t in sorted(by_bar):
            now = _minutes(t)
            if not lo <= now <= hi or now < busy_until or now < cooldown_until:
                continue
            if taken >= r.max_trades or losses_in_row >= r.max_losses or day_r <= -r.daily_loss_r:
                break
            best: tuple[float, Observation] | None = None
            for o in by_bar[t]:
                ok, score = decide(o)
                if ok and (best is None or score > best[0]):
                    best = (score, o)
            if best is None:
                continue
            o = best[1]
            net = o.net_r
            book.trades.append({"day": day, "time": t, "strategy": o.strategy, "side": o.side, "net_r": round(net, 3),
                                "why": o.why})
            day_r += net
            taken += 1
            busy_until = now + max(1, o.bars or 1) * r.timeframe
            if net < 0:
                losses_in_row += 1
                cooldown_until = busy_until + r.cooldown_minutes
            else:
                losses_in_row = 0
        book.daily[day] = round(day_r, 4)

    # ------------------------------------------------------------- running

    def run(self, progress: Callable[[float], None] | None = None, seed: int = 7) -> dict[str, Any]:
        r = self.rules
        if len(self.days) <= WARMUP_DAYS:
            return {"ok": False, "why": f"needs more than {WARMUP_DAYS} trading days of replayed history "
                                        f"(the long-run memory has {len(self.days)})", "days": len(self.days)}
        test_days = self.days[WARMUP_DAYS:]
        books = {name: Book() for name in POLICIES}
        alone: dict[str, Book] = {}
        by_day: dict[str, list[Observation]] = {}
        for o in self.obs:
            by_day.setdefault(o.day, []).append(o)
        for i, day in enumerate(test_days):
            signals = by_day.get(day, [])
            deciders = self._deciders(day)
            for name in POLICIES:
                self._trade_day(signals, deciders[name], books[name], day)
            for strat in {o.strategy for o in signals} | set(alone):
                self._trade_day([o for o in signals if o.strategy == strat], lambda o: (True, 0.0),
                                alone.setdefault(strat, Book()), day)
            if progress and i % 20 == 0:
                progress(i / len(test_days))
        if progress:
            progress(1.0)
        summaries = {name: books[name].summary(r.risk_usd) for name in POLICIES}
        comparison = compare(books["evidence"].daily, books["classic"].daily, seed=seed)
        winner, why = choose(summaries, comparison, len(test_days))
        return {
            "ok": True, "at": datetime.now(UTC).isoformat(timespec="seconds"),
            "from": test_days[0], "to": test_days[-1], "test_days": len(test_days), "history_days": len(self.days),
            "signals": len(self.obs), "policies": summaries, "comparison": comparison,
            "alone": {name: b.summary(r.risk_usd) for name, b in sorted(alone.items())},
            "winner": winner, "why": why,
            "recent_trades": {name: books[name].trades[-10:] for name in POLICIES},
            "rules": {"min_days": MIN_DAYS, "min_trades": MIN_TRADES, "confidence": CONFIDENCE},
        }


def compare(a: dict[str, float], b: dict[str, float], *, seed: int = 7, samples: int = BOOTSTRAP) -> dict[str, Any]:
    """Day-by-day difference a - b: its total, and how often resampled days keep it above zero."""
    days = sorted(set(a) | set(b))
    diffs = [a.get(d, 0.0) - b.get(d, 0.0) for d in days]
    if not diffs:
        return {"days": 0, "diff_r": 0.0, "p_better": None, "low": None, "high": None}
    rng = random.Random(seed)
    totals = sorted(sum(rng.choice(diffs) for _ in diffs) for _ in range(samples))
    return {"days": len(diffs), "diff_r": round(sum(diffs), 2),
            "p_better": round(sum(t > 0 for t in totals) / samples, 3),
            "low": round(totals[int(0.05 * samples)], 2), "high": round(totals[int(0.95 * samples) - 1], 2)}


def choose(summaries: dict[str, dict], comparison: dict[str, Any], test_days: int) -> tuple[str, str]:
    """Which rule ``policy: auto`` should trade with, and why, in one sentence."""
    e, c = summaries["evidence"], summaries["classic"]
    if test_days < MIN_DAYS:
        return "classic", (f"only {test_days} test days so far (needs {MIN_DAYS}): too little history to change the rule - "
                           "keeping the classic rule")
    if min(e["trades"], c["trades"]) < MIN_TRADES:
        return "classic", (f"too few trades to compare ({e['trades']} vs {c['trades']}, needs {MIN_TRADES} each) - "
                           "keeping the classic rule")
    p = comparison["p_better"] or 0.0
    if p >= CONFIDENCE and comparison["diff_r"] > 0:
        return "evidence", (f"the evidence rule made {comparison['diff_r']:+.1f}R more over {test_days} days "
                            f"({round(100 * p)}% of resampled histories agree)")
    return "classic", (f"the evidence rule was {comparison['diff_r']:+.1f}R against the classic rule over {test_days} "
                       f"days, which is not convincing ({round(100 * p)}% sure, needs {round(100 * CONFIDENCE)}%) - "
                       "keeping the classic rule")


# --------------------------------------------------------------------------- saved results

def lab_path(cfg: BotConfig) -> Path:
    return cfg.longrun_knowledge_path.with_name(f"lab_{cfg.instrument.symbol}_{cfg.instrument.timeframe_minutes}m.json")


def save_result(cfg: BotConfig, result: dict[str, Any]) -> None:
    path = lab_path(cfg)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(result, indent=1), encoding="utf-8")
        tmp.replace(path)
    except OSError as exc:
        log.warning("Could not save the decision lab's result: %s", exc)


def load_result(cfg: BotConfig) -> dict[str, Any] | None:
    try:
        data = json.loads(lab_path(cfg).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def resolve_policy(cfg: BotConfig, result: dict[str, Any] | None = None) -> tuple[str, str]:
    """The decision rule to trade with now, and why. ``auto`` follows the lab; without a lab result: classic."""
    chosen = cfg.knowledge.policy
    if chosen != "auto":
        return chosen, f"set in config.yaml (knowledge.policy: {chosen})"
    result = result if result is not None else load_result(cfg)
    if not result or not result.get("ok"):
        why = (result or {}).get("why") or "the decision lab hasn't run yet"
        return "classic", f"automatic: {why}, so the classic rule"
    return result["winner"], "automatic: " + result["why"]


def run_lab(cfg: BotConfig, observations: Iterable[Observation], progress: Callable[[float], None] | None = None,
            save: bool = True) -> dict[str, Any]:
    result = Lab(observations, Rules.from_config(cfg)).run(progress)
    if save:
        save_result(cfg, result)
    return result


def result_text(result: dict[str, Any] | None) -> str:
    """The lab's result in a few plain sentences (Telegram, command line, the central intelligence)."""
    if not result:
        return "The decision lab hasn't run yet. It runs after the long-run memory learns (Learn from history now)."
    if not result.get("ok"):
        return f"The decision lab couldn't compare the rules yet: {result.get('why')}."
    lines = [f"Decision lab: {result['test_days']} trading days ({result['from']} to {result['to']}), replayed without "
             f"peeking, after fees and slippage:"]
    names = {"classic": "Classic rule (1.2)", "evidence": "Evidence rule (2.2)"}
    for name in POLICIES:
        s = result["policies"][name]
        avg = f"{s['avg_r']:+.2f}R a trade" if s["avg_r"] is not None else "no trades"
        lines.append(f"  {names[name]}: {s['total_r']:+.1f}R over {s['trades']} trades ({avg}, "
                     f"{round(100 * (s['win_rate'] or 0))}% won, worst drawdown {s['max_drawdown_r']:.1f}R)")
    best = sorted(result["alone"].items(), key=lambda kv: kv[1]["total_r"], reverse=True)[:3]
    if best:
        lines.append("  Each strategy alone (every signal): " + ", ".join(f"{n} {s['total_r']:+.1f}R/{s['trades']}" for n, s in best))
    lines.append(f"Choice: {result['winner']} - {result['why']}.")
    return "\n".join(lines)
