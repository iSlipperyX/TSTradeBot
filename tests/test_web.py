"""The small web server behind the dashboard, and the pages it serves."""

import asyncio
import re
import shutil
import subprocess
from importlib import resources

import pytest

from topstep_bot.phone_access import SIGN_IN_PAGE
from topstep_bot.web import MAX_BODY, HttpServer

from .conftest import run


async def raw(port: int, request: bytes) -> tuple[int, bytes]:
    """Send bytes exactly as given (httpx can't send a hand-made chunked body) and read the answer."""
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(request)
    await writer.drain()
    answer = await reader.read()
    writer.close()
    head, _, body = answer.partition(b"\r\n\r\n")
    return int(head.split()[1]), body


def with_echo(body):
    async def go():
        server = HttpServer("127.0.0.1", 0, [("POST", "/echo", lambda r: {"got": r.json()})], name="test")
        await server.start()
        try:
            await body(server.port)
        finally:
            await server.stop()
    run(go())


def post(body: bytes, *headers: str) -> bytes:
    return ("POST /echo HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Type: application/json\r\n"
            + "".join(f"{h}\r\n" for h in headers) + "\r\n").encode() + body


def test_reads_a_chunked_body():
    """cloudflared may relay a phone's request in chunks, without a Content-Length."""
    async def body(port):
        chunked = b'7\r\n{"a": 1\r\n3;ext=1\r\n, "\r\n7\r\nb": [2]\r\n1\r\n}\r\n0\r\nX-Trailer: y\r\n\r\n'
        status, answer = await raw(port, post(chunked, "Transfer-Encoding: chunked"))
        assert status == 200 and b'"got": {"a": 1, "b": [2]}' in answer
        status, answer = await raw(port, post(b'{"a": 1}', "Content-Length: 8"))
        assert status == 200 and b'"got": {"a": 1}' in answer
    with_echo(body)


def test_refuses_bodies_that_are_too_big_or_broken():
    async def body(port):
        big = b"x" * (MAX_BODY + 1)
        assert (await raw(port, post(big, f"Content-Length: {len(big)}")))[0] == 400
        chunk = f"{len(big):x}\r\n".encode() + big + b"\r\n0\r\n\r\n"
        assert (await raw(port, post(chunk, "Transfer-Encoding: chunked")))[0] == 400
        assert (await raw(port, post(b"zz\r\n{}\r\n0\r\n\r\n", "Transfer-Encoding: chunked")))[0] == 400
        assert (await raw(port, post(b"{}", "Content-Length: two")))[0] == 400
    with_echo(body)


# ------------------------------------------------------------------------------ the pages

def page_scripts(name: str) -> list[str]:
    html = {"dashboard": resources.files("topstep_bot.dashboard").joinpath("index.html").read_text(encoding="utf-8"),
            "phone sign-in": SIGN_IN_PAGE}[name]
    return re.findall(r"<script>(.*?)</script>", html, re.S)


@pytest.mark.skipif(shutil.which("node") is None, reason="needs Node.js to parse JavaScript")
@pytest.mark.parametrize("name", ["dashboard", "phone sign-in"])
def test_page_scripts_parse(name, tmp_path):
    """One syntax error and the page shows nothing at all - on a phone, with no console to say why."""
    path = tmp_path / "page.js"
    # a page's scripts share one global scope: checked together, a name declared in two of them fails too
    path.write_text("\n;\n".join(page_scripts(name)), encoding="utf-8")
    result = subprocess.run(["node", "--check", str(path)], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr


def test_the_dashboard_reports_its_own_failures_from_a_script_of_its_own():
    """The error reporter runs first and alone, so it works even when the main script can't run at all."""
    reporter, main = page_scripts("dashboard")
    assert 'addEventListener("error"' in reporter and "/api/page-error" in reporter
    assert "const TOKEN" in main and "/api/page-error" not in main
