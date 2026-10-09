import json
from datetime import timedelta
from pathlib import Path

from topstep_bot.broker.paper import PaperBroker
from topstep_bot.config import BotConfig
from topstep_bot.factory import build_core
from topstep_bot.journal import Journal
from topstep_bot.models import OrderSide, Signal

from .conftest import bar, ct, run

T0 = ct(2026, 3, 3, 9, 0)


def make_core(contract, **cfg):
    config = BotConfig.model_validate(cfg)
    now = [T0]
    broker = PaperBroker(contract, 50_000, slippage_ticks=0, fees_round_turn=1.22)
    core = build_core(config, contract, broker, clock=lambda: now[0], account_label="test", journal=Journal(":memory:"))
    core.balance = 50_000
    return core, broker, now


def test_engine_sizes_entry_from_stop_and_flattens_at_session_end(mnq):
    async def go():
        core, broker, now = make_core(mnq, risk={"risk_per_trade": 100, "slippage_ticks": 0})
        await core.begin_day(core.schedule.trading_day(T0), 50_000)
        b = bar(T0, 100, 100, 100, 100)
        core.last_price = 100.0
        sig = Signal("long", stop_price=90.0, target_price=150.0, reason="test")
        await core._handle_entry(sig, b, core.context(b))
        # Sized for the worst allowed fill: (40 + 8 slippage) ticks x $0.50 = $24 + $1.22 fees -> 3 contracts for $100
        assert core.orders.trade.size == 3
        await broker.on_bar(bar(T0 + timedelta(minutes=5), 100, 101, 99, 100))
        assert core.orders.position == 3
        flat_time = ct(2026, 3, 3, 15, 0)
        now[0] = flat_time
        await core.on_clock(flat_time)
        await broker.on_bar(bar(flat_time, 100, 100, 100, 100))
        assert broker.position == 0
        assert core.closed_trades and "session flatten" in core.closed_trades[0].exit_reason
        assert core.journal.trades()[0]["size"] == 3
    run(go())


def test_engine_flattens_on_personal_daily_loss(mnq):
    async def go():
        core, broker, now = make_core(mnq, risk={"risk_per_trade": 400, "personal_daily_loss_limit": 450})
        await core.begin_day(core.schedule.trading_day(T0), 50_000)
        b = bar(T0, 100, 100, 100, 100)
        core.last_price = 100.0
        await core._handle_entry(Signal("long", stop_price=50.0, reason="t"), b, core.context(b))
        await broker.on_bar(bar(T0 + timedelta(minutes=5), 100, 100, 100, 100))
        size = core.orders.position
        assert size >= 1
        drop = 100 - 455 / (size * mnq.point_value)
        # Feed the drop to the risk monitor only (e.g. a gap): the backstop must flatten.
        broker.live = True
        await core.on_price(T0 + timedelta(minutes=6), drop)
        await broker.drain()
        assert broker.position == 0
        assert core.risk.lock_reason and "daily loss" in core.risk.lock_reason
        assert core.risk.entry_block_reason(T0 + timedelta(minutes=30), core.balance) is not None
    run(go())


def test_engine_ignores_signal_when_stop_on_wrong_side(mnq):
    async def go():
        core, _, _ = make_core(mnq)
        await core.begin_day(core.schedule.trading_day(T0), 50_000)
        b = bar(T0, 100, 100, 100, 100)
        core.last_price = 100.0
        await core._handle_entry(Signal("short", stop_price=95.0, reason="bad"), b, core.context(b))
        assert core.orders.trade is None
    run(go())


def test_late_bar_is_checked_against_the_real_time(mnq):
    """A bar fetched late must not open a trade in a news blackout that started since, or act on an old signal."""
    from topstep_bot.news import NewsCalendar, NewsEvent

    async def go():
        core, broker, now = make_core(mnq, session={"trade_start": "07:00"})
        release = ct(2026, 3, 3, 7, 31)  # news blackout 07:26-07:41
        cal = NewsCalendar("http://unused", Path("unused.json"), ["High"], ["USD"], 5, 10)
        cal.events, cal.fetched_at = [NewsEvent("CPI m/m", "USD", "High", release)], release - timedelta(hours=1)
        core.schedule.news = cal
        await core.begin_day(core.schedule.trading_day(release), 50_000)
        core.last_price = 100.0
        core.strategy.on_bar = lambda b, ctx: Signal("long", stop_price=90.0, reason="t")
        now[0] = ct(2026, 3, 3, 7, 27)
        await core.on_bar(bar(ct(2026, 3, 3, 7, 20), 100, 100, 100, 100))  # closed 07:25, before the blackout
        assert core.orders.trade is None
        assert "news blackout" in core.events[0]["message"]
        now[0] = ct(2026, 3, 3, 8, 0)
        await core.on_bar(bar(ct(2026, 3, 3, 7, 45), 100, 100, 100, 100))  # closed 10 minutes ago
        assert core.orders.trade is None
        assert "came in late" in core.events[0]["message"]
        await core.on_bar(bar(ct(2026, 3, 3, 7, 55), 100, 100, 100, 100))  # just closed: trades
        assert core.orders.trade is not None
    run(go())


def test_a_late_bar_from_a_finished_day_never_rolls_the_day_back(mnq):
    async def go():
        core, broker, now = make_core(mnq)
        day = core.schedule.trading_day(T0)
        await core.begin_day(day, 50_000)
        core.risk.trades_today, core.balance = 2, 51_400
        now[0] = ct(2026, 3, 3, 17, 0)
        await core.on_clock(now[0])  # the day closes at 17:00
        next_day = core.current_day
        assert next_day > day and core.risk.trades_today == 0
        assert core.tracker.floor == 49_400  # the MLL trails the $51,400 close
        now[0] = ct(2026, 3, 3, 17, 5)
        await core.on_bar(bar(ct(2026, 3, 3, 15, 55), 100, 100, 100, 100))  # missed at 16:00, fetched now
        await core.on_clock(now[0])
        assert core.current_day == next_day
        row = next(r for r in core.journal.daily("test") if r["trading_day"] == day.isoformat())
        assert row["net_pnl"] == 1_400 and row["trades"] == 2
        assert core.journal.get_state("last_eod:test") == day.isoformat()
    run(go())


def test_flatten_retry_uses_the_pc_clock_and_halt_always_says_so(mnq):
    """Quotes carry the exchange's timestamps: a PC clock behind them must not silence a later flatten or halt."""
    async def go():
        core, broker, now = make_core(mnq, risk={"risk_per_trade": 400, "personal_daily_loss_limit": 450})
        await core.begin_day(core.schedule.trading_day(T0), 50_000)
        b = bar(T0, 100, 100, 100, 100)
        core.last_price = 100.0
        await core._handle_entry(Signal("long", stop_price=50.0, reason="t"), b, core.context(b))
        await broker.on_bar(bar(T0 + timedelta(minutes=5), 100, 100, 100, 100))
        calls = []

        async def flatten_all(reason):
            calls.append(reason)
        core.orders.flatten_all = flatten_all
        now[0] = T0 + timedelta(minutes=6)
        await core.on_price(now[0] + timedelta(minutes=1), 20.0)  # the exchange's clock is a minute ahead
        assert len(calls) == 1
        now[0] += timedelta(seconds=10)
        await core.halt("KILL file found")
        assert len(calls) == 2 and "KILL file found" in calls[1]
        await core.halt("KILL file found")  # moments later: no second flatten, but still reported
        assert len(calls) == 2
        assert "FLATTEN: halted: KILL file found" in core.events[0]["message"]
    run(go())


def test_skipped_signals_are_noted_once_per_day_each(mnq):
    core, _, _ = make_core(mnq)
    day = core.schedule.trading_day(T0)
    run(core.begin_day(day, 50_000))
    for message in ("Skipped LONG: A", "Skipped SHORT: B", "Skipped LONG: A", "Skipped SHORT: B"):
        core._note_skip(message)
    assert [e["message"] for e in core.events] == ["Skipped SHORT: B", "Skipped LONG: A"]
    run(core.begin_day(day + timedelta(days=1), 50_000))
    core._note_skip("Skipped LONG: A")  # a new day: noted again
    assert len(core.events) == 3


def test_widened_stop_still_respects_max_stop_ticks(mnq):
    core, _, _ = make_core(mnq, risk={"min_stop_atr": 2.0, "max_stop_ticks": 40})
    run(core.begin_day(core.schedule.trading_day(T0), 50_000))
    for _ in range(14):
        core.atr.update(110.0, 100.0, 105.0)  # ATR 10 points, so the minimum stop is 20 points (80 ticks)
    assert core.plan_entry(Signal("long", stop_price=99.0, reason="t"), 100.0) == "stop is 80 ticks away (max 40)"


def test_bot_api_requires_token_and_local_host(mnq):
    """The bot's private API (used by the controller): token on every request, local Host only."""
    import httpx

    from topstep_bot.control import BotActions
    from topstep_bot.live import Controls
    from topstep_bot.worker_api import build_worker_api

    async def go():
        core, _, _ = make_core(mnq)
        await core.begin_day(core.schedule.trading_day(T0), 50_000)
        server = build_worker_api(BotActions(core, Controls()), core.snapshot, 0, "secret-token")
        await server.start()
        base = f"http://127.0.0.1:{server.port}"
        async with httpx.AsyncClient(base_url=base) as c:
            assert (await c.get("/status")).status_code == 403  # no token, even for reads
            ok = await c.get("/status", headers={"X-Token": "secret-token"})
            assert ok.json()["bot"]["account"] == "test"
            bad_host = await c.get("/status", headers={"X-Token": "secret-token", "Host": "evil.example"})
            assert bad_host.status_code == 403
            r = await c.post("/action/pause", json={"source": "test"}, headers={"X-Token": "secret-token"})
            assert r.json()["ok"] and core.risk.paused
            r = await c.post("/action/nope", headers={"X-Token": "secret-token"})
            assert r.json()["ok"] is False and "unknown action" in r.json()["message"]
        await server.stop()
    run(go())


def test_snapshot_is_json_serializable(mnq):
    core, _, _ = make_core(mnq)
    run(core.begin_day(core.schedule.trading_day(T0), 50_000))
    snap = core.snapshot()
    json.dumps(snap, default=str)
    assert snap["risk"]["max_contracts"] == 50
    assert snap["profit_target"] == 3000


def test_side_helpers():
    assert OrderSide.BUY.opposite == OrderSide.SELL and OrderSide.SELL.sign == -1
