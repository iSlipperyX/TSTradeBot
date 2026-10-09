"""Remote-control actions for the running bot (reached through its local API by the dashboard
and Telegram, which live in the separate controller process).

Every action is logged in the activity feed with where it came from, and none of them can
loosen a risk limit: the strongest thing a remote command can do is let the bot continue
trading within the limits already in config.yaml.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from topstep_bot.engine import TradingCore
from topstep_bot.live import Controls


def _money(v: float | None) -> str:
    if v is None:
        return "-"
    return f"-${abs(v):,.2f}" if v < 0 else f"${v:,.2f}"


class BotActions:
    def __init__(self, core: TradingCore, controls: Controls, retrain: Callable[[str], Awaitable[str]] | None = None):
        self.core = core
        self.controls = controls
        self._retrain = retrain  # LiveRunner.retrain: downloads history and rebuilds the knowledge base

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
            f"Balance: {_money(s['balance'])}",
            f"Today: {_money(r['day_pnl'])}  (open {_money(s['open_pnl'])})",
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
            f"MLL room: {_money(r['mll_room'])} (floor {_money(r['mll_floor'])})",
            f"Daily loss used: {_money(max(0.0, -r['day_pnl']))} of {_money(r['daily_loss_limit'])}",
            f"Trades today: {r['trades_today']} of {r['max_trades']}",
        ]
        if s["profit_target"]:
            lines.append(f"Combine progress: {_money(s['total_profit'])} of {_money(s['profit_target'])}")
        lines.append(f"Time: {s['time']}")
        return "\n".join(lines)

    def trades_text(self, limit: int = 5) -> str:
        trades = list(reversed(self.core.closed_trades[-limit:]))
        if not trades and self.core.journal:
            rows = self.core.journal.trades(limit=limit, account=self.core.account_label)
            if not rows:
                return "No closed trades yet."
            return "Recent trades:\n" + "\n".join(
                f"{(r['exit_time'] or '')[:16].replace('T', ' ')}  {r['side']} {r['size']}  "
                f"{_money(r['net_pnl'] or 0)}  ({r['exit_reason']})"
                for r in rows
            )
        if not trades:
            return "No closed trades yet."
        return "Recent trades:\n" + "\n".join(
            f"{self.core.schedule.local(t.closed_at).strftime('%m-%d %H:%M')}  {t.side.label} {t.filled_size}  "
            f"{_money(t.net_pnl)}  ({t.exit_reason})"
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
        return self._remote().apply(key, value, source)

    def reset_settings(self, source: str) -> str:
        return self._remote().reset(source)

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

    def insights(self) -> dict:
        """The "What the bot learned" report for the dashboard's Knowledge tab."""
        return {"report": self.core.insights()}

    def insights_csv(self) -> dict:
        """Every observation as CSV (the Knowledge tab's Download button)."""
        from topstep_bot.insights import csv_text

        kb = self.core.knowledge
        if kb is None:
            raise RuntimeError("The knowledge base is turned off (knowledge.enabled: false)")
        name = kb.path.stem if kb.path else "knowledge"
        return {"csv": csv_text(kb), "filename": f"{name}.csv"}

    async def train(self, source: str) -> str:
        """Rebuild the knowledge base from recent history (the bot keeps trading meanwhile)."""
        if self._retrain is None:
            raise RuntimeError("Training is only available while the bot is connected to TopstepX")
        return await self._retrain(source)

    # ------------------------------------------------------------------ single entry point

    async def handle(self, name: str, payload: dict) -> dict | str:
        """Run action ``name`` (the bot's local API calls this). Raises ValueError for bad input."""
        source = str(payload.get("source") or "dashboard")
        simple = {"pause": self.pause, "resume": self.resume, "flatten": self.flatten, "stop": self.stop}
        if name in simple:
            return simple[name](source)
        if name == "train":
            return await self.train(source)
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
        texts = {"status_text": self.status_text, "ideas_text": self.ideas_text, "trades_text": self.trades_text,
                 "log_text": self.log_text, "settings_text": self.settings_text, "knowledge_text": self.knowledge_text}
        if name in texts:
            return {"text": texts[name]()}
        if name == "insights":
            return self.insights()
        if name == "insights_csv":
            return self.insights_csv()
        if name == "open_ideas":
            return {"items": self.open_ideas()}
        if name == "find_idea":
            return {"item": self.find_idea(str(payload.get("id", "")))}
        raise ValueError(f"unknown action '{name}'")
