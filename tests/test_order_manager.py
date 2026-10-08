from datetime import timedelta

import pytest

from topstep_bot.broker.paper import PaperBroker
from topstep_bot.execution import OrderManager, TradeState
from topstep_bot.models import OrderSide, OrderStatus, OrderType, Position

from .conftest import bar, ct, run

T0 = ct(2026, 3, 3, 9, 0)


def setup(contract, **kw):
    now = [T0]
    broker = PaperBroker(contract, 50_000, slippage_ticks=0, fees_round_turn=1.0)
    om = OrderManager(broker, contract, fees_round_turn=1.0, clock=lambda: now[0], **kw)
    closed = []

    async def on_closed(t):
        closed.append(t)

    om.on_trade_closed = on_closed
    return broker, om, closed, now


def working(broker, type_):
    return [o for o in broker.orders.values() if o.type == type_ and o.status.is_working]


def test_entry_places_stop_and_target_then_target_cancels_stop(mnq):
    async def go():
        broker, om, closed, _ = setup(mnq)
        t = await om.enter(OrderSide.BUY, 2, 95.0, 110.0, "test", ref_price=100.0)
        assert t.state == TradeState.PENDING
        await broker.on_bar(bar(T0, 100, 101, 99, 100))
        assert t.state == TradeState.OPEN and t.entry_price == 100.0
        assert len(working(broker, OrderType.STOP)) == 1
        assert len(working(broker, OrderType.LIMIT)) == 1
        await broker.on_bar(bar(T0 + timedelta(minutes=5), 101, 111, 100, 109))
        assert working(broker, OrderType.STOP) == []  # OCO: stop cancelled
        assert len(closed) == 1 and closed[0].exit_reason == "profit target"
        assert closed[0].net_pnl == pytest.approx(10 * 2 * 2 - 2.0)
        assert om.trade is None and om.position == 0
    run(go())


def test_stop_hit_first_when_both_in_same_bar(mnq):
    async def go():
        broker, om, closed, _ = setup(mnq)
        await om.enter(OrderSide.BUY, 1, 95.0, 110.0, "test", ref_price=100.0)
        await broker.on_bar(bar(T0, 100, 100, 100, 100))
        await broker.on_bar(bar(T0 + timedelta(minutes=5), 100, 111, 94, 100))
        assert closed[0].exit_reason == "stop loss"
        assert closed[0].r_multiple() == pytest.approx(-1.0)
    run(go())


def test_exit_closes_position_and_cancels_protection(mnq):
    async def go():
        broker, om, closed, _ = setup(mnq)
        await om.enter(OrderSide.SELL, 1, 105.0, None, "test", ref_price=100.0)
        await broker.on_bar(bar(T0, 100, 100, 100, 100))
        await om.exit("manual")
        await broker.on_bar(bar(T0 + timedelta(minutes=5), 98, 98, 98, 98))
        assert closed and closed[0].exit_price == 98 and closed[0].exit_reason == "manual"
        assert working(broker, OrderType.STOP) == []
        assert broker.position == 0
    run(go())


def test_stop_only_moves_in_favour(mnq):
    async def go():
        broker, om, _, _ = setup(mnq)
        t = await om.enter(OrderSide.BUY, 1, 95.0, None, "test", ref_price=100.0)
        await broker.on_bar(bar(T0, 100, 100, 100, 100))
        assert await om.update_stop(97.0)
        assert not await om.update_stop(96.0)
        await broker.drain()
        assert t.stop_price == 97.0
        assert working(broker, OrderType.STOP)[0].stop_price == 97.0
    run(go())


def test_failed_stop_placement_flattens(mnq):
    async def go():
        broker, om, closed, _ = setup(mnq)
        real_place = broker.place_order

        async def failing(contract_id, type_, side, size, **kw):
            if type_ == OrderType.STOP:
                raise RuntimeError("exchange rejected")
            return await real_place(contract_id, type_, side, size, **kw)

        broker.place_order = failing
        await om.enter(OrderSide.BUY, 1, 95.0, None, "test", ref_price=100.0)
        await broker.on_bar(bar(T0, 100, 100, 100, 100))
        await broker.on_bar(bar(T0 + timedelta(minutes=5), 100, 100, 100, 100))
        assert broker.position == 0
        assert closed and "protective stop" in closed[0].exit_reason
    run(go())


def test_externally_cancelled_stop_is_replaced(mnq):
    async def go():
        broker, om, _, _ = setup(mnq)
        t = await om.enter(OrderSide.BUY, 1, 95.0, None, "test", ref_price=100.0)
        await broker.on_bar(bar(T0, 100, 100, 100, 100))
        first = t.stop_order_id
        await broker.cancel_order(first)
        await broker.drain()
        assert t.stop_order_id not in (None, first)
        assert broker.orders[t.stop_order_id].status == OrderStatus.OPEN
    run(go())


def test_reconcile_flattens_orphan_position(mnq):
    async def go():
        broker, om, _, _ = setup(mnq, orphan_policy="flatten")
        broker.position, broker.avg_price = 3, 100.0
        broker.live = True
        broker.last_price = 100.0
        await om.reconcile()
        await broker.drain()
        assert broker.position == 0
    run(go())


def test_reconcile_adopts_orphan_with_stop(mnq):
    async def go():
        broker, om, _, _ = setup(mnq, orphan_policy="adopt")
        broker.position, broker.avg_price = -2, 100.0
        await om.reconcile()
        await broker.drain()
        assert om.trade is not None and om.trade.side == OrderSide.SELL
        stops = working(broker, OrderType.STOP)
        assert len(stops) == 1 and stops[0].side == OrderSide.BUY and stops[0].stop_price > 100
    run(go())


def test_position_closed_outside_bot_finalizes_trade(mnq):
    async def go():
        broker, om, closed, _ = setup(mnq)
        await om.enter(OrderSide.BUY, 1, 95.0, None, "test", ref_price=100.0)
        await broker.on_bar(bar(T0, 100, 100, 100, 100))
        om.last_price = 101.0
        await om.handle_position(Position(1, mnq.id, 0, 0.0))
        assert closed and closed[0].exit_reason == "position closed outside the bot"
        assert working(broker, OrderType.STOP) == []
    run(go())


def test_only_one_trade_at_a_time(mnq):
    async def go():
        _, om, _, _ = setup(mnq)
        assert await om.enter(OrderSide.BUY, 1, 95.0, None, "a", ref_price=100.0)
        assert await om.enter(OrderSide.SELL, 1, 105.0, None, "b", ref_price=100.0) is None
    run(go())


def test_second_exit_request_never_reverses_the_position(mnq):
    """Regression: a strategy exit and the session flatten in the same bar sent two closing orders."""
    async def go():
        broker, om, closed, _ = setup(mnq)
        await om.enter(OrderSide.SELL, 5, 105.0, None, "test", ref_price=100.0)
        await broker.on_bar(bar(T0, 100, 100, 100, 100))
        await om.exit("strategy exit")
        await om.flatten_all("session flatten time")
        await om.exit("again")
        await broker.close_position(mnq.id)  # even a direct duplicate close request
        await broker.on_bar(bar(T0 + timedelta(minutes=5), 99, 99, 99, 99))
        assert broker.position == 0 and om.position == 0 and om.trade is None
        assert len(closed) == 1 and closed[0].exit_reason == "strategy exit"
        assert sum(1 for o in broker.orders.values() if o.type == OrderType.MARKET and o.status == OrderStatus.FILLED) == 2
    run(go())
