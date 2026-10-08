"""Broker interface shared by the live TopstepX broker and the simulated paper broker."""

from __future__ import annotations

import inspect
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

from topstep_bot.models import Account, Fill, Order, OrderSide, OrderType, Position

EventCallback = Callable[[Any], Awaitable[None] | None]


class Broker(ABC):
    """Places orders and reports order/position/fill/account changes through callbacks."""

    name = "broker"

    def __init__(self, account_id: int):
        self.account_id = account_id
        self.on_order: EventCallback | None = None
        self.on_position: EventCallback | None = None
        self.on_fill: EventCallback | None = None
        self.on_account: EventCallback | None = None

    async def _emit(self, callback: EventCallback | None, value: Any) -> None:
        if callback is None:
            return
        result = callback(value)
        if inspect.isawaitable(result):
            await result

    @property
    def connected(self) -> bool:
        return True

    async def start(self) -> None:  # noqa: B027 - optional hook
        pass

    async def stop(self) -> None:  # noqa: B027 - optional hook
        pass

    @abstractmethod
    async def get_account(self) -> Account: ...

    @abstractmethod
    async def place_order(
        self,
        contract_id: str,
        type_: OrderType,
        side: OrderSide,
        size: int,
        *,
        limit_price: float | None = None,
        stop_price: float | None = None,
        tag: str | None = None,
        stop_loss_ticks: int | None = None,
        take_profit_ticks: int | None = None,
        ioc: bool = False,
    ) -> int:
        """Place an order. ``ioc``: cancel whatever doesn't fill immediately."""

    @abstractmethod
    async def cancel_order(self, order_id: int) -> None: ...

    @abstractmethod
    async def modify_order(
        self, order_id: int, *, size: int | None = None, limit_price: float | None = None, stop_price: float | None = None
    ) -> None: ...

    @abstractmethod
    async def close_position(self, contract_id: str) -> None: ...

    @abstractmethod
    async def open_orders(self) -> list[Order]: ...

    @abstractmethod
    async def positions(self) -> list[Position]: ...

    @abstractmethod
    async def get_order(self, order_id: int, since: datetime) -> Order | None: ...

    @abstractmethod
    async def fills_since(self, start: datetime) -> list[Fill]: ...

    async def realized_pnl_since(self, start: datetime) -> tuple[float, list[tuple[datetime, float]]]:
        """Net realized P&L (after all fees) since ``start``, and each closed trade as (time, net P&L).

        A trade is one closing order: its fills are added up, so a 3-contract exit filled in three
        pieces counts once. Its net P&L subtracts the closing fees twice to cover the entry side.
        """
        fills = sorted(await self.fills_since(start), key=lambda f: f.ts)
        net = sum((f.pnl or 0.0) - f.fees for f in fills)
        closes: dict[int, list[Fill]] = {}
        for f in fills:
            if f.pnl is not None:
                closes.setdefault(f.order_id, []).append(f)
        trades = [(group[-1].ts, sum((f.pnl or 0.0) - 2 * f.fees for f in group)) for group in closes.values()]
        return net, sorted(trades)
