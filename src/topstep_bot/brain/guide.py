"""Explain anything in the bot: the user guide, the Topstep rules page and every setting, searchable.

The ``explain`` lookup (facts.py) finds the guide sections and settings that match a topic ("the
consistency target", "what does max_conviction do", "how does the knowledge network grow"), so both
the built-in answers and the AI can explain any part of the bot from its own documentation, with the
values this account actually uses.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from topstep_bot.config import BotConfig

ROOT = Path(__file__).resolve().parents[3]  # the repository (or the folder the bot was unpacked to)
DOCS = (("README.md", "README"), ("docs/HOW_TO_USE.md", "How-to guide"), ("docs/TOPSTEP_RULES.md", "Topstep rules"))
MAX_SECTION_CHARS = 2400
_STOP_WORDS = ("a an and are as at be by can do does for from has have how i if in is it its me my of on or our so that "
               "the their them then there these this to was what when where which who why will with you your about into "
               "did not no yes get got bot bots work works working mean means tell explain show please")
STOP = frozenset(_STOP_WORDS.split())

_HEADING = re.compile(r"^(#{1,4})\s+(.*)$")


@dataclass(frozen=True)
class Section:
    source: str
    title: str  # "Heading > Subheading"
    text: str
    words: tuple[str, ...]
    title_words: frozenset[str]


def _stem(word: str) -> str:
    for suffix in ("ing", "ies", "ed", "es", "s"):
        if len(word) > len(suffix) + 3 and word.endswith(suffix):
            return word[: -len(suffix)] + ("y" if suffix == "ies" else "")
    return word


def tokens(text: str) -> list[str]:
    return [_stem(w) for w in re.findall(r"[a-z0-9_]+", text.lower().replace("-", " ")) if len(w) > 1 and w not in STOP]


def _section(label: str, trail: list[str], body: list[str]) -> Section | None:
    text = "\n".join(body).strip()
    if not text or not trail or trail[-1].lower() in ("contents", "table of contents"):
        return None
    return Section(label, " > ".join(trail[1:] or trail), text, tuple(tokens(text)), frozenset(tokens(" ".join(trail))))


@lru_cache(maxsize=1)
def sections() -> tuple[Section, ...]:
    """Every section of the guides, split at their headings (a code block's # lines are not headings)."""
    out: list[Section | None] = []
    for rel, label in DOCS:
        try:
            lines = (ROOT / rel).read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        trail: list[str] = []
        body: list[str] = []
        in_code = False
        for line in lines:
            if line.startswith("```"):
                in_code = not in_code
            m = None if in_code else _HEADING.match(line)
            if m:
                out.append(_section(label, trail, body))
                body = []
                trail = [*trail[: len(m.group(1)) - 1], re.sub(r"[`*]", "", m.group(2)).strip()]
            else:
                body.append(line)
        out.append(_section(label, trail, body))
    return tuple(x for x in out if x is not None)


def settings(cfg: BotConfig | None = None) -> list[dict[str, Any]]:
    """Every setting in config.yaml: its key, what it does and (with ``cfg``) the value this account uses."""
    from pydantic import BaseModel

    from topstep_bot.config import BotConfig

    rows: list[dict[str, Any]] = []

    def walk(model: type[BaseModel], value: Any, prefix: str) -> None:
        for name, field in model.model_fields.items():
            key = f"{prefix}{name}"
            ann = field.annotation
            current = getattr(value, name, None) if value is not None else None
            if isinstance(ann, type) and issubclass(ann, BaseModel):
                walk(ann, current, key + ".")
                continue
            if field.description:
                rows.append({"key": key, "description": field.description,
                             "value": current if cfg is not None else field.default})

    walk(BotConfig, cfg, "")
    return rows


def _score(query: list[str], words: tuple[str, ...], title: frozenset[str], df: dict[str, int], n_docs: int,
           avg_len: float) -> float:
    if not words and not title:
        return 0.0
    counts: dict[str, int] = {}
    for w in words:
        counts[w] = counts.get(w, 0) + 1
    score = 0.0
    for q in set(query):
        idf = math.log(1 + (n_docs - df.get(q, 0) + 0.5) / (df.get(q, 0) + 0.5))
        tf = counts.get(q, 0)
        score += idf * (tf * 2.2) / (tf + 1.2 * (0.25 + 0.75 * len(words) / avg_len)) if tf else 0.0
        if q in title:
            score += 2.5 * idf
    return score


def search(topic: str, cfg: BotConfig | None = None, *, limit: int = 3) -> dict[str, Any]:
    """The guide sections and settings that best match ``topic``."""
    query = tokens(topic)
    if not query:
        return {"sections": [], "settings": [], "text": "Name what you'd like explained, e.g. \"the consistency target\"."}
    secs = sections()
    df: dict[str, int] = {}
    for s in secs:
        for w in set(s.words) | s.title_words:
            df[w] = df.get(w, 0) + 1
    avg = sum(len(s.words) for s in secs) / len(secs) if secs else 1.0
    ranked = sorted(((_score(query, s.words, s.title_words, df, len(secs), avg), s) for s in secs), key=lambda x: -x[0])
    best = [s for score, s in ranked[:limit] if score > 0]
    if ranked and ranked[0][0] > 0:  # drop weak tail matches
        best = [s for score, s in ranked[:limit] if score >= ranked[0][0] * 0.35]
    found = [{"source": s.source, "title": s.title, "text": _trim(s.text)} for s in best]
    rows = settings(cfg)
    key_q = [q for q in re.findall(r"[a-z0-9_.]+", topic.lower()) if "_" in q or "." in q]
    # settings: rare words count more ("several" says more than "market", which most settings mention)
    hays = [set(tokens(row["key"].replace(".", " ").replace("_", " ") + " " + row["description"])) for row in rows]
    sdf: dict[str, int] = {}
    for hay in hays:
        for w in hay:
            sdf[w] = sdf.get(w, 0) + 1
    idf = {q: math.log(1 + (len(rows) - sdf.get(q, 0) + 0.5) / (sdf.get(q, 0) + 0.5)) for q in set(query)}
    scored = []
    for row, hay in zip(rows, hays, strict=True):
        hit = sum(idf[q] for q in set(query) if q in hay)
        exact = any(k == row["key"] or row["key"].endswith("." + k) or k in row["key"] for k in key_q)
        if hit or exact:
            in_key = sum(0.5 * idf[q] for q in set(query) if q in row["key"])
            scored.append((hit + in_key + (100 if exact else 0), row))
    scored.sort(key=lambda x: -x[0])
    top = max((sc for sc, _ in scored), default=0)
    setting_rows = [row for sc, row in scored[:6] if sc >= top * 0.5]
    return {"sections": found, "settings": setting_rows, "text": search_text(topic, found, setting_rows)}


def _trim(text: str) -> str:
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if len(text) <= MAX_SECTION_CHARS:
        return text
    cut = text[:MAX_SECTION_CHARS]
    return cut[: cut.rfind("\n") if "\n" in cut[MAX_SECTION_CHARS // 2:] else len(cut)].rstrip() + "\n..."


def search_text(topic: str, found: list[dict[str, Any]], rows: list[dict[str, Any]]) -> str:
    """The built-in answer: the best section in plain words (shortened), where to read more, and the settings."""
    if not found and not rows:
        return (f"I couldn't find \"{topic}\" in the guide or the settings. Try other words, or open "
                "docs/HOW_TO_USE.md (the full guide).")
    parts = []
    if found:
        first = found[0]
        body, cut = _excerpt(plain(first["text"]), ANSWER_CHARS)
        parts.append(f"From the {first['source'].lower()}, \"{first['title']}\":\n{body}"
                     + (f"\n(More in the {first['source'].lower()}.)" if cut else ""))
        if len(found) > 1:
            parts.append("Also see: " + "; ".join(f"{f['title']} ({f['source'].lower()})" for f in found[1:]))
    if rows:
        lines = [f"• {r['key']} (yours: {_value(r['value'])}): {r['description']}" for r in rows[:ANSWER_SETTINGS]]
        parts.append("Settings:\n" + "\n".join(lines))
    return "\n\n".join(parts)


ANSWER_CHARS = 1400  # a chat answer shows this much of the section; the AI gets the whole (trimmed) section
ANSWER_SETTINGS = 3
_ITEM = re.compile(r"\s*(• |\d+\. )")


def plain(text: str) -> str:
    """Markdown to plain text for a chat answer: links keep their words, emphasis and code marks go, tables become
    lines, and the guide's hard-wrapped paragraphs are joined (list items, tables and code blocks stay as they are)."""
    out: list[str] = []
    in_code = False
    for raw in text.splitlines():
        if raw.lstrip().startswith("```"):
            in_code = not in_code
            continue
        if in_code:
            out.append(raw)
            continue
        line = raw.rstrip()
        cells = line.strip()
        row = cells.startswith("|") and cells.endswith("|")
        if row:
            if set(cells) <= set("|-: "):  # a table's separator row
                continue
            line = " · ".join(c.strip() for c in cells.strip("|").split("|") if c.strip() not in ("", "–"))
        line = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", line)
        line = re.sub(r"\*\*(.+?)\*\*", r"\1", line)
        line = re.sub(r"(?<![\w*])\*(?=\S)(.+?)(?<=\S)\*(?![\w*])", r"\1", line)
        line = re.sub(r"^(\s*)[-*] ", r"\1• ", line.replace("`", ""))
        if line.strip() and out and out[-1].strip() and not row and not _ITEM.match(line) and not out[-1].endswith(":"):
            out[-1] = out[-1].rstrip() + " " + line.strip()  # the rest of a hard-wrapped paragraph or list item
        else:
            out.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()


def _excerpt(text: str, limit: int) -> tuple[str, bool]:
    """``text`` cut at a paragraph (or line) before ``limit`` characters, and whether anything was cut."""
    if len(text) <= limit:
        return text, False
    cut = text[:limit]
    at = cut.rfind("\n\n")
    if at < limit // 3:
        at = cut.rfind("\n")
    return (cut[:at] if at > limit // 3 else cut.rsplit(" ", 1)[0] + " ...").rstrip(), True


def _value(v: Any) -> str:
    """A setting's value as it is written in config.yaml."""
    if isinstance(v, bool):
        return "true" if v else "false"
    if v is None:
        return "not set"
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(str(x) for x in v) + "]" if v else "none"
    if isinstance(v, float):
        return f"{v:g}"
    return str(v)
