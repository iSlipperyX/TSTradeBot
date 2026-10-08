"""A tiny local-only HTTP server (no dependencies), shared by the dashboard and the bot's API.

Security model (both servers bind to 127.0.0.1 only):
  * The Host header must be 127.0.0.1/localhost - blocks DNS-rebinding attacks.
  * State-changing requests (and, for the bot API, every request) need a random per-run token in
    the X-Token header. Other websites in your browser can't read the token or send the header.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlsplit

log = logging.getLogger(__name__)

ALLOWED_HOSTS = ("127.0.0.1", "localhost")
MAX_BODY = 64_000
REASONS = {200: "OK", 400: "Bad Request", 403: "Forbidden", 404: "Not Found", 500: "Server Error", 503: "Unavailable"}


class HttpError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


@dataclass
class Request:
    method: str
    path: str
    query: dict[str, str]
    headers: dict[str, str]
    body: bytes = b""
    param: str = ""  # the part of the path matched by a trailing "*" in the route

    def json(self) -> dict:
        if not self.body:
            return {}
        try:
            data = json.loads(self.body)
        except ValueError:
            raise HttpError(400, "bad JSON body") from None
        if not isinstance(data, dict):
            raise HttpError(400, "expected a JSON object")
        return data


@dataclass
class Response:
    status: int = 200
    body: bytes = b""
    content_type: str = "application/json"
    headers: dict[str, str] = field(default_factory=dict)


def json_response(data: Any, status: int = 200) -> Response:
    return Response(status, json.dumps(data, default=str).encode("utf-8"))


def html_response(text: str) -> Response:
    return Response(200, text.encode("utf-8"), "text/html; charset=utf-8")


Handler = Callable[[Request], Awaitable[Response | dict | str] | Response | dict | str]


class HttpServer:
    def __init__(
        self,
        host: str,
        port: int,
        routes: list[tuple[str, str, Handler]],
        *,
        token: str | None = None,
        token_for_reads: bool = False,
        name: str = "http",
    ):
        """routes: (method, path, handler); a path ending in "*" matches any suffix (in request.param)."""
        self.host = host
        self.port = port
        self.routes = routes
        self.token = token
        self.token_for_reads = token_for_reads
        self.name = name
        self._server: asyncio.base_events.Server | None = None

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}/"

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, self.host, self.port)
        self.port = self._server.sockets[0].getsockname()[1]  # real port when 0 was requested
        log.info("%s listening on %s", self.name, self.url)

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    def _route(self, method: str, path: str) -> tuple[Handler, str] | None:
        for m, pattern, handler in self.routes:
            if m != method:
                continue
            if pattern.endswith("*") and path.startswith(pattern[:-1]):
                return handler, path[len(pattern) - 1:]
            if path == pattern:
                return handler, ""
        return None

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            raw = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=10)
            lines = raw.decode("latin-1").split("\r\n")
            method, target, _ = lines[0].split(" ", 2)
            headers = {}
            for line in lines[1:]:
                if ":" in line:
                    k, v = line.split(":", 1)
                    headers[k.strip().lower()] = v.strip()
            url = urlsplit(target)
            req = Request(method, url.path, {k: v[-1] for k, v in parse_qs(url.query).items()}, headers)
            response = await self._dispatch(req, reader)
        except (TimeoutError, asyncio.IncompleteReadError, ConnectionError, ValueError):
            writer.close()
            return
        try:
            await self._write(writer, response)
        except ConnectionError:
            pass
        finally:
            writer.close()

    async def _dispatch(self, req: Request, reader: asyncio.StreamReader) -> Response:
        if req.headers.get("host", "").rsplit(":", 1)[0] not in ALLOWED_HOSTS:
            return json_response({"ok": False, "message": "forbidden host"}, 403)
        if self.token and (req.method != "GET" or self.token_for_reads) and req.headers.get("x-token") != self.token:
            return json_response({"ok": False, "message": "bad token"}, 403)
        found = self._route(req.method, req.path)
        if found is None:
            return json_response({"ok": False, "message": "not found"}, 404)
        handler, req.param = found
        length = min(int(req.headers.get("content-length", "0") or 0), MAX_BODY)
        if length:
            req.body = await asyncio.wait_for(reader.readexactly(length), timeout=10)
        try:
            result = handler(req)
            if inspect.isawaitable(result):
                result = await result
        except HttpError as exc:
            return json_response({"ok": False, "message": str(exc)}, exc.status)
        except (ValueError, RuntimeError) as exc:  # user-facing problems: validation, "not running", ...
            return json_response({"ok": False, "message": str(exc)})
        except Exception:  # noqa: BLE001
            log.exception("%s: request %s %s failed", self.name, req.method, req.path)
            return json_response({"ok": False, "message": "internal error - see logs/errors.log"}, 500)
        if isinstance(result, Response):
            return result
        if isinstance(result, str) or result is None:
            return json_response({"ok": True, "message": result})
        return json_response({"ok": True, **result})

    @staticmethod
    async def _write(writer: asyncio.StreamWriter, r: Response) -> None:
        extra = "".join(f"{k}: {v}\r\n" for k, v in r.headers.items())
        head = (
            f"HTTP/1.1 {r.status} {REASONS.get(r.status, 'OK')}\r\nContent-Type: {r.content_type}\r\n"
            f"Content-Length: {len(r.body)}\r\nCache-Control: no-store\r\nX-Frame-Options: DENY\r\n"
            f"X-Content-Type-Options: nosniff\r\n{extra}Connection: close\r\n\r\n"
        )
        writer.write(head.encode("latin-1") + r.body)
        await writer.drain()
