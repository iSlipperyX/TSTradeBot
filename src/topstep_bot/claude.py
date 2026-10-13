"""Claude, in one place: the key, the models and their prices, the spending ledger and plain-words errors.

The bot talks to Claude (Anthropic's API) for two things, and both go through this module:

  * the AI coach (coach.py): long review sessions with tools and simulations;
  * AI answers on the Ask tab and in Telegram (brain/llm.py): short look-up-and-answer turns.

They share one API key (``COACH_ANTHROPIC_API_KEY`` in .env, saved from the Setup tab or Ask > Deep review),
one model catalogue with prices, one spending log (``data/coach/usage.jsonl``) and one monthly
budget (``coach.monthly_budget_usd``). Saving or removing the key anywhere updates every user of it
at once, masks it in the logs, and never writes it anywhere but .env on this PC.

Nothing here can trade or change a setting: it only sends the bot's own records to Claude and
brings back text.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from topstep_bot.config import CLAUDE_KEY_ENV

if TYPE_CHECKING:
    from topstep_bot.config import Secrets

log = logging.getLogger("topstep_bot.claude")
UTC = timezone.utc

KEY_ENV = CLAUDE_KEY_ENV
KEY_PREFIX = "sk-ant-"
CONSOLE_URL = "https://platform.claude.com"
BILLING_URL = "https://platform.claude.com/settings/billing"
KEYS_URL = "https://platform.claude.com/settings/keys"
# Refusal fallbacks: when a model's safeguards decline a request, the API re-runs it on another model
# inside the same call ("default" routes by refusal category, so no model list to maintain).
FALLBACK_BETA = "server-side-fallback-2026-07-01"
INSTALL_HINT = "The Claude API package isn't installed. Run the update, or: .venv\\Scripts\\python -m pip install anthropic"


@dataclass(frozen=True)
class Model:
    id: str
    label: str
    prices: tuple[float, float, float, float]  # $ per million tokens: input, output, cache read, cache write (5 min)
    fallbacks: bool  # supports server-side refusal fallbacks


MODELS: dict[str, Model] = {m.id: m for m in (
    Model("claude-opus-5-5", "Claude Opus 5.5 - recommended: thorough and careful", (4.00, 20.00, 0.20, 5.00), True),
    Model("claude-sonnet-5-5", "Claude Sonnet 5.5 - about half the price of Opus", (2.00, 10.00, 0.20, 2.50), True),
    Model("claude-haiku-5-5", "Claude Haiku 5.5 - cheapest, for quick answers", (0.10, 0.50, 0.01, 0.125), False),
    Model("claude-fable-5-1", "Claude Fable 5.1 - most capable, about 2.5x the price of Opus", (10.00, 50.00, 0.25, 12.50), True),
)}
DEFAULT_MODEL = "claude-opus-5-5"
EFFORTS = ("low", "medium", "high", "xhigh", "max")


class ClaudeError(RuntimeError):
    """Claude can't be used right now. The message says why, in plain words, and what to do about it."""


# ------------------------------------------------------------------------------------------- requests

def fallback_args(model: str) -> dict[str, Any]:
    """The request fields that turn on refusal fallbacks for ``model`` (none for models without them)."""
    m = MODELS.get(model)
    return {"betas": [FALLBACK_BETA], "fallbacks": "default"} if m and m.fallbacks else {}


def client(api_key: str | None, *, timeout: float = 120.0, retries: int = 2) -> Any:
    """An async Claude client for ``api_key``. Raises ClaudeError when it can't be made."""
    try:
        import anthropic
    except ImportError:
        raise ClaudeError(INSTALL_HINT) from None
    if not api_key:
        raise ClaudeError(f"Add your Claude API key first (dashboard > Setup tab, or Ask > Deep review). Create one at {KEYS_URL}")
    return anthropic.AsyncAnthropic(api_key=api_key, max_retries=retries, timeout=timeout)


async def create(cl: Any, request: dict[str, Any], model: str, state: dict[str, Any] | None = None) -> Any:
    """Send one Messages request, with refusal fallbacks when the model has them.

    An account or API version that refuses the fallback parameter gets the request again without it
    (and, through ``state``, every later request of the same conversation too). A credit or billing
    problem is never retried."""
    import anthropic

    state = state if state is not None else {}
    extra = {} if state.get("no_fallbacks") else fallback_args(model)
    if not extra:
        return await cl.beta.messages.create(**request)
    try:
        return await cl.beta.messages.create(**request, **extra)
    except anthropic.BadRequestError as exc:
        if _billing(api_message(exc)):
            raise
        log.info("Claude refused the request with refusal fallbacks (%s); sending it without them", api_message(exc))
        state["no_fallbacks"] = True
        return await cl.beta.messages.create(**request)


def api_message(exc: Any) -> str:
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        err = body.get("error") or {}
        if isinstance(err, dict) and err.get("message"):
            return str(err["message"])
    return str(getattr(exc, "message", "") or exc)


def _billing(message: str) -> bool:
    low = message.lower()
    return any(w in low for w in ("credit", "balance", "billing"))


def explain(exc: BaseException, model: str = "") -> str:
    """What went wrong with a Claude request, in plain words with what to do next."""
    try:
        import anthropic
    except ImportError:
        return INSTALL_HINT
    if isinstance(exc, ClaudeError):
        return str(exc)
    if isinstance(exc, anthropic.AuthenticationError):
        return f"Claude refused the API key. Copy it again from {KEYS_URL} and save it on the Setup tab (or Ask > Deep review)."
    if isinstance(exc, anthropic.PermissionDeniedError):
        return f"The API key isn't allowed to do that ({api_message(exc)}). Check its workspace on {KEYS_URL}."
    if isinstance(exc, anthropic.NotFoundError):
        return f"Model '{model}' isn't available to this key: pick another model." if model else "Claude couldn't find that."
    if isinstance(exc, anthropic.RateLimitError):
        return "The Claude API is rate-limiting this key right now. Try again in a minute."
    if isinstance(exc, anthropic.BadRequestError):
        msg = api_message(exc)
        if _billing(msg):
            return f"Your Claude API credit balance is too low. Buy credits at {BILLING_URL}"
        return f"The Claude API rejected the request: {msg}"
    if isinstance(exc, anthropic.APIStatusError):
        if exc.status_code == 402:
            return f"Your Claude API credit balance is too low. Buy credits at {BILLING_URL}"
        return f"The Claude API had a problem ({exc.status_code}): {api_message(exc)}. Try again later."
    if isinstance(exc, anthropic.APIConnectionError):
        return "Couldn't reach the Claude API. Check the internet connection and try again."
    return f"Claude failed: {exc}"


async def check_key(api_key: str, model: str = DEFAULT_MODEL) -> str:
    """Check a key with the Claude API (free: it only reads the model's details). Returns what to tell you.

    Raises ClaudeError when the key can't be used; a key that works but can't use ``model`` passes
    with a note, so you can pick another model."""
    import anthropic

    key = (api_key or "").strip()
    if not key.startswith(KEY_PREFIX):
        raise ClaudeError(f"That doesn't look like a Claude API key (they start with {KEY_PREFIX}). Create one at {KEYS_URL}")
    cl = client(key, timeout=30.0, retries=1)
    try:
        info = await cl.models.retrieve(model)
    except anthropic.NotFoundError:
        return f"The key works, but {model} isn't available to it: pick another model."
    except anthropic.APIError as exc:
        raise ClaudeError(explain(exc, model)) from None
    finally:
        await cl.close()
    return f"Key works: {getattr(info, 'display_name', None) or model} is available."


# ------------------------------------------------------------------------------------------- spending

def tokens(usage: Any, key: str) -> int:
    value = usage.get(key) if isinstance(usage, dict) else getattr(usage, key, 0)
    return int(value or 0)


def price(model: str, usage: Any) -> float:
    """Dollars for one response (every attempt, when the response fell back to another model)."""
    def one(m: str, u: Any) -> float:
        pin, pout, pread, pwrite = (MODELS.get(m) or MODELS[DEFAULT_MODEL]).prices
        return (tokens(u, "input_tokens") * pin + tokens(u, "output_tokens") * pout
                + tokens(u, "cache_read_input_tokens") * pread + tokens(u, "cache_creation_input_tokens") * pwrite) / 1_000_000

    if usage is None:
        return 0.0
    iterations = usage.get("iterations") if isinstance(usage, dict) else getattr(usage, "iterations", None)
    if iterations:
        total = 0.0
        for it in iterations:
            m = (it.get("model") if isinstance(it, dict) else getattr(it, "model", None)) or model
            total += one(m, it)
        return total
    return one(model, usage)


class Ledger:
    """Every Claude response's cost, in ``usage.jsonl``: the one monthly budget the coach and Ask share."""

    def __init__(self, folder: Path):
        self.folder = Path(folder)
        self.path = self.folder / "usage.jsonl"

    def spent(self, month: str | None = None, source: str | None = None) -> float:
        """Dollars spent this calendar month (UTC), by everything or by one ``source`` ("coach", "ask")."""
        month = month or datetime.now(UTC).strftime("%Y-%m")
        if not self.path.exists():
            return 0.0
        total = 0.0
        for line in self.path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if str(row.get("at", "")).startswith(month) and (source is None or row.get("source", "coach") == source):
                total += float(row.get("cost", 0.0))
        return round(total, 4)

    def log(self, model: str, usage: Any, source: str, cost: float | None = None) -> float:
        cost = price(model, usage) if cost is None else cost
        self.folder.mkdir(parents=True, exist_ok=True)
        row = {"at": datetime.now(UTC).isoformat(timespec="seconds"), "source": source, "model": model,
               "input": tokens(usage, "input_tokens"), "output": tokens(usage, "output_tokens"),
               "cache_read": tokens(usage, "cache_read_input_tokens"),
               "cache_write": tokens(usage, "cache_creation_input_tokens"), "cost": round(cost, 6)}
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")
        return cost

    def check(self, budget: float) -> None:
        """Raise ClaudeError once this month's spending has reached ``budget``."""
        spent = self.spent()
        if spent >= budget:
            raise ClaudeError(f"This month's Claude budget is used up (${spent:.2f} of ${budget:.2f}, shared by the AI coach "
                              "and the Ask answers). Raise it on the dashboard's Ask tab (Deep review) to continue.")


def ledger_for(data_path: Path) -> Ledger:
    return Ledger(Path(data_path) / "coach")


# ------------------------------------------------------------------------------------------- the key

def current_key() -> str | None:
    """The saved key (the dashboard updates it without a restart)."""
    return os.environ.get(KEY_ENV) or None


def store_key(env_path: Path, key: str | None, secrets: Secrets | None = None) -> None:
    """Save (or, with None, remove) the shared key: .env on this PC, this process, the log masks."""
    from topstep_bot.logging_setup import add_secret
    from topstep_bot.wizard import remove_env_keys, update_env_file

    if key:
        update_env_file(Path(env_path), {KEY_ENV: key})
        os.environ[KEY_ENV] = key
        add_secret(key)
    else:
        remove_env_keys(Path(env_path), {KEY_ENV})
        os.environ.pop(KEY_ENV, None)
    if secrets is not None:
        secrets.anthropic_api_key = key or None
