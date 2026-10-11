"""Optional AI answers: a Claude model reads the bot's records through the same lookups (facts.py).

Off unless you turn it on (brain.ai) and save an Anthropic API key on the Setup tab. The model
gets no trading tools - only the read-only lookups - and is told to answer from what they return
and nothing else. Each question costs a little on your Anthropic account. If the AI can't be
reached, the bot answers by itself (intents.py) and says so.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from topstep_bot.brain.facts import TOOLS

if TYPE_CHECKING:
    from topstep_bot.brain.intents import Fetch

log = logging.getLogger("topstep_bot.brain")

MAX_TOKENS = 4000
MAX_RESULT_CHARS = 24_000  # one lookup's result as the model sees it
FALLBACK_BETA = "server-side-fallback-2026-07-01"

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
    return model.startswith("claude-opus-5")


class AIUnavailable(Exception):
    """The AI couldn't answer (no key, network, refused, ...). The message says why, in plain words."""


async def ask(question: str, history: list[dict[str, Any]], fetch: Fetch, *, api_key: str, model: str,
              effort: str = "medium", max_tool_calls: int = 8, context: str = "") -> str:
    """The model's answer to ``question`` after looking up what it needs (at most ``max_tool_calls`` lookups)."""
    try:
        import anthropic
    except ImportError as exc:
        raise AIUnavailable("the 'anthropic' package isn't installed (run the update, or pip install anthropic)") from exc

    client = anthropic.AsyncAnthropic(api_key=api_key, max_retries=2, timeout=90.0)
    messages: list[dict[str, Any]] = [*history, {"role": "user", "content": question}]
    tools = tool_specs()
    fallbacks = uses_fallbacks(model)
    calls = 0
    try:
        while True:
            last_round = calls >= max_tool_calls
            kwargs: dict[str, Any] = {"model": model, "max_tokens": MAX_TOKENS, "system": SYSTEM.format(context=context),
                                      "tools": tools, "messages": messages, "output_config": {"effort": effort}}
            if last_round:
                kwargs["tool_choice"] = {"type": "none"}
            response = await _create(client, kwargs, fallbacks)
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
    except anthropic.AuthenticationError as exc:
        raise AIUnavailable("Anthropic rejected the API key - check it on the Setup tab") from exc
    except anthropic.PermissionDeniedError as exc:
        raise AIUnavailable("the API key isn't allowed to use this model") from exc
    except anthropic.NotFoundError as exc:
        raise AIUnavailable(f"model '{model}' wasn't found - check brain.model") from exc
    except anthropic.RateLimitError as exc:
        raise AIUnavailable("Anthropic's rate limit was reached - try again in a minute") from exc
    except anthropic.APIConnectionError as exc:
        raise AIUnavailable("Anthropic couldn't be reached (internet connection?)") from exc
    except anthropic.APIStatusError as exc:
        raise AIUnavailable(f"Anthropic returned an error ({exc.status_code})") from exc
    finally:
        await client.close()


async def _create(client: Any, kwargs: dict[str, Any], fallbacks: bool) -> Any:
    """One request; on Claude Opus 5 models a declined request is retried on another model by the server."""
    import anthropic

    if not fallbacks:
        return await client.messages.create(**kwargs)
    try:
        return await client.beta.messages.create(**kwargs, betas=[FALLBACK_BETA], fallbacks="default")
    except anthropic.BadRequestError:
        log.info("Server-side fallbacks were refused; asking without them")
        return await client.messages.create(**kwargs)


async def check_key(api_key: str, model: str) -> str | None:
    """None when ``api_key`` can use ``model``, else what is wrong (Setup tab). Costs nothing: it reads the model's details."""
    try:
        import anthropic
    except ImportError:
        return "the 'anthropic' package isn't installed (run the update, or pip install anthropic)"
    client = anthropic.AsyncAnthropic(api_key=api_key, max_retries=1, timeout=20.0)
    try:
        await client.models.retrieve(model)
    except anthropic.AuthenticationError:
        return "Anthropic rejected this API key"
    except anthropic.NotFoundError:
        return f"this key can't use model '{model}'"
    except anthropic.APIConnectionError:
        return "Anthropic couldn't be reached (internet connection?)"
    except anthropic.APIStatusError as exc:
        return f"Anthropic returned an error ({exc.status_code})"
    finally:
        await client.close()
    return None
