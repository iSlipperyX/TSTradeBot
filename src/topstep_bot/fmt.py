"""Number formats shared by every report, message and answer (one place, so they all read the same)."""

from __future__ import annotations

import math


def money(v: float | None) -> str:
    """-$1,234.50 / $1,234.50; "-" for unknown."""
    if v is None:
        return "-"
    return f"-${abs(v):,.2f}" if v < 0 else f"${v:,.2f}"


def dollars(v: float | None) -> str:
    """Whole dollars: -$1,235 / $1,235; "-" for unknown."""
    if v is None:
        return "-"
    return f"-${abs(v):,.0f}" if v < 0 else f"${v:,.0f}"


def r(v: float | None) -> str:
    """+0.25R; "-" for unknown."""
    return "-" if v is None else f"{v:+.2f}R"


def pct(v: float | None, digits: int = 0) -> str:
    """A fraction as a percentage: 0.456 -> 46%; "-" for unknown."""
    return "-" if v is None else f"{v * 100:.{digits}f}%"


def pf(v: float) -> str:
    """A profit factor: 1.25, or ∞ when nothing was lost."""
    return "∞" if math.isinf(v) else f"{v:.2f}"
