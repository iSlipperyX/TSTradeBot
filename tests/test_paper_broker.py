from datetime import timedelta

import pytest

from topstep_bot.broker.paper import PaperBroker
from topstep_bot.models import OrderSide, OrderStatus, OrderType

from .conftest import bar, ct, run

T0 = ct(2026, 3, 3, 9, 0)


def test_market_order_fills_at_next_open_with_slippage(mnq):
    async def go():
        b = PaperBroker(mnq, 50_000, slippage_ticks=1, fees_round_turn=1.0)
        oid = await b.place_order(mnq.id, OrderType.MARKET, OrderSide.BUY, 2)
        assert b.orders[oid].status == OrderStatus.OPEN
        await b.on_bar(bar(T0, 100, 101, 99, 100.5))
        assert b.orders[oid].filled_price == 100.25
        assert b.position == 2 and b.avg_price == 100.25
        assert b.balance == pytest.approx(50_000 - 1.0)  # $0.50/side x 2 contracts
    run(go())


def test_round_trip_pnl_and_fees(mnq):
    async def go():
        b = PaperBroker(mnq, 50_000, slippage_ticks=0, fees_round_turn=1.0)
        await b.place_order(mnq.id, OrderType.MARKET, OrderSide.SELL, 1)
        await b.on_bar(bar(T0, 100, 100, 100, 100))
        await b.place_order(mnq.id, OrderType.MARKET, OrderSide.BUY, 1)
        await b.on_bar(bar(T0 + timedelta(minutes=5), 90, 90, 90, 90))
        assert b.position == 0
        assert b.balance == pytest.approx(50_000 + 10 * 2.0 - 1.0)  # 10 points x $2/point
        assert b.fills[-1].pnl == pytest.approx(20.0)
    run(go())


def test_stop_fills_at_stop_or_gap_open(mnq):
    async def go():
        b = PaperBroker(mnq, 50_000, slippage_ticks=1)
        b.position, b.avg_price = 1, 100.0
        s1 = await b.place_order(mnq.id, OrderType.STOP, OrderSide.SELL, 1, stop_price=95.0)
        await b.on_bar(bar(T0, 99, 99.5, 94, 96))
        assert b.orders[s1].filled_price == 94.75  # stop minus 1 tick slippage
        b.position, b.avg_price = 1, 100.0
        s2 = await b.place_order(mnq.id, OrderType.STOP, OrderSide.SELL, 1, stop_price=95.0)
        await b.on_bar(bar(T0 + timedelta(minutes=5), 90, 91, 89, 90))
        assert b.orders[s2].filled_price == 89.75  # gapped through: open minus slippage
    run(go())


def test_limit_needs_trade_through(mnq):
    async def go():
        b = PaperBroker(mnq, 50_000)
        b.position, b.avg_price = 1, 100.0
        lim = await b.place_order(mnq.id, OrderType.LIMIT, OrderSide.SELL, 1, limit_price=105.0)
        await b.on_bar(bar(T0, 101, 105, 100, 104))  # touch only
        assert b.orders[lim].status == OrderStatus.OPEN
        await b.on_bar(bar(T0 + timedelta(minutes=5), 104, 105.25, 103, 105))
        assert b.orders[lim].filled_price == 105.0
    run(go())


def test_events_delivered_in_order_via_drain(mnq):
    async def go():
        b = PaperBroker(mnq, 50_000)
        seen = []
        b.on_order = lambda o: seen.append(("order", o.status.name))
        b.on_fill = lambda f: seen.append(("fill", f.price))
        b.on_position = lambda p: seen.append(("position", p.size))
        await b.place_order(mnq.id, OrderType.MARKET, OrderSide.BUY, 1)
        assert seen == []  # nothing delivered until drained
        await b.on_bar(bar(T0, 100, 100, 100, 100))
        assert [k for k, _ in seen] == ["order", "order", "fill", "position"]
    run(go())


def test_live_paper_fills_at_quote(mnq):
    async def go():
        b = PaperBroker(mnq, 50_000, slippage_ticks=0, live=True)
        await b.on_price(T0, 100.0, bid=99.75, ask=100.25)
        oid = await b.place_order(mnq.id, OrderType.MARKET, OrderSide.BUY, 1)
        assert b.orders[oid].filled_price == 100.25  # buys at the ask
        await b.drain()
    run(go())
