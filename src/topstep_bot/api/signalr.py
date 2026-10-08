"""A small, dependency-light SignalR (JSON protocol) client over WebSockets.

ProjectX's realtime hubs speak ASP.NET Core SignalR. We connect straight to the
WebSocket endpoint (skipping negotiation, as ProjectX's docs recommend), perform the
JSON handshake, keep the connection alive with pings, and automatically reconnect and
re-subscribe with a fresh token when the connection drops.

Wire format: JSON records terminated by the 0x1E record separator.
  type 1 = invocation, 3 = completion, 6 = ping, 7 = close.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import itertools
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import quote

import websockets
from websockets.asyncio.client import connect

log = logging.getLogger(__name__)

RS = "\x1e"
HANDSHAKE = json.dumps({"protocol": "json", "version": 1}) + RS
PING = json.dumps({"type": 6}) + RS

Handler = Callable[..., Any]


def encode(message: dict) -> str:
    return json.dumps(message, separators=(",", ":")) + RS


def decode(frame: str | bytes) -> list[dict]:
    if isinstance(frame, bytes):
        frame = frame.decode("utf-8")
    return [json.loads(part) for part in frame.split(RS) if part.strip()]


def hub_ws_url(http_url: str, token: str) -> str:
    url = http_url.replace("https://", "wss://", 1).replace("http://", "ws://", 1)
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}access_token={quote(token, safe='')}"


class HubClosedError(Exception):
    pass


class HubConnection:
    def __init__(
        self,
        url: str,
        token_provider: Callable[[], Awaitable[str]],
        name: str = "hub",
        *,
        ping_interval: float = 10.0,
        connect_timeout: float = 15.0,
        reconnect_delays: tuple[float, ...] = (1, 2, 5, 10, 20, 30),
    ):
        self.url = url
        self.name = name
        self._token_provider = token_provider
        self._ping_interval = ping_interval
        self._connect_timeout = connect_timeout
        self._reconnect_delays = reconnect_delays
        self._handlers: dict[str, list[Handler]] = {}
        self._subscriptions: list[tuple[str, tuple[Any, ...]]] = []
        self._on_connected: list[Callable[[], Any]] = []
        self._on_disconnected: list[Callable[[], Any]] = []
        self._ws: Any = None
        self._ids = itertools.count(1)
        self._pending: dict[str, asyncio.Future] = {}
        self._queue: asyncio.Queue[tuple[str, list]] = asyncio.Queue()
        self._stopping = asyncio.Event()
        self._connected = asyncio.Event()

    # -------------------------------------------------------------- public API

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    def on(self, event: str, handler: Handler) -> None:
        self._handlers.setdefault(event.lower(), []).append(handler)

    def on_connected(self, callback: Callable[[], Any]) -> None:
        self._on_connected.append(callback)

    def on_disconnected(self, callback: Callable[[], Any]) -> None:
        self._on_disconnected.append(callback)

    def add_subscription(self, method: str, *args: Any) -> None:
        """Register a subscription that is (re-)sent every time the hub connects."""
        self._subscriptions.append((method, args))
        if self.connected:
            asyncio.ensure_future(self.send(method, *args))

    async def wait_connected(self, timeout: float | None = None) -> bool:
        try:
            await asyncio.wait_for(self._connected.wait(), timeout)
            return True
        except TimeoutError:
            return False

    async def send(self, method: str, *args: Any) -> None:
        """Fire-and-forget invocation."""
        await self._send_raw(encode({"type": 1, "target": method, "arguments": list(args)}))

    async def invoke(self, method: str, *args: Any, timeout: float = 10.0) -> Any:
        """Invocation that waits for the server's completion message."""
        inv_id = str(next(self._ids))
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[inv_id] = fut
        try:
            await self._send_raw(
                encode({"type": 1, "invocationId": inv_id, "target": method, "arguments": list(args)})
            )
            return await asyncio.wait_for(fut, timeout)
        finally:
            self._pending.pop(inv_id, None)

    async def stop(self) -> None:
        self._stopping.set()
        if self._ws is not None:
            await self._ws.close()

    async def run(self) -> None:
        """Connect and keep the connection alive until stop() is called."""
        dispatcher = asyncio.create_task(self._dispatch_loop(), name=f"{self.name}-dispatch")
        failures = 0
        try:
            while not self._stopping.is_set():
                try:
                    await self._session()
                    failures = 0
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - any failure triggers a reconnect
                    if self._stopping.is_set():
                        break
                    log.warning("%s hub connection lost: %s", self.name, exc)
                if self._stopping.is_set():
                    break
                delay = self._reconnect_delays[min(failures, len(self._reconnect_delays) - 1)]
                failures += 1
                log.info("%s hub reconnecting in %ss", self.name, delay)
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._stopping.wait(), delay)
        finally:
            dispatcher.cancel()

    # ------------------------------------------------------------- internals

    async def _send_raw(self, text: str) -> None:
        if self._ws is None:
            raise HubClosedError(f"{self.name} hub is not connected")
        await self._ws.send(text)

    async def _session(self) -> None:
        token = await self._token_provider()
        ws_url = hub_ws_url(self.url, token)
        async with connect(
            ws_url, open_timeout=self._connect_timeout, ping_interval=None, max_size=None
        ) as ws:
            self._ws = ws
            await ws.send(HANDSHAKE)
            first = await asyncio.wait_for(ws.recv(), self._connect_timeout)
            records = decode(first)
            if not records or records[0].get("error"):
                raise HubClosedError(f"handshake rejected: {records[0].get('error') if records else 'empty'}")
            for rec in records[1:]:
                self._handle(rec)

            self._connected.set()
            log.info("%s hub connected", self.name)
            for method, args in self._subscriptions:
                await self.send(method, *args)
            # Run connect callbacks (e.g. a REST reconcile) without blocking the read loop.
            asyncio.ensure_future(self._fire(self._on_connected))

            pinger = asyncio.create_task(self._ping_loop(ws))
            try:
                async for frame in ws:
                    for rec in decode(frame):
                        self._handle(rec)
            except websockets.ConnectionClosed as exc:
                if not self._stopping.is_set():
                    raise HubClosedError(f"socket closed ({exc.code})") from exc
            finally:
                pinger.cancel()
                self._ws = None
                self._connected.clear()
                for fut in self._pending.values():
                    if not fut.done():
                        fut.set_exception(HubClosedError("connection closed"))
                await self._fire(self._on_disconnected)
        if not self._stopping.is_set():
            raise HubClosedError("socket closed by server")

    async def _ping_loop(self, ws: Any) -> None:
        while True:
            await asyncio.sleep(self._ping_interval)
            try:
                await ws.send(PING)
            except websockets.ConnectionClosed:
                return

    def _handle(self, rec: dict) -> None:
        kind = rec.get("type")
        if kind == 1:
            self._queue.put_nowait((str(rec.get("target", "")), rec.get("arguments") or []))
        elif kind == 3:
            fut = self._pending.get(str(rec.get("invocationId")))
            if fut and not fut.done():
                if rec.get("error"):
                    fut.set_exception(HubClosedError(str(rec["error"])))
                else:
                    fut.set_result(rec.get("result"))
        elif kind == 7:
            raise HubClosedError(f"server closed connection: {rec.get('error') or 'no reason'}")

    async def _dispatch_loop(self) -> None:
        """Run handlers sequentially, in arrival order, off the socket-reading task."""
        while True:
            target, args = await self._queue.get()
            for handler in self._handlers.get(target.lower(), []):
                try:
                    result = handler(*args)
                    if inspect.isawaitable(result):
                        await result
                except Exception:  # noqa: BLE001 - a bad handler must not kill the stream
                    log.exception("%s hub handler for %s failed", self.name, target)

    async def _fire(self, callbacks: list[Callable[[], Any]]) -> None:
        for cb in callbacks:
            try:
                result = cb()
                if inspect.isawaitable(result):
                    await result
            except Exception:  # noqa: BLE001
                log.exception("%s hub callback failed", self.name)
