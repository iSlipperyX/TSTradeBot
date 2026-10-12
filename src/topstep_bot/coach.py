"""The AI coach: Claude studies what the bot knows, runs its own simulations and teaches the bot lessons.

A coaching session is a conversation between Claude (through the Claude API, with your API key and
your API credits) and the bot's own evidence. Claude gets tools to read, and one to experiment:

  read      the account and Topstep's rules, the Simulation Lab report, the knowledge base (what each
            strategy's signals have been worth by time of day and volatility), the long-run memory,
            the trade journal, and the strategy catalogue with each strategy's settings
  test      run_simulation: a Simulation Lab experiment of its own (markets, strategies, settings,
            risk per trade), played through the bot's real trading code on history. Experiments are
            kept apart: they never replace the lab's report on the dashboard
  teach     record_lesson: a lesson with its evidence, kept in data/coach/lessons.json. Every later
            session starts by reading them, so what the coach learns builds up over time
  propose   propose_change: a market + strategy (a playbook) or a risk setting to change. Proposals
            wait on the dashboard's Coach tab: nothing changes until you press Apply, and a proposal
            can only make risk smaller, never larger

The coach never places, closes or sizes a trade, and it can't change a setting by itself. It is told
to be honest about small samples rather than tune results until they look good.

Spending: every API call's token usage is priced and logged in data/coach/usage.jsonl. A session stops
once it reaches coach.max_session_usd, and no session starts once this month's spending reaches
coach.monthly_budget_usd. API credits are bought on the Claude Console (platform.claude.com).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets as pysecrets
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from topstep_bot.config import BotConfig, load_secrets

log = logging.getLogger("topstep_bot.coach")
UTC = timezone.utc

ENV_KEY = "COACH_ANTHROPIC_API_KEY"  # its own name: a system-wide ANTHROPIC_API_KEY is never picked up by accident
CONSOLE_URL = "https://platform.claude.com"
BILLING_URL = "https://platform.claude.com/settings/billing"
KEYS_URL = "https://platform.claude.com/settings/keys"
FALLBACK_BETA = "server-side-fallback-2026-07-01"
MAX_TOKENS = 16000
LESSONS_KEPT = 60
SESSIONS_KEPT = 40
MAX_SIM_SPECS = 160  # one coach experiment: at most this many simulations (keeps a session quick)
TOOL_RESULT_CHARS = 24000

# $ per million tokens: input, output, cache read, cache write (5-minute)
PRICES: dict[str, tuple[float, float, float, float]] = {
    "claude-opus-5-5": (4.00, 20.00, 0.20, 5.00),
    "claude-sonnet-5-5": (2.00, 10.00, 0.20, 2.50),
    "claude-haiku-5-5": (0.10, 0.50, 0.01, 0.125),
    "claude-fable-5-1": (10.00, 50.00, 0.25, 12.50),
}
MODELS = {
    "claude-opus-5-5": "Claude Opus 5.5 - recommended: thorough and careful",
    "claude-sonnet-5-5": "Claude Sonnet 5.5 - about half the price of Opus",
    "claude-haiku-5-5": "Claude Haiku 5.5 - cheapest, for quick check-ins",
    "claude-fable-5-1": "Claude Fable 5.1 - most capable, about 2.5x the price of Opus",
}
FALLBACK_MODELS = {"claude-opus-5-5", "claude-sonnet-5-5", "claude-fable-5-1"}

SYSTEM = """You are the trading coach for an automated futures trading bot that trades Topstep accounts \
(Trading Combine evaluations and Express Funded Accounts) through the TopstepX API. You work for the bot's owner.

Your job in each session: study the evidence the bot has, test ideas with the Simulation Lab, record what you \
learn as lessons, and propose at most a few concrete changes. You cannot place trades or change settings; \
your proposals wait for the owner to approve them.

How the bot works, in short:
- It trades one futures market at a time on 5-minute bars, usually from 08:30 Chicago time, and is always flat \
by 15:10 CT (a Topstep rule; grains and livestock close earlier and are left before their own close). Topstep \
permits about 50 markets: equity indexes, crypto, currencies, energy, metals, interest rates and agriculture. \
The owner can switch the market for a day, and the bot can pick its market each morning. get_markets lists \
them all with the history the bot has.
- Each strategy signals trades with a stop; the size comes from the account's risk per trade. The 'adaptive' \
strategy runs all the others and only takes a signal where its knowledge base shows that strategy has been \
working at this time of day (open / midday / close) in the current volatility regime (calm / volatile).
- Topstep's Combine: reach the profit target without the balance (including open P&L) ever touching the \
trailing Maximum Loss Limit, and with no single day over the consistency limit of the profit. One breach \
ends the account.
- The Simulation Lab plays the real trading code through history as fresh Combines started on different days \
(live-like runs), resamples whole days into thousands of Combine paths (Monte Carlo), splits the history in \
halves, and checks whether the first-half winner holds up on the second half. It covers every market with \
history and lists "opportunities": market x strategy x time-of-day pockets that made money in both halves. \
With hundreds of pockets tested some always look good by luck: weigh them against how many were tested.

Standards you hold yourself to:
- Be honest. A couple of months of 5-minute bars is a small sample. Say how many trades and days a claim rests \
on. Never present a backtest as a promise.
- Guard against overfitting. Every extra setting you try makes the best result look better than it is. Prefer \
changes that hold up in both halves of the history and across several markets or start days, and prefer \
fewer, simpler changes. If nothing holds up, say so plainly: 'no edge found' is a valid, useful answer.
- Protect the account first. Passing a Combine needs surviving more than winning. You may propose lower risk, \
fewer trades or a different playbook; you may never propose more risk than the account uses now.
- Use the tools: read before you conclude, and test before you propose. Keep experiments small and targeted \
(a few markets and strategies at a time).
- Record each durable finding with record_lesson (with its evidence and your confidence), and each concrete \
change with propose_change. Lessons from earlier sessions are given to you: build on them, and correct any \
that new evidence contradicts.

Finish with a short report for the owner in plain English (no jargon without explaining it): what you looked \
at, what you found, what you tested, what you recommend and why, and what would change your mind. Use \
Markdown headings and short bullet lists. Keep it under about 400 words."""


class CoachError(RuntimeError):
    """The coach can't run: no key, no credits, over budget, or the API refused (the message says which)."""


# ------------------------------------------------------------------------------------------- tools

def _tool(name: str, description: str, properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {"name": name, "description": description, "strict": True,
            "input_schema": {"type": "object", "properties": properties, "required": required or list(properties),
                             "additionalProperties": False}}


TOOLS: list[dict[str, Any]] = [
    _tool("get_account_overview",
          "The account (plan, stage, balance rules), Topstep's rules for it, and the bot's current settings: market, "
          "strategy, risk per trade, daily limits, trading hours. Read this first.", {}),
    _tool("get_simulation_report",
          "The latest Simulation Lab report: the best playbooks (market + strategy) with their live-like Combine runs, "
          "resampled Combine pass/fail rates, net result, profit factor and halves; a summary per asset class; the "
          "opportunities (time-of-day pockets); and the held-out check.", {}),
    _tool("get_markets",
          "Every market Topstep permits: asset class, contract specs and fees, the price history on this PC, whether "
          "this account's plan may trade it, and long-term context (20-day trend, volatility vs its own 10-year past).", {}),
    _tool("get_knowledge",
          "What the bot's knowledge base has learned for one market: each strategy's average result in R by time-of-day "
          "slot and volatility regime, after costs, plus the long-run memory's report if it has one.",
          {"symbol": {"type": "string", "description": "Market root, e.g. MNQ, MES, 6E, GC, ZN."}}),
    _tool("get_trade_journal",
          "The bot's most recent closed trades (paper and live), newest first, with how each ended.",
          {"limit": {"type": "integer", "description": "How many trades, 1-100."}}),
    _tool("get_strategy_catalog",
          "Every strategy the bot has: what it does, its default settings, and the settings worth testing.", {}),
    _tool("run_simulation",
          "Run a Simulation Lab experiment through the bot's real trading code on history: fresh Combines started on "
          "several days per market and strategy, plus resampled Combine paths. Takes up to a few minutes. Keep it "
          "targeted: at most about 6 markets x 4 strategies. Results are returned to you only.",
          {"symbols": {"type": "array", "items": {"type": "string"}, "description": "Markets, e.g. [\"MNQ\", \"MES\"]."},
           "strategies": {"type": "array", "items": {"type": "string"}, "description": "Strategy names from the catalog."},
           "starts": {"type": "integer", "description": "Combine start days per playbook, 1-8 (4 is typical)."},
           "params_json": {"type": "string", "description": "Strategy settings to try instead of the defaults, as a JSON "
                           "object keyed by strategy, e.g. {\"orb\": {\"target_r\": 1.5}}. Use \"{}\" for the defaults."},
           "risk_per_trade": {"type": "number", "description": "Dollars risked per trade to simulate; 0 = the account's own."}}),
    _tool("record_lesson",
          "Save a durable lesson for the bot's memory (shown on the dashboard and given to every later session).",
          {"text": {"type": "string", "description": "The lesson in one or two plain sentences."},
           "evidence": {"type": "string", "description": "The numbers behind it: trades, days, results, which test."},
           "confidence": {"type": "string", "enum": ["low", "medium", "high"]}}),
    _tool("propose_change",
          "Propose one concrete change for the owner to approve on the dashboard. kind 'playbook' switches the market "
          "and strategy; kind 'risk' lowers one risk setting. Nothing changes until the owner presses Apply.",
          {"kind": {"type": "string", "enum": ["playbook", "risk"]},
           "symbol": {"type": "string", "description": "playbook: the market root; risk: \"\"."},
           "strategy": {"type": "string", "description": "playbook: the strategy name; risk: \"\"."},
           "setting": {"type": "string", "enum": ["", "risk_per_trade", "personal_daily_loss_limit", "max_trades_per_day"],
                       "description": "risk: which setting to lower; playbook: \"\"."},
           "value": {"type": "number", "description": "risk: the new, lower value; playbook: 0."},
           "reason": {"type": "string", "description": "Why, in one or two sentences."},
           "evidence": {"type": "string", "description": "The test results that support it."}}),
]


# ------------------------------------------------------------------------------------------- coach

class Coach:
    """Runs coaching sessions for one account's settings and keeps their lessons, proposals and spending."""

    def __init__(self, cfg: BotConfig, api_key: str | None = None):
        self.cfg = cfg
        self.folder = cfg.data_path / "coach"
        self._api_key = api_key
        self.running = False
        self.stage = ""
        self.started: float | None = None
        self.last_error: str | None = None
        self.session_cost = 0.0
        self._cancel = False

    # ---------------------------------------------------------------- key and spending

    @property
    def api_key(self) -> str | None:
        return self._api_key or os.environ.get(ENV_KEY) or load_secrets().anthropic_api_key

    def set_key(self, key: str | None) -> None:
        self._api_key = key or None

    def _client(self):
        try:
            import anthropic
        except ImportError:
            raise CoachError("The Claude API package isn't installed. Run: .venv\\Scripts\\python -m pip install anthropic") from None
        key = self.api_key
        if not key:
            raise CoachError("Add your Claude API key first (dashboard > Coach). Create one at " + KEYS_URL)
        return anthropic.AsyncAnthropic(api_key=key, max_retries=3, timeout=600.0)

    async def test_key(self, key: str) -> str:
        """Check a key with the Claude API (a free call: it looks up the model). Returns a message."""
        try:
            import anthropic
        except ImportError:
            raise CoachError("The Claude API package isn't installed. Run: .venv\\Scripts\\python -m pip install anthropic") from None
        client = anthropic.AsyncAnthropic(api_key=key, max_retries=1, timeout=30.0)
        try:
            model = await client.models.retrieve(self.cfg.coach.model)
        except anthropic.AuthenticationError:
            raise CoachError("Claude refused that key. Copy it again from " + KEYS_URL) from None
        except anthropic.PermissionDeniedError:
            raise CoachError("That key may not use the Claude API (permission denied). Check its workspace on " + KEYS_URL) from None
        except anthropic.NotFoundError:
            return f"The key works, but {self.cfg.coach.model} isn't available to it: pick another model."
        except anthropic.APIConnectionError:
            raise CoachError("Couldn't reach the Claude API. Check the internet connection and try again.") from None
        finally:
            await client.close()
        return f"Key works: {getattr(model, 'display_name', None) or self.cfg.coach.model} is available."

    def _usage_path(self) -> Path:
        return self.folder / "usage.jsonl"

    def spent(self, month: str | None = None) -> float:
        """Dollars spent on coaching this calendar month (UTC), from the usage log."""
        month = month or datetime.now(UTC).strftime("%Y-%m")
        total = 0.0
        path = self._usage_path()
        if not path.exists():
            return 0.0
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if str(row.get("at", "")).startswith(month):
                total += float(row.get("cost", 0.0))
        return round(total, 4)

    @staticmethod
    def price(model: str, usage: Any) -> float:
        """Dollars for one response's usage (every attempt, if the response fell back to another model)."""
        def one(m: str, u: Any) -> float:
            pin, pout, pread, pwrite = PRICES.get(m, PRICES["claude-opus-5-5"])
            return (_tokens(u, "input_tokens") * pin + _tokens(u, "output_tokens") * pout
                    + _tokens(u, "cache_read_input_tokens") * pread + _tokens(u, "cache_creation_input_tokens") * pwrite) / 1_000_000

        iterations = getattr(usage, "iterations", None) if not isinstance(usage, dict) else usage.get("iterations")
        if iterations:
            total = 0.0
            for it in iterations:
                m = (it.get("model") if isinstance(it, dict) else getattr(it, "model", None)) or model
                total += one(m, it)
            return total
        return one(model, usage)

    def _log_usage(self, model: str, usage: Any, cost: float) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        row = {"at": datetime.now(UTC).isoformat(timespec="seconds"), "model": model, "input": _tokens(usage, "input_tokens"),
               "output": _tokens(usage, "output_tokens"), "cache_read": _tokens(usage, "cache_read_input_tokens"),
               "cache_write": _tokens(usage, "cache_creation_input_tokens"), "cost": round(cost, 6)}
        with self._usage_path().open("a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")

    # ---------------------------------------------------------------- lessons and proposals

    def _read(self, name: str, default: Any) -> Any:
        path = self.folder / name
        if not path.exists():
            return default
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return default

    def _write(self, name: str, data: Any) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        path = self.folder / name
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1, default=str), encoding="utf-8")
        os.replace(tmp, path)

    def lessons(self) -> list[dict[str, Any]]:
        return self._read("lessons.json", [])

    def proposals(self) -> list[dict[str, Any]]:
        return self._read("proposals.json", [])

    def sessions(self, limit: int = 10) -> list[dict[str, Any]]:
        folder = self.folder / "sessions"
        if not folder.exists():
            return []
        out = []
        for path in sorted(folder.glob("*.json"), reverse=True)[:limit]:
            try:
                out.append(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                continue
        return out

    def add_lesson(self, text: str, evidence: str, confidence: str, session: str) -> dict[str, Any]:
        text = " ".join(str(text).split())[:500]
        if not text:
            raise ValueError("a lesson needs text")
        lesson = {"id": pysecrets.token_hex(4), "at": datetime.now(UTC).isoformat(timespec="seconds"), "text": text,
                  "evidence": " ".join(str(evidence).split())[:800], "confidence": confidence if confidence in ("low", "medium", "high") else "low",
                  "session": session}
        items = [x for x in self.lessons() if x.get("text") != text] + [lesson]
        self._write("lessons.json", items[-LESSONS_KEPT:])
        return lesson

    def forget_lesson(self, lesson_id: str) -> bool:
        items = self.lessons()
        kept = [x for x in items if x.get("id") != lesson_id]
        self._write("lessons.json", kept)
        return len(kept) < len(items)

    def add_proposal(self, body: dict[str, Any], session: str) -> dict[str, Any]:
        """Check and keep a proposed change (see propose_change). Raises ValueError with the reason when it is refused."""
        from topstep_bot.risk.topstep import PLANS, product_limit
        from topstep_bot.strategies import STRATEGIES

        kind = body.get("kind")
        prop: dict[str, Any] = {"id": pysecrets.token_hex(4), "at": datetime.now(UTC).isoformat(timespec="seconds"),
                                "kind": kind, "reason": " ".join(str(body.get("reason", "")).split())[:500],
                                "evidence": " ".join(str(body.get("evidence", "")).split())[:800], "status": "pending",
                                "session": session}
        if kind == "playbook":
            symbol = str(body.get("symbol", "")).strip().upper()
            strategy = str(body.get("strategy", "")).strip()
            if strategy not in STRATEGIES:
                raise ValueError(f"unknown strategy '{strategy}'")
            from topstep_bot.instruments import SPECS

            if symbol not in SPECS:
                raise ValueError(f"unknown market '{symbol}'")
            if product_limit(symbol, PLANS[self.cfg.account.plan]) == 0:
                raise ValueError(f"Topstep doesn't allow {symbol} on a {self.cfg.account.plan} account")
            if symbol == self.cfg.instrument.symbol and strategy == self.cfg.strategy.name and not self.cfg.instrument.auto_pick:
                raise ValueError("that is already the bot's playbook")
            prop.update(symbol=symbol, strategy=strategy, title=f"Trade {symbol} with {STRATEGIES[strategy].title}")
        elif kind == "risk":
            setting = body.get("setting")
            current = {"risk_per_trade": self.cfg.risk.risk_per_trade,
                       "personal_daily_loss_limit": self.cfg.risk.personal_daily_loss_limit,
                       "max_trades_per_day": self.cfg.risk.max_trades_per_day}
            if setting not in current:
                raise ValueError("setting must be risk_per_trade, personal_daily_loss_limit or max_trades_per_day")
            value = float(body.get("value") or 0)
            if setting == "max_trades_per_day":
                value = float(int(value))
            if value <= 0:
                raise ValueError("the new value must be above zero")
            if value >= current[setting]:
                raise ValueError(f"the coach may only lower risk: {setting} is {current[setting]:g} now, {value:g} isn't lower")
            label = {"risk_per_trade": "Risk per trade", "personal_daily_loss_limit": "Daily loss limit",
                     "max_trades_per_day": "Max trades per day"}[setting]
            money = setting != "max_trades_per_day"
            fmt = (lambda v: f"${v:,.0f}") if money else (lambda v: f"{v:g}")
            prop.update(setting=setting, value=value, old=current[setting],
                        title=f"{label}: {fmt(current[setting])} -> {fmt(value)}")
        else:
            raise ValueError("kind must be 'playbook' or 'risk'")
        items = [p for p in self.proposals() if not (p.get("status") == "pending" and p.get("title") == prop["title"])]
        self._write("proposals.json", (items + [prop])[-40:])
        return prop

    def set_proposal_status(self, proposal_id: str, status: str) -> dict[str, Any]:
        items = self.proposals()
        for p in items:
            if p.get("id") == proposal_id:
                p["status"] = status
                p["decided"] = datetime.now(UTC).isoformat(timespec="seconds")
                self._write("proposals.json", items)
                return p
        raise ValueError("No such proposal (it may have been replaced by a newer one).")

    # ---------------------------------------------------------------- tool implementations

    def _overview(self) -> dict[str, Any]:
        from topstep_bot.risk.topstep import PLANS

        cfg = self.cfg
        plan = PLANS[cfg.account.plan]
        out: dict[str, Any] = {
            "account": {"plan": plan.name, "stage": cfg.account.stage, "account_size": plan.account_size,
                        "profit_target": plan.profit_target, "max_loss_limit_trailing": plan.max_loss_limit,
                        "max_minis": plan.max_minis, "topstep_daily_loss_limit": cfg.account.topstep_daily_loss_limit},
            "bot": {"market": cfg.instrument.symbol, "auto_pick_market": cfg.instrument.auto_pick,
                    "timeframe_minutes": cfg.instrument.timeframe_minutes, "strategy": cfg.strategy.name,
                    "strategy_params": cfg.strategy.params, "risk_per_trade": cfg.risk.risk_per_trade,
                    "personal_daily_loss_limit": cfg.risk.personal_daily_loss_limit,
                    "max_trades_per_day": cfg.risk.max_trades_per_day, "max_consecutive_losses": cfg.risk.max_consecutive_losses,
                    "mll_buffer": cfg.risk.mll_buffer, "slippage_ticks_assumed": cfg.risk.slippage_ticks,
                    "trade_start": str(cfg.session.trade_start), "last_entry": str(cfg.session.last_entry),
                    "flatten_at": str(cfg.session.flatten_at)},
            "rules": ["Combine: reach the profit target; the balance including open P&L may never touch the trailing "
                      "Maximum Loss Limit (it trails the end-of-day balance up to the starting balance).",
                      "Consistency: the best day may be at most about half of the total profit when the target is reached.",
                      "Flat by 15:10 CT every day; no trading outside the best bid/offer; no hedging across accounts.",
                      "Orders must come from the owner's own PC; no high-frequency trading."],
        }
        try:
            from topstep_bot.journal import Journal

            for mode in ("live", "paper"):
                path = cfg.journal_path_for(mode)
                if path.exists():
                    j = Journal(path)
                    try:
                        rows = j.trades(limit=1000)
                        out[f"journal_{mode}"] = {"trades": len(rows), "net": round(sum(r.get("net_pnl") or 0 for r in rows), 2),
                                                  "wins": sum(1 for r in rows if (r.get("net_pnl") or 0) > 0)}
                    finally:
                        j.close()
        except Exception as exc:  # noqa: BLE001 - the journal is optional context
            out["journal_error"] = str(exc)
        return out

    def _simulation_report(self) -> dict[str, Any]:
        from topstep_bot.simlab import load_report

        rep = load_report(self.cfg)
        if not rep:
            return {"report": None, "note": "No Simulation Lab report yet. Use run_simulation to create evidence."}
        trimmed = {k: v for k, v in rep.items() if k not in ("runs",)}
        playbooks = trimmed.get("playbooks", [])
        for p in playbooks:
            p.pop("equity", None)
        if len(playbooks) > 30:  # every market: the best ones, the current one, and the count
            current = [p for p in playbooks[30:] if p["symbol"] == self.cfg.instrument.symbol]
            trimmed["playbooks"] = playbooks[:30] + current[:8]
            trimmed["playbooks_note"] = f"{len(playbooks)} playbooks in all; the 30 best and the current market's are shown."
        opp = trimmed.get("opportunities")
        if opp and len(opp.get("items", [])) > 25:
            trimmed["opportunities"] = {**opp, "items": opp["items"][:25]}
        return trimmed

    def _markets(self) -> list[dict[str, Any]]:
        from topstep_bot.market_data import coverage

        keep = ("symbol", "description", "class", "tick_size", "tick_value", "fees", "hours", "days", "last", "sources",
                "allowed", "context")
        return [{k: m[k] for k in keep} for m in coverage(self.cfg)]

    def _knowledge(self, symbol: str) -> dict[str, Any]:
        from topstep_bot.insights import build_report
        from topstep_bot.knowledge import KnowledgeBase
        from topstep_bot.strategies import BASE_STRATEGIES, STRATEGIES

        cfg = self.cfg.model_copy(deep=True)
        cfg.instrument.symbol = (symbol or cfg.instrument.symbol).upper()
        names = [(n, STRATEGIES[n].title) for n in BASE_STRATEGIES]
        out: dict[str, Any] = {"symbol": cfg.instrument.symbol}
        for label, path in (("recent", cfg.knowledge_path), ("long_run", cfg.longrun_knowledge_path)):
            if not path.exists():
                out[label] = None
                continue
            kb = KnowledgeBase(path, backups=0, ledger=False, half_life_days=cfg.knowledge.half_life_days,
                               min_samples=cfg.knowledge.min_samples, min_edge_r=cfg.knowledge.min_edge_r)
            if label == "recent":
                out["recent_text"] = kb.text(names)
            rep = build_report(kb, names, slippage_ticks=cfg.risk.slippage_ticks)
            out[label] = {"coverage": rep.get("coverage"),
                          "strategies": [{k: r.get(k) for k in ("name", "n", "win_rate", "gross_r", "net_r", "mfe_r", "mae_r",
                                                                   "gave_back", "real_n", "real_net_r")} for r in rep["strategies"]],
                          "hints": [{"strategy": h.get("title"), "feature": h.get("feature_name"), "best": h.get("best"),
                                     "worst": h.get("worst")} for h in rep.get("hints", [])][:8]}
        return out

    def _journal(self, limit: int) -> list[dict[str, Any]]:
        from topstep_bot.journal import Journal

        limit = max(1, min(100, int(limit or 20)))
        out: list[dict[str, Any]] = []
        for mode in ("live", "paper"):
            path = self.cfg.journal_path_for(mode)
            if not path.exists():
                continue
            j = Journal(path)
            try:
                for r in j.trades(limit=limit):
                    out.append({"mode": mode, **{k: r.get(k) for k in ("trading_day", "strategy", "side", "size", "entry_price",
                                                                     "exit_price", "net_pnl", "r_multiple", "exit_reason")}})
            finally:
                j.close()
        out.sort(key=lambda r: str(r.get("trading_day") or ""), reverse=True)
        return out[:limit]

    def _catalog(self) -> list[dict[str, Any]]:
        from topstep_bot.strategies import STRATEGIES
        from topstep_bot.training import GRIDS

        out = []
        for name, cls in STRATEGIES.items():
            grid = GRIDS.get(name) or []
            tried: dict[str, list] = {}
            for combo in grid:
                for k, v in combo.items():
                    tried.setdefault(k, [])
                    if v not in tried[k]:
                        tried[k].append(v)
            out.append({"name": name, "title": cls.title, "description": " ".join(str(getattr(cls, "description", "")).split()),
                        "defaults": cls.defaults, "values_worth_testing": tried})
        return out

    async def _simulate(self, body: dict[str, Any], progress: Callable[[str], None] | None) -> dict[str, Any]:
        from topstep_bot.simlab import available_markets, lab_strategies, run_lab

        if not self.cfg.coach.allow_simulations:
            return {"error": "Simulations are turned off for the coach (coach.allow_simulations)."}
        available = available_markets(self.cfg)
        symbols = [s.strip().upper() for s in body.get("symbols") or [] if s.strip()] or [self.cfg.instrument.symbol]
        missing = [s for s in symbols if s not in available]
        symbols = [s for s in symbols if s in available]
        if not symbols:
            return {"error": f"No history on this PC for {', '.join(missing)}. Markets with history: {', '.join(available) or 'none'}."}
        known = lab_strategies(self.cfg)
        strategies = [s for s in body.get("strategies") or [] if s in known] or [self.cfg.strategy.name]
        starts = max(1, min(8, int(body.get("starts") or 4)))
        if len(symbols) * len(strategies) * (starts + 1) > MAX_SIM_SPECS:
            return {"error": f"That is {len(symbols) * len(strategies) * (starts + 1)} simulations; keep one experiment under "
                             f"{MAX_SIM_SPECS} (fewer markets, strategies or start days)."}
        try:
            params = json.loads(body.get("params_json") or "{}")
            if not isinstance(params, dict) or not all(isinstance(v, dict) for v in params.values()):
                raise ValueError("params_json must be an object of objects, keyed by strategy")
        except ValueError as exc:
            return {"error": f"Bad params_json: {exc}"}
        risk = float(body.get("risk_per_trade") or 0) or None
        if risk is not None and risk > self.cfg.risk.risk_per_trade * 1.5:
            return {"error": f"Keep risk_per_trade at most 1.5x the account's (${self.cfg.risk.risk_per_trade:,.0f})."}

        def step(done: int, total: int, stage: str) -> None:
            if progress:
                progress(f"simulating: {stage}")

        try:
            rep = await run_lab(self.cfg, starts=starts, symbols=symbols, strategies=strategies, params=params or None,
                                risk_per_trade=risk, save=False, progress=step)
        except (ValueError, TypeError) as exc:
            return {"error": str(exc)}
        for p in rep["playbooks"]:
            p.pop("equity", None)
        return {"note": (f"Not in history here: {', '.join(missing)}. " if missing else "") + "Experiment only: the lab's main report is unchanged.",
                **{k: rep[k] for k in ("markets", "totals", "playbooks", "holdout", "opportunities", "params",
                                       "risk_per_trade", "caveats")}}

    async def _run_tool(self, name: str, args: dict[str, Any], session: dict[str, Any],
                        progress: Callable[[str], None] | None) -> str:
        try:
            if name == "get_account_overview":
                result: Any = self._overview()
            elif name == "get_simulation_report":
                result = self._simulation_report()
            elif name == "get_markets":
                result = self._markets()
            elif name == "get_knowledge":
                result = await asyncio.to_thread(self._knowledge, str(args.get("symbol") or ""))
            elif name == "get_trade_journal":
                result = await asyncio.to_thread(self._journal, int(args.get("limit") or 20))
            elif name == "get_strategy_catalog":
                result = self._catalog()
            elif name == "run_simulation":
                result = await self._simulate(args, progress)
                session["experiments"].append({k: args.get(k) for k in ("symbols", "strategies", "starts", "params_json", "risk_per_trade")})
            elif name == "record_lesson":
                lesson = self.add_lesson(args.get("text", ""), args.get("evidence", ""), args.get("confidence", "low"), session["id"])
                session["lessons"].append(lesson)
                result = {"saved": True, "id": lesson["id"]}
            elif name == "propose_change":
                prop = self.add_proposal(args, session["id"])
                session["proposals"].append(prop)
                result = {"saved": True, "id": prop["id"], "title": prop["title"], "note": "Waiting for the owner's approval."}
            else:
                result = {"error": f"unknown tool {name}"}
        except ValueError as exc:
            result = {"error": str(exc)}
        except Exception as exc:  # noqa: BLE001 - a broken tool is reported to Claude, not fatal
            log.exception("Coach tool %s failed", name)
            result = {"error": f"{type(exc).__name__}: {exc}"}
        text = json.dumps(result, default=str)
        if len(text) > TOOL_RESULT_CHARS:
            text = text[:TOOL_RESULT_CHARS] + ' ... (cut short: ask for less)'
        return text

    # ---------------------------------------------------------------- a session

    def _opening(self, question: str) -> str:
        lessons = self.lessons()[-20:]
        recent = "\n".join(f"- [{x['confidence']}] {x['text']} (evidence: {x['evidence']}; {x['at'][:10]})" for x in lessons)
        pending = [p for p in self.proposals() if p.get("status") == "pending"]
        decided = [p for p in self.proposals() if p.get("status") in ("applied", "dismissed")][-6:]
        parts = [f"Today is {datetime.now(UTC):%Y-%m-%d} (UTC)."]
        parts.append("Lessons from earlier sessions:\n" + recent if recent else "No lessons recorded yet: this is the first session.")
        if pending:
            parts.append("Proposals still waiting for the owner: " + "; ".join(p["title"] for p in pending))
        if decided:
            parts.append("Owner decisions on earlier proposals: " + "; ".join(f"{p['title']}: {p['status']}" for p in decided))
        parts.append(f"The owner asks: {question}" if question else
                     "The owner asks for a full review: is the bot set up to pass its Combine, what does the evidence say, "
                     "and what should change before the next session?")
        return "\n\n".join(parts)

    async def run(self, question: str = "", progress: Callable[[str], None] | None = None) -> dict[str, Any]:
        """One coaching session. Returns the saved session (report, lessons, proposals, cost)."""
        import anthropic

        if self.running:
            raise CoachError("A coaching session is already running.")
        c = self.cfg.coach
        if not c.enabled:
            raise CoachError("The coach is turned off (coach.enabled in config.yaml).")
        month = self.spent()
        if month >= c.monthly_budget_usd:
            raise CoachError(f"This month's coaching budget is used up (${month:.2f} of ${c.monthly_budget_usd:.2f}). "
                             "Raise coach.monthly_budget_usd on the Coach tab to continue.")
        client = self._client()
        self.running, self._cancel, self.last_error = True, False, None
        self.started, self.session_cost = time.time(), 0.0
        stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        session: dict[str, Any] = {"id": stamp, "question": question, "model": c.model, "effort": c.effort,
                                   "started": datetime.now(UTC).isoformat(timespec="seconds"), "tools": [], "experiments": [],
                                   "lessons": [], "proposals": [], "report": "", "cost_usd": 0.0, "turns": 0,
                                   "stopped": None}

        def say(text: str) -> None:
            self.stage = text
            if progress:
                progress(text)

        messages: list[dict[str, Any]] = [{"role": "user", "content": self._opening(question)}]
        extra: dict[str, Any] = {}
        if c.model in FALLBACK_MODELS:
            extra = {"betas": [FALLBACK_BETA], "fallbacks": "default"}
        try:
            for turn in range(c.max_turns):
                if self._cancel:
                    session["stopped"] = "stopped by you"
                    break
                if self.session_cost >= c.max_session_usd:
                    session["stopped"] = f"reached the session budget (${c.max_session_usd:.2f})"
                    break
                if month + self.session_cost >= c.monthly_budget_usd:
                    session["stopped"] = f"reached the monthly budget (${c.monthly_budget_usd:.2f})"
                    break
                say("thinking" if turn == 0 else "reviewing the results")
                last = turn == c.max_turns - 1 or self.session_cost >= 0.85 * c.max_session_usd
                if last:  # nearly out of turns or budget: ask for the report now (an operator message after the tool results)
                    messages.append({"role": "system", "content": "The session budget is nearly used: write your final "
                                                                  "report now, without more tool calls."})
                request = {"model": c.model, "max_tokens": MAX_TOKENS, "system": SYSTEM, "tools": TOOLS,
                           "tool_choice": {"type": "none"} if last else {"type": "auto"}, "messages": messages,
                           "thinking": {"type": "adaptive"}, "output_config": {"effort": c.effort},
                           "cache_control": {"type": "ephemeral"}}
                try:
                    response = await client.beta.messages.create(**request, **extra)
                except anthropic.BadRequestError as exc:
                    msg = _api_message(exc).lower()
                    if not extra or any(w in msg for w in ("credit", "balance", "billing")):
                        raise
                    # an account or API version without refusal fallbacks: carry on without them
                    log.warning("The Claude API refused the request with refusal fallbacks (%s); retrying without them", msg)
                    extra = {}
                    response = await client.beta.messages.create(**request)
                cost = self.price(c.model, response.usage)
                self.session_cost += cost
                self._log_usage(response.model or c.model, response.usage, cost)
                session["turns"] = turn + 1
                if response.stop_reason == "refusal":
                    session["stopped"] = "Claude declined to continue this session"
                    break
                content = _echoable(response.content)
                messages.append({"role": "assistant", "content": content})
                text = "\n".join(b.text for b in response.content if getattr(b, "type", "") == "text").strip()
                if text:
                    session["report"] = text
                calls = [b for b in response.content if getattr(b, "type", "") == "tool_use"]
                if response.stop_reason != "tool_use" or not calls:
                    if response.stop_reason == "max_tokens" and not text:
                        session["stopped"] = "the answer was too long"
                    break
                results = []
                for call in calls:
                    say(_tool_label(call.name, call.input))
                    session["tools"].append({"name": call.name, "input": call.input})
                    out = await self._run_tool(call.name, dict(call.input or {}), session, say)
                    results.append({"type": "tool_result", "tool_use_id": call.id, "content": out,
                                    **({"is_error": True} if out.startswith('{"error"') else {})})
                messages.append({"role": "user", "content": results})
        except anthropic.AuthenticationError:
            raise self._fail("Claude refused the API key. Add it again on the Coach tab (" + KEYS_URL + ").") from None
        except anthropic.PermissionDeniedError as exc:
            raise self._fail(f"The API key isn't allowed to do that: {_api_message(exc)}") from None
        except anthropic.RateLimitError:
            raise self._fail("The Claude API is rate-limiting this key right now. Try again in a minute.") from None
        except anthropic.BadRequestError as exc:
            msg = _api_message(exc)
            if "credit" in msg.lower() or "balance" in msg.lower() or "billing" in msg.lower():
                raise self._fail("Your Claude API credit balance is too low. Buy credits at " + BILLING_URL) from None
            raise self._fail(f"The Claude API rejected the request: {msg}") from None
        except anthropic.APIStatusError as exc:
            if exc.status_code == 402:
                raise self._fail("Your Claude API credit balance is too low. Buy credits at " + BILLING_URL) from None
            raise self._fail(f"The Claude API had a problem ({exc.status_code}): {_api_message(exc)}. Try again later.") from None
        except anthropic.APIConnectionError:
            raise self._fail("Couldn't reach the Claude API. Check the internet connection and try again.") from None
        finally:
            self.running = False
            self.stage = ""
            await client.close()
            session["cost_usd"] = round(self.session_cost, 4)
            session["finished"] = datetime.now(UTC).isoformat(timespec="seconds")
            if not session["report"] and (session["lessons"] or session["proposals"] or session["stopped"]):
                session["report"] = _fallback_report(session)
            self._save_session(session)
        return session

    def _fail(self, message: str) -> CoachError:
        self.last_error = message
        return CoachError(message)

    def cancel(self) -> bool:
        if not self.running:
            return False
        self._cancel = True
        return True

    def _save_session(self, session: dict[str, Any]) -> None:
        folder = self.folder / "sessions"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{session['id']}.json").write_text(json.dumps(session, indent=1, default=str), encoding="utf-8")
        for old in sorted(folder.glob("*.json"))[:-SESSIONS_KEPT]:
            old.unlink(missing_ok=True)
        if session.get("report"):
            (self.folder / "latest_report.md").write_text(session["report"], encoding="utf-8")

    # ---------------------------------------------------------------- status

    def status(self) -> dict[str, Any]:
        c = self.cfg.coach
        latest = self.sessions(1)
        return {"enabled": c.enabled, "key_saved": bool(self.api_key), "model": c.model, "models": MODELS, "effort": c.effort,
                "max_session_usd": c.max_session_usd, "monthly_budget_usd": c.monthly_budget_usd,
                "spent_month": self.spent(), "running": self.running, "stage": self.stage,
                "elapsed": round(time.time() - self.started) if self.running and self.started else None,
                "session_cost": round(self.session_cost, 4), "error": self.last_error,
                "lessons": list(reversed(self.lessons()))[:30],
                "proposals": [p for p in reversed(self.proposals())][:20],
                "latest": ({k: latest[0].get(k) for k in ("id", "question", "model", "report", "cost_usd", "turns", "stopped",
                                                         "started", "finished")}
                           | {"tools": len(latest[0].get("tools", [])), "experiments": latest[0].get("experiments", [])}
                           if latest else None),
                "history": [{k: s.get(k) for k in ("id", "question", "cost_usd", "turns", "stopped")} for s in self.sessions(8)],
                "links": {"billing": BILLING_URL, "keys": KEYS_URL, "console": CONSOLE_URL}}

    def status_text(self) -> str:
        s = self.status()
        lines = [f"AI coach: {'on' if s['enabled'] else 'off'} · {s['model']} · key {'saved' if s['key_saved'] else 'missing'}",
                 f"Spent this month: ${s['spent_month']:.2f} of ${s['monthly_budget_usd']:.2f} (session cap ${s['max_session_usd']:.2f})"]
        if s["lessons"]:
            lines.append("Latest lessons:")
            lines += [f"  [{x['confidence']}] {x['text']}" for x in s["lessons"][:6]]
        pending = [p for p in s["proposals"] if p["status"] == "pending"]
        if pending:
            lines.append("Waiting for your approval: " + "; ".join(p["title"] for p in pending))
        if not s["key_saved"]:
            lines.append(f"Add a Claude API key on the dashboard's Coach tab (create one at {KEYS_URL}).")
        return "\n".join(lines)


def _tokens(usage: Any, key: str) -> int:
    value = usage.get(key) if isinstance(usage, dict) else getattr(usage, key, 0)
    return int(value or 0)


def _echoable(content: list[Any]) -> list[Any]:
    """The assistant turn to send back. After a fallback to another model mid-answer, the declined model's
    thinking and tool calls before the switch are left out (the API's rule for echoing fallback turns)."""
    idx = max((i for i, b in enumerate(content) if getattr(b, "type", "") == "fallback"), default=None)
    if idx is None:
        return list(content)
    drop = ("thinking", "redacted_thinking", "tool_use")
    return [b for i, b in enumerate(content) if i > idx or getattr(b, "type", "") not in drop + ("fallback",)]


def _api_message(exc: Any) -> str:
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        err = body.get("error") or {}
        if isinstance(err, dict) and err.get("message"):
            return str(err["message"])
    return str(getattr(exc, "message", "") or exc)


def _tool_label(name: str, args: Any) -> str:
    args = args or {}
    if name == "run_simulation":
        return f"simulating {', '.join(args.get('strategies') or [])} on {', '.join(args.get('symbols') or [])}"
    return {"get_account_overview": "reading the account and rules", "get_simulation_report": "reading the Simulation Lab report",
            "get_knowledge": f"reading what the bot knows about {args.get('symbol') or 'the market'}",
            "get_trade_journal": "reading the trade journal", "get_strategy_catalog": "reading the strategy catalogue",
            "record_lesson": "recording a lesson", "propose_change": "writing a proposal"}.get(name, name)


def _fallback_report(session: dict[str, Any]) -> str:
    lines = ["## Session summary", "", f"The session ended before Claude wrote its report ({session.get('stopped') or 'no final report'})."]
    if session.get("tools"):
        lines += ["", f"It made {len(session['tools'])} tool call(s) first. Raise the per-session limit on the Coach tab for a full review."]
    if session["lessons"]:
        lines += ["", "### Lessons recorded"] + [f"- {x['text']}" for x in session["lessons"]]
    if session["proposals"]:
        lines += ["", "### Proposals"] + [f"- {p['title']}: {p['reason']}" for p in session["proposals"]]
    return "\n".join(lines)
