"""Safety regressions: immediate-or-cancel entries, close-only exits, the backtest fill order,
reconcile races, partial fills, restart state and background-loop crashes."""

import asyncio
from datetime import timedelta

import pytest

from topstep_bot.broker.paper import PaperBroker
from topstep_bot.config import BotConfig, Secrets
from topstep_bot.execution import OrderManager, TradeState
from topstep_bot.factory import build_core
from topstep_bot.live import Controls, LiveRunner
from topstep_bot.models import Fill, OrderSide, OrderStatus, OrderType, Position

from .conftest import bar, ct, run

T0 = ct(2026, 3, 3, 9, 0)


def manager(contract, **kw):
    broker = PaperBroker(contract, 50_000, slippage_ticks=0, fees_round_turn=1.0)
    om = OrderManager(broker, contract, fees_round_turn=1.0, clock=lambda: T0, **kw)
    closed = []

    async def on_closed(t):
        closed.append(t)

    om.on_trade_closed = on_closed
    return broker, om, closed


def working(broker, type_):
    return [o for o in broker.orders.values() if o.type == type_ and o.status.is_working]


# ------------------------------------------------- immediate-or-cancel entries

def test_ioc_entry_fills_at_open_never_worse_than_limit(mnq):
    async def go():
        broker, om, _ = manager(mnq)
        t = await om.enter(OrderSide.BUY, 1, 95.0, None, "t", ref_price=100.0, limit_price=101.0)
        await broker.on_bar(bar(T0, 100.5, 102, 100, 101))
        assert t.state == TradeState.OPEN and t.entry_price == 100.5  # filled at the open, inside the limit
    run(go())


def test_ioc_entry_is_cancelled_when_price_has_moved_away(mnq):
    async def go():
        broker, om, closed = manager(mnq)
        await om.enter(OrderSide.BUY, 1, 95.0, None, "t", ref_price=100.0, limit_price=101.0)
        await broker.on_bar(bar(T0, 103, 104, 99, 100))  # opens beyond the limit, trades back later
        assert om.trade is None and broker.position == 0  # never rests, even though 101 traded later
        assert all(o.status == OrderStatus.CANCELLED for o in broker.orders.values())
        assert closed == []
    run(go())


def test_live_paper_ioc_limit_cancels_unless_marketable(mnq):
    async def go():
        broker = PaperBroker(mnq, 50_000, slippage_ticks=0, live=True)
        await broker.on_price(T0, 100.0, 99.75, 100.0)
        missed = await broker.place_order(mnq.id, OrderType.LIMIT, OrderSide.BUY, 1, limit_price=99.5, ioc=True)
        assert broker.orders[missed].status == OrderStatus.CANCELLED and broker.position == 0
        hit = await broker.place_order(mnq.id, OrderType.LIMIT, OrderSide.BUY, 1, limit_price=100.5, ioc=True)
        assert broker.orders[hit].status == OrderStatus.FILLED and broker.orders[hit].filled_price == 100.0
    run(go())


def test_backtest_stop_sees_the_rest_of_the_entry_bar(mnq):
    """An entry filled at a bar's open must have its stop checked against that same bar.

    Regression: the stop used to be placed after the bar's stop checks had run, so a spike
    straight through it went unnoticed (a $2,958 loss instead of about $80 on real NQ data)."""
    async def go():
        broker, om, closed = manager(mnq)
        await om.enter(OrderSide.SELL, 15, 102.0, None, "t", ref_price=100.0, limit_price=99.5)
        await broker.on_bar(bar(T0, 100.0, 125.0, 99.75, 124.0))  # spikes 25 points against the short
        assert om.trade is None and broker.position == 0
        assert closed and closed[0].exit_reason == "stop loss"
        assert closed[0].exit_price == 102.0  # filled at the stop, not 25 points later
    run(go())


def test_backtest_stop_before_target_when_entry_bar_hits_both(mnq):
    async def go():
        broker, om, closed = manager(mnq)
        await om.enter(OrderSide.BUY, 1, 98.0, 103.0, "t", ref_price=100.0, limit_price=100.5)
        await broker.on_bar(bar(T0, 100.0, 104.0, 97.0, 101.0))
        assert closed and closed[0].exit_reason == "stop loss"  # conservative: assume the stop came first
    run(go())


# ------------------------------------------------------------ close-only exits

def test_exit_and_stop_in_the_same_gap_close_exactly_once(mnq):
    async def go():
        broker, om, closed = manager(mnq)
        await om.enter(OrderSide.BUY, 2, 95.0, None, "t", ref_price=100.0)
        await broker.on_bar(bar(T0, 100, 100.5, 99.5, 100))
        await om.exit("strategy exit")  # close queued; the stop stays until the position is flat
        await broker.on_bar(bar(T0 + timedelta(minutes=5), 94.0, 94.5, 93.0, 94.0))  # gaps through the stop
        await broker.drain()
        assert broker.position == 0  # never reversed into a short
        filled = sum(o.fill_volume for o in broker.orders.values() if o.side == OrderSide.SELL)
        assert filled == 2 and len(closed) == 1
        assert working(broker, OrderType.STOP) == []
    run(go())


def test_close_request_when_already_stopped_out_is_dropped(mnq):
    async def go():
        broker = PaperBroker(mnq, 50_000, slippage_ticks=0)
        broker.position, broker.avg_price = 2, 100.0
        await broker.close_position(mnq.id)  # in flight...
        broker.position = 0  # ...but the position was closed by a stop first
        await broker.on_bar(bar(T0, 94.0, 94.5, 93.0, 94.0))
        assert broker.position == 0
        assert all(o.status == OrderStatus.CANCELLED for o in broker.orders.values())
    run(go())


def test_close_position_accounts_for_closes_already_in_flight(mnq):
    async def go():
        broker = PaperBroker(mnq, 50_000)
        broker.position, broker.avg_price = 3, 100.0
        await broker.close_position(mnq.id)
        await broker.close_position(mnq.id)  # duplicate request
        markets = [o for o in broker.orders.values() if o.type == OrderType.MARKET and o.status.is_working]
        assert len(markets) == 1 and markets[0].size == 3
    run(go())


def test_close_only_order_shrinks_to_what_is_left(mnq):
    async def go():
        broker = PaperBroker(mnq, 50_000, slippage_ticks=0)
        broker.position, broker.avg_price = 2, 100.0
        await broker.close_position(mnq.id)
        broker.position = 1  # one contract left by other means before the close executes
        await broker.on_bar(bar(T0, 100, 100, 100, 100))
        assert broker.position == 0
    run(go())


# ------------------------------------------------------------- reconcile races

def test_reconcile_discards_a_snapshot_that_went_stale_while_fetching(mnq):
    """A fill that lands while reconcile is fetching must not be mistaken for 'position closed
    outside the bot' (which used to cancel the brand-new trade's stop)."""
    async def go():
        broker, om, closed = manager(mnq)
        t = await om.enter(OrderSide.BUY, 1, 95.0, None, "t", ref_price=100.0)
        stale_positions = await broker.positions()  # flat: the entry hasn't filled yet
        real_positions = broker.positions

        async def positions_with_fill_mid_fetch():
            await broker.on_bar(bar(T0, 100, 100.5, 99.5, 100))  # entry fills, stop placed, during the fetch
            return stale_positions

        broker.positions = positions_with_fill_mid_fetch
        await om.reconcile()
        broker.positions = real_positions
        assert t.state == TradeState.OPEN and closed == []
        assert len(working(broker, OrderType.STOP)) == 1  # its stop is intact, and not duplicated
        await om.reconcile()  # a fresh, consistent snapshot changes nothing
        assert len(working(broker, OrderType.STOP)) == 1 and om.trade is t
    run(go())


def test_reconcile_resizes_protection_to_the_position(mnq):
    async def go():
        broker, om, _ = manager(mnq)
        t = await om.enter(OrderSide.BUY, 2, 95.0, 110.0, "t", ref_price=100.0)
        await broker.on_bar(bar(T0, 100, 100.5, 99.5, 100))
        stop = working(broker, OrderType.STOP)[0]
        broker.orders[stop.id].size = 1  # e.g. a resize that failed earlier
        await om.reconcile()
        assert broker.orders[stop.id].size == 2 and working(broker, OrderType.LIMIT)[0].size == 2
        assert t.filled_size == 2
    run(go())


def test_partial_entry_fill_resizes_stop_when_the_rest_arrives(mnq):
    async def go():
        broker, om, _ = manager(mnq)
        t = await om.enter(OrderSide.BUY, 2, 95.0, 110.0, "t", ref_price=100.0)
        await om.handle_position(Position(1, mnq.id, 1, 100.0))  # first contract
        await broker.drain()
        assert t.state == TradeState.OPEN and working(broker, OrderType.STOP)[0].size == 1
        await om.handle_position(Position(1, mnq.id, 2, 100.25))  # second contract
        await broker.drain()
        assert t.filled_size == 2 and t.entry_price == 100.25
        assert working(broker, OrderType.STOP)[0].size == 2 and working(broker, OrderType.LIMIT)[0].size == 2
    run(go())


# --------------------------------------------------------------- restart state

def test_realized_results_group_fills_by_closing_order(mnq):
    async def go():
        broker = PaperBroker(mnq, 50_000)
        broker.fills = [
            Fill(1, 10, mnq.id, OrderSide.BUY, 3, 100.0, T0, None, 0.6),  # entry
            Fill(2, 11, mnq.id, OrderSide.SELL, 1, 99.0, T0 + timedelta(minutes=5), -2.0, 0.2),  # stop, 3 pieces
            Fill(3, 11, mnq.id, OrderSide.SELL, 1, 99.0, T0 + timedelta(minutes=5), -2.0, 0.2),
            Fill(4, 11, mnq.id, OrderSide.SELL, 1, 99.0, T0 + timedelta(minutes=5), -2.0, 0.2),
            Fill(5, 12, mnq.id, OrderSide.SELL, 1, 101.0, T0 + timedelta(minutes=30), None, 0.2),
            Fill(6, 13, mnq.id, OrderSide.BUY, 1, 100.0, T0 + timedelta(minutes=40), 2.0, 0.2),
        ]
        net, trades = await broker.realized_pnl_since(T0 - timedelta(hours=1))
        assert net == pytest.approx(-6.0 + 2.0 - 1.6)
        assert len(trades) == 2  # one losing exit (in three pieces) and one winner - not four trades
        assert trades[0][1] < 0 < trades[1][1]
    run(go())


def test_restart_restores_losing_streak_and_cooldown(mnq):
    cfg = BotConfig.model_validate({"risk": {"max_consecutive_losses": 2, "cooldown_minutes_after_loss": 10}})
    core = build_core(cfg, mnq, PaperBroker(mnq, 50_000), clock=lambda: T0, account_label="t")
    day = core.schedule.trading_day(T0)
    closed_today = [(T0 - timedelta(minutes=40), -60.0), (T0 - timedelta(minutes=5), -55.0)]
    run(core.begin_day(day, 49_885.0, -115.0, closed_today))
    assert core.risk.trades_today == 2 and core.risk.consecutive_losses == 2
    assert "consecutive losses" in core.risk.entry_block_reason(T0, 49_885.0)
    assert core.risk.day_start_balance == 50_000.0
    run(core.begin_day(day, 49_940.0, -60.0, [(T0 - timedelta(minutes=5), -60.0)]))
    assert "cooling down" in core.risk.entry_block_reason(T0, 49_940.0)


def test_paper_restart_reads_todays_trades_from_the_journal(mnq, tmp_path):
    from topstep_bot.execution import ManagedTrade
    from topstep_bot.journal import Journal

    j = Journal(tmp_path / "j.db")
    for i, pnl in enumerate((-40.0, 25.0)):
        when = T0 + timedelta(minutes=10 * i)
        t = ManagedTrade(tag=f"tsb000000000{i}", side=OrderSide.BUY, size=1, stop_price=95.0, target_price=None,
                         reason="t", created_at=when, entry_price=100.0, filled_size=1, closed_at=when, gross_pnl=pnl)
        j.record_trade(t, T0.date(), "PAPER-X", mnq.name)
    rows = j.closed_trades("PAPER-X", T0.date())
    assert [p for _, p in rows] == [-40.0, 25.0] and rows[0][0].tzinfo is not None
    assert j.closed_trades("OTHER", T0.date()) == [] and j.trading_days("PAPER-X") == [T0.date().isoformat()]
    j.close()


# -------------------------------------------------------- background loop crash

def test_a_crashed_background_loop_stops_the_bot_loudly(mnq, tmp_path):
    async def go():
        cfg = BotConfig.model_validate({"data_dir": str(tmp_path)})
        runner = LiveRunner(cfg, Secrets(username="u", api_key="k"), Controls())
        runner.core = build_core(cfg, mnq, PaperBroker(mnq, 50_000), clock=runner.now, account_label="t")

        async def broken():
            raise RuntimeError("boom")

        task = asyncio.create_task(broken(), name="bars")
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        runner._loop_ended(task)
        assert runner.controls.stop.is_set() and "bars" in runner.controls.failure
        assert any("Internal error" in e["message"] for e in runner.core.events)

        quiet = asyncio.create_task(asyncio.sleep(10), name="clock")
        quiet.cancel()
        await asyncio.sleep(0)
        runner.controls = Controls()
        runner._loop_ended(quiet)  # cancellation at shutdown is normal
        assert not runner.controls.stop.is_set()
        runner.journal.close()
        await runner.client.close()
    run(go())
