from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from topstep_bot.instruments import offline_contract
from topstep_bot.models import Bar

UTC = timezone.utc
CT = ZoneInfo("America/Chicago")


def run(coro):
    return asyncio.run(coro)


def ct(y: int, m: int, d: int, hh: int, mm: int) -> datetime:
    """A Chicago wall-clock time as an aware UTC datetime."""
    return datetime(y, m, d, hh, mm, tzinfo=CT).astimezone(UTC)


def bar(ts: datetime, o: float, h: float, l: float, c: float, v: float = 100) -> Bar:  # noqa: E741
    return Bar(ts, o, h, l, c, v)


@pytest.fixture(autouse=True)
def offline_research(monkeypatch):
    """The controller's research collector reaches the internet: keep it idle in tests (research tests call it directly)."""
    from topstep_bot.research import Research

    async def idle(self, check_every: float = 60.0) -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(Research, "run", idle)
    # ...and so does the every-market history refresh; the nightly lab run would simulate in the background
    from topstep_bot.lab_service import LabService
    from topstep_bot.market_data import MarketData

    async def idle_loop(self, check_every: float = 0.0) -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(MarketData, "run", idle_loop)
    monkeypatch.setattr(LabService, "_nightly_loop", idle_loop)


@pytest.fixture
def mnq():
    return offline_contract("MNQ")


@pytest.fixture
def es():
    return offline_contract("ES")


@pytest.fixture
def restore_logging():
    import logging

    root = logging.getLogger()
    saved = root.handlers[:], root.level
    yield
    for handler in root.handlers[:]:
        if handler not in saved[0]:
            root.removeHandler(handler)
            handler.close()
    root.handlers[:] = saved[0]
    root.setLevel(saved[1])
