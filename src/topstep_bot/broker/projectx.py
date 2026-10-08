"""Live broker: routes orders to a TopstepX account through the ProjectX Gateway API."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

import httpx

from topstep_bot.api.realtime import UserStream
from topstep_bot.api.rest import ProjectXClient, ProjectXError
from topstep_bot.broker.base import Broker
from topstep_bot.models import Account, Fill, Order, OrderSide, OrderType, Position

log = logging.getLogger(__name__)
UTC = timezone.utc
IOC_CANCEL_AFTER = 5.0  # seconds an entry limit may wait before it is cancelled


class ProjectXBroker(Broker):
    name = "topstepx"

    def __init__(self, client: ProjectXClient, account_id: int, user_hub_url: str):
        super().__init__(account_id)
        self.client = client
        self.stream = UserStream(user_hub_url, client.get_token, account_id)
        self.stream.on_order = lambda o: self._emit(self.on_order, o)
        self.stream.on_position = lambda p: self._emit(self.on_position, p)
        self.stream.on_fill = lambda f: self._emit(self.on_fill, f)
        self.stream.on_account = lambda a: self._emit(self.on_account, a)
        self._task: asyncio.Task | None = None
        self._background: set[asyncio.Task] = set()

    @property
    def connected(self) -> bool:
        return self.stream.hub.connected

    async def start(self) -> None:
        from topstep_bot.logging_setup import spawn

        self._task = spawn(self.stream.hub.run(), name="user-hub")
        if not await self.stream.hub.wait_connected(timeout=20):
            log.warning("User hub not connected yet; relying on REST reconciliation until it is")

    async def stop(self) -> None:
        await self.stream.hub.stop()
        if self._task:
            self._task.cancel()

    async def get_account(self) -> Account:
        for acct in await self.client.search_accounts(only_active=False):
            if acct.id == self.account_id:
                return acct
        raise ProjectXError(f"Account {self.account_id} not found")

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
        try:
            order_id = await self.client.place_order(
                self.account_id,
                contract_id,
                type_,
                side,
                size,
                limit_price=limit_price,
                stop_price=stop_price,
                custom_tag=tag,
                stop_loss_ticks=stop_loss_ticks,
                take_profit_ticks=take_profit_ticks,
            )
            if ioc:
                self._spawn(self._cancel_later(order_id, IOC_CANCEL_AFTER))
            return order_id
        except httpx.TransportError:
            # The request may or may not have reached the server. Look for it by tag before failing.
            found = await self._find_by_tag(tag)
            if found is not None:
                log.warning("Order %s was placed despite a network error (id %s)", tag, found)
                return found
            raise
        except ProjectXError as exc:
            if exc.payload.get("success") is None and (exc.code or 0) >= 500:
                # HTTP 5xx: like a network error, the order may still have been created.
                found = await self._find_by_tag(tag)
                if found is not None:
                    log.warning("Order %s was placed despite a server error (id %s)", tag, found)
                    return found
                raise
            # With 'Position Brackets' mode, bracket params are rejected but the order is still created.
            order_id = exc.payload.get("orderId")
            if exc.code == 2 and order_id:
                log.error(
                    "Native brackets rejected (enable 'Auto OCO Brackets' in TopstepX risk settings); "
                    "order %s was still placed - protective orders will be managed by the bot",
                    order_id,
                )
                return int(order_id)
            raise

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def _cancel_later(self, order_id: int, delay: float) -> None:
        await asyncio.sleep(delay)
        try:
            await self.client.cancel_order(self.account_id, order_id)  # fails harmlessly if already filled
        except Exception:  # noqa: BLE001
            pass

    async def _find_by_tag(self, tag: str | None) -> int | None:
        if not tag:
            return None
        since = datetime.now(UTC) - timedelta(minutes=10)
        try:
            orders = await self.client.search_orders(self.account_id, since)
        except Exception as exc:  # noqa: BLE001 - report the original failure, not this one
            log.warning("Could not check whether order %s was placed: %s", tag, exc)
            return None
        return next((o.id for o in orders if o.custom_tag == tag), None)

    async def cancel_order(self, order_id: int) -> None:
        await self.client.cancel_order(self.account_id, order_id)

    async def modify_order(
        self, order_id: int, *, size: int | None = None, limit_price: float | None = None, stop_price: float | None = None
    ) -> None:
        await self.client.modify_order(
            self.account_id, order_id, size=size, limit_price=limit_price, stop_price=stop_price
        )

    async def close_position(self, contract_id: str) -> None:
        await self.client.close_position(self.account_id, contract_id)

    async def open_orders(self) -> list[Order]:
        return await self.client.search_open_orders(self.account_id)

    async def positions(self) -> list[Position]:
        return await self.client.search_open_positions(self.account_id)

    async def get_order(self, order_id: int, since: datetime) -> Order | None:
        for order in await self.client.search_orders(self.account_id, since - timedelta(minutes=1)):
            if order.id == order_id:
                return order
        return None

    async def fills_since(self, start: datetime) -> list[Fill]:
        return [f for f in await self.client.search_trades(self.account_id, start) if not f.voided]
