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
