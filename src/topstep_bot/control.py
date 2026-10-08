"""Remote-control actions shared by the web dashboard and the Telegram bot.

Every action is logged in the activity feed with where it came from, and none of them can
loosen a risk limit: the strongest thing a remote command can do is let the bot continue
trading within the limits already in config.yaml.
"""

from __future__ import annotations

from topstep_bot.engine import TradingCore
from topstep_bot.live import Controls


def _money(v: float | None) -> str:
    if v is None:
        return "-"
    return f"-${abs(v):,.2f}" if v < 0 else f"${v:,.2f}"


class BotActions:
    def __init__(self, core: TradingCore, controls: Controls):
        self.core = core
        self.controls = controls

    def pause(self, source: str) -> str:
        if self.core.risk.paused:
            return "New trades are already paused."
        self.core.risk.paused = True
        self.core.event("warning", f"New trades paused from {source}")
        return "Paused: no new trades. Any open trade keeps its stop and target."

    def resume(self, source: str) -> str:
        if self.core.halted:
            return "The bot is halted (after a flatten). Restart it on your PC to trade again."
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
        return "Stopping the bot (it flattens first). It can only be restarted from your PC."

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
