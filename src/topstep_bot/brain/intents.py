"""Answering without AI: recognise what a question is about and reply from the matching facts.

The bot's built-in answers. They cover the questions people ask most - why a trade was taken or
skipped, what the bot learned, how a strategy is doing, what's next, the decision rule, the rules
- and say plainly when a question is outside what they understand. With AI answers on (llm.py),
these are still the fallback when the AI can't be reached.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from typing import Any

from topstep_bot.brain.facts import find_strategy

Fetch = Callable[[str, dict], Awaitable[dict[str, Any]]]

HELP = """I'm the bot's central intelligence. I answer from its own records. Try:
• why did you take the last trade?  • why the 10:35 trade?
• why didn't you trade today?  • what did you learn?
• how is ORB doing?  • what do you know?
• when is the next trade?  • how am I doing today?
• which decision rule are you using?  • what are my rules?
• what did the simulations find?  • what has the coach learned?
• how are the markets moving together?  • is gold stressed?
• which markets are you trading?  • what are the odds on ORB?
• where does your knowledge come from?  • what is the consistency target?
• explain max_conviction  • how does the knowledge network grow?
I can explain and report, but I can't place trades or change settings - use the dashboard for that.
In Telegram, send /help for the bot's commands."""

TIME = re.compile(r"\b(\d{1,2})[:.h](\d{2})\b")
DAY = re.compile(r"\b(20\d\d-\d\d-\d\d)\b")


def _has(q: str, *words: str) -> bool:
    return any(re.search(rf"\b{w}", q) for w in words)


CURRENT = ("doing", "today", r"now\b", "my rules", "last trade", r"next\b", "position")  # about right now, not how it works


NAME_SKIP = {"mini", "micro", "futures", "index", "year", "note", "bond", "dollar", "short", "tons", "ultra",
             "australian", "british", "canadian", "japanese", "swiss", "new", "zealand", "mexican", "live", "lean"}


def _symbol(question: str) -> str | None:
    """The market a question names: a symbol written in capitals (MNQ, CL) or a word of its name (gold, crude)."""
    from topstep_bot.market_knowledge import MICROS, PRODUCTS

    for tok in re.findall(r"\b[A-Z0-9]{2,4}\b", question):
        if tok in PRODUCTS or tok in MICROS:
            return tok
    words = set(re.findall(r"[a-z]+", question.lower()))
    for root, p in PRODUCTS.items():
        if words & {w for w in re.findall(r"[a-z]+", p.name.lower()) if len(w) > 3 and w not in NAME_SKIP}:
            return root
    return None


async def answer(question: str, fetch: Fetch) -> str:
    """The built-in answer to ``question``, built only from ``fetch(lookup, args)`` results."""
    q = question.lower().strip()
    if not q or _has(q, "help", "what can you", "commands", r"hi\b", r"hello\b", r"hey\b", "good (morning|afternoon|evening)"):
        return HELP
    time = TIME.search(q)
    day = DAY.search(q)
    args: dict[str, Any] = {}
    if time:
        args["time"] = f"{int(time.group(1)):02d}:{time.group(2)}"
    if day:
        args["day"] = day.group(1)
    strategy = find_strategy(q)

    async def text(name: str, a: dict | None = None) -> str:
        data = await fetch(name, a or {})
        return data.get("text") or data.get("error") or "No answer."

    # "how do I ...", "can the bot ...": how the bot works, from its guide (not what it is doing right now)
    if not time and _has(q, "how do i", "how can i", r"how to\b", r"can i\b", r"can (the bot|it|you)\b", "is it possible",
                         r"how does .* work", "what happens (if|when)") and not _has(q, "why", *CURRENT):
        return await text("explain", {"topic": question})
    if _has(q, "skip", "pass", "didn.?t (you )?(trade|take)", "did not (trade|take)", "no trades?", "not trad", "why not"):
        if time:
            return await text("explain_decision", args)
        passed = await fetch("decisions", {**args, "status": "passed", "limit": 5})
        skipped = await fetch("decisions", {**args, "status": "skipped", "limit": 5})
        items = sorted(passed.get("items", []) + skipped.get("items", []), key=lambda d: (d["day"], d["time"]), reverse=True)
        if not items:
            return ("I haven't passed on or skipped any signal I've recorded" + (f" on {args['day']}" if day else "") +
                    ". When I don't trade it is usually because no strategy signalled at all - ask \"when is the next "
                    "trade?\" for what I'm waiting for.")
        first = await text("explain_decision", {"id": items[0]["id"]})
        rest = "\n".join("• " + d["summary"] for d in items[1:5])
        return first + (f"\n\nOther signals I didn't trade:\n{rest}" if rest else "")
    asks_why = _has(q, "why", "explain", "reason", "how did you decide", "what made you")
    if asks_why and (time or _has(q, "last", "latest", "trade", "entry", "position", r"that\b", r"this\b", r"it\b")):
        return await text("explain_decision", args)
    if _has(q, "which markets", "what markets", "how many markets", r"markets? (are|is) (you|it|the bot) trad",
            r"trad\w* (several|multiple|many|more|other) markets", r"(add|drop|switch|remove) (a |the )?market"):
        return await text("markets")
    if _has(q, "probab", "odds", "chance", r"p\(win", "expected value", r"ev\b", "calibrat", "kelly", "how likely",
            "win rate", "how sure"):
        return await text("probability", {"strategy": strategy} if strategy else {})
    if _has(q, "coach"):
        return await text("coach")
    if _has(q, "simulat", "opportunit", "combines? pass", "pass rate", "playbook"):
        return await text("simulations")
    if _has(q, "learn", "lesson", "taught", "improv"):
        return await text("lessons")
    if strategy:
        return await text("strategy", {"name": strategy})
    if _has(q, "decision lab", "rule you", "decision rule", "policy", "evidence rule", "classic rule", "which rule"):
        return await text("lab")
    if _has(q, "next", "forecast", "when will", "when do you", "waiting"):
        return await text("forecast")
    if _has(q, "should i", "good time", "trade now", "take a trade"):
        return (await text("forecast") + "\n\nThis is what my records show, not advice: no setup is certain, and every "
                "trade (yours or mine) goes through the same risk limits.")
    if _has(q, "decision", "signals", "today"):
        if _has(q, "p&l", "pnl", "profit", "balance", "doing", "status"):
            return await text("status")
        return await text("decisions", args)
    if _has(q, "trades", "history", "recent trades", "journal"):
        return await text("trades")
    if _has(q, "status", "doing", "p&l", "pnl", "balance", "position", "running", "how am i", "how are we"):
        return await text("status")
    if _has(q, "source", "come from", "where do you learn", "knowledge network", "related markets", "what feeds"):
        return await text("sources")
    if _has(q, "know", "knowledge", "brain", "memory", "what works", "best strateg"):
        return await text("knowledge")
    if _has(q, "network", "correlat", "move together", "moving together", "moves with", "stressed", "risk appetite", "risk.on", "risk.off",
            "all markets", "across markets", "jump"):
        return await text("network", {"symbol": sym} if (sym := _symbol(question)) else {})
    if _has(q, "what is", "what's", "what does", "how does", "how do i", "how to", "meaning", "explain",
            r"what do .* mean", "what are .* for") and not _has(q, *CURRENT):
        return await text("explain", {"topic": question})
    if _has(q, "market", "news", "headline", "level"):
        return await text("market")
    if _has(q, "rule", "limit", "risk", "topstep", "drawdown", "mll", "consistency"):
        return await text("rules")
    if asks_why:
        return await text("explain_decision", args)
    return "I'm not sure what you're asking. " + HELP
