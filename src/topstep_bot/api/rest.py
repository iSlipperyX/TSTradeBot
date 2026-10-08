"""Async REST client for the ProjectX Gateway API (TopstepX).

Handles authentication with an API key, automatic token refresh (tokens live 24h),
client-side rate limiting (bars: 50 / 30s, everything else: 200 / 60s), and retries.
Order placement is never blindly retried on a network error, because the first attempt
may have reached the exchange; callers de-duplicate with the order's ``customTag`` instead.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from topstep_bot.api import parse
from topstep_bot.models import (
    Account,
    Bar,
    BarUnit,
    Contract,
    Fill,
    Order,
    OrderSide,
    OrderType,
    Position,
)

log = logging.getLogger(__name__)
UTC = timezone.utc

TOKEN_REFRESH_AFTER = 20 * 3600  # refresh well before the 24h expiry
MAX_BARS_PER_REQUEST = 20_000


class ProjectXError(Exception):
    """The API answered with success=false (or an unusable HTTP status)."""

    def __init__(self, message: str, code: int | None = None, payload: dict | None = None):
        super().__init__(message)
        self.code = code
        self.payload = payload or {}


class AuthError(ProjectXError):
    pass


class RateLimiter:
    """Sliding-window limiter: at most ``max_calls`` per ``period`` seconds."""

    def __init__(self, max_calls: int, period: float):
        self.max_calls = max_calls
        self.period = period
        self._calls: deque[float] = deque()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                while self._calls and now - self._calls[0] >= self.period:
                    self._calls.popleft()
                if len(self._calls) < self.max_calls:
                    self._calls.append(now)
                    return
                await asyncio.sleep(self.period - (now - self._calls[0]) + 0.01)


class ProjectXClient:
    def __init__(
        self,
        username: str,
        api_key: str,
        base_url: str = "https://api.topstepx.com",
        timeout: float = 15.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self._username = username
        self._api_key = api_key
        self.base_url = base_url.rstrip("/")
        self._http = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=timeout,
            transport=transport,
            headers={"accept": "text/plain", "Content-Type": "application/json"},
        )
        self._token: str | None = None
        self._token_time = 0.0
        self._auth_lock = asyncio.Lock()
        # Stay a little under the published limits.
        self._bars_limiter = RateLimiter(45, 30.0)
        self._general_limiter = RateLimiter(180, 60.0)

    async def __aenter__(self) -> ProjectXClient:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    async def close(self) -> None:
        await self._http.aclose()

    # ------------------------------------------------------------------ auth

    async def login(self) -> str:
        async with self._auth_lock:
            return await self._login_locked()

    async def _login_locked(self) -> str:
        await self._general_limiter.acquire()
        resp = await self._http.post("/api/Auth/loginKey", json={"userName": self._username, "apiKey": self._api_key})
        if resp.status_code != 200:
            raise AuthError(f"Login failed: HTTP {resp.status_code}", resp.status_code)
        data = resp.json()
        if not data.get("success") or not data.get("token"):
            raise AuthError(
                f"Login failed (code {data.get('errorCode')}): {data.get('errorMessage') or 'check username and API key'}",
                data.get("errorCode"),
                data,
            )
        self._token = data["token"]
        self._token_time = time.monotonic()
        log.info("Authenticated with TopstepX API")
        return self._token

    async def get_token(self) -> str:
        """A valid session token (logs in or refreshes as needed). Also used by the realtime hubs."""
        async with self._auth_lock:
            if self._token is None:
                return await self._login_locked()
            if time.monotonic() - self._token_time > TOKEN_REFRESH_AFTER:
                try:
                    await self._general_limiter.acquire()
                    resp = await self._http.post(
                        "/api/Auth/validate", headers={"Authorization": f"Bearer {self._token}"}
                    )
                    data = resp.json() if resp.status_code == 200 else {}
                    if data.get("success") and data.get("newToken"):
                        self._token = data["newToken"]
                        self._token_time = time.monotonic()
                        log.info("Session token refreshed")
                    else:
                        await self._login_locked()
                except (httpx.HTTPError, ValueError):
                    await self._login_locked()
            return self._token

    # --------------------------------------------------------------- transport

    async def _post(self, path: str, body: dict, *, idempotent: bool = True, bars: bool = False) -> dict:
        limiter = self._bars_limiter if bars else self._general_limiter
        reauthed = False
        attempt = 0
        while True:
            attempt += 1
            token = await self.get_token()
            await limiter.acquire()
            try:
                resp = await self._http.post(path, json=body, headers={"Authorization": f"Bearer {token}"})
            except httpx.TransportError as exc:
                if not idempotent or attempt >= 4:
                    raise
                delay = min(2 ** attempt, 15)
                log.warning("Network error on %s (%s); retrying in %ss", path, exc, delay)
                await asyncio.sleep(delay)
                continue

            if resp.status_code == 401 and not reauthed:
                reauthed = True
                async with self._auth_lock:
                    await self._login_locked()
                continue
            if resp.status_code == 429 and attempt < 6:
                delay = min(2 ** attempt, 30)
                log.warning("Rate limited on %s; backing off %ss", path, delay)
                await asyncio.sleep(delay)
                continue
            if resp.status_code >= 500 and idempotent and attempt < 4:
                await asyncio.sleep(min(2 ** attempt, 15))
                continue
            if resp.status_code != 200:
                raise ProjectXError(f"{path} failed: HTTP {resp.status_code} {resp.text[:200]}", resp.status_code)

            data = resp.json()
            if data.get("success") is False:
                raise ProjectXError(
                    f"{path} failed (code {data.get('errorCode')}): {data.get('errorMessage')}",
                    data.get("errorCode"),
                    data,
                )
            return data

    # ---------------------------------------------------------------- accounts

    async def search_accounts(self, only_active: bool = True) -> list[Account]:
        data = await self._post("/api/Account/search", {"onlyActiveAccounts": only_active})
        return [parse.account(a) for a in data.get("accounts") or []]

    # --------------------------------------------------------------- contracts

    async def search_contracts(self, text: str, live: bool = False) -> list[Contract]:
        data = await self._post("/api/Contract/search", {"searchText": text, "live": live})
        return [parse.contract(c) for c in data.get("contracts") or []]

    async def contract_by_id(self, contract_id: str) -> Contract:
        data = await self._post("/api/Contract/searchById", {"contractId": contract_id})
        if not data.get("contract"):
            raise ProjectXError(f"Contract {contract_id} not found")
        return parse.contract(data["contract"])

    # ------------------------------------------------------------- market data

    async def retrieve_bars(
        self,
        contract_id: str,
        start: datetime,
        end: datetime,
        unit: BarUnit = BarUnit.MINUTE,
        unit_number: int = 1,
        limit: int = MAX_BARS_PER_REQUEST,
        live: bool = False,
        include_partial: bool = False,
    ) -> list[Bar]:
        body = {
            "contractId": contract_id,
            "live": live,
            "startTime": parse.format_ts(start),
            "endTime": parse.format_ts(end),
            "unit": int(unit),
            "unitNumber": unit_number,
            "limit": min(limit, MAX_BARS_PER_REQUEST),
            "includePartialBar": include_partial,
        }
        data = await self._post("/api/History/retrieveBars", body, bars=True)
        bars = [parse.bar(b) for b in data.get("bars") or []]
        bars.sort(key=lambda b: b.ts)
        return bars

    async def retrieve_bars_range(
        self,
        contract_id: str,
        start: datetime,
        end: datetime,
        unit: BarUnit = BarUnit.MINUTE,
        unit_number: int = 1,
        live: bool = False,
    ) -> list[Bar]:
        """Fetch an arbitrarily long range by paging backwards in 20k-bar chunks."""
        out: dict[datetime, Bar] = {}
        cursor = end
        while cursor > start:
            chunk = await self.retrieve_bars(contract_id, start, cursor, unit, unit_number, live=live)
            if not chunk:
                break
            for b in chunk:
                out[b.ts] = b
            earliest = chunk[0].ts
            if len(chunk) < MAX_BARS_PER_REQUEST or earliest >= cursor:
                break
            cursor = earliest - timedelta(seconds=1)
        return [out[k] for k in sorted(out)]

    # ------------------------------------------------------------------ orders

    async def place_order(
        self,
        account_id: int,
        contract_id: str,
        type_: OrderType,
        side: OrderSide,
        size: int,
        *,
        limit_price: float | None = None,
        stop_price: float | None = None,
        trail_price: float | None = None,
        custom_tag: str | None = None,
        stop_loss_ticks: int | None = None,
        take_profit_ticks: int | None = None,
    ) -> int:
        body: dict[str, Any] = {
            "accountId": account_id,
            "contractId": contract_id,
            "type": int(type_),
            "side": int(side),
            "size": int(size),
            "limitPrice": limit_price,
            "stopPrice": stop_price,
            "trailPrice": trail_price,
            "customTag": custom_tag,
        }
        if stop_loss_ticks:
            body["stopLossBracket"] = {"ticks": int(stop_loss_ticks), "type": int(OrderType.STOP)}
        if take_profit_ticks:
            body["takeProfitBracket"] = {"ticks": int(take_profit_ticks), "type": int(OrderType.LIMIT)}
        data = await self._post("/api/Order/place", body, idempotent=False)
        return int(data["orderId"])

    async def cancel_order(self, account_id: int, order_id: int) -> None:
        await self._post("/api/Order/cancel", {"accountId": account_id, "orderId": order_id})

    async def modify_order(
        self,
        account_id: int,
        order_id: int,
        *,
        size: int | None = None,
        limit_price: float | None = None,
        stop_price: float | None = None,
        trail_price: float | None = None,
    ) -> None:
        body = {
            "accountId": account_id,
            "orderId": order_id,
            "size": size,
            "limitPrice": limit_price,
            "stopPrice": stop_price,
            "trailPrice": trail_price,
        }
        await self._post("/api/Order/modify", body)

    async def search_orders(self, account_id: int, start: datetime, end: datetime | None = None) -> list[Order]:
        body = {"accountId": account_id, "startTimestamp": parse.format_ts(start)}
        if end is not None:
            body["endTimestamp"] = parse.format_ts(end)
        data = await self._post("/api/Order/search", body)
        return [parse.order(o) for o in data.get("orders") or []]

    async def search_open_orders(self, account_id: int) -> list[Order]:
        data = await self._post("/api/Order/searchOpen", {"accountId": account_id})
        return [parse.order(o) for o in data.get("orders") or []]

    # --------------------------------------------------------------- positions

    async def search_open_positions(self, account_id: int) -> list[Position]:
        data = await self._post("/api/Position/searchOpen", {"accountId": account_id})
        return [parse.position(p) for p in data.get("positions") or []]

    async def close_position(self, account_id: int, contract_id: str) -> None:
        await self._post("/api/Position/closeContract", {"accountId": account_id, "contractId": contract_id})

    async def partial_close_position(self, account_id: int, contract_id: str, size: int) -> None:
        await self._post(
            "/api/Position/partialCloseContract", {"accountId": account_id, "contractId": contract_id, "size": size}
        )

    # ------------------------------------------------------------------ trades

    async def search_trades(self, account_id: int, start: datetime, end: datetime | None = None) -> list[Fill]:
        body = {"accountId": account_id, "startTimestamp": parse.format_ts(start)}
        if end is not None:
            body["endTimestamp"] = parse.format_ts(end)
        data = await self._post("/api/Trade/search", body)
        return [parse.fill(t) for t in data.get("trades") or []]

    # ----------------------------------------------------------------- helpers

    async def resolve_contract(self, root: str, live: bool = False) -> Contract:
        """Find the active front-month contract for a root symbol such as 'MNQ' or 'ES'."""
        from topstep_bot.instruments import matches_root

        candidates = [c for c in await self.search_contracts(root, live=live) if matches_root(c.name, root)]
        active = [c for c in candidates if c.active] or candidates
        if not active:
            raise ProjectXError(f"No contract found for symbol '{root}'")
        return active[0]
