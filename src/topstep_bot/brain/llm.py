"""Optional AI answers: a Claude model reads the bot's records through the same lookups (facts.py).

Off unless you turn it on (brain.ai) and save a Claude API key (Setup tab or Ask > Deep review: the coach
uses the same key). The model gets no trading tools - only the read-only lookups - and is told to
answer from what they return and nothing else. Each question costs a little on your Claude API
account and counts toward the coach's monthly budget (coach.monthly_budget_usd). If the AI can't be
reached, the bot answers by itself (intents.py) and says so.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from topstep_bot import claude
from topstep_bot.brain.facts import TOOLS

if TYPE_CHECKING:
    from topstep_bot.brain.intents import Fetch

log = logging.getLogger("topstep_bot.brain")

MAX_TOKENS = 4000
MAX_RESULT_CHARS = 24_000  # one lookup's result as the model sees it
FALLBACK_BETA = claude.FALLBACK_BETA

SYSTEM = """You are the central intelligence of a futures trading bot that trades a Topstep account for its owner. \
The owner asks you about the bot: why it took or skipped a trade, what it has learned, how strategies are doing, \
what it will do next.

How to answer:
- Use the lookups for every fact. Never invent a number, a trade, a time or a reason: if the lookups don't have it, \
say you don't have that information.
- Explain the bot's reasoning the way it was recorded: the signal, each strategy's verdict, the evidence (recent \
record, the after-cost estimate, long-run memory, similar past situations), the market at the time, the risk limits, \
the result and the lesson. "R" means multiples of the money a trade risked (+1R = won what it risked).
- Be honest about uncertainty: small samples prove little, similar-situation matches only explain, and no strategy \
or bot guarantees profit or passing the Combine. Never promise results.
- You can't place, change or close trades and can't change settings. If asked, say so and point to the dashboard.
- Answer in plain text for a phone screen: short paragraphs or simple bullet lines, no tables, no markdown headings. \
Lead with the answer. Times are Chicago time.

{context}"""


def tool_specs() -> list[dict[str, Any]]:
    return [{"name": name, "description": desc,
             "input_schema": {"type": "object", "properties": props, "additionalProperties": False}}
            for name, (desc, props) in TOOLS.items()]


def uses_fallbacks(model: str) -> bool:
    return bool(claude.fallback_args(model))


class AIUnavailable(Exception):
    """The AI couldn't answer (no key, budget, network, refused, ...). The message says why, in plain words."""


async def ask(question: str, history: list[dict[str, Any]], fetch: Fetch, *, api_key: str, model: str,
              effort: str = "medium", max_tool_calls: int = 8, context: str = "",
              on_usage: Callable[[str, Any], None] | None = None) -> str:
    """The model's answer to ``question`` after looking up what it needs (at most ``max_tool_calls`` lookups).

    ``on_usage(model, usage)`` is called after every response, so each one is priced and logged."""
    try:
        cl = claude.client(api_key, timeout=90.0, retries=2)
    except claude.ClaudeError as exc:
        raise AIUnavailable(str(exc)) from None
    import anthropic

    messages: list[dict[str, Any]] = [*history, {"role": "user", "content": question}]
    tools = tool_specs()
    state: dict[str, Any] = {}
    calls = 0
    try:
        while True:
            last_round = calls >= max_tool_calls
            request: dict[str, Any] = {"model": model, "max_tokens": MAX_TOKENS, "system": SYSTEM.format(context=context),
                                       "tools": tools, "messages": messages, "output_config": {"effort": effort},
                                       "cache_control": {"type": "ephemeral"}}
            if last_round:
                request["tool_choice"] = {"type": "none"}
            response = await claude.create(cl, request, model, state)
            if on_usage is not None:
                try:
                    on_usage(getattr(response, "model", None) or model, getattr(response, "usage", None))
                except Exception:  # noqa: BLE001 - logging the cost must never lose the answer
                    log.exception("Could not log the AI answer's usage")
            if response.stop_reason == "refusal":
                raise AIUnavailable("the AI declined to answer that")
            uses = [b for b in response.content if b.type == "tool_use"]
            if response.stop_reason != "tool_use" or not uses:
                text = "".join(b.text for b in response.content if b.type == "text").strip()
                if not text:
                    raise AIUnavailable("the AI returned an empty answer")
                return text
            messages.append({"role": "assistant", "content": response.content})
            results = []
            for block in uses:
                calls += 1
                data = await fetch(block.name, dict(block.input or {}))
                results.append({"type": "tool_result", "tool_use_id": block.id,
                                "content": json.dumps(data, default=str)[:MAX_RESULT_CHARS]})
            messages.append({"role": "user", "content": results})
    except anthropic.APIError as exc:
        raise AIUnavailable(claude.explain(exc, model)) from exc
    finally:
        await cl.close()


async def check_key(api_key: str, model: str) -> str | None:
    """None when ``api_key`` can use ``model``, else what is wrong (Setup tab). Costs nothing: it reads the model's details."""
    try:
        note = await claude.check_key(api_key, model)
    except claude.ClaudeError as exc:
        return str(exc)
    return None if note.startswith("Key works") else note
