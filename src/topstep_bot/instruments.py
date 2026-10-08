"""Built-in specs for popular CME futures so backtests and paper trading work offline.

Live trading always uses the tick size/value reported by the API; these are fallbacks.
Fees are TopstepX all-in round-turn estimates (commission + exchange + NFA) and can be
overridden with ``risk.fees_per_contract_round_turn`` in your config.
"""

from __future__ import annotations

from dataclasses import dataclass

from topstep_bot.models import Contract


@dataclass(frozen=True)
class InstrumentSpec:
    root: str
    description: str
    tick_size: float
    tick_value: float
    fees_round_turn: float
    rth_open: str = "08:30"  # Chicago time
    rth_close: str = "15:00"


SPECS: dict[str, InstrumentSpec] = {
    s.root: s
    for s in [
        InstrumentSpec("ES", "E-mini S&P 500", 0.25, 12.50, 3.78),
        InstrumentSpec("MES", "Micro E-mini S&P 500", 0.25, 1.25, 1.22),
        InstrumentSpec("NQ", "E-mini Nasdaq-100", 0.25, 5.00, 3.78),
        InstrumentSpec("MNQ", "Micro E-mini Nasdaq-100", 0.25, 0.50, 1.22),
        InstrumentSpec("YM", "E-mini Dow", 1.0, 5.00, 3.78),
        InstrumentSpec("MYM", "Micro E-mini Dow", 1.0, 0.50, 1.22),
        InstrumentSpec("RTY", "E-mini Russell 2000", 0.10, 5.00, 3.78),
        InstrumentSpec("M2K", "Micro E-mini Russell 2000", 0.10, 0.50, 1.22),
        InstrumentSpec("CL", "Crude Oil", 0.01, 10.00, 4.20, rth_open="08:00", rth_close="13:30"),
        InstrumentSpec("MCL", "Micro Crude Oil", 0.01, 1.00, 1.40, rth_open="08:00", rth_close="13:30"),
        InstrumentSpec("GC", "Gold", 0.10, 10.00, 4.40, rth_open="07:20", rth_close="12:30"),
        InstrumentSpec("MGC", "Micro Gold", 0.10, 1.00, 1.40, rth_open="07:20", rth_close="12:30"),
    ]
}


def get_spec(root: str) -> InstrumentSpec:
    try:
        return SPECS[root.upper()]
    except KeyError:
        known = ", ".join(sorted(SPECS))
        raise KeyError(f"Unknown symbol '{root}'. Built-in symbols: {known}") from None


def offline_contract(root: str) -> Contract:
    """A Contract built from the local spec table (used for backtests and offline paper mode)."""
    spec = get_spec(root)
    return Contract(
        id=f"OFFLINE.{spec.root}",
        name=spec.root,
        tick_size=spec.tick_size,
        tick_value=spec.tick_value,
        description=spec.description,
        symbol_id=f"F.US.{spec.root}",
        root=spec.root,
    )


MONTH_CODES = "FGHJKMNQUVXZ"


def matches_root(contract_name: str, root: str) -> bool:
    """True if an API contract name like 'MNQZ5' or 'ESH26' belongs to the given root symbol."""
    name = contract_name.upper()
    root = root.upper()
    if not name.startswith(root):
        return False
    rest = name[len(root):]
    return len(rest) >= 2 and rest[0] in MONTH_CODES and rest[1:].isdigit()
