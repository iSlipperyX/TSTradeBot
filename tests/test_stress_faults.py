"""Stress tests: the order path when TopstepX times out, loses realtime messages or answers late.

The deterministic tests pin down each failure the chaos simulation found; the simulation itself
then runs thousands of steps with random failures and checks the safety invariants throughout.
"""

import contextlib
import random
from collections import Counter
from datetime import timedelta

import pytest

from topstep_bot.broker.paper import PaperBroker
from topstep_bot.execution import OrderManager, TradeState, may_have_reached_broker
from topstep_bot.models import Fill, OrderSide, OrderType, Position
from topstep_bot.risk.guards import OrderGuard

from .conftest import ct, run

T0 = ct(2026, 3, 3, 9, 0)


class FlakyBroker(PaperBroker):
    """A live paper broker whose requests can time out (before or after TopstepX acts on them) and whose
    realtime messages can be lost."""

    def __init__(self, contract, rng=None, fail=0.0, drop=0.0):
        super().__init__(contract, 50_000, slippage_ticks=0, fees_round_turn=1.0, live=True)
        self.rng, self.fail, self.drop = rng or random.Random(0), fail, drop
        self.timeout_after: set[str] = set()  # the next such request is carried out, but the answer is lost
        self.timeout_before: set[str] = set()  # the next such request never arrives
        self.lose: set[str] = set()  # lose every realtime message of these kinds ("Position", "Fill", "Order")
        self.errors = Counter()

    async def _emit(self, callback, value):
        if type(value).__name__ in self.lose or self.rng.random() < self.drop:
            return
        await super()._emit(callback, value)

    async def _call(self, name, coro_fn):
        if name in self.timeout_before or self.rng.random() < self.fail / 2:
            self.timeout_before.discard(name)
            self.errors[name] += 1
            raise TimeoutError(f"{name} timed out")
        result = await coro_fn()
        if name in self.timeout_after or self.rng.random() < self.fail / 2:
            self.timeout_after.discard(name)
            self.errors[name] += 1
            raise TimeoutError(f"{name} timed out after it was carried out")
        return result

    async def place_order(self, *a, **k):
        return await self._call("place", lambda: PaperBroker.place_order(self, *a, **k))

    async def cancel_order(self, order_id):
        return await self._call("cancel", lambda: PaperBroker.cancel_order(self, order_id))

    async def modify_order(self, order_id, **k):
        return await self._call("modify", lambda: PaperBroker.modify_order(self, order_id, **k))

    async def close_position(self, contract_id):
        return await self._call("close", lambda: PaperBroker.close_position(self, contract_id))

    async def positions(self):
        return await self._call("positions", lambda: PaperBroker.positions(self))

    async def open_orders(self):
        return await self._call("open_orders", lambda: PaperBroker.open_orders(self))


def setup(contract, **broker_kw):
    now = [T0]
    broker = FlakyBroker(contract, **broker_kw)
    om = OrderManager(broker, contract, fees_round_turn=1.0, clock=lambda: now[0], entry_timeout=5)
    om.guard = OrderGuard(lambda: 30, max_actions_per_minute=100_000, max_entries_per_day=100_000)
    closed = []

    async def on_closed(t):
        closed.append(t)

    om.on_trade_closed = on_closed
    return broker, om, closed, now


async def tick(broker, now, price, seconds=1):
    now[0] += timedelta(seconds=seconds)
    await broker.on_price(now[0], price, price - 0.25, price)
    await broker.drain()


def stops(broker, side):
    return [o for o in broker.orders.values() if o.status.is_working and o.type == OrderType.STOP and o.side == side]


def test_what_counts_as_an_order_that_may_have_gone_through():
    from topstep_bot.api.rest import ProjectXError

    assert may_have_reached_broker(TimeoutError()) and may_have_reached_broker(ConnectionError())
    assert may_have_reached_broker(ProjectXError("HTTP 502", 502))  # a server error: maybe
    assert not may_have_reached_broker(ProjectXError("rejected", 3, {"success": False}))  # TopstepX said no
    assert not may_have_reached_broker(ValueError("order size must be positive"))


def test_an_entry_that_timed_out_but_filled_is_protected_not_doubled(mnq):
    """The entry request timed out after TopstepX filled it, and the fill messages were lost too."""
    async def go():
        broker, om, closed, now = setup(mnq)
        await tick(broker, now, 100.0)
        broker.timeout_after.add("place")
        broker.lose = {"Position", "Fill"}
        t = await om.enter(OrderSide.BUY, 2, 95.0, None, "t", ref_price=100.0)
        assert t is not None and om.trade is t and not om.is_flat  # kept: it may have gone through
        assert broker.position == 2
        assert await om.enter(OrderSide.BUY, 2, 95.0, None, "again", ref_price=100.0) is None  # no doubling
        broker.lose = set()
        await om.reconcile()
        assert t.state == TradeState.OPEN and t.filled_size == 2
        assert [o.size for o in stops(broker, OrderSide.SELL)] == [2]  # and it is protected
        assert broker.position == 2
    run(go())


def test_an_entry_that_timed_out_and_never_arrived_is_abandoned(mnq):
    async def go():
        broker, om, closed, now = setup(mnq)
        await tick(broker, now, 100.0)
        broker.timeout_before.add("place")
        t = await om.enter(OrderSide.BUY, 1, 95.0, None, "t", ref_price=100.0)
        assert t is not None and t.state == TradeState.PENDING
        await om.reconcile()
        assert om.trade is t  # not yet: the order could still show up
        await tick(broker, now, 100.0, seconds=6)
        await om.reconcile()
        assert om.trade is None and om.is_flat and broker.position == 0 and closed == []
        assert await om.enter(OrderSide.BUY, 1, 95.0, None, "next", ref_price=100.0) is not None
    run(go())


def test_an_entry_the_broker_refuses_is_forgotten_at_once(mnq):
    async def go():
        broker, om, _, now = setup(mnq)
        await tick(broker, now, 100.0)
        assert await om.enter(OrderSide.BUY, 0, 95.0, None, "bad size", ref_price=100.0) is None  # ValueError
        assert om.trade is None and om.is_flat
    run(go())


def test_an_old_stop_whose_cancel_failed_never_fills_into_the_next_trade(mnq):
    async def go():
        broker, om, closed, now = setup(mnq)
        await tick(broker, now, 100.0)
        await om.enter(OrderSide.BUY, 1, 95.0, None, "first", ref_price=100.0)
        await broker.drain()
        old_stop = om.trade.stop_order_id
        broker.timeout_before.add("cancel")  # cancelling the stop after the exit times out
        await om.exit("done")
        await broker.drain()
        assert om.trade is None and closed and broker.position == 0
        assert broker.orders[old_stop].status.is_working  # still at TopstepX
        assert await om.enter(OrderSide.SELL, 1, 105.0, None, "next", ref_price=100.0) is None  # waits for it
        await om.reconcile()  # cancels the leftover
        assert not broker.orders[old_stop].status.is_working
        assert await om.enter(OrderSide.SELL, 1, 105.0, None, "next", ref_price=100.0) is not None
        await broker.drain()
        await tick(broker, now, 94.0)  # through the old stop's price: nothing happens to the new short
        assert broker.position == -1 and om.trade.side == OrderSide.SELL
    run(go())


def test_a_close_whose_position_update_was_lost_cancels_the_stop_from_the_fill(mnq):
    async def go():
        broker, om, closed, now = setup(mnq)
        await tick(broker, now, 100.0)
        await om.enter(OrderSide.BUY, 1, 95.0, None, "t", ref_price=100.0)
        await broker.drain()
        broker.lose = {"Position"}
        await om.exit("done")
        await broker.drain()
        assert broker.position == 0 and om.trade is None and closed  # finished from the close's fill
        assert stops(broker, OrderSide.SELL) == []  # the stop is gone...
        await tick(broker, now, 94.0)
        assert broker.position == 0  # ...so it can't reverse the trade into a short
    run(go())


def test_a_position_on_the_wrong_side_is_closed_not_protected(mnq):
    async def go():
        broker, om, closed, now = setup(mnq)
        await tick(broker, now, 100.0)
        t = await om.enter(OrderSide.BUY, 1, 95.0, None, "t", ref_price=100.0)
        await broker.drain()
        # Somehow the account ends up short 2 while the bot thinks it is long 1 (e.g. an old order filled).
        await PaperBroker.place_order(broker, mnq.id, OrderType.MARKET, OrderSide.SELL, 3)
        broker.lose = {"Position", "Fill", "Order"}
        await broker.drain()
        broker.lose = set()
        await om.reconcile()
        await broker.drain()
        assert broker.position == 0 and closed and closed[-1] is t
        assert stops(broker, OrderSide.SELL) == [] and stops(broker, OrderSide.BUY) == []
    run(go())


def test_a_fill_for_part_of_the_close_keeps_the_stop_even_if_reported_twice(mnq):
    async def go():
        broker, om, closed, now = setup(mnq)
        await tick(broker, now, 100.0)
        t = await om.enter(OrderSide.BUY, 2, 95.0, None, "t", ref_price=100.0)
        await broker.drain()
        half = Fill(999, 12345, mnq.id, OrderSide.SELL, 1, 100.0, now[0])
        await om.handle_fill(half)
        await om.handle_fill(half)  # the same execution reported twice is still only half
        assert om.trade is t and stops(broker, OrderSide.SELL)  # still protected
        await om.handle_position(Position(1, mnq.id, 2, 100.0))
        assert om.trade is t
    run(go())


# ---------------------------------------------------------------- the chaos simulation

async def chaos(seed: int, steps: int = 2000, max_size: int = 3) -> dict:
    """Random entries, exits, stop moves and flattens while 15% of requests time out (half of them
    after TopstepX carried them out) and 20% of realtime messages are lost. Reconcile runs every
    5 seconds, like a busy day with a bad connection."""
    rng = random.Random(seed)
    from topstep_bot.instruments import offline_contract

    c = offline_contract("MNQ")
    broker, om, closed, now = setup(c, rng=rng, fail=0.15, drop=0.2)
    price = 20_000.0
    worst = Counter()
    for i in range(steps):
        price = c.round_price(price + rng.choice([-1, 1]) * rng.choice([0.25, 0.5, 1, 2, 4]))
        await tick(broker, now, price)
        r = rng.random()
        try:
            if om.is_flat and r < 0.05:
                side = rng.choice([OrderSide.BUY, OrderSide.SELL])
                await om.enter(side, rng.randint(1, max_size), price - side.sign * rng.choice([5, 10, 20]),
                               price + side.sign * rng.choice([5, 10, 40]), "chaos", ref_price=price)
            elif r < 0.07:
                await om.exit("chaos exit")
            elif r < 0.08:
                await om.flatten_all("chaos flatten")
            elif r < 0.12 and om.trade and om.trade.state == TradeState.OPEN:
                await om.update_stop(price - om.trade.side.sign * 3)
        except TimeoutError:
            pass  # a request made outside the order manager's own error handling (e.g. listing orders)
        if i % 5 == 0:
            with contextlib.suppress(TimeoutError):
                await om.reconcile()
        await broker.drain()
        worst["position"] = max(worst["position"], abs(broker.position))
        if broker.position and om.trade and (broker.position > 0) != (om.trade.side == OrderSide.BUY):
            worst["wrong_side"] += 1
    broker.fail = broker.drop = 0  # the connection recovers
    for _ in range(3):
        await tick(broker, now, price, seconds=11)
        await om.reconcile()
    await om.flatten_all("end of test")
    for _ in range(3):
        await tick(broker, now, price, seconds=11)
        await om.reconcile()
    return {"max_position": worst["position"], "wrong_side": worst["wrong_side"], "final_position": broker.position,
            "working": [o.id for o in broker.orders.values() if o.status.is_working], "trade": om.trade,
            "closed": len(closed), "errors": sum(broker.errors.values())}


@pytest.mark.parametrize("seed", [*range(100), 30003, 30027, 30089, 30098, 30153, 30235, 31078, 31282, 31314])
def test_chaos_never_doubles_reverses_or_leaves_anything_behind(seed):
    result = run(chaos(seed))
    assert result["errors"] > 100 and result["closed"] > 20  # it really was a rough session
    assert result["max_position"] <= 3, result  # never more than one entry's size
    assert result["wrong_side"] == 0, result  # never a position opposite to the trade it belongs to
    assert result["final_position"] == 0 and result["working"] == [] and result["trade"] is None, result


# ---------------------------------------------------------------- a damaged or locked journal

def test_a_damaged_journal_is_set_aside_and_a_new_one_started(tmp_path):
    from topstep_bot.journal import Journal

    path = tmp_path / "journal_live.db"
    path.write_bytes(b"\x00" * 4096)  # e.g. a power cut, or a sync tool overwriting it
    journal, damaged = Journal.open_or_recover(path)
    assert damaged is not None and damaged.exists() and damaged.read_bytes() == b"\x00" * 4096
    journal.set_state("x", 1)
    assert journal.get_state("x") == 1
    journal.close()
    again, damaged = Journal.open_or_recover(path)  # a healthy journal is just opened
    assert damaged is None and again.get_state("x") == 1
    again.close()


def test_a_live_bot_with_a_damaged_journal_starts_paused_and_says_why(tmp_path):
    import asyncio

    import websockets

    from .test_live_runner import FakeTopstepX, make_runner

    async def go():
        fake = FakeTopstepX()
        (tmp_path / "journal_live.db").write_bytes(b"not a database at all, not even close" * 10)
        async with websockets.serve(fake.hub, "127.0.0.1", 0) as srv:
            runner = make_runner(fake, srv.sockets[0].getsockname()[1], "live", tmp_path)
            ready = asyncio.Event()

            async def on_ready(core):
                ready.set()

            task = asyncio.create_task(runner.run(on_ready))
            await asyncio.wait_for(ready.wait(), 30)
            core = runner.core
            assert core.risk.paused
            assert any(e["level"] == "critical" and "journal was damaged" in e["message"] for e in core.events)
            runner.controls.stop.set()
            await asyncio.wait_for(task, 20)
        assert list(tmp_path.glob("journal_live.db.damaged-*"))

    run(go())


def test_a_journal_that_cannot_be_written_never_stops_the_protective_flatten(mnq):
    """E.g. the journal is open in another program and SQLite answers 'database is locked'."""
    import sqlite3

    from topstep_bot.config import BotConfig
    from topstep_bot.factory import build_core
    from topstep_bot.journal import Journal

    class LockedJournal(Journal):
        def __init__(self):
            super().__init__(":memory:")

        def log_event(self, level, message):
            raise sqlite3.OperationalError("database is locked")

        def record_trade(self, *a, **k):
            raise sqlite3.OperationalError("database is locked")

        def record_day(self, *a, **k):
            raise sqlite3.OperationalError("database is locked")

    async def go():
        broker = FlakyBroker(mnq)
        now = [T0]
        core = build_core(BotConfig(), mnq, broker, clock=lambda: now[0], account_label="T", journal=LockedJournal())
        await core.begin_day(core.schedule.trading_day(T0), 50_000)
        await tick(broker, now, 100.0)
        await core.orders.enter(OrderSide.BUY, 1, 95.0, None, "t", ref_price=100.0)
        broker.timeout_before.add("place")  # the entry went through, the stop can't be placed...
        await broker.drain()
        await broker.drain()
        assert broker.position == 0  # ...so it was flattened, even though nothing could be journaled
        assert core.orders.trade is None and core.closed_trades
        day = core.current_day
        await core.roll_day_if_needed(now[0] + timedelta(days=1))  # and the next day still begins
        assert core.current_day != day and core.risk.trades_today == 0

    run(go())


# ---------------------------------------------------------------- TopstepX unreachable at startup

def test_a_bot_started_during_a_topstepx_outage_waits_for_it_instead_of_crashing(tmp_path, monkeypatch):
    import asyncio

    import httpx
    import websockets

    import topstep_bot.live as live
    from topstep_bot.api.rest import ProjectXError

    from .test_live_runner import FakeTopstepX, make_runner

    monkeypatch.setattr(live, "PREPARE_RETRY_FIRST", 1)
    assert live.unreachable(httpx.ConnectError("down")) and live.unreachable(ProjectXError("HTTP 503", 503))
    assert not live.unreachable(ProjectXError("Login failed (code 3): bad key", 3, {"success": False}))
    assert not live.unreachable(ValueError("a bug"))

    class Outage(FakeTopstepX):
        down = True

        def handler(self, request):
            if self.down:
                raise httpx.ConnectError("TopstepX is unreachable")
            return super().handler(request)

    async def go():
        fake = Outage()
        async with websockets.serve(fake.hub, "127.0.0.1", 0) as srv:
            runner = make_runner(fake, srv.sockets[0].getsockname()[1], "live", tmp_path)
            ready = asyncio.Event()

            async def on_ready(core):
                ready.set()

            task = asyncio.create_task(runner.run(on_ready))
            await asyncio.sleep(2.5)
            assert not task.done() and not ready.is_set()  # still waiting, not crashed
            fake.down = False
            await asyncio.wait_for(ready.wait(), 30)  # starts as soon as TopstepX answers
            runner.controls.stop.set()
            await asyncio.wait_for(task, 20)

            fake.down = True  # and a stop while it waits ends it at once
            runner = make_runner(fake, srv.sockets[0].getsockname()[1], "live", tmp_path / "again")
            task = asyncio.create_task(runner.run(on_ready))
            await asyncio.sleep(1.5)
            runner.controls.stop.set()
            await asyncio.wait_for(task, 5)

    run(go())


def test_an_outage_is_one_log_line_and_a_bug_keeps_its_traceback(caplog):
    import logging

    import httpx

    from topstep_bot import live

    caplog.set_level(logging.WARNING, logger="topstep_bot.live")
    for exc in (httpx.ConnectError("[Errno 11001] getaddrinfo failed"), KeyError("bug")):
        try:
            raise exc
        except Exception as caught:  # noqa: BLE001
            live.log_failure("Clock loop error", caught)
    outage, bug = caplog.records[-2:]
    assert outage.levelname == "WARNING" and outage.exc_info is None
    assert outage.getMessage() == "Clock loop error: [Errno 11001] getaddrinfo failed (connection problem; trying again)"
    assert bug.levelname == "ERROR" and bug.exc_info is not None
