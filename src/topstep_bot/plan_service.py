"""The day plan in the controller: the morning goal question, goals from the dashboard and Telegram (day_plan.py).

Goals are saved per account in ``day_goals.json`` (the account's data folder), so they hold whether or not
the account's bot is running; a running bot reads a new goal at once. Setting a goal needs no confirmation
tap: a goal can only end a trading day sooner, never make a trade bigger.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from topstep_bot.day_plan import entry_for, goal_day, question, save_goal

if TYPE_CHECKING:
    from topstep_bot.controller import BotProcess, Controller

log = logging.getLogger("topstep_bot.plan_service")
UTC = timezone.utc
CHECK_SECONDS = 30
STATE_KEY = "goal_asked"  # controller.json: the trading day the morning question was last sent for


class PlanService:
    def __init__(self, ctl: Controller):
        self.ctl = ctl
        self.waiting: tuple[str, datetime] | None = None  # (day, until): typed goals are taken as answers until then

    # ------------------------------------------------------------------ goals

    def _bots(self, account: str | None) -> list[BotProcess]:
        if account in (None, "", "*"):
            return list(self.ctl.bots.values())
        return self.ctl.pick(account)

    async def _plan(self, bot: BotProcess) -> dict | None:
        if not (bot.running and bot.state == "running"):
            return None
        try:
            return (await bot.action("plan")).get("plan")
        except (RuntimeError, ValueError, KeyError):
            return None

    async def entries(self, day: str | None = None) -> list[dict[str, Any]]:
        out = []
        for bot in self.ctl.bots.values():
            cfg = self.ctl.account_cfg(bot.account)
            d = day or goal_day(cfg)
            out.append(entry_for(cfg, bot.account, await self._plan(bot), d))
        return out

    async def set_goal(self, body: dict, source: str) -> dict[str, Any]:
        """Save a goal (``goal``: dollars, null / "none" for no goal, "suggested" for the bot's suggestion) for one
        account, or every account with ``account`` "all" / "*" (each gets its own suggestion)."""
        account = body.get("account")
        if account in ("all", "*"):
            account = "*"
        elif not self.ctl.multi:
            account = None
        bots = self._bots(account)
        raw = body.get("goal")
        lines, plans = [], {}
        for bot in bots:
            cfg = self.ctl.account_cfg(bot.account)
            day = goal_day(cfg)
            if raw == "suggested":
                e = entry_for(cfg, bot.account, await self._plan(bot), day)
                goal = e.get("suggested")
            elif raw in (None, "", "none", "off"):
                goal = None
            else:
                try:
                    goal = float(str(raw).replace("$", "").replace(",", ""))
                except ValueError:
                    raise ValueError("The goal must be an amount of dollars, e.g. 400 (or none)") from None
            save_goal(cfg, day, goal, source)
            log.info("Goal for %s on %s set from %s: %s", bot.account, day, source, goal)
            plan = None
            if bot.running and bot.state == "running":
                try:
                    await bot.action("goal_reload")
                except (RuntimeError, ValueError) as exc:
                    log.info("Could not tell %s about the goal at once (it reads it within seconds): %s", bot.account, exc)
                plan = await self._plan(bot)
            plans[bot.account] = plan
            who = f"{bot.account}: " if self.ctl.multi else ""
            if plan is not None and plan.get("day") == day:
                lines.append(f"🎯 {who}{plan['headline']}")
            else:
                g = f"${goal:,.0f}" if goal is not None else "no goal"
                later = " The route is planned that morning." if plan is not None else \
                    " The route is planned once the bot is running."
                lines.append(f"🎯 {who}goal for {day}: {g}.{later}")
        if self.waiting and all(e["kind"] in ("set", "off") for e in await self.entries(self.waiting[0])):
            self.waiting = None
        return {"message": "\n".join(lines), "plans": plans}

    async def goals_text(self) -> str:
        """Every account's goal for today, in a few lines."""
        lines = []
        for e in await self.entries():
            who = f"{e['account']}: " if self.ctl.multi else ""
            have = f"${e['goal']:,.0f} ({e['text']})" if e["goal"] is not None else e["text"]
            lines.append(f"🎯 {who}{have}" + (f" - {e['headline']}" if e.get("headline") else ""))
        return "\n".join(lines) or "No accounts."

    async def question(self) -> tuple[str, dict]:
        cfg = self.ctl.cfg
        entries = await self.entries()
        ask_by = cfg.session.trade_start.strftime("%H:%M")
        return question(entries, multi=self.ctl.multi, ask_by=ask_by, unanswered=cfg.day_plan.unanswered)

    # ------------------------------------------------------------------ the morning question

    def _due(self, now: datetime) -> str | None:
        """The trading day to ask about now, or None (not time yet, asked already, not a trading day)."""
        from topstep_bot.risk.topstep import FLAT_BY
        from topstep_bot.sessions import SessionSchedule

        cfg = self.ctl.cfg
        if not any(self.ctl.account_cfg(b).day_plan.ask for b in self.ctl.bots):
            return None
        schedule = SessionSchedule(cfg.session)
        local = schedule.local(now)
        day = schedule.trading_day(now)
        if local.date() != day or not schedule.is_trade_day(day):
            return None  # evenings belong to the next day: ask on its morning
        if not cfg.day_plan.ask_at <= local.time() < FLAT_BY:
            return None
        from topstep_bot.controller import load_state

        if load_state(cfg).get(STATE_KEY) == day.isoformat():
            return None
        return day.isoformat()

    async def ask_if_due(self, now: datetime | None = None) -> bool:
        """Send the morning question once per trading day (only for accounts without a goal yet)."""
        tg = self.ctl.telegram
        if tg is None:
            return False
        now = now or datetime.now(UTC)
        day = self._due(now)
        if day is None:
            return False
        from topstep_bot.controller import load_state, save_state

        state = load_state(self.ctl.cfg)
        state[STATE_KEY] = day
        save_state(self.ctl.cfg, state)  # once, even if sending fails: a question an hour late helps no one
        entries = [e for e in await self.entries(day)
                   if self.ctl.account_cfg(e["account"]).day_plan.ask]
        if not entries or all(e["kind"] in ("set", "off") for e in entries):
            return False
        cfg = self.ctl.cfg
        text, keyboard = question(entries, multi=self.ctl.multi, ask_by=cfg.session.trade_start.strftime("%H:%M"),
                                  unanswered=cfg.day_plan.unanswered)
        from topstep_bot.sessions import SessionSchedule

        schedule = SessionSchedule(cfg.session)
        until = schedule.at(schedule.trading_day(now), cfg.session.last_entry)
        self.waiting = (day, until)
        await tg.send(text, keyboard)
        log.info("Asked for today's goals on Telegram (%s)", day)
        return True

    def awaiting(self, now: datetime | None = None) -> bool:
        """Typed goals count as answers (the morning question is open)."""
        if self.waiting is None:
            return False
        if (now or datetime.now(UTC)) >= self.waiting[1]:
            self.waiting = None
            return False
        return True

    async def run(self) -> None:
        await asyncio.sleep(20)  # let Telegram and the bots start first
        while True:
            try:
                await self.ask_if_due()
            except Exception:  # noqa: BLE001 - the bot plans with the last goal or its suggestion
                log.exception("Could not ask for today's goals")
            await asyncio.sleep(CHECK_SECONDS)

    def summary(self) -> dict[str, Any]:
        return {"waiting": self.waiting[0] if self.waiting else None}

