"""Several markets in one account: shared Topstep limits (risk/book.py) and the runner's lanes (live.py)."""

import asyncio
import json
from datetime import datetime, timedelta, timezone

import httpx
import websockets

from topstep_bot.api.parse import format_ts, parse_ts
from topstep_bot.api.rest import ProjectXClient
from topstep_bot.api.signalr import RS, decode, encode
from topstep_bot.backtest.data import synthetic_bars
from topstep_bot.bars import resample
from topstep_bot.broker.paper import PaperBroker, PaperWallet
from topstep_bot.config import BotConfig, Secrets
from topstep_bot.execution import TradeState
from topstep_bot.factory import build_core
from topstep_bot.live import Controls, LiveRunner
from topstep_bot.market_pick import lane_symbols, same_product
from topstep_bot.models import OrderSide
from topstep_bot.risk.book import AccountBook
from topstep_bot.risk.guards import DEFAULT_MAX_ENTRIES_PER_DAY
from topstep_bot.risk.topstep import PLANS, LossLimitTracker

from .conftest import ct, offline_contract, run

UTC = timezone.utc


def desk(cfg: BotConfig, *symbols: str):
    """Cores for several markets on one paper account, sharing the book, the MLL tracker and the order guard."""
    book = AccountBook(shared=True)
    tracker = LossLimitTracker(50_000, PLANS[cfg.account.plan].max_loss_limit)
    wallet = PaperWallet(50_000)
    cores = []
    for s in symbols:
        c = offline_contract(s)
        broker = PaperBroker(c, 50_000, wallet=wallet)
        core = build_core(cfg, c, broker, clock=lambda: ct(2026, 3, 3, 9, 0), account_label="ACC", book=book,
                          tracker=tracker, guard_from=cores[0].orders.guard if cores else None)
        core.balance = 50_000
        cores.append(core)
    book.start_day(ct(2026, 3, 3, 9, 0).date(), 50_000)
    return book, cores


def test_one_market_alone_is_sized_exactly_as_before(mnq):
    cfg = BotConfig()
    alone = build_core(cfg, mnq, PaperBroker(mnq, 50_000), clock=lambda: ct(2026, 3, 3, 9, 0), account_label="A")
    run(alone.begin_day(ct(2026, 3, 3, 9, 0).date(), 50_000))
    book, (shared,) = desk(cfg, "MNQ")
    for entry, stop in ((20_000, 19_990), (20_000, 19_960)):
        assert alone.risk.position_size(entry, stop, 50_000) == shared.risk.position_size(entry, stop, 50_000)
    assert alone.risk.max_contracts_topstep() == shared.risk.max_contracts_topstep()


def test_an_open_trade_in_one_market_shrinks_what_the_others_may_risk():
    cfg = BotConfig.model_validate({"risk": {"risk_per_trade": 400, "personal_daily_loss_limit": 600}})
    book, (mnq, mes) = desk(cfg, "MNQ", "MES")
    before = mes.risk.position_size(6000, 5990, 50_000)
    assert before > 0

    async def open_mnq():
        mnq.last_price = 20_000.0
        mnq.orders.last_price = 20_000.0
        await mnq.orders.enter(OrderSide.BUY, 10, 19_980.0, 20_040.0, "test", ref_price=20_000.0)

    run(open_mnq())
    others = mes.risk.others()
    # 10 MNQ with a 20-point stop: $400 to the stop, plus slippage, the jump allowance and fees
    assert others.at_risk > 400 and others.contracts == 10 and others.minis == 1.0
    after = mes.risk.position_size(6000, 5990, 50_000)
    assert after < before  # the daily loss limit's room is shared: MNQ's possible loss is taken off first
    worst = mes.risk.worst_loss_per_contract(6000, 5990)
    assert after * worst + others.at_risk <= 600 + 1e-6
    # Topstep's position cap counts both: 50K Combine = 5 minis (50 micros); MNQ holds 10 micros
    assert mes.risk.max_contracts_topstep() == 40
    # the consistency room is shared too: MNQ's target could still be reached
    room_alone = mnq.risk.reward_room(50_000)
    assert mes.risk.reward_room(50_000) == room_alone - others.upside


def test_the_day_is_one_for_the_whole_account():
    book, (mnq, mes) = desk(BotConfig(), "MNQ", "MES")
    mnq.risk.record_trade(-120.0, ct(2026, 3, 3, 9, 30))
    assert mes.risk.trades_today == 1 and mes.risk.consecutive_losses == 1 and mes.risk.last_loss_at is not None
    mes.risk.lock("daily profit target reached")
    assert mnq.risk.lock_reason == "daily profit target reached"
    mnq.risk.paused = True
    assert mes.risk.paused
    # the rate breaker and the daily entry count are the account's, not one market's
    assert mnq.orders.guard._counts is mes.orders.guard._counts
    # a market started later the same day does not reset the account's day
    run(mes.begin_day(ct(2026, 3, 3, 9, 0).date(), 50_000))
    assert book.trades_today == 1


def test_paper_markets_share_one_balance():
    async def go():
        wallet = PaperWallet(50_000)
        a = PaperBroker(offline_contract("MNQ"), 50_000, wallet=wallet, live=True)
        b = PaperBroker(offline_contract("MES"), 50_000, wallet=wallet, live=True)
        seen = []
        b.on_account = lambda acct: seen.append(acct.balance)
        a.last_price = 20_000.0
        await a.place_order(a.contract.id, 2, OrderSide.BUY, 1)  # market buy
        await a.on_price(ct(2026, 3, 3, 9, 1), 20_010.0)
        await a.place_order(a.contract.id, 2, OrderSide.SELL, 1)
        await a.drain()
        await b.drain()
        assert wallet.balance == a.balance == b.balance != 50_000
        assert seen and seen[-1] == wallet.balance  # the other market heard about it

    run(go())


def test_which_markets_trade_together():
    assert same_product("MNQ", "NQ") and not same_product("MNQ", "MES")
    cfg = BotConfig.model_validate({"instrument": {"symbol": "MNQ", "markets": ["NQ", "MES", "MGC"]}})
    assert lane_symbols(cfg, "MNQ") == ["MNQ", "MES", "MGC"]  # NQ is MNQ's product: never both


# ------------------------------------------------------------------------------ the runner, end to end

CONTRACTS = {
    "MNQ": {"id": "CON.F.US.MNQ.Z26", "name": "MNQZ6", "tickSize": 0.25, "tickValue": 0.5, "activeContract": True},
    "MES": {"id": "CON.F.US.MES.Z26", "name": "MESZ6", "tickSize": 0.25, "tickValue": 1.25, "activeContract": True},
    "MGC": {"id": "CON.F.US.MGC.Z26", "name": "MGCZ6", "tickSize": 0.1, "tickValue": 1.0, "activeContract": True},
}


class FakeDesk:
    """A fake TopstepX with several markets: contracts, bars and positions per contract, one user stream."""

    def __init__(self):
        now = datetime.now(UTC)
        self.bars = {}
        for i, s in enumerate(CONTRACTS):
            bars = list(resample(synthetic_bars(s, days=30, seed=11 + i), 5))
            self.bars[CONTRACTS[s]["id"]] = [b for b in bars if b.ts + timedelta(minutes=5) <= now]
        self.price = {cid: bars[-1].close for cid, bars in self.bars.items()}
        self.orders: dict[int, dict] = {}
        self.position: dict[str, int] = {cid: 0 for cid in self.bars}
        self.next_id = 100
        self.calls: list[tuple[str, dict]] = []
        self.user_events: asyncio.Queue = asyncio.Queue()

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
            text = str(body.get("searchText", "")).upper()
            return httpx.Response(200, json={**ok, "contracts": [c for s, c in CONTRACTS.items() if s == text]})
        if path == "/api/History/retrieveBars":
            start, end = parse_ts(body["startTime"]), parse_ts(body["endTime"])
            sel = [b for b in self.bars.get(body["contractId"], []) if start <= b.ts <= end][-body["limit"]:]
            out = [{"t": format_ts(b.ts), "o": b.open, "h": b.high, "l": b.low, "c": b.close, "v": b.volume} for b in reversed(sel)]
            return httpx.Response(200, json={**ok, "bars": out})
        if path == "/api/Trade/search":
            return httpx.Response(200, json={**ok, "trades": []})
        if path == "/api/Order/place":
            oid, cid = self.next_id, body["contractId"]
            self.next_id += 1
            order = {"id": oid, "accountId": 7, "contractId": cid, "type": body["type"], "side": body["side"],
                     "size": body["size"], "status": 1, "stopPrice": body.get("stopPrice"), "limitPrice": body.get("limitPrice"),
                     "customTag": body.get("customTag"), "fillVolume": 0}
            self.orders[oid] = order
            if body["type"] == 2:
                order.update(status=2, filledPrice=self.price[cid], fillVolume=body["size"])
                self.position[cid] += body["size"] * (1 if body["side"] == 0 else -1)
                self._push("GatewayUserOrder", dict(order))
                self._push_position(cid)
            else:
                self._push("GatewayUserOrder", dict(order))
            return httpx.Response(200, json={**ok, "orderId": oid})
        if path == "/api/Order/cancel":
            order = self.orders[body["orderId"]]
            if order["status"] == 1:
                order["status"] = 3
                self._push("GatewayUserOrder", dict(order))
            return httpx.Response(200, json=ok)
        if path == "/api/Order/modify":
            order = self.orders[body["orderId"]]
            for k in ("size", "stopPrice", "limitPrice"):
                if body.get(k) is not None:
                    order[k] = body[k]
            self._push("GatewayUserOrder", dict(order))
            return httpx.Response(200, json=ok)
        if path == "/api/Order/searchOpen":
            return httpx.Response(200, json={**ok, "orders": [o for o in self.orders.values() if o["status"] == 1]})
        if path == "/api/Order/search":
            return httpx.Response(200, json={**ok, "orders": list(self.orders.values())})
        if path == "/api/Position/searchOpen":
            pos = [{"id": i, "accountId": 7, "contractId": cid, "type": 1 if p > 0 else 2, "size": abs(p),
                    "averagePrice": self.price[cid]} for i, (cid, p) in enumerate(self.position.items()) if p]
            return httpx.Response(200, json={**ok, "positions": pos})
        if path == "/api/Position/closeContract":
            cid = body["contractId"]
            p = self.position[cid]
            if p:
                self._push("GatewayUserTrade", {"id": 9, "accountId": 7, "contractId": cid, "price": self.price[cid],
                                                "profitAndLoss": 0.0, "fees": 1.0, "side": 1 if p > 0 else 0,
                                                "size": abs(p), "orderId": 999})
            self.position[cid] = 0
            self._push_position(cid)
            return httpx.Response(200, json=ok)
        return httpx.Response(200, json=ok)

    def _push(self, target: str, data: dict) -> None:
        self.user_events.put_nowait((target, data))

    def _push_position(self, cid: str) -> None:
        p = self.position[cid]
        self._push("GatewayUserPosition", {"id": 1, "accountId": 7, "contractId": cid, "type": 1 if p >= 0 else 2,
                                           "size": abs(p), "averagePrice": self.price[cid]})

    async def hub(self, ws):
        decode(await ws.recv())
        await ws.send("{}" + RS)
        is_user = "/hubs/user" in ws.request.path

        async def sender():
            while True:
                if is_user:
                    target, data = await self.user_events.get()
                    await ws.send(encode({"type": 1, "target": target, "arguments": [data]}))
                else:
                    for cid, price in self.price.items():
                        await ws.send(encode({"type": 1, "target": "GatewayQuote", "arguments": [
                            cid, {"lastPrice": price, "bestBid": price - 0.25, "bestAsk": price}]}))
                    await asyncio.sleep(0.1)

        task = asyncio.create_task(sender())
        try:
            async for _ in ws:
                pass
        except websockets.ConnectionClosed:
            pass
        finally:
            task.cancel()


def make_desk(fake: FakeDesk, port: int, mode: str, tmp_path, markets=("MES",)) -> LiveRunner:
    cfg = BotConfig.model_validate({
        "mode": mode,
        "data_dir": str(tmp_path),
        "instrument": {"symbol": "MNQ", "markets": list(markets)},
        "api": {"user_hub_url": f"http://127.0.0.1:{port}/hubs/user", "market_hub_url": f"http://127.0.0.1:{port}/hubs/market"},
        "execution": {"reconcile_interval_seconds": 0.5},
        "news": {"enabled": False, "headlines": False},
        "knowledge": {"deep_learning": False, "auto_train": False},
    })
    runner = LiveRunner(cfg, Secrets(username="u", api_key="k"), Controls())
    runner.client = ProjectXClient("u", "k", transport=httpx.MockTransport(fake.handler))
    return runner


async def _wait(cond, seconds: float = 10) -> None:
    for _ in range(int(seconds / 0.05)):
        if cond():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("condition not reached")


def test_live_markets_trade_side_by_side_on_one_connection(tmp_path):
    async def go():
        fake = FakeDesk()
        async with websockets.serve(fake.hub, "127.0.0.1", 0) as srv:
            port = srv.sockets[0].getsockname()[1]
            runner = make_desk(fake, port, "live", tmp_path)
            ready = asyncio.Event()

            async def on_ready(core):
                for c in runner.cores():
                    c.schedule.must_be_flat = lambda ts: False
                ready.set()

            task = asyncio.create_task(runner.run(on_ready))
            await asyncio.wait_for(ready.wait(), 30)
            assert list(runner.lanes) == ["MNQ", "MES"] and runner.hub.lanes == 2
            mnq, mes = runner.lanes["MNQ"].core, runner.lanes["MES"].core
            assert mnq.risk.book is mes.risk.book and mnq.tracker is mes.tracker

            for core, cid in ((mnq, CONTRACTS["MNQ"]["id"]), (mes, CONTRACTS["MES"]["id"])):
                price = fake.price[cid]
                await core.orders.enter(OrderSide.BUY, 1, price - 10, price + 20, "side by side", ref_price=price)
            await _wait(lambda: all(c.orders.trade and c.orders.trade.state == TradeState.OPEN
                                    and c.orders.trade.stop_order_id for c in (mnq, mes)))
            # each market only ever touched its own contract's orders
            for core in (mnq, mes):
                t = core.orders.trade
                own = [o for o in fake.orders.values() if (o["customTag"] or "").startswith(t.tag)]
                assert {o["contractId"] for o in own} == {core.contract.id}
            snap = runner.snapshot()
            assert [m["symbol"] for m in snap["markets"]["lanes"]] == ["MNQ", "MES"]
            assert snap["markets"]["exposure"]["contracts"] == 2

            runner.controls.stop.set()  # shutdown flattens both markets
            await asyncio.wait_for(task, 20)
        assert all(p == 0 for p in fake.position.values())
        assert {t.exit_reason for c in (mnq, mes) for t in c.closed_trades} == {"bot shutdown"}

    run(go())


def test_markets_are_added_dropped_and_switched_without_a_restart(tmp_path):
    async def go():
        fake = FakeDesk()
        async with websockets.serve(fake.hub, "127.0.0.1", 0) as srv:
            port = srv.sockets[0].getsockname()[1]
            runner = make_desk(fake, port, "paper", tmp_path, markets=())
            ready = asyncio.Event()

            async def on_ready(core):
                core.schedule.must_be_flat = lambda ts: False
                ready.set()

            task = asyncio.create_task(runner.run(on_ready))
            await asyncio.wait_for(ready.wait(), 30)
            assert list(runner.lanes) == ["MNQ"]
            text = await runner.add_market("MES", "test")
            assert "Now trading MES" in text and list(runner.lanes) == ["MNQ", "MES"]
            assert runner.lanes["MES"].core.last_bar is not None  # warmed up like the first market
            guards = {c.orders.guard.max_entries_per_day for c in runner.cores()}
            assert guards == {2 * DEFAULT_MAX_ENTRIES_PER_DAY}  # the shared safety limit: one allowance per market
            for bad, why in (("NQ", "same product"), ("MES", "already trades"), ("XYZ", "Unknown market")):
                try:
                    await runner.add_market(bad, "test")
                except ValueError as exc:
                    assert why in str(exc)
                else:
                    raise AssertionError(f"{bad} should have been refused")

            # a market with an open trade takes no new trades and goes once it is flat
            mes = runner.lanes["MES"].core
            price = fake.price[CONTRACTS["MES"]["id"]]
            await mes.orders.enter(OrderSide.BUY, 1, price - 10, None, "test", ref_price=price)
            await _wait(lambda: mes.orders.position == 1)
            text = await runner.remove_market("MES", "test")
            assert "dropped as soon as its open trade is closed" in text and "MES" in runner.lanes
            await mes.orders.flatten_all("test close")
            await _wait(lambda: "MES" not in runner.lanes)

            text = await runner.switch_market("MNQ", "MGC", "test")
            assert list(runner.lanes) == ["MGC"] and runner.focus == "MGC" and "Stopped trading MNQ" in text
            from topstep_bot import market_pick as mp

            assert mp.load_today(runner.cfg)["markets"] == ["MGC"]  # a restart today keeps it
            assert runner.core.orders.guard.max_entries_per_day == DEFAULT_MAX_ENTRIES_PER_DAY
            try:
                await runner.remove_market("MGC", "test")
            except ValueError as exc:
                assert "only market left" in str(exc)
            runner.controls.stop.set()
            await asyncio.wait_for(task, 20)
        assert not {p for p, _ in fake.calls} & {"/api/Order/place"}  # paper: nothing sent to TopstepX

    run(go())


def test_actions_name_a_market(tmp_path):
    from topstep_bot.control import BotActions

    async def go():
        fake = FakeDesk()
        async with websockets.serve(fake.hub, "127.0.0.1", 0) as srv:
            port = srv.sockets[0].getsockname()[1]
            runner = make_desk(fake, port, "paper", tmp_path)
            ready = asyncio.Event()

            async def on_ready(core):
                ready.set()

            task = asyncio.create_task(runner.run(on_ready))
            await asyncio.wait_for(ready.wait(), 30)
            actions = BotActions(runner.core, runner.controls, desk=runner)
            view = await actions.handle("markets", {})
            assert [m["symbol"] for m in view["lanes"]] == ["MNQ", "MES"] and "MGC" in view["available"]
            text = (await actions.handle("status_text", {"market": "MES"}))["text"]
            assert "MESZ6" in text
            assert actions.core is runner.lanes["MNQ"].core  # the market shown is back after the request
            assert "Showing MES" in await actions.handle("focus_market", {"symbol": "MES"})
            assert actions.core is runner.lanes["MES"].core
            assert "MNQ" in (await actions.handle("markets_text", {}))["text"]
            runner.controls.stop.set()
            await asyncio.wait_for(task, 20)

    run(go())


def test_a_market_dropped_in_a_trade_is_managed_again_after_a_restart(tmp_path):
    from topstep_bot import market_pick as mp

    async def go():
        fake = FakeDesk()
        mes_id = CONTRACTS["MES"]["id"]
        async with websockets.serve(fake.hub, "127.0.0.1", 0) as srv:
            port = srv.sockets[0].getsockname()[1]
            runner = make_desk(fake, port, "live", tmp_path)
            runner.cfg.execution.flatten_on_shutdown = False

            async def on_ready(core):
                for c in runner.cores():
                    c.schedule.must_be_flat = lambda ts: False

            task = asyncio.create_task(runner.run(on_ready))
            await _wait(lambda: "MES" in runner.lanes and runner.lanes["MES"].core is not None, 30)
            mes = runner.lanes["MES"].core
            price = fake.price[mes_id]
            await mes.orders.enter(OrderSide.BUY, 1, price - 10, None, "test", ref_price=price)
            await _wait(lambda: mes.orders.trade and mes.orders.trade.state == TradeState.OPEN
                        and mes.orders.trade.stop_order_id)
            stop_id = mes.orders.trade.stop_order_id
            await runner.remove_market("MES", "test")  # no flatten: it stays until its trade is closed
            today = mp.load_today(runner.cfg)
            assert today["closing"] == ["MES"] and today["markets"] == ["MNQ"]
            runner.controls.stop.set()  # the PC "crashes" before the trade is closed
            await asyncio.wait_for(task, 20)
            assert fake.position[mes_id] == 1 and fake.orders[stop_id]["status"] == 1

            r2 = make_desk(fake, port, "live", tmp_path, markets=())

            async def on_ready2(core):
                for c in r2.cores():
                    c.schedule.must_be_flat = lambda ts: False

            task = asyncio.create_task(r2.run(on_ready2))
            await _wait(lambda: "MES" in r2.lanes and r2.lanes["MES"].core is not None, 30)
            lane = r2.lanes["MES"]
            assert lane.closing and lane.core.closing  # managed, but takes no new trades
            await _wait(lambda: lane.core.orders.trade is not None and lane.core.orders.trade.stop_order_id == stop_id)
            fake.position[mes_id] = 0  # its stop fills (or it is closed by hand)
            fake._push_position(mes_id)
            await _wait(lambda: "MES" not in r2.lanes)
            assert mp.load_today(r2.cfg)["closing"] == []
            r2.controls.stop.set()
            await asyncio.wait_for(task, 20)

    run(go())


def test_a_position_in_a_market_with_no_lane_is_flattened_at_start(tmp_path):
    from topstep_bot import market_pick as mp

    async def go():
        fake = FakeDesk()
        mgc_id = CONTRACTS["MGC"]["id"]
        async with websockets.serve(fake.hub, "127.0.0.1", 0) as srv:
            port = srv.sockets[0].getsockname()[1]
            runner = make_desk(fake, port, "live", tmp_path)
            until = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
            path = runner.cfg.account_dir / mp.TODAY_FILE
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"symbol": "MNQ", "markets": ["MNQ", "MES"], "until": until}), encoding="utf-8")
            fake.position[mgc_id] = 1
            fake.orders[55] = {"id": 55, "accountId": 7, "contractId": mgc_id, "type": 4, "side": 1, "size": 1, "status": 1,
                               "stopPrice": fake.price[mgc_id] - 5, "limitPrice": None, "customTag": None, "fillVolume": 0}
            stale = runner.cfg.data_path / "positions" / "main~MGC.json"
            stale.parent.mkdir(parents=True, exist_ok=True)
            stale.write_text("{}", encoding="utf-8")
            other = runner.cfg.data_path / "positions" / "c2~MGC.json"
            other.write_text("{}", encoding="utf-8")
            await runner.prepare()
            close = ("/api/Position/closeContract", {"accountId": 7, "contractId": mgc_id})
            cancel = ("/api/Order/cancel", {"accountId": 7, "orderId": 55})
            assert close in fake.calls and cancel in fake.calls and fake.calls.index(close) < fake.calls.index(cancel)
            assert fake.position[mgc_id] == 0 and "MGC" not in runner.lanes
            assert any(e["level"] == "critical" and "MGC" in e["message"] for e in runner.core.events)
            assert not stale.exists() and other.exists()  # this account's stale hedge-guard file only
            for lane in list(runner.lanes.values()):
                await lane.stop()
            await runner.client.close()
            runner.journal.close()

    run(go())
