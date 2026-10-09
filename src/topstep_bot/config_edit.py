"""Change settings in config.yaml without losing anything else in it.

The dashboard's Setup tab writes the settings it owns (account, symbol, strategy, risk, ...)
straight into config.yaml. Every other line, including comments and hand edits, stays exactly
as it was: a value is replaced in place, a commented-out example (``# account_id: 123``) is
switched on, and a missing setting is added at the end of its section.
"""

from __future__ import annotations

import re
from typing import Any

import yaml

_TOP_KEY = re.compile(r"^([A-Za-z_][\w-]*)\s*:")
_KEY = re.compile(r"^(\s+)([A-Za-z_][\w-]*)\s*:")
_COMMENTED_KEY = re.compile(r"^(\s+)#\s*([A-Za-z_][\w-]*)\s*:")
REMOVE = object()  # set_value(..., REMOVE) deletes the setting (the default applies again)


def dump_value(value: Any) -> str:
    """A value as it is written after ``key:`` (one line)."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str) and re.fullmatch(r"[A-Za-z][\w.-]*", value) and value.lower() not in (
            "true", "false", "yes", "no", "on", "off", "null", "none"):
        return value
    text = yaml.safe_dump(value, default_flow_style=True, width=10_000, sort_keys=False).strip()
    return text.removesuffix("\n...").strip()


def _split_comment(rest: str) -> tuple[str, str]:
    """'value   # comment' -> ('value', '   # comment'), ignoring '#' inside quotes."""
    quote = None
    for i, ch in enumerate(rest):
        if quote:
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
        elif ch == "#" and (i == 0 or rest[i - 1] in " \t"):
            j = i
            while j > 0 and rest[j - 1] in " \t":
                j -= 1
            return rest[:j], rest[j:]
    return rest.rstrip(), ""


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" \t"))


def _section(lines: list[str], name: str) -> tuple[int, int] | None:
    """(index of 'name:', index after the section's last line) for a top-level section."""
    start = next((i for i, line in enumerate(lines) if (m := _TOP_KEY.match(line)) and m.group(1) == name), None)
    if start is None:
        return None
    end = start + 1
    while end < len(lines) and not _TOP_KEY.match(lines[end]):
        end += 1
    return start, end


def _children_end(lines: list[str], i: int, end: int) -> int:
    """Index after the lines nested under line i (a block-style value spread over several lines)."""
    base = _indent(lines[i])
    j = i + 1
    while j < end and (not lines[j].strip() or _indent(lines[j]) > base):
        j += 1
    while j > i + 1 and not lines[j - 1].strip():
        j -= 1  # blank lines belong to what follows
    return j


def _comment_column(line: str, comment: str) -> int:
    return len(line) - len(comment.lstrip(" \t"))


def _line(indent: str, key: str, value: Any, comment: str, width: int) -> str:
    text = f"{indent}{key}: {dump_value(value)}"
    if comment:
        text = text.ljust(width) if width > len(text) else text + "  "
        text += comment.lstrip(" \t")
    return text


def set_value(text: str, section: str | None, key: str, value: Any) -> str:
    """Return ``text`` with ``section.key`` (or the top-level ``key`` when section is None) set to value."""
    lines = text.splitlines()
    if section is None:
        for i, line in enumerate(lines):
            m = _TOP_KEY.match(line)
            if m and m.group(1) == key:
                rest = line[m.end():]
                _, comment = _split_comment(rest)
                if value is REMOVE:
                    del lines[i]
                else:
                    lines[i] = _line("", key, value, comment, _comment_column(line, comment))
                return "\n".join(lines) + "\n"
        if value is not REMOVE:
            lines.insert(0, f"{key}: {dump_value(value)}")
        return "\n".join(lines) + "\n"

    found = _section(lines, section)
    if found is None:
        if value is REMOVE:
            return text if text.endswith("\n") or not text else text + "\n"
        while lines and not lines[-1].strip():
            lines.pop()
        lines += ["", f"{section}:", f"  {key}: {dump_value(value)}"]
        return "\n".join(lines) + "\n"
    start, end = found
    # a nested block written on the section line itself ("risk: {a: 1}") is replaced by block style
    head_rest = lines[start][_TOP_KEY.match(lines[start]).end():]
    head_value, head_comment = _split_comment(head_rest)
    if head_value.strip() not in ("", "{}"):
        current = yaml.safe_load(head_value) or {}
        if not isinstance(current, dict):
            current = {}
        lines[start] = f"{section}:{head_comment}"
        lines[start + 1:start + 1] = [f"  {k}: {dump_value(v)}" for k, v in current.items()]
        return set_value("\n".join(lines) + "\n", section, key, value)
    indents = [_indent(line) for line in lines[start + 1:end] if _KEY.match(line)]
    pad = " " * (min(indents) if indents else 2)

    for i in range(start + 1, end):
        m = _KEY.match(lines[i])
        if m and m.group(2) == key and len(m.group(1)) == len(pad):
            rest = lines[i][m.end():]
            _, comment = _split_comment(rest)
            stop = _children_end(lines, i, end)
            if value is REMOVE:
                del lines[i:stop]
            else:
                lines[i:stop] = [_line(m.group(1), key, value, comment, _comment_column(lines[i], comment))]
            return "\n".join(lines) + "\n"
    if value is REMOVE:
        return "\n".join(lines) + "\n"
    for i in range(start + 1, end):
        m = _COMMENTED_KEY.match(lines[i])
        if m and m.group(2) == key and len(m.group(1)) == len(pad):
            # switch the commented-out example on, keeping its explanation
            _, comment = _split_comment(lines[i][m.end():])
            lines[i] = _line(m.group(1), key, value, comment, _comment_column(lines[i], comment))
            return "\n".join(lines) + "\n"
    last = start  # after the section's last setting (and anything nested under it), before trailing comments
    for i in range(start + 1, end):
        m = _KEY.match(lines[i])
        if m and len(m.group(1)) == len(pad):
            last = _children_end(lines, i, end) - 1
    lines.insert(last + 1, f"{pad}{key}: {dump_value(value)}")
    return "\n".join(lines) + "\n"


def set_values(text: str, changes: dict[tuple[str | None, str], Any]) -> str:
    for (section, key), value in changes.items():
        text = set_value(text, section, key, value)
    return text
