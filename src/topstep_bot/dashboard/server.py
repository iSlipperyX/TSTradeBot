"""Tiny local web dashboard (no extra dependencies).

Listens on 127.0.0.1 only. Control actions require a random per-run token that is
embedded in the page, sent back as an X-Token header, and checked together with the
Host header - so other websites open in your browser cannot trigger actions.

Actions receive the request's JSON body (a dict) and return a message string or a dict.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import secrets
from collections.abc import Callable
from importlib import resources
from typing import Any

log = logging.getLogger(__name__)

ALLOWED_HOSTS = ("127.0.0.1", "localhost")
MAX_BODY = 64_000


class DashboardServer:
    def __init__(
        self,
        host: str,
        port: int,
        snapshot: Callable[[], dict],
        actions: dict[str, Callable[[dict], Any]],
        log_stats: bool = False,
    ):
        self.host = host
        self.port = port
        self.snapshot = snapshot
        self.actions = actions
        self.log_stats = log_stats
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

    def status(self) -> dict:
        data = self.snapshot()
        if self.log_stats:
            from topstep_bot.logging_setup import stats

            data["log"] = stats.snapshot()
        return data

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
                await self._json(writer, 200, self.status())
            elif method == "POST" and path.startswith("/api/"):
                if headers.get("x-token") != self.token:
                    await self._send(writer, 403, "text/plain", b"bad token")
                    return
                action = self.actions.get(path.removeprefix("/api/"))
                if action is None:
                    await self._send(writer, 404, "text/plain", b"unknown action")
                    return
                length = min(int(headers.get("content-length", "0") or 0), MAX_BODY)
                raw = await asyncio.wait_for(reader.readexactly(length), timeout=5) if length else b""
                try:
                    payload = json.loads(raw) if raw else {}
                    if not isinstance(payload, dict):
                        raise ValueError("expected a JSON object")
                except ValueError:
                    await self._json(writer, 400, {"ok": False, "message": "bad request body"})
                    return
                try:
                    result = action(payload)
                    if inspect.isawaitable(result):
                        result = await result
                except (ValueError, RuntimeError) as exc:  # user-facing problems (validation etc.)
                    await self._json(writer, 200, {"ok": False, "message": str(exc)})
                    return
                body = result if isinstance(result, dict) else {"message": result}
                await self._json(writer, 200, {"ok": True, **body})
            else:
                await self._send(writer, 404, "text/plain", b"not found")
        except (asyncio.TimeoutError, asyncio.IncompleteReadError, ConnectionError, ValueError):
            pass
        except Exception:  # noqa: BLE001
            log.exception("Dashboard request failed")
            try:
                await self._json(writer, 500, {"ok": False, "message": "internal error - see logs/errors.log"})
            except Exception:  # noqa: BLE001
                pass
        finally:
            writer.close()

    async def _json(self, writer: asyncio.StreamWriter, status: int, data: dict) -> None:
        await self._send(writer, status, "application/json", json.dumps(data, default=str).encode("utf-8"))

    @staticmethod
    async def _send(writer: asyncio.StreamWriter, status: int, ctype: str, body: bytes) -> None:
        reason = {200: "OK", 400: "Bad Request", 403: "Forbidden", 404: "Not Found", 500: "Server Error"}.get(status, "OK")
        head = (
            f"HTTP/1.1 {status} {reason}\r\nContent-Type: {ctype}\r\nContent-Length: {len(body)}\r\n"
            "Cache-Control: no-store\r\nX-Frame-Options: DENY\r\nConnection: close\r\n\r\n"
        )
        writer.write(head.encode("latin-1") + body)
        await writer.drain()
