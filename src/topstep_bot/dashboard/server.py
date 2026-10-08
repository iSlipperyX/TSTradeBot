"""Tiny local web dashboard (no extra dependencies).

Listens on 127.0.0.1 only. Control actions require a random per-run token that is
embedded in the page, sent back as an X-Token header, and checked together with the
Host header - so other websites open in your browser cannot trigger actions.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
from collections.abc import Callable
from importlib import resources

log = logging.getLogger(__name__)

ALLOWED_HOSTS = ("127.0.0.1", "localhost")


class DashboardServer:
    def __init__(
        self,
        host: str,
        port: int,
        snapshot: Callable[[], dict],
        actions: dict[str, Callable[[], object]],
    ):
        self.host = host
        self.port = port
        self.snapshot = snapshot
        self.actions = actions
        self.token = secrets.token_urlsafe(24)
        self._server: asyncio.base_events.Server | None = None
        self._page = resources.files("topstep_bot.dashboard").joinpath("index.html").read_text(encoding="utf-8")

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}/"

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, self.host, self.port)
        log.info("Dashboard running at %s", self.url)

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=5)
            lines = request.decode("latin-1").split("\r\n")
            method, path, _ = lines[0].split(" ", 2)
            headers = {}
            for line in lines[1:]:
                if ":" in line:
                    k, v = line.split(":", 1)
                    headers[k.strip().lower()] = v.strip()
            host = headers.get("host", "").rsplit(":", 1)[0]
            if host not in ALLOWED_HOSTS:
                await self._send(writer, 403, "text/plain", b"forbidden")
                return
            if method == "GET" and path in ("/", "/index.html"):
                body = self._page.replace("__TOKEN__", self.token).encode("utf-8")
                await self._send(writer, 200, "text/html; charset=utf-8", body)
            elif method == "GET" and path == "/api/status":
                body = json.dumps(self.snapshot(), default=str).encode("utf-8")
                await self._send(writer, 200, "application/json", body)
            elif method == "POST" and path.startswith("/api/"):
                if headers.get("x-token") != self.token:
                    await self._send(writer, 403, "text/plain", b"bad token")
                    return
                action = self.actions.get(path.removeprefix("/api/"))
                if action is None:
                    await self._send(writer, 404, "text/plain", b"unknown action")
                    return
                result = action()
                if asyncio.iscoroutine(result):
                    await result
                await self._send(writer, 200, "application/json", b'{"ok":true}')
            else:
                await self._send(writer, 404, "text/plain", b"not found")
        except (asyncio.TimeoutError, asyncio.IncompleteReadError, ConnectionError, ValueError):
            pass
        except Exception:  # noqa: BLE001
            log.exception("Dashboard request failed")
        finally:
            writer.close()

    @staticmethod
    async def _send(writer: asyncio.StreamWriter, status: int, ctype: str, body: bytes) -> None:
        reason = {200: "OK", 403: "Forbidden", 404: "Not Found"}.get(status, "OK")
        head = (
            f"HTTP/1.1 {status} {reason}\r\nContent-Type: {ctype}\r\nContent-Length: {len(body)}\r\n"
            "Cache-Control: no-store\r\nX-Frame-Options: DENY\r\nConnection: close\r\n\r\n"
        )
        writer.write(head.encode("latin-1") + body)
        await writer.drain()
