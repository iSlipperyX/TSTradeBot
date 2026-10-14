"""The order manager when TopstepX answers late or its reports are lost.

Each test is one failure the chaos simulation found (tests/test_stress_faults.py), pinned down so it
stays fixed: an order the bot placed but never got the id of, and a close whose every report was lost.
"""

import pytest

from topstep_bot.broker.paper import PaperBroker
from topstep_bot.execution import TradeState
from topstep_bot.models import OrderSide, OrderType, Position

from .conftest import run
from .test_stress_faults import setup, stops, tick


def targets(broker, side):
    return [o for o in broker.orders.values() if o.status.is_working and o.type == OrderType.LIMIT and o.side == side]


def test_a_stop_placed_without_an_answer_is_adopted_and_cancelled_with_its_trade(mnq):
    """The stop request timed out after TopstepX placed it; its own order update says it is working."""
    async def go():
        broker, om, closed, now = setup(mnq)
        await tick(broker, now, 100.0)
        t = await om.enter(OrderSide.BUY, 1, 95.0, None, "t", ref_price=100.0)
        broker.timeout_after.add("place")  # the stop the fill places: carried out, answer lost -> the bot flattens
        await broker.drain()
        assert closed and closed[-1] is t and broker.position == 0
        assert stops(broker, OrderSide.SELL) == []  # the stop it never got an id for was cancelled too
        await tick(broker, now, 94.0)
        assert broker.position == 0  # so it can't open a short on the flat account
    run(go())


def test_a_target_placed_without_an_answer_blocks_the_next_entry_until_it_is_found(mnq):
    async def go():
        broker, om, closed, now = setup(mnq)
        await tick(broker, now, 100.0)
        t = await om.enter(OrderSide.BUY, 1, 95.0, 110.0, "t", ref_price=100.0)
        broker.lose = {"Order"}  # the target's own order update is lost too
        place = broker.place_order

        async def answer_lost_for_the_target(*a, **k):
            if (k.get("tag") or "").endswith("-T"):
                broker.timeout_after.add("place")
            return await place(*a, **k)

        broker.place_order = answer_lost_for_the_target
        await broker.drain()
        assert t.state == TradeState.OPEN and t.stop_order_id is not None and t.target_order_id is None
        assert len(targets(broker, OrderSide.SELL)) == 1  # working at TopstepX, unknown to the bot
        await tick(broker, now, 94.0)  # the stop fills: the trade ends, the unknown target is left working
        assert om.trade is None and broker.position == 0 and closed[-1] is t
        broker.lose = set()
        assert await om.enter(OrderSide.SELL, 1, 99.0, None, "next", ref_price=94.0) is None  # waits for it
        await om.reconcile()  # lists the orders: the old target is cancelled
        assert targets(broker, OrderSide.SELL) == []
        assert await om.enter(OrderSide.SELL, 1, 99.0, None, "next", ref_price=94.0) is not None
    run(go())


def test_reconcile_adopts_a_stop_it_never_got_the_id_of_and_cancels_a_second_one(mnq):
    async def go():
        broker, om, closed, now = setup(mnq)
        await tick(broker, now, 100.0)
        t = await om.enter(OrderSide.BUY, 2, 95.0, None, "t", ref_price=100.0)
        await broker.drain()
        first = t.stop_order_id
        # A second stop with this trade's tag (e.g. a re-place whose answer was lost), and the bot lost the id
        # of the first one.
        broker.lose = {"Order"}
        second = await PaperBroker.place_order(broker, mnq.id, OrderType.STOP, OrderSide.SELL, 2, stop_price=95.0,
                                               tag=f"{t.tag}-S2")
        await broker.drain()
        broker.lose = set()
        t.stop_order_id = None
        await om.reconcile()
        working = stops(broker, OrderSide.SELL)
        assert len(working) == 1 and t.stop_order_id == working[0].id and working[0].id in (first, second)
        await om.exit("done")
        await broker.drain()
        assert om.trade is None and stops(broker, OrderSide.SELL) == [] and broker.position == 0
    run(go())


def test_a_close_reported_only_by_its_order_update_ends_the_trade_and_cancels_the_stop(mnq):
    async def go():
        broker, om, closed, now = setup(mnq)
        await tick(broker, now, 100.0)
        t = await om.enter(OrderSide.BUY, 2, 95.0, 110.0, "t", ref_price=100.0)
        await broker.drain()
        broker.lose = {"Position", "Fill"}  # only the close's order update gets through
        await om.exit("done")
        await broker.drain()
        assert om.trade is None and closed[-1] is t and t.exit_price == 100.0 - 0.25
        assert stops(broker, OrderSide.SELL) == [] and targets(broker, OrderSide.SELL) == []
        await tick(broker, now, 94.0)
        assert broker.position == 0
    run(go())


def test_a_stop_that_fills_after_a_close_whose_reports_were_lost_is_closed_at_once(mnq):
    """The close went through but its order update, fill and flat position were all lost. The trailed stop
    then fills on the flat account and reverses it: the bot closes that at once, not at the next check."""
    async def go():
        broker, om, closed, now = setup(mnq)
        await tick(broker, now, 100.0)
        t = await om.enter(OrderSide.BUY, 1, 95.0, None, "t", ref_price=100.0)
        await broker.drain()
        assert await om.update_stop(99.0)
        broker.lose = {"Position", "Fill", "Order"}
        await om.exit("done")
        await broker.drain()
        assert t.state == TradeState.EXITING and broker.position == 0  # the bot doesn't know it is flat
        broker.lose = set()
        await tick(broker, now, 98.0, seconds=0.5)  # the kept stop fills: short 1
        assert broker.position == 0  # closed again straight from the stop's report
        assert om.trade is None and closed[-1] is t
        assert stops(broker, OrderSide.SELL) == [] and stops(broker, OrderSide.BUY) == []
    run(go())


def test_reconcile_acts_on_the_positions_when_listing_the_orders_fails(mnq):
    async def go():
        broker, om, closed, now = setup(mnq)
        await tick(broker, now, 100.0)
        t = await om.enter(OrderSide.BUY, 1, 95.0, 110.0, "t", ref_price=100.0)
        await broker.drain()
        broker.lose = {"Position", "Fill", "Order"}
        await PaperBroker.close_position(broker, mnq.id)  # closed by hand; every report lost
        await broker.drain()
        broker.lose = set()
        broker.timeout_before.add("open_orders")
        with pytest.raises(TimeoutError):
            await om.reconcile()  # the failure is still reported...
        assert om.trade is None and closed[-1] is t  # ...but the flat position was acted on
        assert stops(broker, OrderSide.SELL) == [] and targets(broker, OrderSide.SELL) == []
    run(go())


def test_a_position_on_the_wrong_side_while_exiting_is_closed_from_the_report(mnq):
    async def go():
        broker, om, closed, now = setup(mnq)
        await tick(broker, now, 100.0)
        t = await om.enter(OrderSide.BUY, 1, 95.0, None, "t", ref_price=100.0)
        await broker.drain()
        broker.lose = {"Position", "Fill", "Order"}
        await om.exit("done")
        await PaperBroker.place_order(broker, mnq.id, OrderType.MARKET, OrderSide.SELL, 1)  # an old order fills
        await broker.drain()
        assert broker.position == -1 and t.state == TradeState.EXITING
        broker.lose = set()
        await tick(broker, now, 100.0, seconds=2)
        await om.handle_position(Position(1, mnq.id, -1, 100.0))  # TopstepX's report of it
        await broker.drain()
        assert broker.position == 0
    run(go())


def test_an_entry_that_fills_bigger_than_ordered_raises_the_alarm(mnq):
    async def go():
        broker, om, closed, now = setup(mnq)
        events = []
        om.on_event = lambda level, message: events.append((level, message))
        await tick(broker, now, 100.0)
        broker.lose = {"Position", "Fill", "Order"}
        await om.enter(OrderSide.BUY, 1, 95.0, None, "t", ref_price=100.0)
        await PaperBroker.place_order(broker, mnq.id, OrderType.MARKET, OrderSide.BUY, 1)  # a stray order
        await broker.drain()
        broker.lose = set()
        await om.reconcile()
        assert any(level == "critical" and "only 1 were ordered" in message for level, message in events)
    run(go())
