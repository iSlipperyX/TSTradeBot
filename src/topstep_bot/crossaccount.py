"""Rules across accounts: never hold opposite positions in related markets on two accounts.

Topstep prohibits hedging across accounts - for example long MNQ on one Combine while short NQ
(or, by most readings, short MES) on another. When several accounts trade different strategies
on the same or related markets, their calls will sometimes point opposite ways; this guard lets
the first account trade and makes the other skip that call.

Each account's bot is its own process, so they coordinate through small files:

    data/positions/<account>.json   {"side": 1 | -1, "group": ..., "mode": ..., "since": ...}

An entry is claimed under a lock file before the order is sent (two bots can't both claim
opposite sides at the same moment), and released once that account is flat again. Paper and
live accounts are checked separately: paper positions aren't real, but paper mode behaves the
same as live so what you test is what you get.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from topstep_bot.market_knowledge import product_for

log = logging.getLogger(__name__)
UTC = timezone.utc

LOCK_STALE_SECONDS = 10.0
CLAIM_GRACE_SECONDS = 60.0  # an entry order may take a moment to show up as a position


def market_key(symbol: str, scope: str) -> str:
    """The market an instrument belongs to for the hedge check ('NQ' for MNQ, or its group).

    Mini and micro of the same market count as one product; related markets form a group
    (see market_knowledge.py).
    """
    product = product_for(symbol)
    if product is None:
        return symbol.upper()
    return product.group if scope == "correlated" else product.root


class PositionBook:
    """This account's view of what every account holds (see the module docstring)."""

    def __init__(self, folder: Path, account: str, mode: str, symbol: str, scope: str = "correlated",
                 clock: Callable[[], float] = time.time, lane: str | None = None):
        self.folder = Path(folder)
        self.account = account
        self.mode = mode
        self.symbol = symbol.upper()
        self.scope = scope
        self.key = market_key(symbol, scope)
        self.clock = clock
        self.folder.mkdir(parents=True, exist_ok=True)
        # With several markets in one account, each market (lane) keeps its own file: the guard then also stops
        # this account from holding opposite sides of related markets at once.
        self.lane = lane
        self._file = self.folder / (f"{account}.json" if lane is None else f"{account}~{lane}.json")
        self._lock = self.folder / ".lock"
        self._side: int | None = None  # unknown until the first sync, which rewrites any file left from before
        self._claimed_at = 0.0

    @property
    def enabled(self) -> bool:
        return self.scope != "off"

    # ---------------------------------------------------------------- reading

    def others(self) -> list[dict]:
        out = []
        for path in self.folder.glob("*.json"):
            if path == self._file:
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if data.get("side") and data.get("mode") == self.mode:
                out.append(data)
        return out

    def conflict(self, side: int) -> str | None:
        """Why an entry on ``side`` (+1 long / -1 short) would be a cross-account hedge, else None."""
        if not self.enabled:
            return None
        for o in self.others():
            if o.get("side") == -side and market_key(o.get("symbol", ""), self.scope) == self.key:
                held = "long" if o["side"] > 0 else "short"
                if o.get("account") == self.account:
                    return (f"this account is already {held} {o.get('symbol')}: the opposite side of a related market "
                            "at the same time would be a hedge, so the bot doesn't")
                return (f"account {o.get('account')} is {held} {o.get('symbol')} - opposite positions across accounts "
                        "count as hedging, which Topstep prohibits")
        return None

    # ---------------------------------------------------------------- writing

    def claim(self, side: int) -> str | None:
        """Reserve ``side`` for this account before sending an entry. Returns why not, or None if OK."""
        if not self.enabled:
            return None
        with self._locked():
            reason = self.conflict(side)
            if reason is None:
                self._claimed_at = self.clock()
                self._write(side)
            return reason

    def sync(self, side: int) -> None:
        """Record what this account actually holds now (called every second by the bot)."""
        if side == self._side:
            return
        if side == 0 and self.clock() - self._claimed_at < CLAIM_GRACE_SECONDS:
            return  # the entry we just claimed may not have filled yet
        with self._locked():
            self._write(side)

    def release(self) -> None:
        with contextlib.suppress(OSError):
            self._file.unlink()
        self._side = 0

    @property
    def side(self) -> int:
        return self._side or 0

    def _write(self, side: int) -> None:
        self._side = side
        if side == 0:
            with contextlib.suppress(OSError):
                self._file.unlink()
            return
        data = {"account": self.account, "mode": self.mode, "symbol": self.symbol, "side": side, "pid": os.getpid(),
                "since": datetime.now(UTC).isoformat(timespec="seconds")}
        tmp = self._file.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(data), encoding="utf-8")
            os.replace(tmp, self._file)
        except OSError as exc:
            log.warning("Could not record this account's position for the hedge guard: %s", exc)

    @contextlib.contextmanager
    def _locked(self):
        deadline = time.monotonic() + 3
        fd = None
        while fd is None:
            try:
                fd = os.open(self._lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                with contextlib.suppress(OSError):
                    if time.time() - self._lock.stat().st_mtime > LOCK_STALE_SECONDS:
                        self._lock.unlink()  # left behind by a bot that was killed mid-check
                        continue
                if time.monotonic() > deadline:
                    log.warning("Hedge-guard lock busy for 3s - checking without it")
                    break
                time.sleep(0.02)
        try:
            yield
        finally:
            if fd is not None:
                os.close(fd)
                with contextlib.suppress(OSError):
                    self._lock.unlink()
