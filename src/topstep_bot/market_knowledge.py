"""What the bot knows about each market it can trade: the contract, what moves it, and which
scheduled reports matter for it.

This is reference knowledge written down once (sources: CME Group contract specifications and
the usual economic calendar). It drives three things:
  * which calendar events pause trading for an instrument (news.py, ``news.per_instrument``)
  * the instrument profile on the dashboard's Market tab and in Telegram /brief
  * the headline feed symbol for each market (market_intel.py)
Times are US Central (exchange time).
"""

from __future__ import annotations

from dataclasses import dataclass

# Micro contracts trade the same market as their mini.
MICROS = {"MES": "ES", "MNQ": "NQ", "MYM": "YM", "M2K": "RTY", "MGC": "GC", "MCL": "CL"}

# High-impact US releases that move every market listed here (matched against calendar titles).
US_MACRO = ("CPI", "Non-Farm", "FOMC", "Federal Funds Rate", "Fed Chair", "PCE", "GDP", "Retail Sales", "PPI",
            "ISM", "Unemployment Claims", "JOLTS")


@dataclass(frozen=True)
class Product:
    root: str
    name: str
    exchange: str
    group: str  # markets in the same group move together (used by the cross-account hedge guard)
    yahoo: tuple[str, ...]  # headline feed symbols: the future, then ETFs tracking it (their feeds are busier)
    months: str  # listed contract months (CME month codes)
    expiry: str
    roll: str
    drivers: tuple[str, ...]
    key_events: tuple[str, ...]  # calendar title keywords that matter for this market even at medium impact
    notes: tuple[str, ...] = ()


PRODUCTS: dict[str, Product] = {p.root: p for p in [
    Product(
        "ES", "E-mini S&P 500", "CME", "US equity index", ("ES=F", "SPY"), "HMUZ (Mar, Jun, Sep, Dec)",
        "3rd Friday of the contract month, 08:30 CT",
        "Volume moves to the next quarter about 8 days before expiry (the Thursday of the week before). "
        "The bot always trades the front contract TopstepX reports, picked up at the daily restart.",
        ("US macro data: CPI, payrolls, FOMC decisions and the Fed chair's press conference",
         "Treasury yields and the dollar", "Earnings of the largest S&P companies (Apple, Microsoft, Nvidia, Amazon...)",
         "Risk sentiment / VIX"),
        US_MACRO,
        ("The benchmark: other US index futures usually follow its direction.",),
    ),
    Product(
        "NQ", "E-mini Nasdaq-100", "CME", "US equity index", ("NQ=F", "QQQ"), "HMUZ (Mar, Jun, Sep, Dec)",
        "3rd Friday of the contract month, 08:30 CT",
        "Rolls with ES, about 8 days before expiry.",
        ("Mega-cap tech earnings (Nvidia, Apple, Microsoft, Amazon, Alphabet, Meta, Tesla)",
         "Interest rates: long-duration growth stocks react strongly to yields", "US macro data and the Fed"),
        US_MACRO,
        ("Usually moves 1.3-1.6x as much as ES in percentage terms: wider stops, smaller size for the same risk.",),
    ),
    Product(
        "YM", "E-mini Dow ($5)", "CBOT", "US equity index", ("YM=F", "DIA", "SPY"), "HMUZ (Mar, Jun, Sep, Dec)",
        "3rd Friday of the contract month, 08:30 CT",
        "Rolls with ES, about 8 days before expiry.",
        ("US macro data and the Fed", "Price-weighted: high-priced stocks (UnitedHealth, Goldman Sachs...) dominate",
         "Industrials and financials"),
        US_MACRO,
    ),
    Product(
        "RTY", "E-mini Russell 2000", "CME", "US equity index", ("RTY=F", "IWM", "SPY"), "HMUZ (Mar, Jun, Sep, Dec)",
        "3rd Friday of the contract month, 08:30 CT",
        "Rolls with ES, about 8 days before expiry.",
        ("Interest rates and credit conditions (small companies borrow more)", "The domestic US economy",
         "US macro data and the Fed"),
        US_MACRO,
        ("Thinner and choppier than ES/NQ; gaps and false breakouts are more common.",),
    ),
    Product(
        "GC", "Gold (100 oz)", "COMEX", "gold", ("GC=F", "GLD"), "GJMQVZ (Feb, Apr, Jun, Aug, Oct, Dec)",
        "3rd last business day of the contract month",
        "Volume moves to the next active month in the second half of the month before delivery.",
        ("Real interest rates (yields minus inflation)", "The US dollar", "Fed policy and inflation data",
         "Geopolitical stress and central-bank buying"),
        US_MACRO,
        ("Most active 07:20-12:30 CT (the old pit session) and around US data at 07:30 CT.",),
    ),
    Product(
        "CL", "WTI Crude Oil (1,000 bbl)", "NYMEX", "crude oil", ("CL=F", "USO"), "every month",
        "3 business days before the 25th of the month before delivery",
        "Rolls monthly, about a week before expiry - check the dashboard's contract name after the daily restart.",
        ("Weekly US inventories: API (Tue 15:30 CT) and the EIA report (Wed 09:30 CT)", "OPEC+ production decisions",
         "Geopolitics in producing regions", "Global growth, the dollar and US macro data"),
        (*US_MACRO, "Crude Oil Inventories", "OPEC", "API Weekly"),
        ("The Wednesday 09:30 CT EIA report regularly moves crude more than any other scheduled release.",),
    ),
]}


def product_for(symbol: str) -> Product | None:
    """The product a symbol trades (MNQ -> NQ)."""
    root = symbol.upper()
    return PRODUCTS.get(MICROS.get(root, root))


def event_matters(title: str, symbol: str) -> bool:
    """True if a calendar event title is one of this market's key reports."""
    product = product_for(symbol)
    keys = product.key_events if product else US_MACRO
    low = title.lower()
    return any(k.lower() in low for k in keys)
