"""A close that TopstepX refuses: the position is certainly still open, so it keeps its stop and its target."""

from topstep_bot.api.rest import ProjectXError
from topstep_bot.execution import TradeState
from topstep_bot.models import OrderSide, OrderType

from .conftest import run
from .test_stress_faults import setup, stops, tick


def targets(broker):
    return [o for o in broker.orders.values() if o.status.is_working and o.type == OrderType.LIMIT]


def refuse_the_next_close(broker):
    close = broker.close_position

    async def refused(contract_id):
        broker.close_position = close
        raise ProjectXError("/api/Position/closeContract failed (code 5): refused", 5, {"success": False})

    broker.close_position = refused


def test_a_refused_close_keeps_the_stop_and_the_target_until_a_close_goes_through(mnq):
    async def go():
        broker, om, closed, now = setup(mnq)
        events = []
        om.on_event = lambda level, message: events.append((level, message))
        await tick(broker, now, 100.0)
        t = await om.enter(OrderSide.BUY, 1, 95.0, 110.0, "t", ref_price=100.0)
        await broker.drain()
        refuse_the_next_close(broker)
        await om.exit("done")
        await broker.drain()
        assert broker.position == 1 and t.state == TradeState.EXITING and t.close_refused
        assert len(stops(broker, OrderSide.SELL)) == 1 and len(targets(broker)) == 1  # both legs still manage it
        assert any(level == "critical" and "REFUSED" in message for level, message in events)
        await tick(broker, now, 100.0, seconds=3)
        await om.reconcile()  # the retried close goes through: the kept target goes with it
        await broker.drain()
        assert broker.position == 0 and om.trade is None and closed[-1] is t
        assert stops(broker, OrderSide.SELL) == [] and targets(broker) == []
    run(go())


def test_flatten_with_a_refused_close_does_not_cancel_the_target(mnq):
    async def go():
        broker, om, closed, now = setup(mnq)
        await tick(broker, now, 100.0)
        t = await om.enter(OrderSide.BUY, 1, 95.0, 110.0, "t", ref_price=100.0)
        await broker.drain()
        refuse_the_next_close(broker)
        await om.flatten_all("stop pressed")  # what the bot's Stop does
        await broker.drain()
        assert broker.position == 1 and t.close_refused
        assert len(stops(broker, OrderSide.SELL)) == 1 and len(targets(broker)) == 1
    run(go())
