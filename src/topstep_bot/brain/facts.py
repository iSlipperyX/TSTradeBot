"""The facts the central intelligence answers from: read-only lookups over the bot's own records.

Every answer - with or without AI - is built from these and nothing else: the decision records
(reasoning.py), the trade journal, the knowledge base, the long-run memory, the decision lab, the
Simulation Lab's latest report, the AI coach's lessons and, while the bot runs, its live state. A lookup never changes anything.

``Facts`` works in two places: inside the running bot (with its live state), and in the dashboard
process while the bot is stopped (from the files on disk only).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import date, datetime, timezone
from typing import TYPE_CHECKING, Any

from topstep_bot.brain.explain import decision_text, decisions_list, headline, lesson_text
from topstep_bot.fmt import money, pct, r

if TYPE_CHECKING:
    from topstep_bot.config import BotConfig
    from topstep_bot.control import BotActions
    from topstep_bot.engine import TradingCore
    from topstep_bot.journal import Journal
    from topstep_bot.knowledge import KnowledgeBase

UTC = timezone.utc
MAX_LIST = 25

# name -> (what it answers, JSON schema of its arguments). The AI sees exactly these (llm.py).
TOOLS: dict[str, tuple[str, dict]] = {
    "status": ("The bot's state now: running or not, mode, balance, today's P&L and trades, open position, risk room, "
               "the decision rule in use.", {}),
    "decisions": ("The bot's recent decisions, newest first: each signal it took, skipped (a rule stopped it) or passed "
                  "on (not enough evidence), with the result once known.",
                  {"day": {"type": "string", "description": "only this trading day, YYYY-MM-DD"},
                   "status": {"type": "string", "enum": ["taken", "skipped", "passed"]},
                   "limit": {"type": "integer", "minimum": 1, "maximum": MAX_LIST}}),
    "explain_decision": ("Everything recorded about one decision: the signal, every strategy's verdict, the evidence "
                         "(recent record, after-cost estimate, long-run memory, similar past situations), the market, "
                         "the risk limits, the result and what the bot learned from it. Without arguments: the latest "
                         "trade taken.",
                         {"id": {"type": "string", "description": "a decision id from `decisions`"},
                          "time": {"type": "string", "description": "HH:MM (Chicago time) of the signal"},
                          "day": {"type": "string", "description": "YYYY-MM-DD; default: the latest day with decisions"},
                          "status": {"type": "string", "enum": ["taken", "skipped", "passed"]}}),
    "trades": ("Closed trades from the journal, newest first (time, side, size, P&L, R, exit reason).",
               {"limit": {"type": "integer", "minimum": 1, "maximum": MAX_LIST}}),
    "strategy": ("What the bot knows about one strategy: its record by time of day and market, whether it may trade now, "
                 "its long-run record and how it did alone in the decision lab.",
                 {"name": {"type": "string", "description": "strategy name or title, e.g. orb, vwap pullback"}}),
    "knowledge": ("What the bot knows overall, in its own words: how much it has seen, what it would trade now and why, "
                  "which strategies work after costs, lessons worth testing.", {}),
    "lessons": ("What the bot learned from its latest finished decisions: how each result moved its verdict.",
                {"limit": {"type": "integer", "minimum": 1, "maximum": MAX_LIST}}),
    "lab": ("The decision lab: the classic (1.2) and evidence (2.2) decision rules replayed on the long-run memory "
            "without peeking, each strategy alone, and which rule the bot trades with and why.", {}),
    "simulations": ("The Simulation Lab's latest report: fresh Topstep Combines replayed through the real bot on every "
                    "market and strategy with history, how many passed, the best playbooks, and the opportunities (market x "
                    "strategy x time of day that made money in both halves of the history) with how many luck alone would give.",
                    {}),
    "coach": ("The AI coach's latest lessons (with its confidence in each), its proposals waiting for Apply, and this "
              "month's Claude spending.", {}),
    "forecast": ("When the next automatic trade is likely and what it is waiting for (only while the bot runs).", {}),
    "market": ("Live market picture for what the bot trades: levels, today's reports, headlines (only while running).", {}),
    "network": ("The market network: every market read together - trend and volatility state, which markets move "
                "together, markets moving against their peers, risk appetite, the next 24 hours' reports and headlines, "
                "and each market's jump allowance (the sudden gap the risk manager budgets for).",
                {"symbol": {"type": "string", "description": "a market to describe in detail, e.g. MNQ, CL, ZC (optional)"}}),
    "rules": ("The Topstep rules for this account and what the bot does about each.", {}),
    "markets": ("The markets the bot trades on this account right now (it can trade several at once and add, drop or "
                "switch them without a restart): each one's position, open trade and trades today, and the account's "
                "combined open risk.", {}),
    "assistant": ("The AI trading assistant: whether it may manage open trades and place its own, today's decisions "
                  "(what it saw, what it chose, how sure it was, why, and what the bot then did), its own trades' record "
                  "and how the trades it managed did compared with leaving them alone.",
                  {"day": {"type": "string", "description": "only this trading day, YYYY-MM-DD (default: today)"},
                   "limit": {"type": "integer", "minimum": 1, "maximum": MAX_LIST}}),
    "probability": ("The probability calculator: for each strategy and side, P(win), average win and loss in R, expected "
                    "value after costs and an 80% range, pooled from this market (its time of day and market mood), the "
                    "long-run memory and related markets; plus how well its past predictions held up (calibration).",
                    {"strategy": {"type": "string", "description": "only this strategy, e.g. orb (optional)"},
                     "market": {"type": "string", "description": "a market the bot trades, e.g. MES (default: the one shown)"}}),
    "sources": ("Where the bot's knowledge comes from, all together: its real trades, the signals it watched live, "
                "training replays of real market history, the Simulation Lab's simulated Combines, the long-run memory, "
                "related markets (the knowledge network) and the price history saved on this PC.", {}),
    "explain": ("Explain any part of the bot from its user guide, the Topstep rules page and its settings (with this "
                "account's values): a feature, a setting, a rule, a dashboard panel or a term.",
                {"topic": {"type": "string", "description": "what to explain, e.g. consistency target, max_conviction, "
                                                            "knowledge network, the trade chart"}}),
}


def strategy_names() -> list[tuple[str, str]]:
    from topstep_bot.strategies import BASE_STRATEGIES, STRATEGIES

    return [(n, STRATEGIES[n].title) for n in BASE_STRATEGIES]


def find_strategy(text: str) -> str | None:
    """The strategy a question names ("orb", "VWAP pullback", "noise breakout", ...), or None."""
    t = " " + re.sub(r"[^a-z0-9]+", " ", text.lower()) + " "
    best: tuple[int, str] | None = None
    for name, title in strategy_names():
        aliases = {name.replace("_", " "), re.sub(r"[^a-z0-9]+", " ", title.lower()).strip(),
                   re.sub(r"\(.*?\)", "", title.lower()).strip()}
        if name == "noise_breakout":
            aliases |= {"noise", "noise area", "intraday momentum"}
        if name == "late_day_momentum":
            aliases |= {"late day", "late momentum"}
        if name == "ema_trend":
            aliases |= {"ema", "ema crossover"}
        if name == "orb":
            aliases |= {"opening range breakout"}
        for a in aliases:
            if a and f" {a} " in t and (best is None or len(a) > best[0]):
                best = (len(a), name)
    return best[1] if best else None


class Facts:
    def __init__(self, cfg: BotConfig, *, core: TradingCore | None = None, actions: BotActions | None = None,
                 offline: str = ""):
        self.cfg = cfg
        self.core = core
        self.actions = actions
        self.offline = offline  # while stopped: the controller's words for the bot's state
        self._kb: tuple[tuple, KnowledgeBase] | None = None

    @property
    def running(self) -> bool:
        return self.core is not None

    # ------------------------------------------------------------------ sources

    def _journal(self) -> tuple[Journal | None, Callable[[], None]]:
        if self.core is not None:
            return self.core.journal, lambda: None
        from topstep_bot.journal import Journal

        path = self.cfg.journal_path
        if not path.exists():
            return None, lambda: None
        j = Journal(path)
        return j, j.close

    def knowledge(self) -> KnowledgeBase | None:
        if self.core is not None:
            return self.core.knowledge
        path = self.cfg.knowledge_path
        if not self.cfg.knowledge.enabled or not path.exists():
            return None
        stamp = (path.stat().st_mtime_ns, path.stat().st_size)
        if self._kb is None or self._kb[0] != stamp:
            from topstep_bot.knowledge import KnowledgeBase

            self._kb = (stamp, KnowledgeBase.from_config(self.cfg, path))
        return self._kb[1]

    def _today(self) -> date:
        if self.core is not None:
            return self.core.schedule.trading_day(self.core.clock())
        return datetime.now(UTC).date()

    # ------------------------------------------------------------------ the lookups

    def call(self, name: str, args: dict | None = None) -> dict[str, Any]:
        """Run lookup ``name``. Unknown names and bad arguments come back as {"error": ...}, never raise."""
        if name not in TOOLS:
            return {"error": f"unknown lookup '{name}'"}
        fn = getattr(self, f"fact_{name}")
        allowed = TOOLS[name][1]
        kwargs = {k: v for k, v in (args or {}).items() if k in allowed and v not in (None, "")}
        try:
            return fn(**kwargs)
        except (ValueError, TypeError, KeyError) as exc:
            return {"error": str(exc)}

    def fact_status(self) -> dict[str, Any]:
        if self.actions is None:
            return {"running": False, "text": self.offline or "The bot is not running."}
        out: dict[str, Any] = {"running": True, "text": self.actions.status_text()}
        if self.core is not None and self.core.judge is not None:
            out["decision_rule"] = self.core.judge.describe()
        return out

    def fact_decisions(self, day: str | None = None, status: str | None = None, limit: int = 10) -> dict[str, Any]:
        journal, done = self._journal()
        try:
            rows = journal.decisions(limit=max(1, min(MAX_LIST, int(limit))), day=day, status=status) if journal else []
        finally:
            done()
        items = [{"id": d["id"], "day": d["day"], "time": d["time"], "status": d["status"], "strategy": d["strategy"],
                  "side": d["side"], "result": (d.get("outcome") or {}).get("result"), "r": (d.get("outcome") or {}).get("r"),
                  "summary": headline(d)} for d in rows]
        return {"items": items, "text": decisions_list(rows)}

    def fact_explain_decision(self, id: str | None = None, time: str | None = None, day: str | None = None,  # noqa: A002
                              status: str | None = None) -> dict[str, Any]:
        journal, done = self._journal()
        try:
            rec = self._find_decision(journal, id, time, day, status) if journal else None
        finally:
            done()
        if rec is None:
            what = f" at {time}" if time else ""
            return {"found": False, "text": f"I have no recorded decision{what}" + (f" on {day}" if day else "") +
                    ". Decisions are recorded from version 2.2 on, while the bot runs."}
        trimmed = {k: v for k, v in rec.items() if k not in ("market",)}
        trimmed["market"] = (rec.get("market") or {}).get("described")
        return {"found": True, "record": trimmed, "text": decision_text(rec)}

    @staticmethod
    def _find_decision(journal: Journal, id: str | None, time: str | None, day: str | None,  # noqa: A002
                       status: str | None) -> dict | None:
        if id:
            return journal.decision(id)
        if time:
            m = re.match(r"^(\d{1,2})[:.h]?(\d{2})", time.strip())
            if not m:
                raise ValueError("time must look like 10:35")
            hhmm = f"{int(m.group(1)):02d}:{m.group(2)}"
            if day is None:
                latest = journal.decisions(limit=1)
                day = latest[0]["day"] if latest else None
            rows = journal.decisions(limit=200, day=day, status=status) if day else []
            before = [d for d in rows if d["time"] <= hhmm]
            if not before:
                return None
            best = before[0]  # newest first: the latest decision at or before that time
            taken = [d for d in before if d["time"] == best["time"] and d["status"] == "taken"]
            return taken[0] if taken else best
        rows = journal.decisions(limit=1, day=day, status=status or "taken")
        if not rows and status is None:
            rows = journal.decisions(limit=1, day=day)
        return rows[0] if rows else None

    def fact_trades(self, limit: int = 10) -> dict[str, Any]:
        journal, done = self._journal()
        try:
            rows = journal.trades(limit=max(1, min(MAX_LIST, int(limit))), account=self.cfg.active_account or None) \
                if journal else []
            if journal and not rows:
                rows = journal.trades(limit=max(1, min(MAX_LIST, int(limit))))
        finally:
            done()
        items = [{"day": t["trading_day"], "entry": (t["entry_time"] or "")[:16], "exit": (t["exit_time"] or "")[:16],
                  "strategy": t["strategy"], "side": t["side"], "size": t["size"], "net_usd": t["net_pnl"],
                  "r": t["r_multiple"], "exit_reason": t["exit_reason"], "entry_reason": t["entry_reason"]} for t in rows]
        if not items:
            return {"items": [], "text": "No closed trades in the journal yet."}
        lines = [f"• {t['day']} {t['side']} {t['size']} ({t['strategy']}): {money(t['net_usd'])} "
                 f"{r(t['r'])} - {t['exit_reason']}" for t in items]
        return {"items": items, "text": "Closed trades, newest first:\n" + "\n".join(lines)}

    def fact_strategy(self, name: str) -> dict[str, Any]:
        from topstep_bot.decision_lab import load_result
        from topstep_bot.knowledge import REGIMES, SLOT_NAMES

        key = name if name in dict(strategy_names()) else find_strategy(name)
        if key is None:
            raise ValueError(f"no strategy called '{name}' (known: {', '.join(n for n, _ in strategy_names())})")
        title = dict(strategy_names())[key]
        kb = self.knowledge()
        today = self._today()
        out: dict[str, Any] = {"strategy": key, "title": title}
        lines = [f"{title} ({key}):"]
        if kb is not None:
            overall = kb.stats(key, None, None, today)
            out["overall"] = overall.to_dict(kb.min_samples)
            lines.append(f"Recent knowledge: {r(overall.mean_r)} a signal before costs over {overall.n} "
                         f"({pct(overall.wins / overall.n) if overall.n else '-'} won).")
            cells = {}
            judge = self.core.judge if self.core is not None else None
            for sl in SLOT_NAMES:
                for rg in REGIMES:
                    v = judge.verdict(key, sl, rg, today) if judge else kb.verdict(key, sl, rg, today)
                    cells[f"{sl}/{rg}"] = {"allowed": v.allowed, "level": v.level, "score": round(v.score, 3), "why": v.why}
                    lines.append(f"  {sl}/{rg}: {'trades' if v.allowed else 'does not trade'} - {v.why}")
            out["verdicts"] = cells
            if self.core is not None:
                out["now"] = f"{self.core.slot()}/{self.core.regime.value}"
                lines.append(f"Now it is {out['now']}.")
        mem = self.core.memory.knowledge if self.core is not None and self.core.memory else None
        if mem is None and self.cfg.longrun_knowledge_path.exists():
            from topstep_bot.knowledge import KnowledgeBase

            mem = KnowledgeBase.from_config(self.cfg, self.cfg.longrun_knowledge_path)
        if mem is not None:
            mine = [o for o in mem.obs if o.strategy == key]
            if mine:
                net = sum(o.net_r for o in mine) / len(mine)
                out["longrun"] = {"n": len(mine), "mean_net_r": round(net, 3), "from": mine[0].day, "to": mine[-1].day,
                                  "win_rate": round(sum(o.net_r > 0 for o in mine) / len(mine), 3)}
                lines.append(f"Long-run memory: {r(net)} a signal after costs over {len(mine):,} signals "
                             f"({mine[0].day} to {mine[-1].day}).")
        lab = load_result(self.cfg)
        alone = ((lab or {}).get("alone") or {}).get(key)
        if alone:
            out["lab_alone"] = alone
            lines.append(f"Decision lab, trading every signal alone: {alone['total_r']:+.1f}R over {alone['trades']} trades.")
        return {**out, "text": "\n".join(lines)}

    def fact_knowledge(self) -> dict[str, Any]:
        if self.actions is not None:
            return {"text": self.actions.brief_text()}
        kb = self.knowledge()
        if kb is None:
            return {"text": "The knowledge base is empty or turned off."}
        return {"text": kb.text(strategy_names(), today=self._today())}

    def fact_lessons(self, limit: int = 8) -> dict[str, Any]:
        journal, done = self._journal()
        try:
            rows = journal.decisions(limit=200) if journal else []
        finally:
            done()
        learned = [d for d in rows if d.get("lesson")][: max(1, min(MAX_LIST, int(limit)))]
        if not learned:
            return {"items": [], "text": "No lessons recorded yet: they come with the results of decisions the bot made "
                                        "while running version 2.2."}
        items = [{"id": d["id"], "summary": headline(d), "lesson": lesson_text(d)} for d in learned]
        return {"items": items, "text": "\n".join(f"• {i['summary']}\n  {i['lesson']}" for i in items)}

    def fact_lab(self) -> dict[str, Any]:
        from topstep_bot.decision_lab import load_result, resolve_policy, result_text

        result = load_result(self.cfg)
        policy, why = resolve_policy(self.cfg, result)
        if self.core is not None and self.core.judge is not None:
            policy, why = self.core.judge.policy, self.core.judge.why
        summary = None
        if result and result.get("ok"):
            summary = {k: result[k] for k in ("from", "to", "test_days", "policies", "comparison", "winner", "why")}
        return {"policy": policy, "why": why, "result": summary,
                "text": result_text(result) + f"\nThe bot trades with the {policy} rule ({why})."}

    def fact_simulations(self) -> dict[str, Any]:
        from topstep_bot.simlab import load_report, report_text

        report = load_report(self.cfg)
        out: dict[str, Any] = {"text": report_text(report, top=6)}
        if report:
            out["generated"] = report.get("generated")
            out["totals"] = report.get("totals")
        return out

    def fact_coach(self) -> dict[str, Any]:
        from topstep_bot.coach import Coach

        coach = Coach(self.cfg)
        lessons = list(reversed(coach.lessons()))[:8]  # newest first
        pending = [p for p in coach.proposals() if p.get("status") == "pending"]
        return {"lessons": lessons, "pending_proposals": pending[:8], "spent_month_usd": coach.spent(),
                "text": coach.status_text()}

    def fact_forecast(self) -> dict[str, Any]:
        if self.actions is None:
            return {"text": "The next-trade forecast needs the bot running."}
        return {"text": self.actions.next_text()}

    def fact_market(self) -> dict[str, Any]:
        if self.actions is None:
            return {"text": "The live market picture needs the bot running."}
        return {"text": self.actions.market_text()}

    def fact_network(self, symbol: str = "") -> dict[str, Any]:
        from topstep_bot import network
        from topstep_bot.market_pick import current_symbol

        picture = network.load(self.cfg)
        focus = [r for sym in (symbol, current_symbol(self.cfg)) for r in network.focus_for(sym)]
        focus = list(dict.fromkeys(focus))
        out: dict[str, Any] = {"text": network.text(picture, focus)}
        if picture:
            out["built"] = picture.get("built")
            out["fresh"] = network.fresh(picture)
            out["markets"] = {r: picture["markets"][r] for r in focus if r in picture["markets"]}
        return out

    # ------------------------------------------------------------------ markets, odds, sources, the guide

    def _desk(self):
        return getattr(self.actions, "desk", None) if self.actions is not None else None

    def _core_for(self, market: str | None) -> TradingCore | None:
        desk = self._desk()
        if market and desk is not None:
            return desk.lane_for(market).core
        return self.core

    def fact_markets(self) -> dict[str, Any]:
        desk = self._desk()
        if desk is not None:
            return {"view": desk.markets_view(), "text": self.actions.markets_text()}
        from topstep_bot import market_pick as mp

        today = mp.load_today(self.cfg)
        markets = today["markets"] if today else mp.lane_symbols(self.cfg, self.cfg.instrument.symbol, None)
        state = self.offline or "The bot is not running."
        text = (f"{state} It is set to trade {', '.join(markets)}"
                + (f" today (switched by {today.get('source', 'you')})" if today else "")
                + ". While it runs, markets can be added, dropped or switched without a restart (Trade tab or /markets).")
        return {"markets": markets, "text": text}

    def fact_assistant(self, day: str | None = None, limit: int = 10) -> dict[str, Any]:
        a = self.core.assistant if self.core is not None else None
        out: dict[str, Any] = {}
        if a is not None:
            out["now"] = {k: v for k, v in a.view().items() if k != "decisions"}
            text = a.text()
        else:
            c = self.cfg.assistant
            text = (f"AI assistant: managing open trades {'on' if c.manage else 'off'}, own trades "
                    f"{'on' if c.trade else 'off'}. {self.offline or 'The bot is not running.'}")
        journal, done = self._journal()
        try:
            day = day or self._today().isoformat()
            rows = journal.assistant_decisions(limit=max(1, min(MAX_LIST, int(limit))), day=day) if journal else []
        finally:
            done()
        out["decisions"] = [{k: v for k, v in r.items() if k != "situation"} for r in rows]
        if rows and a is None:
            text += "\n" + "\n".join(f"• {r.get('time')} {r.get('kind')}: {r.get('action') or '-'} - {r.get('result') or ''}"
                                      for r in rows[:5])
        return {**out, "text": text}

    def fact_probability(self, strategy: str | None = None, market: str | None = None) -> dict[str, Any]:
        from topstep_bot.probability import ProbabilityModel, calibration, calibration_text, outlook, outlook_text

        titles = dict(strategy_names())
        names = list(titles)
        if strategy:
            key = strategy if strategy in titles else find_strategy(strategy)
            if key is None:
                raise ValueError(f"no strategy called '{strategy}' (known: {', '.join(titles)})")
            names = [key]
        core = self._core_for(market)
        network: list[str] = []
        if core is not None:
            kb = core.knowledge
            rows = outlook(core.probability, names) if kb is not None else []
            network = core.probability.network_markets
            where = f"for {core.contract.name} now ({core.slot()} session, {core.regime.value} market)"
        else:
            kb = self.knowledge()
            rows = []
            if kb is not None:
                from topstep_bot.probability import load_network, network_paths

                net = load_network(network_paths(self.cfg, self.cfg.instrument.symbol)) if self.cfg.probability.network \
                    else ([], [])
                network = net[1]
                model = ProbabilityModel(kb.obs, today=self._today(), half_life_days=kb.half_life_days,
                                         real_weight=kb.real_weight, network=net[0])
                rows = outlook(model, names)
            where = f"for {self.cfg.instrument.symbol} over the whole day (the bot isn't running)"
        cal = calibration(kb.obs if kb is not None else [])
        text = outlook_text(rows, titles, where=where) + "\n\n" + calibration_text(cal)
        if network:
            text += f"\nRelated markets it also learns from: {', '.join(network)}."
        if not self.cfg.probability.enabled:
            text = "The probability calculator is turned off (probability.enabled).\n" + text
        for row in rows:
            row["title"] = titles.get(row["strategy"], row["strategy"])
        return {"rows": rows[:16], "calibration": cal, "network": network, "where": where, "text": text}

    def fact_sources(self) -> dict[str, Any]:
        from topstep_bot import knowledge_network as kn
        from topstep_bot.probability import calibration
        from topstep_bot.simlab import load_report

        core = self.core
        symbol = (core.contract.root or core.contract.name) if core is not None else self.cfg.instrument.symbol
        desk = self._desk()
        traded = list(desk.lanes) if desk is not None else [symbol]
        kb = core.knowledge if core is not None else self.knowledge()
        out: dict[str, Any] = {"symbol": symbol, "traded": traded}
        lines = [f"Where the bot's knowledge of {symbol} comes from:"]
        if kb is not None:
            counts: dict[str, int] = {}
            for o in kb.obs:
                counts[o.source] = counts.get(o.source, 0) + 1
            out["knowledge"] = {"counts": counts, "total": len(kb.obs), "trained": kb.trained}
            t = kb.trained or {}
            lines.append(f"• Real trades: {counts.get('real', 0):,} by the bot and {counts.get('manual', 0):,} of yours "
                         "(they count double).")
            lines.append(f"• Watched live: {counts.get('shadow', 0):,} signals every strategy gave while the bot ran, "
                         "followed to their stop, target or the session end.")
            lines.append(f"• Real market history: {counts.get('train', 0):,} signals replayed"
                         + (f" over {t.get('days')} days ({t.get('from')} to {t.get('to')}, {t.get('origin') or 'TopstepX'})"
                            if t else "") + ".")
        else:
            lines.append("• The knowledge base is empty or turned off.")
        memory = core.memory.status() if core is not None and core.memory is not None else None
        longrun = kn._file_stats(self.cfg.longrun_knowledge_path)
        if memory is not None:
            lib = memory["library"]
            out["memory"] = {"observations": memory["observations"], "bars": lib["bars"], "days": lib["days"],
                             "first": lib["first"]}
            lines.append(f"• Long-run memory: {memory['observations']:,} signals replayed over {lib['days']:,} days of saved "
                         f"bars (since {lib['first']}).")
        elif longrun:
            out["memory"] = {"observations": longrun["n"], "days": longrun["days"]}
            lines.append(f"• Long-run memory: {longrun['n']:,} signals over {longrun['days']:,} days.")
        report = load_report(self.cfg)
        if report:
            tot = report["totals"]
            best = (report.get("playbooks") or [{}])[0]
            out["simulations"] = {"generated": report.get("generated"), "combines": tot["simulations"],
                                  "passed": tot["passed"], "failed": tot["failed"], "open": tot["open"],
                                  "markets": len(report.get("markets") or []), "best": best.get("label"),
                                  "best_verdict": best.get("verdict")}
            lines.append(f"• Simulations: {tot['simulations']} simulated Combines on {len(report.get('markets') or [])} "
                         f"markets ({tot['passed']} passed, {tot['failed']} failed); best playbook "
                         f"{best.get('label', '-')} ({best.get('verdict', '-')}).")
        else:
            lines.append("• Simulations: none yet (Knowledge tab > Simulations).")
        net = kn.status(self.cfg, symbol, traded=traded)
        if kb is not None:  # the live count (the file on disk is saved every few minutes)
            net["own"] = {**(net.get("own") or {}), "n": len(kb.obs)}
        out["network"] = net
        lines.append(f"• Related markets: {net['outcomes']:,} outcomes on {len(net['markets'])} "
                     f"{net['asset_class'].lower()} market(s)"
                     + (f" ({', '.join(m['symbol'] for m in net['markets'][:8])})" if net["markets"] else "")
                     + (f"; {len(net['learnable'])} more it can learn from saved history" if net["learnable"] else "") + ".")
        try:
            history = kn._history(self.cfg, self.cfg.instrument.timeframe_minutes)
        except Exception:  # noqa: BLE001 - the library is optional
            history = {}
        if history:
            out["library"] = {"markets": len(history), "days": max(h["days"] for h in history.values())}
            lines.append(f"• Price history on this PC: {len(history)} market(s), up to {out['library']['days']:,} "
                         "trading days each.")
        cal = calibration(kb.obs if kb is not None else [])
        out["calibration"] = cal
        if cal["n"]:
            lines.append(f"• Probability calculator: {cal['n']:,} predictions scored"
                         + (f", skill {cal['skill']:+.1%} over always guessing the average" if cal.get("skill") is not None
                            else "") + ".")
        lines.append("All of it is evidence about the past after costs: no source guarantees the next trade.")
        out["text"] = "\n".join(lines)
        return out

    def fact_explain(self, topic: str = "") -> dict[str, Any]:
        from topstep_bot.brain.guide import search

        return search(topic, self.core.cfg if self.core is not None else self.cfg)

    def fact_rules(self) -> dict[str, Any]:
        from topstep_bot.risk.summary import rules_text

        return {"text": rules_text(self.cfg)}
