"""Backtest of the first trade after starting (first_trade.py), against a coin flip.

The question it answers: when the bot is started and makes its first trade within
``first_trade.within_minutes``, does picking the best-supported setup from its knowledge do better
than picking a side at random? And what do those extra trades do to the account?

How: the history is replayed three times through the real trading code (the same bars, rules, fees
and slippage as ``backtest``), with the knowledge base learning walk-forward as it goes, exactly as
the live bot does (it never sees a bar before it happens):

  1. the configured strategy alone (what the bot does without the setting)
  2. plus the first trade, as the live bot makes it ("educated")
  3. plus the first trade with a random side, an ATR stop and a 1.5R target ("coin flip")

Each trading day the bot is "started" once, at a different time of day in turn (before the session,
at the open, mid-morning, midday, afternoon), so every time slot is tested.

Read the result with care: a few dozen first trades are a small sample, and a difference of a
few tenths of an R between "educated" and "coin flip" can be luck. Nothing here can promise that
the first trade makes money; its job is to teach the bot.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import time
from statistics import mean
from typing import Any

from topstep_bot.backtest.metrics import compute_metrics
from topstep_bot.backtest.runner import BacktestResult, run_backtest
from topstep_bot.config import BotConfig
from topstep_bot.knowledge import FIRST_TRADE, SLOT_NAMES
from topstep_bot.models import Bar, Contract

# Simulated start times (Chicago), one per trading day in turn.
START_TIMES = (time(7, 45), time(8, 35), time(9, 20), time(10, 5), time(11, 10), time(12, 25), time(13, 40), time(14, 5))
VARIANTS = (("off", "Strategy alone"), ("best", "Educated first trade"), ("random", "Coin-flip first trade"))


def _attach(cfg: BotConfig, choose: str, seed: int, planners: list) -> tuple[Callable, Callable]:
    """``prepare`` / ``before_bar`` callbacks for run_backtest that start the bot once per trading day."""
    from topstep_bot.first_trade import FirstTradePlanner
    from topstep_bot.knowledge import KnowledgeBase
    from topstep_bot.recommendations import RecommendationBook

    started: set = set()

    def prepare(core) -> None:
        if core.recommender is None:  # a single strategy: the first trade still learns from every strategy
            core.recommender = RecommendationBook(core, quiet=True)
        if core.knowledge is None:
            core.attach_knowledge(KnowledgeBase.from_config(cfg, None))
        planner = FirstTradePlanner(core, cfg.first_trade, choose=choose, seed=seed)
        core.first_trade = planner
        planners.append(planner)

    def before_bar(core, bar: Bar) -> None:
        day = core.schedule.trading_day(bar.ts)
        if day in started or not core.schedule.is_trade_day(day):
            return
        index = len(started)
        at = core.schedule.at(day, START_TIMES[index % len(START_TIMES)])
        if bar.ts + core.tf >= at:
            started.add(day)
            core.first_trade.request(at)

    return prepare, before_bar


def _first_trade_stats(res: BacktestResult, planner) -> dict[str, Any]:
    trades = {t.tag: t for t in res.trades if t.strategy == FIRST_TRADE}
    rows = []
    for rec in planner.records:
        t = trades.get(rec["tag"])
        if t is None or t.r_multiple() is None:
            continue
        rows.append({"slot": rec["slot"], "r": t.r_multiple(), "usd": t.net_pnl, "kind": rec["choice"]["kind"]})

    def block(items: list[dict]) -> dict[str, Any]:
        if not items:
            return {"n": 0, "avg_r": None, "win_rate": None, "net_usd": 0.0}
        return {"n": len(items), "avg_r": round(mean(i["r"] for i in items), 3),
                "win_rate": round(sum(1 for i in items if i["usd"] > 0) / len(items), 3),
                "net_usd": round(sum(i["usd"] for i in items), 2)}

    return {"all": block(rows), "by_slot": {s: block([r for r in rows if r["slot"] == s]) for s in SLOT_NAMES},
            "by_kind": {k: block([r for r in rows if r["kind"] == k]) for k in ("signal", "setup", "lean")}}


async def first_trade_test(cfg: BotConfig, bars: list[Bar], contract: Contract, *, seed: int = 7,
                           progress: Callable[[float], None] | None = None) -> dict[str, Any]:
    """Run the three variants and return their results side by side."""
    out: dict[str, Any] = {"variants": [], "within_minutes": cfg.first_trade.within_minutes,
                           "contracts": cfg.first_trade.contracts}
    for i, (choose, label) in enumerate(VARIANTS):
        trial = cfg.model_copy(deep=True)
        trial.first_trade.enabled = choose != "off"
        planners: list = []
        hooks = {}
        if choose != "off":
            prepare, before_bar = _attach(trial, choose, seed, planners)
            hooks = {"prepare": prepare, "before_bar": before_bar}

        def step(f: float, i: int = i) -> None:
            if progress:
                progress((i + f) / len(VARIANTS))

        # The Maximum Loss Limit is off (as in training), so one bad stretch doesn't end the simulated
        # account and leave the rest of the history untested; every other rule stays on.
        res = await run_backtest(trial, bars, contract, progress=step, enforce_mll=False, **hooks)
        m = compute_metrics(res.trades, res.days, res.starting_balance)
        row: dict[str, Any] = {"key": choose, "label": label, "net_pnl": round(m["net_pnl"], 2), "trades": m["trades"],
                               "avg_r": round(m["avg_r"], 3), "max_drawdown": round(m["max_drawdown"], 2),
                               "first_day": res.first_day.isoformat() if res.first_day else None,
                               "last_day": res.last_day.isoformat() if res.last_day else None, "days": len(res.days)}
        if planners:
            p = planners[0]
            row["first_trades"] = _first_trade_stats(res, p)
            row["outcomes"] = dict(p.outcomes)
            row["missed_because"] = dict(p.missed_because.most_common(4))
        out["variants"].append(row)
    out["verdict"] = verdict(out)
    return out


def verdict(result: dict[str, Any]) -> str:
    """One honest sentence on what the test shows."""
    rows = {r["key"]: r for r in result["variants"]}
    best = rows.get("best", {}).get("first_trades", {}).get("all", {})
    coin = rows.get("random", {}).get("first_trades", {}).get("all", {})
    n = best.get("n") or 0
    if n < 30:
        return (f"Only {n} first trades in this history: too few to judge. Test on more days "
                "(--days 365) before reading anything into it.")
    diff = (best["avg_r"] or 0) - (coin.get("avg_r") or 0)
    if abs(diff) < 0.1:
        return (f"The educated first trade averaged {best['avg_r']:+.2f}R against {coin.get('avg_r') or 0:+.2f}R for a coin "
                "flip: no real difference. Its value is what the bot learns, not an edge.")
    better = "better" if diff > 0 else "worse"
    return (f"The educated first trade averaged {best['avg_r']:+.2f}R against {coin.get('avg_r') or 0:+.2f}R for a coin flip "
            f"({better} by {abs(diff):.2f}R over {n} trades). With this many trades a difference this size can still be luck.")


def result_text(result: dict[str, Any]) -> list[str]:
    """Plain lines for the console."""
    lines = []
    for r in result["variants"]:
        line = (f"{r['label']}: {r['trades']} trades, net ${r['net_pnl']:,.0f}, {r['avg_r']:+.2f}R avg, "
                f"max drawdown ${r['max_drawdown']:,.0f}")
        lines.append(line)
        ft = r.get("first_trades")
        if ft:
            a = ft["all"]
            if a["n"]:
                lines.append(f"    first trades: {a['n']}, {a['avg_r']:+.2f}R avg, {round(a['win_rate'] * 100)}% won, "
                             f"net ${a['net_usd']:,.0f}")
                for slot, b in ft["by_slot"].items():
                    if b["n"]:
                        lines.append(f"      {slot}: {b['n']}, {b['avg_r']:+.2f}R avg")
            else:
                lines.append("    first trades: none (the strategy traded first, or a rule blocked every one)")
            o = r.get("outcomes") or {}
            lines.append(f"    starts: {o.get('requested', 0)}; strategy traded first {o.get('strategy', 0)}, "
                         f"first trade placed {o.get('placed', 0)}, a session passed without one {o.get('missed', 0)}")
            if r.get("missed_because"):
                lines.append("    sessions without one, because: "
                             + ", ".join(f"{k} ({v})" for k, v in r["missed_because"].items()))
    lines.append(result["verdict"])
    return lines


__all__ = ["START_TIMES", "first_trade_test", "result_text", "verdict"]
