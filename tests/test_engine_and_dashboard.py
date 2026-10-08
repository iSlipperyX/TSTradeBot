import json
from datetime import timedelta

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
