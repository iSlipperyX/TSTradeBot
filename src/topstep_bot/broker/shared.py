"""One broker connection shared by several markets (lanes) of the same account.

A TopstepX account has one order/position/fill/account stream. Each market the bot trades has its
own OrderManager and TradingCore, and each of those sets the broker's callbacks for itself. The
``BrokerHub`` owns the real broker's callbacks and hands every event to every lane; a lane's
``LaneBroker`` looks like a whole broker to its OrderManager and passes every request through.

Each lane handles its events on its own line (a queue and a task): a market whose handler waits on
a slow answer from TopstepX keeps its own events in order but never holds up another market's stop.

The OrderManager already ignores events and orders of other contracts, so a lane only ever acts
on its own market. Account updates (the balance) go to every lane.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any

from topstep_bot.broker.base import Broker
from topstep_bot.models import Account, Fill, Order, OrderSide, OrderType, Position

log = logging.getLogger(__name__)
BACKLOG_WARNING = 1_000  # events waiting for one market


class BrokerHub:
    """Fans the real broker's events out to every lane's ``LaneBroker``."""

    def __init__(self, inner: Broker):
        self.inner = inner
        self._lanes: list[LaneBroker] = []
        inner.on_order = lambda v: self._fan("on_order", v)
        inner.on_position = lambda v: self._fan("on_position", v)
        inner.on_fill = lambda v: self._fan("on_fill", v)
        inner.on_account = lambda v: self._fan("on_account", v)

    async def _fan(self, attr: str, value: Any) -> None:
        for lane in list(self._lanes):
            lane._events.put_nowait((attr, value))
            if lane._pump is None or lane._pump.done():  # started on the first event: a hub can be built outside a loop
                lane._pump = asyncio.create_task(self._run(lane), name="lane-events")
            size = lane._events.qsize()
            if size > BACKLOG_WARNING and not lane._backlog_warned:
                lane._backlog_warned = True
                log.warning("%s events are waiting for one market (%s); its handler is slow", size, lane.market or "?")
            elif size <= 1 and lane._backlog_warned:
                lane._backlog_warned = False

    @staticmethod
    async def _run(lane: LaneBroker) -> None:
        while True:
            attr, value = await lane._events.get()
            try:
                await lane._emit(getattr(lane, attr), value)
            except Exception:  # noqa: BLE001 - one bad event must never stop this market's later events
                log.exception("A market could not handle a broker event")

    def lane(self) -> LaneBroker:
        """A new view of the broker for one market."""
        lane = LaneBroker(self)
        self._lanes.append(lane)
        return lane

    def drop(self, lane: LaneBroker) -> None:
        """The market is gone (a lane is only dropped once flat): its waiting events go with it."""
        if lane in self._lanes:
            self._lanes.remove(lane)
        if lane._pump is not None:
            lane._pump.cancel()
            lane._pump = None
        while not lane._events.empty():
            lane._events.get_nowait()

    @property
    def lanes(self) -> int:
        return len(self._lanes)


class LaneBroker(Broker):
    """One market's view of a shared broker: its own callbacks, the shared connection."""

    def __init__(self, hub: BrokerHub):
        super().__init__(hub.inner.account_id)
        self.hub = hub
        self.inner = hub.inner
        self.name = hub.inner.name
        self.market: str | None = None  # for log lines only
        self._events: asyncio.Queue = asyncio.Queue()  # not "_queue": PaperBroker has a method of that name
        self._pump: asyncio.Task | None = None
        self._backlog_warned = False

    @property
    def connected(self) -> bool:
        return self.inner.connected

    async def start(self) -> None:  # the desk starts and stops the shared connection once
        pass

    async def stop(self) -> None:
        pass

    async def get_account(self) -> Account:
        return await self.inner.get_account()

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
        return await self.inner.place_order(contract_id, type_, side, size, limit_price=limit_price,
                                            stop_price=stop_price, tag=tag, stop_loss_ticks=stop_loss_ticks,
                                            take_profit_ticks=take_profit_ticks, ioc=ioc)

    async def cancel_order(self, order_id: int) -> None:
        await self.inner.cancel_order(order_id)

    async def modify_order(self, order_id: int, *, size: int | None = None, limit_price: float | None = None,
                           stop_price: float | None = None) -> None:
        await self.inner.modify_order(order_id, size=size, limit_price=limit_price, stop_price=stop_price)

    async def close_position(self, contract_id: str) -> None:
        await self.inner.close_position(contract_id)

    async def open_orders(self) -> list[Order]:
        return await self.inner.open_orders()

    async def positions(self) -> list[Position]:
        return await self.inner.positions()

    async def get_order(self, order_id: int, since: datetime) -> Order | None:
        return await self.inner.get_order(order_id, since)

    async def fills_since(self, start: datetime) -> list[Fill]:
        return await self.inner.fills_since(start)

    async def realized_pnl_since(self, start: datetime) -> tuple[float, list[tuple[datetime, float]]]:
        return await self.inner.realized_pnl_since(start)

    def __getattr__(self, name: str) -> Any:  # anything else the real broker offers (e.g. a paper broker's prices)
        inner = self.__dict__.get("inner")
        if inner is None:
            raise AttributeError(name)
        return getattr(inner, name)
