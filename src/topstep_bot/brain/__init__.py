"""The bot's central intelligence: ask it about its trades and knowledge from the dashboard or Telegram.

* facts.py - read-only lookups over the bot's own records (the only source of every answer);
* explain.py - a decision record (reasoning.py) in plain sentences;
* intents.py - the built-in answers, no AI needed;
* llm.py - optional AI answers from a Claude model that reads the same lookups.

It lives in the dashboard process, so it answers while the bot is stopped too (from the files on
disk); while the bot runs, its lookups go to the bot for the live picture. It can't trade or change
settings.
"""

from __future__ import annotations

import logging
import os
from collections import deque
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from topstep_bot.brain import intents, llm
from topstep_bot.config import CLAUDE_KEY_ENV

if TYPE_CHECKING:
    from topstep_bot.brain.intents import Fetch
    from topstep_bot.config import BotConfig

log = logging.getLogger("topstep_bot.brain")

MAX_QUESTION = 1000
HISTORY_TURNS = 6  # earlier questions and answers the AI sees (per channel: dashboard, Telegram)


class Brain:
    def __init__(self, cfg: BotConfig):
        self.cfg = cfg
        self.history: dict[str, deque[tuple[str, str]]] = {}

    def _spending(self):
        """The coach's usage log: AI answers are priced and counted against the same monthly budget."""
        from topstep_bot.coach import Coach

        return Coach(self.cfg)

    @property
    def api_key(self) -> str | None:
        return os.environ.get(CLAUDE_KEY_ENV) or None  # the Setup and AI Coach tabs update it without a restart

    @property
    def ai_ready(self) -> bool:
        return self.cfg.brain.ai and bool(self.api_key)

    def mode_text(self) -> str:
        if self.ai_ready:
            return f"AI answers ({self.cfg.brain.model})"
        if self.cfg.brain.ai:
            return "built-in answers (AI is on, but no Claude API key is saved on the Setup or AI Coach tab)"
        return "built-in answers"

    async def ask(self, question: str, fetch: Fetch, channel: str = "dashboard", context: str = "") -> dict[str, Any]:
        """{"answer", "by": "ai" | "built-in", "note"} for one question. Never raises for a bad question."""
        question = " ".join(str(question or "").split())[:MAX_QUESTION]
        if not question:
            return {"answer": intents.HELP, "by": "built-in", "note": None}
        turns = self.history.setdefault(channel, deque(maxlen=HISTORY_TURNS))
        note = None
        if self.ai_ready:
            history: list[dict[str, Any]] = []
            for q, a in turns:
                history += [{"role": "user", "content": q}, {"role": "assistant", "content": a}]
            b = self.cfg.brain
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            ctx = (f"Today (UTC) is {today}. Account: {self.cfg.active_account or 'main'}, {self.cfg.account.plan} "
                   f"{self.cfg.account.stage}, trading {self.cfg.instrument.symbol} on {self.cfg.instrument.timeframe_minutes}-"
                   f"minute bars with the '{self.cfg.strategy.name}' strategy. {context}").strip()
            spending = self._spending()
            try:
                if spending.spent() >= self.cfg.coach.monthly_budget_usd:
                    raise llm.AIUnavailable(f"this month's Claude budget (${self.cfg.coach.monthly_budget_usd:.2f}, shared "
                                            "with the AI coach) is used up - raise it on the AI Coach tab")
                answer = await llm.ask(question, history, fetch, api_key=self.api_key or "", model=b.model, effort=b.effort,
                                       max_tool_calls=b.max_tool_calls, context=ctx,
                                       on_usage=lambda m, u: spending.log_usage(m, u, source="ask"))
                turns.append((question, answer))
                return {"answer": answer, "by": "ai", "note": None}
            except llm.AIUnavailable as exc:
                note = f"AI answers unavailable: {exc}. Here is my built-in answer."
                log.warning("Central intelligence: %s", exc)
            except Exception as exc:  # noqa: BLE001 - always answer something
                note = "AI answers failed unexpectedly (see logs/errors.log). Here is my built-in answer."
                log.exception("Central intelligence: AI answer failed: %s", exc)
        answer = await intents.answer(question, fetch)
        return {"answer": answer, "by": "built-in", "note": note}
