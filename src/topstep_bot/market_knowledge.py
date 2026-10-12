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
MICROS = {"MES": "ES", "MNQ": "NQ", "MYM": "YM", "M2K": "RTY", "MGC": "GC", "MCL": "CL",
          "QM": "CL", "QG": "NG", "MNG": "NG", "E7": "6E", "M6E": "6E", "M6A": "6A", "M6B": "6B",
          "SIL": "SI", "MHG": "HG"}

QUARTERLY = "HMUZ (Mar, Jun, Sep, Dec)"
FX_ROLL = "Rolls quarterly, about a week before the third-Wednesday expiry; the bot trades the front contract TopstepX reports."
TSY_ROLL = "Rolls quarterly near the end of the month before delivery (late Feb, May, Aug, Nov)."
GRAIN_NOTE = ("Trades 08:30-13:20 CT in the day session (19:00-07:45 overnight; no orders 07:45-08:30). The bot is out "
              "5 minutes before the 13:20 close.")

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
        "NKD", "Nikkei 225 (USD)", "CME", "Japanese equity index", ("NKD=F", "EWJ"), QUARTERLY,
        "2nd Friday of the contract month",
        "Rolls quarterly about a week before expiry.",
        ("Japanese exporters and the yen (a weaker yen usually lifts the Nikkei)", "Bank of Japan policy",
         "US tech and the overnight US session"),
        (*US_MACRO, "BOJ", "Japan", "Tankan"),
        ("Its busiest hours are Tokyo's (overnight in Chicago); during US hours it mostly follows US stocks.",),
    ),
    Product(
        "MBT", "Micro Bitcoin (0.1 BTC)", "CME", "crypto", ("BTC=F", "BTC-USD"), "every month (plus quarterlies)",
        "Last Friday of the contract month, 15:00 London time",
        "Rolls monthly; the bot trades the front contract TopstepX reports.",
        ("Crypto flows and spot ETF demand", "Risk appetite: often moves with the Nasdaq", "Regulation news",
         "The dollar and real yields"),
        US_MACRO,
        ("Trades nearly around the clock; big moves often happen while US markets are closed, so gaps at 08:30 are common.",),
    ),
    Product(
        "MET", "Micro Ether (0.1 ETH)", "CME", "crypto", ("ETH=F", "ETH-USD"), "every month (plus quarterlies)",
        "Last Friday of the contract month, 15:00 London time",
        "Rolls monthly; the bot trades the front contract TopstepX reports.",
        ("Bitcoin's direction", "Crypto flows and network activity", "Risk appetite and regulation news"),
        US_MACRO,
        ("Moves more than bitcoin in percentage terms: wider stops for the same risk.",),
    ),
    *(Product(
        root, name, "CME", "currencies", (f"{root}=F", etf), QUARTERLY, "2 business days before the 3rd Wednesday of the contract month",
        FX_ROLL, drivers, (*US_MACRO, *keys), notes)
      for root, name, etf, drivers, keys, notes in [
        ("6E", "Euro FX (125,000 EUR)", "FXE", ("ECB policy and euro-area inflation", "US-German rate spreads", "The dollar's broad trend"),
         ("ECB", "German", "Euro", "Lagarde"), ("The most traded currency future; it moves against the dollar index.",)),
        ("6B", "British Pound (62,500 GBP)", "FXB", ("Bank of England policy and UK inflation", "UK politics", "The dollar's broad trend"),
         ("BOE", "UK", "British", "Bailey"), ()),
        ("6J", "Japanese Yen (12.5M JPY)", "FXY", ("Bank of Japan policy and intervention risk", "US-Japan rate gap", "Risk sentiment: the yen rises in a scare"),
         ("BOJ", "Japan", "Ueda"), ("Quoted as dollars per yen: up means a stronger yen.",)),
        ("6A", "Australian Dollar (100,000 AUD)", "FXA", ("Reserve Bank of Australia", "China's economy and commodity prices", "Risk sentiment"),
         ("RBA", "Australia", "Chinese"), ()),
        ("6C", "Canadian Dollar (100,000 CAD)", "FXC", ("Bank of Canada", "Crude oil prices", "Canadian jobs and inflation"),
         ("BOC", "Canada", "Canadian"), ()),
        ("6S", "Swiss Franc (125,000 CHF)", "FXF", ("Swiss National Bank", "Safe-haven demand", "The euro"),
         ("SNB", "Swiss"), ()),
        ("6M", "Mexican Peso (500,000 MXN)", "EWW", ("Banxico and the US-Mexico rate gap", "Trade and tariff news", "Risk sentiment"),
         ("Banxico", "Mexico", "Mexican"), ("A high-yielding currency: it can drop fast when markets get nervous.",)),
        ("6N", "New Zealand Dollar (100,000 NZD)", "NZDUSD=X", ("Reserve Bank of New Zealand", "Dairy prices and China", "Risk sentiment"),
         ("RBNZ", "New Zealand"), ("Thinner than the other majors outside Asian hours.",)),
    ]),
    Product(
        "NG", "Natural Gas (10,000 MMBtu)", "NYMEX", "natural gas", ("NG=F", "UNG"), "every month",
        "3 business days before the first day of the delivery month",
        "Rolls monthly, about a week before expiry.",
        ("Weather forecasts (heating and cooling demand)", "The weekly EIA storage report (Thu 09:30 CT)", "LNG exports and production"),
        (*US_MACRO, "Natural Gas Storage"),
        ("One of the most volatile futures: the Thursday 09:30 CT storage report can move it several percent.",),
    ),
    *(Product(
        root, name, "NYMEX", "crude oil", (f"{root}=F", etf), "every month", "Last business day of the month before delivery",
        "Rolls monthly with crude oil.",
        ("Crude oil prices", "Refinery outages and the weekly EIA inventories (Wed 09:30 CT)", "Seasonal demand"),
        (*US_MACRO, "Crude Oil Inventories", "OPEC"), ())
      for root, name, etf in [("RB", "RBOB Gasoline (42,000 gal)", "UGA"), ("HO", "Heating Oil / ULSD (42,000 gal)", "USO")]),
    Product(
        "SI", "Silver (5,000 oz)", "COMEX", "precious metals", ("SI=F", "SLV"), "HKNUZ (Mar, May, Jul, Sep, Dec)",
        "3rd last business day of the contract month",
        "Volume moves to the next active month late in the month before delivery.",
        ("Gold's direction", "Industrial demand (solar, electronics)", "The dollar and real yields"),
        US_MACRO,
        ("Moves like gold but about twice as much in percentage terms.",),
    ),
    Product(
        "PL", "Platinum (50 oz)", "NYMEX", "precious metals", ("PL=F", "PPLT"), "FJNV (Jan, Apr, Jul, Oct)",
        "3rd last business day of the contract month",
        "Rolls quarterly before delivery.",
        ("Car-catalyst demand", "South African and Russian supply", "Gold and the dollar"),
        US_MACRO,
        ("Thin compared with gold: wider spreads and jumpy moves.",),
    ),
    Product(
        "HG", "Copper (25,000 lb)", "COMEX", "copper", ("HG=F", "CPER"), "HKNUZ (Mar, May, Jul, Sep, Dec)",
        "3rd last business day of the contract month",
        "Volume moves to the next active month late in the month before delivery.",
        ("China's economy and construction", "Global manufacturing (PMIs)", "Supply disruptions in Chile and Peru"),
        (*US_MACRO, "Chinese", "China"),
        ("Called 'Dr. Copper': a read on world growth.",),
    ),
    *(Product(
        root, name, "CBOT", "US Treasuries", (f"{root}=F", etf), QUARTERLY, "Last business day of the contract month (2-, 5-year: earlier)",
        TSY_ROLL,
        ("Fed policy expectations", "Inflation and jobs data", "Treasury auctions and supply"),
        (*US_MACRO, "Treasury", "Auction", "Bond"), notes)
      for root, name, etf, notes in [
        ("ZT", "2-Year T-Note", "SHY", ("Moves with the Fed's next steps; the calmest of the Treasuries.",)),
        ("ZF", "5-Year T-Note", "IEI", ()),
        ("ZN", "10-Year T-Note", "IEF", ("The benchmark Treasury future: prices rise when yields fall.",)),
        ("TN", "Ultra 10-Year T-Note", "IEF", ()),
        ("ZB", "30-Year T-Bond", "TLT", ()),
        ("UB", "Ultra T-Bond", "TLT", ("The most volatile Treasury future.",)),
    ]),
    *(Product(
        root, name, "CBOT", "grains", (f"{root}=F", etf), months, "Business day before the 15th of the contract month",
        "Rolls before the first notice day at the end of the month before delivery.",
        drivers, (*US_MACRO, "WASDE", "Crop", "USDA"), (GRAIN_NOTE,))
      for root, name, etf, months, drivers in [
        ("ZC", "Corn (5,000 bu)", "CORN", "HKNUZ (Mar, May, Jul, Sep, Dec)", ("US weather and crop condition", "USDA reports (WASDE)", "Ethanol and export demand")),
        ("ZW", "Wheat (5,000 bu)", "WEAT", "HKNUZ (Mar, May, Jul, Sep, Dec)", ("Global supply, including the Black Sea region", "USDA reports", "Weather")),
        ("ZS", "Soybeans (5,000 bu)", "SOYB", "FHKNQUX (Jan, Mar, May, Jul, Aug, Sep, Nov)", ("Chinese demand", "US and Brazilian crops", "USDA reports")),
        ("ZM", "Soybean Meal (100 short tons)", "SOYB", "FHKNQUVZ (Jan, Mar, May, Jul, Aug, Sep, Oct, Dec)", ("Soybean crush and animal feed demand", "Argentina's exports")),
        ("ZL", "Soybean Oil (60,000 lb)", "SOYB", "FHKNQUVZ (Jan, Mar, May, Jul, Aug, Sep, Oct, Dec)", ("Biofuel policy", "Crude and palm oil prices")),
    ]),
    *(Product(
        root, name, "CME", "livestock", (f"{root}=F", "COW"), months, "Last business day of the contract month (hogs: 10th business day)",
        "Rolls before expiry.", drivers, (*US_MACRO, "USDA", "Cattle", "Hogs"),
        ("Trades 08:30-13:05 CT only. The bot is out 5 minutes before the 13:05 close.",))
      for root, name, months, drivers in [
        ("HE", "Lean Hogs (40,000 lb)", "GJKMNQVZ", ("Pork demand and exports", "Feed costs", "USDA hogs and pigs reports")),
        ("LE", "Live Cattle (40,000 lb)", "GJMQVZ (Feb, Apr, Jun, Aug, Oct, Dec)", ("Herd size and USDA cattle on feed", "Beef demand", "Feed costs")),
    ]),
    Product(
        "GC", "Gold (100 oz)", "COMEX", "precious metals", ("GC=F", "GLD"), "GJMQVZ (Feb, Apr, Jun, Aug, Oct, Dec)",
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
