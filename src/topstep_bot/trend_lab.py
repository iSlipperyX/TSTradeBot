"""The trend test: does skipping signals against a strong trend make the bot more, on history it hasn't seen?

Since version 4.2 every signal's market snapshot carries the chart's trend reading (trend.py). This
test replays the long-run memory (memory.py) the same way the decision lab does (decision_lab.py):
day by day, with only what was known before each day, under the bot's own limits (one trade at a
time, entry window, trades per day, the stop after a losing streak, the cooldown, the daily loss
limit), in R after fees and slippage. It runs the bot's decision rule twice on the same days:

* **as it trades now**, and
* **with the trend filter**: a signal against a strong trend (``trend.min_score`` and ``trend.min_adx``,
  the reading saved with it) is skipped, and the next allowed signal can take its place.

``trend.filter: auto`` (the default) switches the filter on only when the filtered run made more,
with at least 95% confidence (days resampled), over at least ``MIN_DAYS`` test days, with at least
``MIN_SKIPPED`` trades the filter actually skipped. Otherwise the bot keeps trading as it did. The
test reruns after every long-run learning run, so the choice follows the evidence as history grows.
Signals saved before the trend reading existed have no reading: the filter never skips those, so
the test only judges the days it can.

It also measures the reading itself on the market library's bars: how often price kept going the
way the reading called, an hour later (``TrendCalls``).

A simulation on replayed bars with assumed slippage; real fills are usually a little worse, and no
result here promises future profit.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from topstep_bot.decision_lab import CONFIDENCE, MIN_DAYS, WARMUP_DAYS, Book, Lab, Rules, compare
from topstep_bot.knowledge import Observation
from topstep_bot.trend import STRONG_ADX, STRONG_SCORE, against, with_trend

if TYPE_CHECKING:
    from topstep_bot.config import BotConfig
    from topstep_bot.models import Bar

log = logging.getLogger("topstep_bot.trend_lab")
UTC = timezone.utc
MIN_SKIPPED = 15  # trades the filter must actually have skipped before it can be judged


def blocks(o: Observation, min_score: float = STRONG_SCORE, min_adx: float = STRONG_ADX) -> bool:
    """Would the trend filter skip this signal? (against a strong trend, by the reading saved with it)"""
    ctx = o.ctx or {}
    if ctx.get("tscore") is None:
        return False
    adx = ctx.get("tadx")
    if min_adx and (adx is None or adx < min_adx):
        return False
    return against(ctx["tscore"], o.side, min_score)


def split_by_trend(obs: Iterable[Observation], min_score: float) -> dict[str, dict[str, Any]]:
    """Every signal's average result with the trend, against it and with no clear trend (not just the ones taken)."""
    groups: dict[str, list[float]] = {"with": [], "against": [], "none": []}
    for o in obs:
        key = with_trend(o.ctx, o.side, min_score)
        if key in groups:
            groups[key].append(o.net_r)
    return {k: {"n": len(v), "avg_r": round(sum(v) / len(v), 3) if v else None,
                "win_rate": round(sum(x > 0 for x in v) / len(v), 3) if v else None} for k, v in groups.items()}


class TrendTest(Lab):
    def __init__(self, observations: Iterable[Observation], rules: Rules, policy: str = "classic", *,
                 adaptive: bool = True, min_score: float = STRONG_SCORE, min_adx: float = STRONG_ADX):
        super().__init__(observations, rules)
        self.policy = policy
        self.adaptive = adaptive
        self.min_score, self.min_adx = min_score, min_adx

    def run(self, progress: Callable[[float], None] | None = None, seed: int = 7) -> dict[str, Any]:  # noqa: D102
        r = self.rules
        if len(self.days) <= WARMUP_DAYS:
            return {"ok": False, "why": f"needs more than {WARMUP_DAYS} trading days of replayed history "
                                        f"(the long-run memory has {len(self.days)})", "days": len(self.days)}
        test_days = self.days[WARMUP_DAYS:]
        by_day: dict[str, list[Observation]] = {}
        for o in self.obs:
            by_day.setdefault(o.day, []).append(o)
        base, filtered = Book(), Book()
        read = sum(1 for o in self.obs if (o.ctx or {}).get("tscore") is not None)
        for i, day in enumerate(test_days):
            signals = by_day.get(day, [])
            if self.adaptive:
                decide = self._deciders(day)[self.policy]
            else:
                def decide(o: Observation) -> tuple[bool, float]:
                    return True, 0.0

            def with_filter(o: Observation, decide=decide) -> tuple[bool, float]:
                return (False, 0.0) if blocks(o, self.min_score, self.min_adx) else decide(o)

            self._trade_day(signals, decide, base, day)
            self._trade_day(signals, with_filter, filtered, day)
            if progress and i % 20 == 0:
                progress(i / len(test_days))
        if progress:
            progress(1.0)
        # Trades the unfiltered run took that the filter would have skipped (by their saved reading).
        skipped = sum(1 for t in base.trades if t.get("blocked"))
        summaries = {"now": base.summary(r.risk_usd), "filtered": filtered.summary(r.risk_usd)}
        comparison = compare(filtered.daily, base.daily, seed=seed)
        tested = sorted({o.day for o in self.obs if (o.ctx or {}).get("tscore") is not None and o.day >= test_days[0]})
        on, why = choose(summaries, comparison, len(tested), skipped)
        return {
            "ok": True, "at": datetime.now(UTC).isoformat(timespec="seconds"), "policy": self.policy,
            "from": test_days[0], "to": test_days[-1], "test_days": len(test_days), "days_with_reading": len(tested),
            "signals": len(self.obs), "signals_with_reading": read, "skipped": skipped,
            "runs": summaries, "comparison": comparison, "on": on, "why": why,
            "split": split_by_trend((o for o in self.obs if o.day >= test_days[0]), 0.35),
            "threshold": {"min_score": self.min_score, "min_adx": self.min_adx},
            "rules": {"min_days": MIN_DAYS, "min_skipped": MIN_SKIPPED, "confidence": CONFIDENCE},
        }

    def _trade_day(self, signals, decide, book, day):  # noqa: D102 - marks the trades the filter would skip
        start = len(book.trades)
        super()._trade_day(signals, decide, book, day)
        if not signals:
            return
        by_key = {(o.time, o.strategy, o.side): o for o in signals}
        for t in book.trades[start:]:
            o = by_key.get((t["time"], t["strategy"], t["side"]))
            t["blocked"] = bool(o is not None and blocks(o, self.min_score, self.min_adx))


def choose(runs: dict[str, dict], comparison: dict[str, Any], days_with_reading: int, skipped: int) -> tuple[bool, str]:
    """Should ``trend.filter: auto`` skip signals against a strong trend? And why, in one sentence."""
    if days_with_reading < MIN_DAYS:
        return False, (f"only {days_with_reading} test days carry the trend reading so far (needs {MIN_DAYS}): too little "
                       "history to change how trades are picked - the filter stays off")
    if skipped < MIN_SKIPPED:
        return False, (f"the filter would have skipped only {skipped} trade(s) (needs {MIN_SKIPPED} to judge) - "
                       "it stays off")
    p = comparison["p_better"] or 0.0
    diff = comparison["diff_r"]
    if p >= CONFIDENCE and diff > 0:
        return True, (f"skipping signals against a strong trend made {diff:+.1f}R more over {comparison['days']} days "
                      f"({round(100 * p)}% of resampled histories agree) - the filter is on")
    return False, (f"skipping signals against a strong trend changed the result by {diff:+.1f}R over {comparison['days']} "
                   f"days, which is not convincing ({round(100 * p)}% sure, needs {round(100 * CONFIDENCE)}%) - the filter "
                   "stays off")


# --------------------------------------------------------------------------- the reading's own record

def reading_record(cfg: BotConfig, bars: list[Bar]) -> dict[str, Any] | None:
    """Stream ``bars`` (the market library) through the trend reader and check every call an hour later."""
    from topstep_bot.instruments import SPECS
    from topstep_bot.sessions import SessionSchedule, market_session
    from topstep_bot.strategies.base import parse_hhmm
    from topstep_bot.trend import CALL_BARS, TrendCalls, TrendReader

    if len(bars) < 500:
        return None
    symbol = cfg.instrument.symbol
    spec = SPECS.get(symbol)
    rth_open, rth_close = parse_hhmm(spec.rth_open if spec else "08:30"), parse_hhmm(spec.rth_close if spec else "15:00")
    schedule = SessionSchedule(market_session(cfg.session, symbol))
    tf = cfg.instrument.timeframe_minutes
    step = timedelta(minutes=tf)
    reader = TrendReader()
    reader.calls = TrendCalls(horizon=max(1, round(60 / tf)), kept=10 ** 7)
    for bar in bars:
        reader.update(bar, bar.ts + step, schedule.trading_day(bar.ts), schedule.is_rth(bar.ts, rth_open, rth_close))
    out = reader.calls.summary()
    out["minutes"] = round(reader.calls.horizon * tf) if reader.calls.horizon else CALL_BARS * tf
    out["from"], out["to"] = bars[0].ts.date().isoformat(), bars[-1].ts.date().isoformat()
    return out


# --------------------------------------------------------------------------- running and saved results

def test_path(cfg: BotConfig) -> Path:
    return cfg.longrun_knowledge_path.with_name(f"trend_{cfg.instrument.symbol}_{cfg.instrument.timeframe_minutes}m.json")


def save_result(cfg: BotConfig, result: dict[str, Any]) -> None:
    path = test_path(cfg)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(result, indent=1), encoding="utf-8")
        tmp.replace(path)
    except OSError as exc:
        log.warning("Could not save the trend test's result: %s", exc)


def load_result(cfg: BotConfig) -> dict[str, Any] | None:
    try:
        data = json.loads(test_path(cfg).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def run_test(cfg: BotConfig, observations: Iterable[Observation], policy: str = "classic", bars: list[Bar] | None = None,
             progress: Callable[[float], None] | None = None, save: bool = True) -> dict[str, Any]:
    rules = Rules.from_config(cfg)
    adaptive = cfg.strategy.name == "adaptive"
    if not adaptive:
        rules.strategies = (cfg.strategy.name,)
    t = cfg.trend
    result = TrendTest(observations, rules, policy, adaptive=adaptive, min_score=t.min_score, min_adx=t.min_adx).run(progress)
    if bars:
        try:
            result["reading"] = reading_record(cfg, bars)
        except Exception:  # noqa: BLE001 - the reading's record is a report; the test stands without it
            log.exception("Could not measure the trend reading on the market library")
    if save:
        save_result(cfg, result)
    return result


def resolve_filter(cfg: BotConfig, result: dict[str, Any] | None = None) -> tuple[bool, str]:
    """Whether the trend filter is on now, and why. ``auto`` follows the trend test; without a result it is off."""
    mode = cfg.trend.filter
    if mode == "on":
        return True, "switched on in the settings (trend.filter: on)"
    if mode == "off":
        return False, "switched off in the settings (trend.filter: off)"
    result = result if result is not None else load_result(cfg)
    if not result or not result.get("ok"):
        why = (result or {}).get("why") or "the trend test hasn't run yet (it runs after the long-run memory learns)"
        return False, f"automatic: {why}, so the filter is off"
    return bool(result.get("on")), "automatic: " + str(result.get("why"))


def result_text(result: dict[str, Any] | None) -> str:
    """The trend test in a few plain sentences (Telegram, the command line, Ask)."""
    if not result:
        return "The trend test hasn't run yet. It runs after the long-run memory learns (Learn from history now, /learn)."
    if not result.get("ok"):
        return f"The trend test couldn't run yet: {result.get('why')}."
    lines = [f"Trend test: {result['test_days']} trading days ({result['from']} to {result['to']}) replayed without peeking, "
             f"after fees and slippage ({result['days_with_reading']} of them carry the trend reading):"]
    names = {"now": "As it trades now", "filtered": "Skipping signals against a strong trend"}
    for key in ("now", "filtered"):
        s = result["runs"][key]
        avg = f"{s['avg_r']:+.2f}R a trade" if s["avg_r"] is not None else "no trades"
        lines.append(f"  {names[key]}: {s['total_r']:+.1f}R over {s['trades']} trades ({avg}, "
                     f"{round(100 * (s['win_rate'] or 0))}% won, worst drawdown {s['max_drawdown_r']:.1f}R)")
    sp = result.get("split") or {}
    parts = [f"{label} {sp[k]['avg_r']:+.2f}R over {sp[k]['n']}" for k, label in
             (("with", "with the trend"), ("against", "against it"), ("none", "no clear trend")) if (sp.get(k) or {}).get("n")]
    if parts:
        lines.append("  Every signal (taken or not): " + ", ".join(parts) + ".")
    reading = result.get("reading")
    if reading and reading.get("all"):
        from topstep_bot.trend import calls_text

        lines.append("  The reading itself: " + calls_text(reading, reading.get("minutes", 60)))
    lines.append(f"Choice: filter {'ON' if result['on'] else 'off'} - {result['why']}.")
    return "\n".join(lines)
