"""Telegram end to end: the real controller and the real trading bot process (`topstep-bot run`),
against a fake TopstepX (REST + realtime hubs) and a fake Telegram Bot API, all over real sockets.

Regression test for "the bot closes after Telegram messages": every command and quick button must
leave the same bot process running; only a confirmed /stop stops it, and even then the controller
and Telegram stay online so /startbot can bring it back.
"""

import asyncio
import contextlib
import json
import logging
import os
import time
from pathlib import Path

import httpx
import pytest
import websockets
import yaml

from topstep_bot import controller as controller_mod
from topstep_bot.config import Secrets, load_config
from topstep_bot.controller import Controller
from topstep_bot.web import HttpServer, Request, Response

from .conftest import run
from .test_live_runner import FakeTopstepX

TOKEN = "123456:TEST"
CHAT = 555
OWNER = 42
SRC = str(Path(__file__).resolve().parents[1] / "src")


class FakeTelegramServer:
    """Just enough of the Telegram Bot API, including long polling, served over HTTP."""

    def __init__(self):
        self.updates: list[dict] = []
        self.replies: list[dict] = []  # sendMessage and editMessageText, in order
        self.next_message_id = 1000
        self.changed = asyncio.Condition()

    async def handle(self, req: Request) -> Response:
        method, body = req.param, req.json()
        result: object = True
        if method == "getMe":
            result = {"id": 1, "username": "test_bot"}
        elif method == "getUpdates":
            result = await self._updates(body)
        elif method in ("sendMessage", "editMessageText"):
            if method == "sendMessage":
                self.next_message_id += 1
                result = {"message_id": self.next_message_id, "chat": {"id": CHAT}}
            async with self.changed:
                self.replies.append({**body, "method": method, "message_id": body.get("message_id", self.next_message_id)})
                self.changed.notify_all()
        return Response(200, json.dumps({"ok": True, "result": result}).encode())

    async def _updates(self, body: dict) -> list[dict]:
        offset = body.get("offset")
        if offset == -1:
            return self.updates[-1:]
        deadline = time.monotonic() + min(float(body.get("timeout", 0)), 2)
        async with self.changed:
            while True:
                pending = [u for u in self.updates if offset is None or u["update_id"] >= offset]
                if pending or time.monotonic() >= deadline:
                    return pending
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self.changed.wait(), deadline - time.monotonic())

    async def _push(self, update: dict, answers: int = 1) -> str:
        """Deliver an update and wait for the bot's answer(s) to it; returns the last one."""
        async with self.changed:
            seen = len(self.replies)
            update["update_id"] = len(self.updates) + 1
            self.updates.append(update)
            self.changed.notify_all()
            await asyncio.wait_for(self.changed.wait_for(lambda: len(self.replies) >= seen + answers), 60)
            return self.replies[-1]["text"]

    async def text(self, text: str, answers: int = 1) -> str:
        return await self._push({"message": {"message_id": 1, "chat": {"id": CHAT}, "from": {"id": OWNER, "username": "me"},
                                             "text": text}}, answers)

    async def tap(self, data: str, message_id: int = 1) -> str:
        return await self._push({"callback_query": {"id": "cb", "data": data, "from": {"id": OWNER, "username": "me"},
                                                    "message": {"message_id": message_id, "chat": {"id": CHAT}}}})

    async def confirm(self, action: str) -> str:
        """Tap 'Yes' on the confirmation question the bot just sent."""
        return await self.tap(f"yes:{action}", self.replies[-1]["message_id"])


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # the bot looks for a KILL file in its working folder
    monkeypatch.setenv("PYTHONPATH", SRC + os.pathsep + os.environ.get("PYTHONPATH", ""))
    monkeypatch.setenv("TOPSTEPX_USERNAME", "u")
    monkeypatch.setenv("TOPSTEPX_API_KEY", "k")
    for var in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "DISCORD_WEBHOOK_URL"):
        monkeypatch.delenv(var, raising=False)  # the bot process must not reach the real Telegram
    monkeypatch.setattr(controller_mod, "notify", lambda *a: None)
    return tmp_path


def test_telegram_commands_never_close_the_bot(workdir, caplog):
    async def go():
        fake = FakeTopstepX()
        tg = FakeTelegramServer()

        def topstepx(req: Request) -> Response:
            r = fake.handler(httpx.Request("POST", f"http://topstepx{req.path}", content=req.body))
            return Response(r.status_code, r.content)

        async with websockets.serve(fake.hub, "127.0.0.1", 0) as hubs:
            hub = f"http://127.0.0.1:{hubs.sockets[0].getsockname()[1]}/hubs"
            api = HttpServer("127.0.0.1", 0, [("POST", "/api/*", topstepx)], name="fake TopstepX")
            telegram = HttpServer("127.0.0.1", 0, [("POST", f"/bot{TOKEN}/*", tg.handle)], name="fake Telegram")
            await api.start()
            await telegram.start()
            cfg_path = workdir / "config.yaml"
            cfg_path.write_text(yaml.safe_dump({
                "mode": "paper",
                "api": {"base_url": f"http://127.0.0.1:{api.port}", "user_hub_url": f"{hub}/user",
                        "market_hub_url": f"{hub}/market"},
                "news": {"enabled": False},
                "knowledge": {"auto_train": False, "history_days": 30},
                "service": {"daily_restart_time": "off", "check_in_time": "off"},
                "dashboard": {"open_browser": False},
            }))
            ctl = Controller(load_config(cfg_path), Secrets(username="u", api_key="k", telegram_bot_token=TOKEN, telegram_chat_id=str(CHAT)),
                             mode="paper", config_path=str(cfg_path), port=0, poll_seconds=0.2)
            ctl.telegram_api = f"http://127.0.0.1:{telegram.port}"
            controller = asyncio.create_task(ctl.run(open_browser=False))

            async def running(timeout=60):
                end = time.monotonic() + timeout
                while not (ctl.bot.state == "running" and ctl.bot.running):
                    assert time.monotonic() < end, f"bot not running: {ctl.bot.state}, last exit {ctl.bot.last_exit}"
                    await asyncio.sleep(0.1)
                return ctl.bot.proc.pid

            try:
                pid = await running()
                assert "online" in tg.replies[0]["text"]

                async def still_up(reply: str, expected: str) -> None:
                    assert expected in reply, reply
                    await asyncio.sleep(0.3)  # give a dying process the chance to show it
                    assert ctl.bot.running and ctl.bot.state == "running" and ctl.bot.proc.pid == pid, \
                        f"after {reply!r}: {ctl.bot.state}, last exit {ctl.bot.last_exit}"

                # Everything a user can type or tap that isn't a confirmed stop/restart.
                for text, expected in [("/start", "Balance"), ("/status", "Balance"), ("hello", "/help"),
                                       ("/help", "/startbot"), ("/pause", "Paused"), ("/resume", "Resumed"),
                                       ("/ideas", "ecommendations"), ("/trades", "trades"), ("/log", "activity"),
                                       ("/settings", "risk_per_trade"), ("/knowledge", "nowledge"),
                                       ("/STATUS@test_bot", "Balance"), ("/nonsense", "/flatten")]:
                    await still_up(await tg.text(text), expected)
                await still_up(await tg.text("/train", answers=2), "trained")  # "Training..." then the result
                for button in ("status", "pause", "resume", "ideas", "trades", "knowledge", "settings"):
                    await still_up(await tg.tap(f"cmd:{button}"), "")
                await still_up(await tg.tap("cmd:stop"), "Stop the bot?")  # old messages still show a Stop button
                await still_up(await tg.tap("no", tg.replies[-1]["message_id"]), "Cancelled")
                for command in ("flatten", "restart", "stop"):  # asked, then cancelled
                    await tg.text(f"/{command}")
                    await still_up(await tg.tap("no", tg.replies[-1]["message_id"]), "Cancelled")
                await tg.text("/set risk 120")
                await still_up(await tg.confirm("set"), "120")
                await tg.text("/reset")
                await still_up(await tg.confirm("reset"), "config.yaml")
                await still_up(await tg.tap("yes:stop", 12345), "expired")  # never asked: nothing happens

                # A confirmed /stop stops the bot - and only the bot: Telegram keeps answering.
                await tg.text("/stop")
                assert "Bot stopped" in await tg.confirm("stop")
                assert ctl.bot.state == "stopped" and not ctl.bot.running and not controller.done()
                assert "STOPPED" in await tg.text("/status")
                await tg.text("/startbot")
                await tg.confirm("startbot")
                restarted = await running()
                assert restarted != pid and "Balance" in await tg.text("/status")
            finally:
                controller.cancel()  # what Ctrl+C / closing the window does
                with contextlib.suppress(asyncio.CancelledError):
                    await controller
                await api.stop()
                await telegram.stop()
            assert "closed on the PC" in tg.replies[-1]["text"]
            assert not ctl.bot.running

    with caplog.at_level(logging.WARNING):
        run(go())
    problems = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
    assert not problems, problems  # e.g. "Background task 'telegram' failed" at shutdown
