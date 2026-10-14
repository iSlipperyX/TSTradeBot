"""Partial exits, reconciles that overlap, and an exiting trade with no stop at the broker.

The paper broker never partly fills an order and never yields to the event loop on its own, so the
partial state is set by hand and the reconcile race is made real with a yield after the orders answer.
"""

import asyncio
from copy import copy

from topstep_bot.broker.paper import PaperBroker
from topstep_bot.execution import TradeState
from topstep_bot.models import OrderSide, OrderType, Position

from .conftest import run
from .test_stress_faults import setup, stops, tick


def test_two_reconciles_started_together_place_one_stop(mnq):
    async def go():
        broker, om, closed, now = setup(mnq)
        await tick(broker, now, 100.0)
        t = await om.enter(OrderSide.BUY, 1, 95.0, None, "t", ref_price=100.0)
        await broker.drain()
        broker.lose = {"Order"}
        await PaperBroker.cancel_order(broker, t.stop_order_id)  # the stop is gone; its update was lost
        await broker.drain()
        broker.lose = set()
        listed = broker.open_orders

        async def slow_listing():
            orders = await listed()
            await asyncio.sleep(0)  # let the other reconcile run meanwhile, as a real request would
            return orders

        broker.open_orders = slow_listing
        placed = []
        place = broker.place_order

        async def counted(*a, **k):
            placed.append(k.get("tag"))
            return await place(*a, **k)

        broker.place_order = counted
        broker.lose = {"Order"}  # no update of the new stop reaches the bot before the second one acts
        await asyncio.gather(om.reconcile(), om.reconcile())  # as at start-up: the hub's and the lane's
        broker.lose = set()
        await broker.drain()
        assert len(stops(broker, OrderSide.SELL)) == 1
        assert len(placed) == 1  # the second did not act on the same snapshot (cancel the new stop, place another)
    run(go())


def test_a_target_that_fills_part_of_the_position_shrinks_the_stop(mnq):
    async def go():
        broker, om, closed, now = setup(mnq)
        await tick(broker, now, 100.0)
        t = await om.enter(OrderSide.BUY, 2, 95.0, 110.0, "t", ref_price=100.0)
        await broker.drain()
        target = broker.orders[t.target_order_id]
        # TopstepX fills 1 of the target's 2 contracts (the paper broker never does this by itself).
        target.fill_volume, target.filled_price = 1, 110.0
        broker.position = 1
        await om.handle_order(copy(target))
        await om.handle_position(Position(1, mnq.id, 2, 100.0))  # a report from before the fill
        await om.handle_position(Position(1, mnq.id, 1, 100.0))
        await broker.drain()
        assert om.trade is t and t.state == TradeState.OPEN and t.filled_size == 1 and t.exited == 1
        assert [o.size for o in stops(broker, OrderSide.SELL)] == [1]  # the stop now covers what is left
        assert broker.orders[t.target_order_id].size == 2  # the target's working rest already fits
        await om.reconcile()
        assert [o.size for o in stops(broker, OrderSide.SELL)] == [1]  # and reconcile leaves it so
        await tick(broker, now, 94.0)  # the stop fills: flat, not reversed into a short
        assert broker.position == 0 and om.trade is None and closed[-1] is t
        # The trade is booked on everything entered: one contract at the target, one at the stop.
        assert t.filled_size == 2 and t.exit_price == (110.0 + 94.0) / 2
        assert t.gross_pnl == mnq.pnl(100.0, t.exit_price, 2) and t.fees == 2.0
    run(go())


def test_two_closes_by_hand_of_one_contract_leave_a_protected_trade_of_one(mnq):
    """A naive shrink compares the closes with what is open now, not with what was entered: the second
    close of 1 then 'covers' the 2 still open and ends a 3-lot trade with one contract open and no stop."""
    async def go():
        broker, om, closed, now = setup(mnq)
        await tick(broker, now, 100.0)
        t = await om.enter(OrderSide.BUY, 3, 95.0, None, "t", ref_price=100.0)
        await broker.drain()
        for left in (2, 1):
            await PaperBroker.place_order(broker, mnq.id, OrderType.MARKET, OrderSide.SELL, 1)
            await broker.drain()
            assert broker.position == left and om.trade is t and t.filled_size == left
        assert [o.size for o in stops(broker, OrderSide.SELL)] == [1]
        await tick(broker, now, 94.0)
        assert broker.position == 0 and om.trade is None and closed[-1] is t and t.filled_size == 3
    run(go())


def test_an_exiting_trade_with_no_stop_at_the_broker_is_protected_while_the_close_is_retried(mnq):
    async def go():
        broker, om, closed, now = setup(mnq)
        events = []
        om.on_event = lambda level, message: events.append((level, message))
        await tick(broker, now, 100.0)
        t = await om.enter(OrderSide.BUY, 1, 95.0, None, "t", ref_price=100.0)
        broker.timeout_before.update({"place", "close"})  # the stop never arrives; the flatten's close fails too
        await broker.drain()  # the stop's placement fails -> flatten -> the close fails
        assert t.state == TradeState.EXITING and t.stop_order_id is None and broker.position == 1
        assert any("NO protective stop" in message for _, message in events)
        assert stops(broker, OrderSide.SELL) == []

        broker.timeout_before.add("close")  # the first retry fails as well: the insurance stop stays
        await tick(broker, now, 100.0, seconds=3)
        await om.reconcile()
        await broker.drain()
        assert broker.position == 1 and len(stops(broker, OrderSide.SELL)) == 1
        assert any(level == "critical" and "NO protective stop working" in message for level, message in events)

        await tick(broker, now, 100.0, seconds=3)  # the next retry goes through
        await om.reconcile()
        await broker.drain()
        assert broker.position == 0 and stops(broker, OrderSide.SELL) == []  # nothing left on the flat account
        assert om.trade is None and closed[-1] is t
    run(go())
