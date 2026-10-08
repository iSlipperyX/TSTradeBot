"""Core domain types shared by every part of the bot.

Enum values mirror the ProjectX Gateway API so they can be sent over the wire as-is.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from enum import IntEnum


class OrderType(IntEnum):
    UNKNOWN = 0
    LIMIT = 1
    MARKET = 2
    STOP_LIMIT = 3
    STOP = 4
    TRAILING_STOP = 5
    JOIN_BID = 6
    JOIN_ASK = 7


class OrderSide(IntEnum):
    BUY = 0  # "Bid" in ProjectX terms
    SELL = 1  # "Ask" in ProjectX terms

    @property
    def opposite(self) -> OrderSide:
        return OrderSide.SELL if self is OrderSide.BUY else OrderSide.BUY

    @property
    def sign(self) -> int:
        """+1 for buys, -1 for sells."""
        return 1 if self is OrderSide.BUY else -1

    @property
    def label(self) -> str:
        return "LONG" if self is OrderSide.BUY else "SHORT"


class OrderStatus(IntEnum):
    NONE = 0
    OPEN = 1
    FILLED = 2
    CANCELLED = 3
    EXPIRED = 4
    REJECTED = 5
    PENDING = 6

    @property
    def is_done(self) -> bool:
        return self in (OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.EXPIRED, OrderStatus.REJECTED)

    @property
    def is_working(self) -> bool:
        return not self.is_done


class PositionType(IntEnum):
    UNDEFINED = 0
    LONG = 1
    SHORT = 2


class BarUnit(IntEnum):
    SECOND = 1
    MINUTE = 2
    HOUR = 3
    DAY = 4
    WEEK = 5
    MONTH = 6


# Micro contracts count as 1/10th of a mini toward Topstep's position limits.
MICRO_ROOTS = frozenset({"MES", "MNQ", "MYM", "M2K", "MCL", "MGC", "SIL", "MHG", "MBT", "MET", "M6E", "M6A", "M6B", "MNG"})


def _decimals_for(step: float) -> int:
    text = f"{step:.10f}".rstrip("0")
    return len(text.split(".")[1]) if "." in text else 0


@dataclass(frozen=True)
class Contract:
    """A tradable futures contract and its tick economics."""

    id: str
    name: str
    tick_size: float
    tick_value: float
    description: str = ""
    symbol_id: str = ""
    active: bool = True
    root: str = ""

    @property
    def point_value(self) -> float:
        """Dollar value of a one-point move for one contract."""
        return self.tick_value / self.tick_size

    @property
    def price_decimals(self) -> int:
        return _decimals_for(self.tick_size)

    @property
    def is_micro(self) -> bool:
        return self.root.upper() in MICRO_ROOTS

    def round_price(self, price: float, mode: str = "nearest") -> float:
        """Snap a price to the tick grid. mode: 'nearest', 'up' or 'down'."""
        raw = price / self.tick_size
        if mode == "up":
            ticks = math.ceil(raw - 1e-9)
        elif mode == "down":
            ticks = math.floor(raw + 1e-9)
        else:
            ticks = round(raw)
        return round(ticks * self.tick_size, self.price_decimals)

    def ticks(self, price_distance: float) -> float:
        return abs(price_distance) / self.tick_size

    def price_offset(self, ticks: float) -> float:
        return round(ticks * self.tick_size, self.price_decimals)

    def pnl(self, entry: float, exit_: float, signed_size: int) -> float:
        """Gross P&L in dollars for a position of signed_size (+long / -short)."""
        return (exit_ - entry) * signed_size * self.point_value


@dataclass
class Account:
    id: int
    name: str
    balance: float
    can_trade: bool = True
    is_visible: bool = True
    simulated: bool = True


@dataclass
class Bar:
    """OHLCV bar. ``ts`` is the bar's *open* time (timezone-aware UTC)."""

    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


@dataclass
class Quote:
    contract_id: str
    ts: datetime
    last: float | None = None
    bid: float | None = None
    ask: float | None = None

    @property
    def mid(self) -> float | None:
        if self.bid is not None and self.ask is not None:
            return (self.bid + self.ask) / 2
        return self.last


@dataclass
class Tick:
    """A single market trade print."""

    contract_id: str
    ts: datetime
    price: float
    size: float


@dataclass
class Order:
    id: int
    account_id: int
    contract_id: str
    type: OrderType
    side: OrderSide
    size: int
    status: OrderStatus
    limit_price: float | None = None
    stop_price: float | None = None
    filled_price: float | None = None
    fill_volume: int = 0
    custom_tag: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass
class Position:
    """Net position in one contract. ``size`` is signed: positive long, negative short."""

    account_id: int
    contract_id: str
    size: int
    avg_price: float

    @property
    def is_flat(self) -> bool:
        return self.size == 0


@dataclass
class Fill:
    """An execution (half-turn). ``pnl`` is set only on position-reducing fills."""

    id: int
    order_id: int
    contract_id: str
    side: OrderSide
    size: int
    price: float
    ts: datetime
    pnl: float | None = None
    fees: float = 0.0
    voided: bool = False


@dataclass
class Signal:
    """A strategy's request to act.

    action: 'long', 'short' or 'exit'.
    stop_price: protective stop (required for entries; drives position sizing).
    target_price: optional profit target.
    """

    action: str
    stop_price: float | None = None
    target_price: float | None = None
    reason: str = ""
    meta: dict = field(default_factory=dict)

    @property
    def side(self) -> OrderSide | None:
        if self.action == "long":
            return OrderSide.BUY
        if self.action == "short":
            return OrderSide.SELL
        return None
