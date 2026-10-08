"""The trading bot's private local API, used by the controller (dashboard + Telegram).

Started only when the controller launches the bot: it passes a port and a random token in the
environment. Every request needs the token, and the server only listens on 127.0.0.1.

  GET  /status          full bot snapshot (plus log health)
  POST /action/<name>   run a BotActions action (pause, set_setting, take_idea, ...)
"""

from __future__ import annotations

import os
from collections.abc import Callable

from topstep_bot.control import BotActions
from topstep_bot.logging_setup import stats
from topstep_bot.web import HttpServer, Request

ENV_PORT = "TOPSTEP_BOT_API_PORT"
ENV_TOKEN = "TOPSTEP_BOT_API_TOKEN"


def build_worker_api(actions: BotActions, snapshot: Callable[[], dict], port: int, token: str) -> HttpServer:
    def status(_: Request) -> dict:
        return {"bot": snapshot(), "log": stats.snapshot()}

    async def action(req: Request) -> dict | str:
        return await actions.handle(req.param, req.json())

    return HttpServer(
        "127.0.0.1", port, [("GET", "/status", status), ("POST", "/action/*", action)],
        token=token, token_for_reads=True, name="bot API",
    )


async def start_worker_api(actions: BotActions, snapshot: Callable[[], dict]) -> HttpServer | None:
    """Start the API if the controller asked for it (environment variables), else do nothing."""
    port, token = os.environ.get(ENV_PORT), os.environ.get(ENV_TOKEN)
    if not port or not token:
        return None
    server = build_worker_api(actions, snapshot, int(port), token)
    await server.start()
    return server
