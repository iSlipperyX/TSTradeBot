"""Every market Topstep lets you trade, with its contract specs, so backtests, the Simulation Lab and
paper trading work offline.

Live trading always uses the tick size/value reported by the API; these are fallbacks.
Sources (checked 2026-10-11): Topstep's "When and What Products Can I Trade?" (the permitted list and
the early closes) and "TopstepX Commissions and Fees" (all-in round-turn fees: commission + exchange +
NFA), CME Group contract specifications (tick sizes and values). Fees can be overridden with
``risk.fees_per_contract_round_turn`` in your config.

``rth_open``/``rth_close`` are each market's busy hours in Chicago time (the strategies' opening range
and day session). ``market_close`` is set for the products that stop trading before Topstep's 15:10 CT
flat-by deadline (the CBOT grains and CME livestock): the bot must be out before that close.
"""

from __future__ import annotations

from dataclasses import dataclass

from topstep_bot.models import Contract

# Asset classes, in the order the dashboard lists them.
CLASSES: dict[str, str] = {
    "equity": "Equity index",
    "crypto": "Crypto",
    "fx": "Currencies",
    "energy": "Energy",
    "metals": "Metals",
    "rates": "Interest rates",
    "ags": "Agriculture",
}


@dataclass(frozen=True)
class InstrumentSpec:
    root: str
    description: str
    tick_size: float
    tick_value: float
    fees_round_turn: float
    rth_open: str = "08:30"  # Chicago time
    rth_close: str = "15:00"
    asset_class: str = "equity"
    exchange: str = "CME"
    market_close: str | None = None  # the product stops trading before 15:10 CT (grains, livestock)
    mini: str | None = None  # a micro or e-mini's larger contract on the same market (MNQ -> NQ)


def _specs(asset_class: str, exchange: str, rth: tuple[str, str], rows: list[tuple], close: str | None = None) -> list[InstrumentSpec]:
    return [InstrumentSpec(root, desc, tick, value, fee, rth[0], rth[1], asset_class, exchange, close, *mini)
            for root, desc, tick, value, fee, *mini in rows]


_EQ = ("08:30", "15:00")
_FX = ("07:20", "14:00")
_ENERGY = ("08:00", "13:30")
_METALS = ("07:20", "12:30")
_RATES = ("07:20", "14:00")
_GRAINS = ("08:30", "13:15")
_LIVESTOCK = ("08:30", "13:05")

SPECS: dict[str, InstrumentSpec] = {
    s.root: s
    for s in [
        *_specs("equity", "CME", _EQ, [
            ("ES", "E-mini S&P 500", 0.25, 12.50, 3.78),
            ("MES", "Micro E-mini S&P 500", 0.25, 1.25, 1.22, "ES"),
            ("NQ", "E-mini Nasdaq-100", 0.25, 5.00, 3.78),
            ("MNQ", "Micro E-mini Nasdaq-100", 0.25, 0.50, 1.22, "NQ"),
            ("RTY", "E-mini Russell 2000", 0.10, 5.00, 3.78),
            ("M2K", "Micro E-mini Russell 2000", 0.10, 0.50, 1.22, "RTY"),
            ("NKD", "Nikkei 225 (USD)", 5.0, 25.00, 5.32),
        ]),
        *_specs("equity", "CBOT", _EQ, [
            ("YM", "E-mini Dow", 1.0, 5.00, 3.78),
            ("MYM", "Micro E-mini Dow", 1.0, 0.50, 1.22, "YM"),
        ]),
        *_specs("crypto", "CME", _EQ, [
            ("MBT", "Micro Bitcoin (0.1 BTC)", 5.0, 0.50, 2.82),
            ("MET", "Micro Ether (0.1 ETH)", 0.50, 0.05, 0.72),
        ]),
        *_specs("fx", "CME", _FX, [
            ("6A", "Australian Dollar", 0.00005, 5.00, 4.22),
            ("M6A", "Micro AUD/USD", 0.0001, 1.00, 1.00, "6A"),
            ("6B", "British Pound", 0.0001, 6.25, 4.22),
            ("M6B", "Micro GBP/USD", 0.0001, 0.625, 1.00, "6B"),
            ("6C", "Canadian Dollar", 0.00005, 5.00, 4.22),
            ("6E", "Euro FX", 0.00005, 6.25, 4.22),
            ("E7", "E-mini Euro FX", 0.0001, 6.25, 2.72, "6E"),
            ("M6E", "Micro EUR/USD", 0.0001, 1.25, 1.00, "6E"),
            ("6J", "Japanese Yen", 0.0000005, 6.25, 4.22),
            ("6S", "Swiss Franc", 0.00005, 6.25, 4.22),
            ("6M", "Mexican Peso", 0.00001, 5.00, 4.22),
            ("6N", "New Zealand Dollar", 0.00005, 5.00, 4.22),
        ]),
        *_specs("energy", "NYMEX", _ENERGY, [
            ("CL", "Crude Oil", 0.01, 10.00, 4.02),
            ("QM", "E-mini Crude Oil", 0.025, 12.50, 3.42, "CL"),
            ("MCL", "Micro Crude Oil", 0.01, 1.00, 1.72, "CL"),
            ("NG", "Natural Gas", 0.001, 10.00, 4.22),
            ("QG", "E-mini Natural Gas", 0.005, 12.50, 2.02, "NG"),
            ("MNG", "Micro Henry Hub Natural Gas", 0.001, 1.00, 1.92, "NG"),
            ("RB", "RBOB Gasoline", 0.0001, 4.20, 4.02),
            ("HO", "Heating Oil", 0.0001, 4.20, 4.02),
        ]),
        *_specs("metals", "NYMEX", ("07:20", "12:05"), [
            ("PL", "Platinum", 0.10, 5.00, 4.32),
        ]),
        *_specs("metals", "COMEX", _METALS, [
            ("GC", "Gold", 0.10, 10.00, 4.32),
            ("MGC", "Micro Gold", 0.10, 1.00, 1.92, "GC"),
            ("SI", "Silver", 0.005, 25.00, 4.32),
            ("SIL", "Micro Silver (1,000 oz)", 0.005, 5.00, 2.72, "SI"),
            ("HG", "Copper", 0.0005, 12.50, 4.32),
            ("MHG", "Micro Copper", 0.0005, 1.25, 1.92, "HG"),
        ]),
        *_specs("rates", "CBOT", _RATES, [
            ("ZT", "2-Year T-Note", 0.00390625, 7.8125, 2.32),
            ("ZF", "5-Year T-Note", 0.0078125, 7.8125, 2.32),
            ("ZN", "10-Year T-Note", 0.015625, 15.625, 2.62),
            ("TN", "Ultra 10-Year T-Note", 0.015625, 15.625, 2.62),
            ("ZB", "30-Year T-Bond", 0.03125, 31.25, 2.76),
            ("UB", "Ultra T-Bond", 0.03125, 31.25, 2.92),
        ]),
        *_specs("ags", "CBOT", _GRAINS, [
            ("ZC", "Corn", 0.25, 12.50, 5.28),
            ("ZW", "Wheat", 0.25, 12.50, 5.28),
            ("ZS", "Soybeans", 0.25, 12.50, 5.28),
            ("ZM", "Soybean Meal", 0.10, 10.00, 5.28),
            ("ZL", "Soybean Oil", 0.01, 6.00, 5.28),
        ], close="13:20"),
        *_specs("ags", "CME", _LIVESTOCK, [
            ("HE", "Lean Hogs", 0.025, 10.00, 5.22),
            ("LE", "Live Cattle", 0.025, 10.00, 5.22),
        ], close="13:05"),
    ]
}


def markets(asset_class: str | None = None) -> list[str]:
    """Every market Topstep permits (or one asset class's), in the catalogue's order."""
    return [s.root for s in SPECS.values() if asset_class is None or s.asset_class == asset_class]


# Words instrument.candidates accepts besides symbols.
CANDIDATE_GROUPS = ("ALL", "MICROS", *(c.upper() for c in CLASSES))


def expand_markets(names: list[str]) -> list[str]:
    """Symbols, ALL, MICROS or asset classes (FX, METALS...) -> the symbols, in the order given (a group in
    the catalogue's order), each once."""
    from topstep_bot.models import MICRO_ROOTS

    out: list[str] = []
    for name in (n.strip().upper() for n in names):
        if name == "ALL":
            out += list(SPECS)
        elif name == "MICROS":
            out += [s for s in SPECS if s in MICRO_ROOTS]
        elif name.lower() in CLASSES:
            out += markets(name.lower())
        elif name in SPECS:
            out.append(name)
    return list(dict.fromkeys(out))


def class_of(root: str) -> str:
    spec = SPECS.get(root.upper())
    return spec.asset_class if spec else "equity"


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
