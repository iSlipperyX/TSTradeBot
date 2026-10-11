"""End-to-end test of the live runner against a fake TopstepX (mock REST + real local SignalR hubs)."""

import asyncio
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import websockets

from topstep_bot.api.parse import format_ts, parse_ts
from topstep_bot.api.rest import ProjectXClient
from topstep_bot.api.signalr import RS, decode, encode
from topstep_bot.backtest.data import synthetic_bars
from topstep_bot.bars import resample
from topstep_bot.config import BotConfig, Secrets
from topstep_bot.execution import TradeState
from topstep_bot.live import Controls, LiveRunner
from topstep_bot.models import OrderSide

from .conftest import run

UTC = timezone.utc
CONTRACT = {"id": "CON.F.US.MNQ.Z26", "name": "MNQZ6", "tickSize": 0.25, "tickValue": 0.5, "activeContract": True}


class FakeTopstepX:
    def __init__(self):
        bars = list(resample(synthetic_bars("MNQ", days=40, seed=5), 5))
        # Only bars that have closed: the bot drops a bar still forming, so serving one made the bar
        # counts below depend on the time of day the tests ran (they failed during the trading session).
        self.bars = [b for b in bars if b.ts + timedelta(minutes=5) <= datetime.now(UTC)]
        self.price = self.bars[-1].close
        self.orders: dict[int, dict] = {}
        self.position = 0
        self.next_id = 100
        self.calls: list[tuple[str, dict]] = []
        self.user_events: asyncio.Queue = asyncio.Queue()

    # ------------------------------------------------------------- REST
    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body = json.loads(request.content or b"{}")
        self.calls.append((path, body))
        ok = {"success": True, "errorCode": 0, "errorMessage": None}
        if path == "/api/Auth/loginKey":
            return httpx.Response(200, json={**ok, "token": "tok"})
        if path == "/api/Account/search":
            return httpx.Response(200, json={**ok, "accounts": [{"id": 7, "name": "50KTC-1", "balance": 50000.0, "canTrade": True}]})
        if path == "/api/Contract/search":
            return httpx.Response(200, json={**ok, "contracts": [CONTRACT]})
        if path == "/api/History/retrieveBars":
            start, end = parse_ts(body["startTime"]), parse_ts(body["endTime"])
            sel = [b for b in self.bars if start <= b.ts <= end][-body["limit"]:]
            out = [{"t": format_ts(b.ts), "o": b.open, "h": b.high, "l": b.low, "c": b.close, "v": b.volume} for b in reversed(sel)]
            return httpx.Response(200, json={**ok, "bars": out})
        if path == "/api/Trade/search":
            return httpx.Response(200, json={**ok, "trades": []})
        if path == "/api/Order/place":
            oid = self.next_id
            self.next_id += 1
            order = {"id": oid, "accountId": 7, "contractId": body["contractId"], "type": body["type"], "side": body["side"],
                     "size": body["size"], "status": 1, "stopPrice": body.get("stopPrice"), "limitPrice": body.get("limitPrice"),
                     "customTag": body.get("customTag"), "fillVolume": 0}
            self.orders[oid] = order
            if body["type"] == 2:  # market: fill immediately
                order.update(status=2, filledPrice=self.price, fillVolume=body["size"])
                self.position += body["size"] * (1 if body["side"] == 0 else -1)
                self._push_order(order)
                self._push_position()
            else:
                self._push_order(order)
            return httpx.Response(200, json={**ok, "orderId": oid})
        if path == "/api/Order/cancel":
            order = self.orders[body["orderId"]]
            if order["status"] == 1:
                order["status"] = 3
                self._push_order(order)
            return httpx.Response(200, json=ok)
        if path == "/api/Order/modify":
            order = self.orders[body["orderId"]]
            for k in ("size", "stopPrice", "limitPrice"):
                if body.get(k) is not None:
                    order[k] = body[k]
            self._push_order(order)
            return httpx.Response(200, json=ok)
        if path == "/api/Order/searchOpen":
            return httpx.Response(200, json={**ok, "orders": [o for o in self.orders.values() if o["status"] == 1]})
        if path == "/api/Order/search":
            return httpx.Response(200, json={**ok, "orders": list(self.orders.values())})
        if path == "/api/Position/searchOpen":
            pos = [] if self.position == 0 else [{"id": 1, "accountId": 7, "contractId": CONTRACT["id"], "type": 1 if self.position > 0 else 2,
                                                   "size": abs(self.position), "averagePrice": self.price}]
            return httpx.Response(200, json={**ok, "positions": pos})
        if path == "/api/Position/closeContract":
            side = 1 if self.position > 0 else 0
            if self.position:
                self.user_events.put_nowait(("GatewayUserTrade", {"id": 9, "accountId": 7, "contractId": CONTRACT["id"], "price": self.price,
                                                                   "profitAndLoss": 0.0, "fees": 1.0, "side": side, "size": abs(self.position), "orderId": 999}))
            self.position = 0
            self._push_position()
            return httpx.Response(200, json=ok)
        return httpx.Response(200, json=ok)

    def _push_order(self, order: dict) -> None:
        self.user_events.put_nowait(("GatewayUserOrder", dict(order)))

    def _push_position(self) -> None:
        self.user_events.put_nowait(("GatewayUserPosition", {"id": 1, "accountId": 7, "contractId": CONTRACT["id"],
                                                             "type": 1 if self.position >= 0 else 2, "size": abs(self.position), "averagePrice": self.price}))

    # -------------------------------------------------------------- hubs
    async def hub(self, ws):
        assert "access_token=tok" in ws.request.path
        decode(await ws.recv())
        await ws.send("{}" + RS)
        is_user = "/hubs/user" in ws.request.path

        async def sender():
            while True:
                if is_user:
                    target, data = await self.user_events.get()
                    await ws.send(encode({"type": 1, "target": target, "arguments": [data]}))
                else:
                    await ws.send(encode({"type": 1, "target": "GatewayQuote", "arguments": [CONTRACT["id"], {"lastPrice": self.price, "bestBid": self.price - 0.25, "bestAsk": self.price}]}))
                    await asyncio.sleep(0.1)

        task = asyncio.create_task(sender())
        try:
            async for _ in ws:  # returns when the client disconnects
                pass
        except websockets.ConnectionClosed:
            pass
        finally:
            task.cancel()


def make_runner(fake: FakeTopstepX, port: int, mode: str, tmp_path) -> LiveRunner:
    cfg = BotConfig.model_validate({
        "mode": mode,
        "data_dir": str(tmp_path),
        "api": {"user_hub_url": f"http://127.0.0.1:{port}/hubs/user", "market_hub_url": f"http://127.0.0.1:{port}/hubs/market"},
        "dashboard": {"enabled": False},
        "execution": {"reconcile_interval_seconds": 0.5},
        "news": {"enabled": False},
    })
    runner = LiveRunner(cfg, Secrets(username="u", api_key="k"), Controls())
    runner.client = ProjectXClient("u", "k", transport=httpx.MockTransport(fake.handler))
    return runner


def test_live_mode_end_to_end(tmp_path):
    async def go():
        fake = FakeTopstepX()
        async with websockets.serve(fake.hub, "127.0.0.1", 0) as srv:
            port = srv.sockets[0].getsockname()[1]
            runner = make_runner(fake, port, "live", tmp_path)
            ready = asyncio.Event()

            async def on_ready(core):
                core.schedule.must_be_flat = lambda ts: False  # make the test independent of the time of day
                ready.set()

            task = asyncio.create_task(runner.run(on_ready))
            await asyncio.wait_for(ready.wait(), 20)
            core = runner.core
            assert core.strategy_day is not None and core.last_bar is not None  # warmed up from history
            assert core.balance == 50_000

            entry = fake.price
            await core.orders.enter(OrderSide.BUY, 2, entry - 10, entry + 20, "integration", ref_price=entry)
            for _ in range(100):
                t = core.orders.trade
                if t and t.state == TradeState.OPEN and t.stop_order_id and t.target_order_id:
                    break
                await asyncio.sleep(0.05)
            t = core.orders.trade
            assert t.state == TradeState.OPEN and t.filled_size == 2
            placed = [b for p, b in fake.calls if p == "/api/Order/place"]
            assert [b["type"] for b in placed] == [2, 4, 1]  # market entry, stop, target
            assert placed[1]["side"] == 1 and placed[1]["size"] == 2 and placed[1]["stopPrice"] == entry - 10
            assert all(b["customTag"].startswith(t.tag) for b in placed)

            runner.controls.stop.set()  # shutdown flattens the open position
            await asyncio.wait_for(task, 20)
        paths = [p for p, _ in fake.calls]
        assert "/api/Position/closeContract" in paths
        assert fake.position == 0
        assert all(o["status"] != 1 for o in fake.orders.values())  # nothing left working
        assert core.closed_trades and core.closed_trades[0].exit_reason == "bot shutdown"

    run(go())


def test_paper_mode_never_sends_orders(tmp_path):
    async def go():
        fake = FakeTopstepX()
        async with websockets.serve(fake.hub, "127.0.0.1", 0) as srv:
            port = srv.sockets[0].getsockname()[1]
            runner = make_runner(fake, port, "paper", tmp_path)
            ready = asyncio.Event()

            async def on_ready(core):
                core.schedule.must_be_flat = lambda ts: False
                ready.set()

            task = asyncio.create_task(runner.run(on_ready))
            await asyncio.wait_for(ready.wait(), 20)
            core = runner.core
            await core.orders.enter(OrderSide.SELL, 1, fake.price + 10, None, "paper test", ref_price=fake.price)
            for _ in range(100):
                if core.orders.position == -1 and core.orders.trade.stop_order_id:
                    break
                await asyncio.sleep(0.05)
            assert core.orders.position == -1  # filled by the paper broker from streamed quotes
            runner.controls.stop.set()
            await asyncio.wait_for(task, 20)
        paths = {p for p, _ in fake.calls}
        assert not paths & {"/api/Order/place", "/api/Order/cancel", "/api/Position/closeContract"}
        assert core.closed_trades and runner.broker.position == 0

    run(go())


def test_runner_keeps_every_bar_and_learns_from_all_of_them(tmp_path):
    async def go():
        fake = FakeTopstepX()
        async with websockets.serve(fake.hub, "127.0.0.1", 0) as srv:
            port = srv.sockets[0].getsockname()[1]
            runner = make_runner(fake, port, "paper", tmp_path)
            runner.cfg.knowledge.deep_history_days = 60
            ready = asyncio.Event()

            async def on_ready(core):
                ready.set()

            task = asyncio.create_task(runner.run(on_ready))
            await asyncio.wait_for(ready.wait(), 30)
            memory = runner.core.memory
            stored = memory.status()["library"]["bars"]
            assert stored == len(fake.bars)  # warm-up and the startup training went straight into the library
            asked = len([p for p, _ in fake.calls if p == "/api/History/retrieveBars"])

            message = await runner.learn("test")
            assert message.startswith("Long-run memory updated") and memory.knowledge.obs
            assert memory.status()["library"]["bars"] == stored
            again = len([p for p, _ in fake.calls if p == "/api/History/retrieveBars"]) - asked
            assert again <= 2  # only the ranges it never had (older than the training window) were asked for
            assert runner.core.snapshot()["memory"]["observations"] == len(memory.knowledge.obs)
            runner.controls.stop.set()
            await asyncio.wait_for(task, 20)
        assert (tmp_path / "market_library.sqlite").exists()

    run(go())


def test_a_restart_trains_from_saved_history_and_downloads_only_what_is_missing(tmp_path, monkeypatch):
    import topstep_bot.knowledge as knowledge

    async def go():
        fake = FakeTopstepX()
        history = "/api/History/retrieveBars"
        async with websockets.serve(fake.hub, "127.0.0.1", 0) as srv:
            port = srv.sockets[0].getsockname()[1]
            asked, origins = [], []
            for _ in range(2):
                runner = make_runner(fake, port, "paper", tmp_path)
                ready = asyncio.Event()

                async def on_ready(core, ready=ready):
                    ready.set()

                before = len([p for p, _ in fake.calls if p == history])
                task = asyncio.create_task(runner.run(on_ready))
                await asyncio.wait_for(ready.wait(), 30)
                asked.append(len([p for p, _ in fake.calls if p == history]) - before)
                origins.append(runner.core.knowledge.trained["origin"])
                assert any(e["message"].startswith("Knowledge ready:") for e in runner.core.events)
                runner.controls.stop.set()
                await asyncio.wait_for(task, 20)
                # an update that changes the strategy code: the next start must retrain
                monkeypatch.setattr(knowledge, "training_fingerprint", lambda: "after-an-update")
            assert origins[0].startswith("saved history + ") and origins[1].startswith("saved history")
            assert asked[1] <= 2  # the warm-up, plus at most the last few minutes: the 60 days come from disk

    run(go())


def test_training_falls_back_to_saved_history_when_the_download_fails(tmp_path):
    from types import SimpleNamespace

    from topstep_bot.memory import MarketLibrary

    fake = FakeTopstepX()
    runner = make_runner(fake, 1, "paper", tmp_path)
    library = MarketLibrary(tmp_path / "lib.sqlite")
    library.add("MNQ", 5, fake.bars)

    class Memory:
        symbol, tf = "MNQ", 5

        async def backfill(self, *args, **kwargs):
            raise httpx.ConnectError("TopstepX is unreachable")

    memory = Memory()
    memory.library = library
    runner.core = SimpleNamespace(memory=memory)
    runner.contract = SimpleNamespace(id=CONTRACT["id"])
    end = fake.bars[-1].ts + timedelta(minutes=5)
    bars, origin = run(runner._training_bars(30, end))
    assert origin == "saved history only: the download failed" and bars[-1] == fake.bars[-1]
    assert bars[0].ts >= end - timedelta(days=30)

    library.close()
    empty = MarketLibrary(tmp_path / "empty.sqlite")
    memory.library = empty
    with pytest.raises(httpx.ConnectError):
        run(runner._training_bars(30, end))
    empty.close()


def test_a_bot_that_runs_for_days_retrains_after_each_session(tmp_path):
    from types import SimpleNamespace

    from topstep_bot.knowledge import KnowledgeBase
    from topstep_bot.sessions import SessionSchedule

    runner = make_runner(FakeTopstepX(), 1, "paper", tmp_path)
    kb = KnowledgeBase(tmp_path / "k.json")
    kb.trained = {"at": "2026-10-08T21:10:00+00:00", "to": "2026-10-08", "days": 60, "code": "old"}
    runner.core = SimpleNamespace(knowledge=kb, schedule=SessionSchedule(runner.cfg.session), event=lambda *a, **k: None)
    calls = []

    async def retrain(source):
        calls.append(source)
        if len(calls) == 1:
            raise RuntimeError("TopstepX is down")
        return "ok"

    runner.retrain = retrain
    friday_evening = datetime(2026, 10, 9, 21, 30, tzinfo=UTC)  # 16:30 CT, after Friday's close
    run(runner._retrain_if_due(friday_evening))
    run(runner._retrain_if_due(friday_evening + timedelta(minutes=5)))  # failed: waits an hour before trying again
    run(runner._retrain_if_due(friday_evening + timedelta(hours=1, minutes=1)))
    assert calls == ["daily", "daily"]
