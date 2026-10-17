"""Remote-control actions for the running bot (reached through its local API by the dashboard
and Telegram, which live in the separate controller process).

Every action is logged in the activity feed with where it came from, and none of them can
loosen a risk limit: the strongest thing a remote command can do is let the bot continue
trading within the limits already in config.yaml.
"""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable
from contextvars import ContextVar

from topstep_bot import fmt
from topstep_bot.engine import TradingCore
from topstep_bot.live import Controls, LiveRunner

# The market one request names (several markets at once): per request, so requests running side by side never mix.
_PINNED: ContextVar[TradingCore | None] = ContextVar("pinned_market", default=None)
MARKET_ACTIONS = ("markets", "add_market", "remove_market", "switch_market", "focus_market", "grow_network")


class BotActions:
    """``core``: the market the actions act on. With ``desk`` (the running bot, live.py) that is the market shown on
    the dashboard, or the one a request names (``market`` in its payload), and the market actions are available."""

    def __init__(self, core: TradingCore, controls: Controls, retrain: Callable[[str], Awaitable[str]] | None = None,
                 learn: Callable[[str], Awaitable[str]] | None = None, desk: LiveRunner | None = None):
        self._core = core
        self.desk = desk
        self.controls = controls
        self._retrain = retrain  # LiveRunner.retrain: downloads history and rebuilds the knowledge base
        self._learn = learn  # LiveRunner.learn: backfills the market library and replays all of it (memory.py)
        self._facts = None  # the central intelligence's lookups (brain/facts.py), made on first use

    @property
    def core(self) -> TradingCore:
        pinned = _PINNED.get()
        if pinned is not None:
            return pinned
        if self.desk is not None and self.desk.core is not None:
            return self.desk.core
        return self._core

    @core.setter
    def core(self, value: TradingCore) -> None:
        self._core = value

    def all_cores(self) -> list[TradingCore]:
        return self.desk.cores() if self.desk is not None and self.desk.cores() else [self.core]

    def pause(self, source: str) -> str:
        if self.core.risk.paused:
            return "New trades are already paused."
        self.core.risk.paused = True
        self.core.event("warning", f"New trades paused from {source}")
        return "Paused: no new trades. Any open trade keeps its stop and target."

    def resume(self, source: str) -> str:
        if self.core.halted:
            return "The bot is halted (after a flatten). Restart the bot (dashboard or /restart) to trade again."
        if not self.core.risk.paused:
            return "Trading is already active."
        self.core.risk.paused = False
        self.core.event("info", f"Trading resumed from {source}")
        return "Resumed: the bot may open new trades again (all risk limits still apply)."

    def flatten(self, source: str) -> str:
        self.controls.flatten_reason = f"flatten requested from {source}"
        self.controls.flatten_requested = True
        return "Flattening: closing any position, cancelling orders and halting trading."

    def stop(self, source: str) -> str:
        self.core.event("warning", f"Stop requested from {source}")
        self.controls.request_stop(f"stop requested from {source}")
        return "Stopping the bot (it flattens first). The dashboard and Telegram stay online - start it again any time."

    # ------------------------------------------------------------------ text

    def status_text(self) -> str:
        s = self.core.snapshot()
        r = s["risk"]
        if s["halted"]:
            state = f"HALTED - {s['halted']}"
        elif r["paused"]:
            state = "PAUSED (no new trades)"
        elif r["locked"]:
            state = f"Done for today - {r['locked']}"
        else:
            state = "Trading normally"
        lines = [
            f"{'🔴 LIVE' if s['mode'] == 'live' else '🔵 PAPER'} | {s['account']} | {s['plan']}",
            f"{s['contract']} {s['timeframe']}m | {s['strategy']}",
            f"Status: {state}",
            f"Feed: {'connected' if s['connected'] else 'DISCONNECTED'}",
            "",
            f"Balance: {fmt.money(s['balance'])}",
            f"Today: {fmt.money(r['day_pnl'])}  (open {fmt.money(s['open_pnl'])})",
        ]
        t = s["trade"]
        if t:
            target = f", target {t['target_price']}" if t["target_price"] is not None else ""
            entry = t["entry_price"] if t["entry_price"] is not None else "pending"
            lines.append(f"Position: {t['side']} {t['size']} @ {entry} (stop {t['stop_price']}{target})")
        elif s["position"]:
            lines.append(f"Position: {s['position']} contracts (not opened by the bot)")
        else:
            lines.append("Position: flat")
        lines += [
            f"MLL room: {fmt.money(r['mll_room'])} (floor {fmt.money(r['mll_floor'])})",
            f"Daily loss used: {fmt.money(max(0.0, -r['day_pnl']))} of {fmt.money(r['daily_loss_limit'])}",
            f"Trades today: {r['trades_today']} of {r['max_trades']}",
        ]
        if s["profit_target"]:
            lines.append(f"Combine progress: {fmt.money(s['total_profit'])} of {fmt.money(s['profit_target'])}")
        lines.append(f"Time: {s['time']}")
        if s.get("forecast"):
            lines.append(f"⏳ {s['forecast']['headline']}")
        return "\n".join(lines)

    def trades_text(self, limit: int = 5) -> str:
        trades = list(reversed(self.core.closed_trades[-limit:]))
        if not trades and self.core.journal:
            rows = self.core.journal.trades(limit=limit, account=self.core.account_label)
            if not rows:
                return "No closed trades yet."
            return "Recent trades:\n" + "\n".join(
                f"{(r['exit_time'] or '')[:16].replace('T', ' ')}  {r['side']} {r['size']}  "
                f"{fmt.money(r['net_pnl'] or 0)}  ({r['exit_reason']})"
                for r in rows
            )
        if not trades:
            return "No closed trades yet."
        return "Recent trades:\n" + "\n".join(
            f"{self.core.schedule.local(t.closed_at).strftime('%m-%d %H:%M')}  {t.side.label} {t.filled_size}  "
            f"{fmt.money(t.net_pnl)}  ({t.exit_reason})"
            for t in trades
            if t.closed_at
        )

    # ------------------------------------------------------------------ remote settings & trades

    def _remote(self):
        if self.core.remote is None:
            raise RuntimeError("Remote settings are not available")
        return self.core.remote

    def preview_setting(self, key: str, value) -> dict:
        return self._remote().preview(key, value)

    def change_setting(self, key: str, value, source: str) -> str:
        """A setting applies to every market the account trades."""
        answer = self._remote().apply(key, value, source)
        for core in self.all_cores():
            if core is not self.core and core.remote is not None:
                try:
                    core.remote.apply(key, value, source)
                except Exception as exc:  # noqa: BLE001 - the main market changed; say what didn't follow
                    answer += f"\n{core.contract.name}: {exc}"
        return answer

    def reset_settings(self, source: str) -> str:
        answer = self._remote().reset(source)
        for core in self.all_cores():
            if core is not self.core and core.remote is not None:
                core.remote.reset(source)
        return answer

    def settings_text(self) -> str:
        return self._remote().settings_text()

    async def take_idea(self, rec_id: str, source: str, size: int | None = None) -> str:
        return await self._remote().take_idea(rec_id, source, size)

    # ------------------------------------------------------------------ manual trades (dashboard trade ticket)

    def trade_ticket(self, payload: dict) -> dict:
        """Preview a manual trade: size, risk, rule checks and what the bot knows. Places nothing."""
        return self.core.manual.ticket(payload.get("side"), payload.get("stop"), payload.get("target"), payload.get("size"))

    async def manual_trade(self, payload: dict, source: str) -> str:
        return await self.core.manual.open(payload.get("side"), payload.get("stop"), payload.get("target"),
                                           payload.get("size"), source, str(payload.get("note") or ""))

    async def close_trade(self, source: str) -> str:
        return await self.core.manual.close(source)

    async def stop_breakeven(self, source: str) -> str:
        return await self.core.manual.stop_to_breakeven(source)

    def open_ideas(self, limit: int = 4) -> list[dict]:
        """Recommendations that can still be taken (newest first)."""
        from topstep_bot.remote import IDEA_MAX_AGE

        book = self.core.recommender
        if book is None:
            return []
        now = self.core.clock()
        fresh = [r for r in book.items if r.is_open and r.status in ("idea", "tracking", "skipped")
                 and r.size and now - r.created <= IDEA_MAX_AGE]
        return [book.to_dict(r) for r in fresh[:limit]]

    def find_idea(self, rec_id: str) -> dict | None:
        book = self.core.recommender
        rec = next((r for r in book.items if r.id == rec_id), None) if book else None
        return book.to_dict(rec) if rec else None

    def ideas_text(self) -> str:
        book = self.core.recommender
        return book.text() if book else "Recommendations are turned off (recommendations.enabled: false)."

    def log_text(self, limit: int = 10) -> str:
        events = list(self.core.events)[:limit]
        if not events:
            return "No activity yet."
        return "Recent activity:\n" + "\n".join(f"{e['ts'][11:19]}  {e['message']}" for e in events)

    # ------------------------------------------------------------------ knowledge base

    def knowledge_text(self) -> str:
        return self.core.knowledge_text()

    def insights(self, scope: str = "recent") -> dict:
        """The "What the bot learned" report for the dashboard's Knowledge tab (``scope`` recent or longrun)."""
        return {"report": self.core.insights(longrun=scope == "longrun"), "scope": scope, "hands": self.core.hand_review()}

    def brief(self) -> dict:
        """What the bot knows, in plain sentences, ending with its next-trade forecast (the Knowledge tab)."""
        return {"brief": self.core.brief()}

    def brief_text(self) -> str:
        from topstep_bot.briefing import brief_text

        return brief_text(self.core.brief())

    def next_text(self) -> str:
        from topstep_bot.forecast import forecast_text

        f = self.core.forecast()
        return forecast_text(f) if f else "The next-trade forecast isn't available right now - see the logs."

    async def learn(self, source: str) -> str:
        """Backfill the long-run memory and replay all of it through every strategy (the bot keeps trading meanwhile)."""
        if self._learn is None:
            raise RuntimeError("Learning from long-run history is only available while the bot is connected to TopstepX")
        return await self._learn(source)

    def insights_csv(self, scope: str = "recent") -> dict:
        """Every observation as CSV (the Knowledge tab's Download button), recent or from the long-run memory."""
        from topstep_bot.insights import csv_text

        if scope == "longrun":
            if self.core.memory is None:
                raise RuntimeError("The long-run memory is turned off (knowledge.deep_learning: false)")
            kb = self.core.memory.knowledge
        else:
            kb = self.core.knowledge
        if kb is None:
            raise RuntimeError("The knowledge base is turned off (knowledge.enabled: false)")
        name = kb.path.stem if kb.path else "knowledge"
        return {"csv": csv_text(kb), "filename": f"{name}.csv"}

    # ------------------------------------------------------------------ the market and funded-account goals

    def market_text(self) -> str:
        """Live levels, today's reports and headlines for what the bot trades (Telegram /market)."""
        if self.core.intel is None:
            return "No market picture yet - it builds from the bot's first bars."
        return self.core.intel.brief_text(self.core.clock())

    def funded_text(self) -> str:
        from topstep_bot.funded import status_text

        return status_text(self.core.funded_status())

    def rules_text(self) -> str:
        from topstep_bot.risk.summary import rules_text

        return rules_text(self.core.cfg)

    async def train(self, source: str) -> str:
        """Rebuild the knowledge base from recent history (the bot keeps trading meanwhile)."""
        if self._retrain is None:
            raise RuntimeError("Training is only available while the bot is connected to TopstepX")
        return await self._retrain(source)

    # ------------------------------------------------------------------ the central intelligence

    def facts(self, name: str, args: dict | None) -> dict:
        """One read-only lookup for the central intelligence (brain/), with the live picture."""
        if self._facts is None or self._facts[0] is not self.core:  # rebuilt when the market shown changes
            from topstep_bot.brain.facts import Facts

            self._facts = (self.core, Facts(self.core.cfg, core=self.core, actions=self))
        return {"data": self._facts[1].call(name, args if isinstance(args, dict) else {})}

    # ------------------------------------------------------------------ the AI trading assistant (assistant.py)

    def assistant(self, payload: dict, source: str) -> str:
        """Switch its two parts on or off for this account (``manage`` / ``trade``: true, false or left out)."""
        a = self.core.assistant
        if a is None:
            return "The AI assistant isn't running here (it runs with the live or paper bot)."

        def flag(key: str) -> bool | None:
            v = payload.get(key)
            if v is None or v == "":
                return None
            if isinstance(v, str):
                return v.strip().lower() in ("1", "true", "on", "yes")
            return bool(v)

        return a.desk.switch(flag("manage"), flag("trade"), source)

    def assistant_text(self) -> str:
        a = self.core.assistant
        if a is None:
            return "The AI assistant isn't running here (it runs with the live or paper bot)."
        return a.text()

    def assistant_retry(self, payload: dict) -> str:
        """The controller saved or removed the Claude key (``key``) or changed the monthly budget
        (``monthly_budget``): use it at once and try again at the next chance."""
        if "key" in payload:
            from topstep_bot import claude
            from topstep_bot.logging_setup import add_secret

            key = str(payload.get("key") or "").strip()
            if key:
                os.environ[claude.KEY_ENV] = key
                add_secret(key)
            else:
                os.environ.pop(claude.KEY_ENV, None)
        desks = {id(c.assistant.desk): c.assistant.desk for c in self.all_cores() if c.assistant is not None}
        for desk in desks.values():
            if payload.get("monthly_budget"):
                desk.cfg.coach.monthly_budget_usd = float(payload["monthly_budget"])
            desk.key_changed()
        return "The AI assistant will try again at the next bar." if desks else "The AI assistant isn't running here."

    def assistant_decisions(self, limit: int = 30) -> dict:
        """Today's decisions on this market in full (with the situation it saw)."""
        core = self.core
        if core.journal is None:
            return {"items": []}
        day = core.schedule.trading_day(core.clock()).isoformat()
        rows = [r for r in core.journal.assistant_decisions(limit=200, day=day) if r.get("symbol") == core.contract.name]
        return {"items": rows[:max(1, min(100, limit))]}

    # ------------------------------------------------------------------ the day plan and the trend

    def plan_text(self) -> str:
        from topstep_bot.day_plan import plan_text

        plan = self.core.plan_view()
        if plan is None:
            return "The day plan runs with the live or paper bot."
        return plan_text(plan)

    def trend_text(self) -> str:
        from topstep_bot.trend import view_text

        return view_text(self.core.trend_view(), self.core.contract.name)

    def set_goal(self, goal: float | None, source: str) -> str:
        """Today's goal for this account (None = no goal today): saved, and used at once (day_plan.py)."""
        from topstep_bot.day_plan import goal_day, save_goal

        cfg = self.core.cfg
        day = goal_day(cfg, self.core.clock())
        save_goal(cfg, day, goal, source, self.core.clock())
        self.goal_reload()
        self.core.event("info", f"Goal for {day} set from {source}: {fmt.money(goal) if goal is not None else 'no goal'}")
        return self.plan_text()

    def goal_reload(self) -> str:
        """The controller saved a goal: read it now instead of within the next few seconds."""
        desk = self.desk
        if desk is not None and desk.planner is not None:
            desk.planner.reload(force=True)
            desk._plan_tick()
            return "Goal read."
        return "No day plan here."

    # ------------------------------------------------------------------ markets (several at once)

    def _desk(self) -> LiveRunner:
        if self.desk is None:
            raise RuntimeError("Markets can only be changed while the bot is running")
        return self.desk

    async def market_action(self, name: str, payload: dict, source: str) -> dict | str:
        desk = self._desk()
        symbol = str(payload.get("symbol") or "").strip().upper()
        if name == "markets":
            return desk.markets_view()
        if name == "add_market":
            return await desk.add_market(symbol, source)
        if name == "remove_market":
            return await desk.remove_market(symbol, source, flatten=bool(payload.get("flatten")))
        if name == "switch_market":
            return await desk.switch_market(payload.get("from") or None, symbol, source)
        if name == "grow_network":
            return await desk.grow_network(source)
        return desk.set_focus(symbol)

    def markets_text(self) -> str:
        if self.desk is None:
            return f"Trading {self.core.contract.name}."
        view = self.desk.markets_view()
        lines = [f"Trading {len(view['lanes'])} market(s) (up to {view['max']} at once):"]
        for m in view["lanes"]:
            t = m.get("trade")
            state = f"{t['side']} {t['size']} @ {t['entry'] or 'pending'}" if t else "flat"
            extra = f" - {m['closing']}" if m.get("closing") else ""
            lines.append(f"• {m['symbol']} ({m.get('contract', '')}): {state}, {m.get('trades_today', 0)} trade(s) "
                         f"today{extra}")
        e = view.get("exposure")
        if e:
            lines.append(f"Open P&L {fmt.money(e['open_pnl'])}, at risk if every stop fills late "
                         f"{fmt.money(e['at_risk'])}, size {e['minis']:g} mini units")
        return "\n".join(lines)

    # ------------------------------------------------------------------ single entry point

    async def handle(self, name: str, payload: dict) -> dict | str:
        """Run action ``name`` (the bot's local API calls this). Raises ValueError for bad input.

        ``market`` in the payload runs it on that market (several markets at once); default: the one shown."""
        source = str(payload.get("source") or "dashboard")
        if name in MARKET_ACTIONS:
            return await self.market_action(name, payload, source)
        market = payload.get("market")
        if market and self.desk is not None and name not in ("pause", "resume", "flatten", "stop"):
            token = _PINNED.set(self.desk.lane_for(str(market)).core)
            try:
                return await self._handle(name, payload, source)
            finally:
                _PINNED.reset(token)
        return await self._handle(name, payload, source)

    async def _handle(self, name: str, payload: dict, source: str) -> dict | str:
        simple = {"pause": self.pause, "resume": self.resume, "flatten": self.flatten, "stop": self.stop}
        if name in simple:
            return simple[name](source)
        if name == "train":
            return await self.train(source)
        if name == "learn":
            return await self.learn(source)
        if name == "preview_setting":
            return self.preview_setting(payload.get("key", ""), payload.get("value"))
        if name == "set_setting":
            return self.change_setting(payload.get("key", ""), payload.get("value"), source)
        if name == "reset_settings":
            return self.reset_settings(source)
        if name == "trade_ticket":
            return {"ticket": self.trade_ticket(payload)}
        if name == "manual_trade":
            return await self.manual_trade(payload, source)
        if name == "close_trade":
            return await self.close_trade(source)
        if name == "stop_breakeven":
            return await self.stop_breakeven(source)
        if name == "take_idea":
            size = payload.get("size")
            return await self.take_idea(str(payload.get("id", "")), source, int(size) if size else None)
        if name == "assistant":
            return self.assistant(payload, source)
        if name == "assistant_retry":
            return self.assistant_retry(payload)
        if name == "assistant_decisions":
            return self.assistant_decisions(int(payload.get("limit") or 30))
        if name == "goal_reload":
            return self.goal_reload()
        if name == "plan":
            return {"plan": self.core.plan_view()}
        texts = {"status_text": self.status_text, "ideas_text": self.ideas_text, "trades_text": self.trades_text,
                 "log_text": self.log_text, "settings_text": self.settings_text, "knowledge_text": self.knowledge_text,
                 "brief_text": self.brief_text, "next_text": self.next_text, "market_text": self.market_text,
                 "funded_text": self.funded_text, "rules_text": self.rules_text, "markets_text": self.markets_text,
                 "assistant_text": self.assistant_text, "plan_text": self.plan_text, "trend_text": self.trend_text}
        if name in texts:
            return {"text": texts[name]()}
        if name == "insights":
            return self.insights(str(payload.get("scope") or "recent"))
        if name == "brief":
            return self.brief()
        if name == "insights_csv":
            return self.insights_csv(str(payload.get("scope") or "recent"))
        if name == "facts":
            return self.facts(str(payload.get("name", "")), payload.get("args"))
        if name == "open_ideas":
            return {"items": self.open_ideas()}
        if name == "find_idea":
            return {"item": self.find_idea(str(payload.get("id", "")))}
        raise ValueError(f"unknown action '{name}'")
