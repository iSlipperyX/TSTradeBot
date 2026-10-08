"""Simulated broker for paper trading and backtesting.

Fill model (deliberately conservative):
  * Market orders fill at the next price (backtests: next bar's open) plus slippage.
  * Stop orders trigger when price trades at/through the stop and fill at the stop plus
    slippage (or at the open, plus slippage, if the market gaps through).
  * Limit orders fill only when price trades *through* the limit (a touch is not a fill).
  * If a stop and a target are both reachable inside one bar, the stop is assumed first.

Events are queued and delivered by ``drain()`` so that callbacks (which may place more
orders) never run re-entrantly inside ``place_order``.
"""

from __future__ import annotations

import asyncio
import itertools
from collections import deque
from copy import copy
from datetime import datetime, timezone

from topstep_bot.broker.base import Broker
from topstep_bot.models import (
    Account,
    Bar,
    Contract,
    Fill,
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
    Position,
)

UTC = timezone.utc


class PaperBroker(Broker):
    name = "paper"

    def __init__(
        self,
        contract: Contract,
        starting_balance: float,
        *,
        slippage_ticks: float = 1.0,
        fees_round_turn: float = 0.0,
        account_id: int = 1,
        account_name: str = "PAPER",
        live: bool = False,
    ):
        super().__init__(account_id)
        self.contract = contract
        self.account_name = account_name
        self.balance = float(starting_balance)
        self.slippage = slippage_ticks * contract.tick_size
        self.fee_per_side = fees_round_turn / 2.0
        self.live = live  # live paper: market orders fill immediately at the current quote
        self.orders: dict[int, Order] = {}
        self.position = 0
        self.avg_price = 0.0
        self.fills: list[Fill] = []
        self.last_price: float | None = None
        self.bid: float | None = None
        self.ask: float | None = None
        self.now = datetime.now(UTC)
        self._order_ids = itertools.count(1)
        self._fill_ids = itertools.count(1)
        self._ioc: set[int] = set()
        self._close_only: set[int] = set()
        self._events: deque[tuple[str, object]] = deque()
        self._draining = False

    # ------------------------------------------------------------- event queue

    def _queue(self, kind: str, value: object) -> None:
        self._events.append((kind, value))
        if self.live:
            asyncio.ensure_future(self.drain())

    async def drain(self) -> None:
        """Deliver queued events in order (including any queued by the callbacks themselves)."""
        if self._draining:
            return
        self._draining = True
        try:
            while self._events:
                kind, value = self._events.popleft()
                callback = {
                    "order": self.on_order,
                    "fill": self.on_fill,
                    "position": self.on_position,
                    "account": self.on_account,
                }[kind]
                await self._emit(callback, value)
        finally:
            self._draining = False

    # ------------------------------------------------------------ broker API

    async def get_account(self) -> Account:
        return Account(self.account_id, self.account_name, self.balance, can_trade=True, simulated=True)

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
        if size <= 0:
            raise ValueError("order size must be positive")
        if type_ == OrderType.STOP and stop_price is None:
            raise ValueError("stop order needs stop_price")
        if type_ == OrderType.LIMIT and limit_price is None:
            raise ValueError("limit order needs limit_price")
        if type_ not in (OrderType.MARKET, OrderType.STOP, OrderType.LIMIT):
            raise ValueError(f"paper broker does not support {type_.name} orders")
        order = Order(
            id=next(self._order_ids),
            account_id=self.account_id,
            contract_id=contract_id,
            type=type_,
            side=side,
            size=size,
            status=OrderStatus.OPEN,
            limit_price=limit_price,
            stop_price=stop_price,
            custom_tag=tag,
            created_at=self.now,
            updated_at=self.now,
        )
        self.orders[order.id] = order
        if ioc:
            self._ioc.add(order.id)
        self._queue("order", copy(order))
        if self.live and self.last_price is not None:
            if type_ == OrderType.MARKET:
                self._fill_market(order)
            elif type_ == OrderType.LIMIT and self._marketable(order):
                # A marketable limit fills at the market (with slippage) but never worse than its limit.
                market = self._adverse(self.ask if side == OrderSide.BUY and self.ask else
                                       self.bid if side == OrderSide.SELL and self.bid else self.last_price, side)
                self._fill(order, min(market, limit_price) if side == OrderSide.BUY else max(market, limit_price))
        if ioc and self.live and order.status.is_working:
            await self.cancel_order(order.id)
        return order.id

    def _marketable(self, order: Order) -> bool:
        if order.side == OrderSide.BUY:
            ref = self.ask if self.ask is not None else self.last_price
            return ref is not None and ref <= order.limit_price
        ref = self.bid if self.bid is not None else self.last_price
        return ref is not None and ref >= order.limit_price

    async def cancel_order(self, order_id: int) -> None:
        order = self.orders.get(order_id)
        if order is None or order.status.is_done:
            return
        order.status = OrderStatus.CANCELLED
        order.updated_at = self.now
        self._queue("order", copy(order))

    async def modify_order(
        self, order_id: int, *, size: int | None = None, limit_price: float | None = None, stop_price: float | None = None
    ) -> None:
        order = self.orders.get(order_id)
        if order is None or order.status.is_done:
            raise ValueError(f"order {order_id} is not working")
        if size is not None:
            order.size = size
        if limit_price is not None:
            order.limit_price = limit_price
        if stop_price is not None:
            order.stop_price = stop_price
        order.updated_at = self.now
        self._queue("order", copy(order))

    async def close_position(self, contract_id: str) -> None:
        # Like a real "close position" request: account for closing orders already in flight.
        pending = sum(o.side.sign * o.size for o in self._working(OrderType.MARKET))
        remaining = self.position + pending
        if remaining == 0:
            return
        side = OrderSide.SELL if remaining > 0 else OrderSide.BUY
        order_id = await self.place_order(contract_id, OrderType.MARKET, side, abs(remaining))
        self._close_only.add(order_id)

    async def open_orders(self) -> list[Order]:
        return [copy(o) for o in self.orders.values() if o.status.is_working]

    async def positions(self) -> list[Position]:
        if self.position == 0:
            return []
        return [Position(self.account_id, self.contract.id, self.position, self.avg_price)]

    async def get_order(self, order_id: int, since: datetime) -> Order | None:
        order = self.orders.get(order_id)
        return copy(order) if order else None

    async def fills_since(self, start: datetime) -> list[Fill]:
        return [f for f in self.fills if f.ts >= start]

    # ------------------------------------------------------------ simulation

    def _working(self, type_: OrderType) -> list[Order]:
        return [o for o in self.orders.values() if o.type == type_ and o.status.is_working]

    def _adverse(self, price: float, side: OrderSide) -> float:
        raw = price + self.slippage if side == OrderSide.BUY else price - self.slippage
        return self.contract.round_price(raw, "up" if side == OrderSide.BUY else "down")

    def _fill_market(self, order: Order, ref_price: float | None = None) -> None:
        if order.id in self._close_only:
            # A close request only ever reduces the position (it may already be flat, e.g. stopped out).
            closing = self.position * -order.side.sign
            if closing <= 0:
                order.status = OrderStatus.CANCELLED
                order.updated_at = self.now
                self._queue("order", copy(order))
                return
            order.size = min(order.size, closing)
        if ref_price is None:
            if order.side == OrderSide.BUY:
                ref_price = self.ask if self.ask is not None else self.last_price
            else:
                ref_price = self.bid if self.bid is not None else self.last_price
        self._fill(order, self._adverse(ref_price, order.side))

    def _fill(self, order: Order, price: float) -> None:
        qty = order.size
        signed = order.side.sign * qty
        realized: float | None = None
        pos = self.position
        if pos == 0 or (pos > 0) == (signed > 0):
            new_pos = pos + signed
            self.avg_price = (self.avg_price * abs(pos) + price * qty) / abs(new_pos)
        else:
            closing = min(abs(pos), qty)
            realized = self.contract.pnl(self.avg_price, price, closing if pos > 0 else -closing)
            new_pos = pos + signed
            if new_pos == 0:
                self.avg_price = 0.0
            elif (new_pos > 0) != (pos > 0):
                self.avg_price = price  # reversed through flat
        self.position = new_pos
        fees = self.fee_per_side * qty
        self.balance += (realized or 0.0) - fees

        order.status = OrderStatus.FILLED
        order.filled_price = price
        order.fill_volume = qty
        order.updated_at = self.now
        fill = Fill(
            id=next(self._fill_ids),
            order_id=order.id,
            contract_id=order.contract_id,
            side=order.side,
            size=qty,
            price=price,
            ts=self.now,
            pnl=realized,
            fees=fees,
        )
        self.fills.append(fill)
        self._queue("order", copy(order))
        self._queue("fill", fill)
        self._queue("position", Position(self.account_id, self.contract.id, self.position, self.avg_price))
        self._queue("account", Account(self.account_id, self.account_name, self.balance, simulated=True))

    async def on_bar(self, bar: Bar) -> None:
        """Backtest step: fill orders against one bar's open/high/low/close.

        Order of events inside the bar: everything that trades at the OPEN first (market orders and
        marketable limits, including immediate-or-cancel entries), then the protective orders those
        fills create see the REST of the bar - stops before targets.
        """
        self.now = bar.ts
        for order in self._working(OrderType.MARKET):
            self._fill_market(order, bar.open)
        await self.drain()

        for order in self._working(OrderType.LIMIT):
            if not order.status.is_working:
                continue
            lim = order.limit_price
            if (order.side == OrderSide.BUY and bar.open <= lim) or (order.side == OrderSide.SELL and bar.open >= lim):
                # Marketable at the open: fills at the open (with slippage) but never worse than the limit.
                adverse = self._adverse(bar.open, order.side)
                self._fill(order, min(lim, adverse) if order.side == OrderSide.BUY else max(lim, adverse))
            elif order.id in self._ioc:
                await self.cancel_order(order.id)  # immediate-or-cancel: never rests
            await self.drain()  # an entry fill places its stop now, so the stop sees the rest of this bar

        # Gaps: resting orders already beyond the open fill at the open.
        for order in self._working(OrderType.STOP):
            if not order.status.is_working:
                continue
            if (order.side == OrderSide.BUY and bar.open >= order.stop_price) or (
                order.side == OrderSide.SELL and bar.open <= order.stop_price
            ):
                self._fill(order, self._adverse(bar.open, order.side))
                await self.drain()

        for order in self._working(OrderType.STOP):
            if not order.status.is_working:
                continue
            if order.side == OrderSide.BUY and bar.high >= order.stop_price:
                self._fill(order, self._adverse(order.stop_price, order.side))
            elif order.side == OrderSide.SELL and bar.low <= order.stop_price:
                self._fill(order, self._adverse(order.stop_price, order.side))
            await self.drain()

        # Resting limits (profit targets) need price to trade THROUGH them.
        for order in self._working(OrderType.LIMIT):
            if not order.status.is_working or order.id in self._ioc:
                continue
            if order.side == OrderSide.BUY and bar.low < order.limit_price:
                self._fill(order, order.limit_price)
            elif order.side == OrderSide.SELL and bar.high > order.limit_price:
                self._fill(order, order.limit_price)
            await self.drain()

        self.last_price = bar.close

    async def on_price(self, ts: datetime, price: float, bid: float | None = None, ask: float | None = None) -> None:
        """Live paper step: fill orders against a fresh trade/quote price."""
        self.now = ts
        self.last_price = price
        self.bid, self.ask = bid, ask
        for order in self._working(OrderType.MARKET):
            self._fill_market(order)
        for order in self._working(OrderType.STOP):
            if not order.status.is_working:
                continue
            if order.side == OrderSide.BUY and price >= order.stop_price:
                self._fill(order, self._adverse(max(price, order.stop_price), order.side))
            elif order.side == OrderSide.SELL and price <= order.stop_price:
                self._fill(order, self._adverse(min(price, order.stop_price), order.side))
            await self.drain()
        for order in self._working(OrderType.LIMIT):
            if not order.status.is_working:
                continue
            if order.side == OrderSide.BUY and price < order.limit_price:
                self._fill(order, order.limit_price)
            elif order.side == OrderSide.SELL and price > order.limit_price:
                self._fill(order, order.limit_price)
            await self.drain()
        await self.drain()
