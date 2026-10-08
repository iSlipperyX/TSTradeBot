"""Typed wrappers around the ProjectX user and market hubs."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any

from topstep_bot.api import parse
from topstep_bot.api.signalr import HubConnection
from topstep_bot.models import Quote, Tick

log = logging.getLogger(__name__)
UTC = timezone.utc

Callback = Callable[[Any], Awaitable[None] | None]


async def _call(cb: Callback | None, value: Any) -> None:
    if cb is None:
        return
    result = cb(value)
    if result is not None and hasattr(result, "__await__"):
        await result


class UserStream:
    """Account, order, position and fill updates for one account."""

    def __init__(self, url: str, token_provider: Callable[[], Awaitable[str]], account_id: int):
        self.account_id = account_id
        self.hub = HubConnection(url, token_provider, name="user")
        self.on_account: Callback | None = None
        self.on_order: Callback | None = None
        self.on_position: Callback | None = None
        self.on_fill: Callback | None = None
        self.hub.on("GatewayUserAccount", self._account)
        self.hub.on("GatewayUserOrder", self._order)
        self.hub.on("GatewayUserPosition", self._position)
        self.hub.on("GatewayUserTrade", self._trade)
        self.hub.add_subscription("SubscribeAccounts")
        self.hub.add_subscription("SubscribeOrders", account_id)
        self.hub.add_subscription("SubscribePositions", account_id)
        self.hub.add_subscription("SubscribeTrades", account_id)

    async def _account(self, *args: Any) -> None:
        for d in parse.unwrap(args[-1] if args else None):
            acct = parse.account(d)
            if acct.id == self.account_id:
                await _call(self.on_account, acct)

    async def _order(self, *args: Any) -> None:
        for d in parse.unwrap(args[-1] if args else None):
            o = parse.order(d)
            if o.account_id in (0, self.account_id):
                await _call(self.on_order, o)

    async def _position(self, *args: Any) -> None:
        for d in parse.unwrap(args[-1] if args else None):
            p = parse.position(d)
            if p.account_id in (0, self.account_id):
                await _call(self.on_position, p)

    async def _trade(self, *args: Any) -> None:
        for d in parse.unwrap(args[-1] if args else None):
            f = parse.fill(d)
            if not f.voided:
                await _call(self.on_fill, f)


class MarketStream:
    """Quotes and trade prints for one contract. Quote updates may be partial, so they are merged."""

    def __init__(self, url: str, token_provider: Callable[[], Awaitable[str]], contract_id: str):
        self.contract_id = contract_id
        self.hub = HubConnection(url, token_provider, name="market")
        self.on_quote: Callback | None = None
        self.on_tick: Callback | None = None
        self.quote = Quote(contract_id, datetime.now(UTC))
        self.hub.on("GatewayQuote", self._quote)
        self.hub.on("GatewayTrade", self._trade)
        self.hub.add_subscription("SubscribeContractQuotes", contract_id)
        self.hub.add_subscription("SubscribeContractTrades", contract_id)

    @staticmethod
    def _split(args: tuple) -> tuple[str | None, Any]:
        if len(args) >= 2:
            return str(args[0]), args[1]
        if len(args) == 1 and isinstance(args[0], list) and len(args[0]) == 2 and isinstance(args[0][0], str):
            return args[0][0], args[0][1]
        return None, args[0] if args else None

    async def _quote(self, *args: Any) -> None:
        contract_id, payload = self._split(args)
        if contract_id not in (None, self.contract_id):
            return
        for d in parse.unwrap(payload):
            q = self.quote
            if d.get("lastPrice") is not None:
                q.last = float(d["lastPrice"])
            if d.get("bestBid") is not None:
                q.bid = float(d["bestBid"])
            if d.get("bestAsk") is not None:
                q.ask = float(d["bestAsk"])
            q.ts = parse.parse_ts(d.get("timestamp") or d.get("lastUpdated")) or datetime.now(UTC)
            await _call(self.on_quote, q)

    async def _trade(self, *args: Any) -> None:
        contract_id, payload = self._split(args)
        if contract_id not in (None, self.contract_id):
            return
        for d in parse.unwrap(payload):
            if d.get("price") is None:
                continue
            tick = Tick(
                contract_id=self.contract_id,
                ts=parse.parse_ts(d.get("timestamp")) or datetime.now(UTC),
                price=float(d["price"]),
                size=float(d.get("volume") or 0.0),
            )
            await _call(self.on_tick, tick)
