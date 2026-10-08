import asyncio
import json

import httpx
import pytest
import websockets

from topstep_bot.api import parse
from topstep_bot.api.realtime import MarketStream, UserStream
from topstep_bot.api.rest import AuthError, ProjectXClient, ProjectXError
from topstep_bot.api.signalr import RS, HubConnection, decode, encode
from topstep_bot.models import OrderSide, OrderStatus, OrderType

from .conftest import run


class FakeApi:
    """Minimal stand-in for the ProjectX REST API."""

    def __init__(self):
        self.calls: list[tuple[str, dict, str | None]] = []
        self.fail_next: dict[str, list] = {}
        self.tokens = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body = json.loads(request.content or b"{}")
        self.calls.append((path, body, request.headers.get("authorization")))
        queued = self.fail_next.get(path)
        if queued:
            item = queued.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        if path == "/api/Auth/loginKey":
            if body.get("apiKey") != "good":
                return httpx.Response(200, json={"token": None, "success": False, "errorCode": 3, "errorMessage": "bad key"})
            self.tokens += 1
            return httpx.Response(200, json={"token": f"tok{self.tokens}", "success": True, "errorCode": 0})
        if path == "/api/Account/search":
            return httpx.Response(200, json={"accounts": [{"id": 7, "name": "COMBINE-1", "balance": 50000, "canTrade": True}], "success": True})
        if path == "/api/History/retrieveBars":
            bars = [{"t": "2026-03-03T15:05:00+00:00", "o": 2, "h": 3, "l": 1, "c": 2.5, "v": 9},
                    {"t": "2026-03-03T15:00:00+00:00", "o": 1, "h": 2, "l": 0.5, "c": 2, "v": 5}]
            return httpx.Response(200, json={"bars": bars, "success": True})
        if path == "/api/Order/place":
            return httpx.Response(200, json={"orderId": 99, "success": True, "errorCode": 0})
        if path == "/api/Contract/search":
            contracts = [
                {"id": "CON.F.US.MNQ.H26", "name": "MNQH6", "tickSize": 0.25, "tickValue": 0.5, "activeContract": True},
                {"id": "CON.F.US.MNQ.M26", "name": "MNQM6", "tickSize": 0.25, "tickValue": 0.5, "activeContract": False},
                {"id": "CON.F.US.ENQ.H26", "name": "NQH6", "tickSize": 0.25, "tickValue": 5, "activeContract": True},
            ]
            return httpx.Response(200, json={"contracts": contracts, "success": True})
        return httpx.Response(200, json={"success": True})


def client(api: FakeApi, key: str = "good") -> ProjectXClient:
    return ProjectXClient("user", key, transport=httpx.MockTransport(api.handler))


def test_login_and_bearer_token():
    api = FakeApi()

    async def go():
        async with client(api) as c:
            accounts = await c.search_accounts()
            assert accounts[0].name == "COMBINE-1" and accounts[0].balance == 50000
    run(go())
    assert api.calls[0][0] == "/api/Auth/loginKey"
    assert api.calls[1][2] == "Bearer tok1"


def test_bad_credentials_raise_auth_error():
    async def go():
        async with client(FakeApi(), key="bad") as c:
            with pytest.raises(AuthError, match="bad key"):
                await c.search_accounts()
    run(go())


def test_401_triggers_relogin_once():
    api = FakeApi()
    api.fail_next["/api/Account/search"] = [httpx.Response(401)]

    async def go():
        async with client(api) as c:
            assert await c.search_accounts()
    run(go())
    assert api.tokens == 2
    assert api.calls[-1][2] == "Bearer tok2"


def test_429_backs_off_and_retries(monkeypatch):
    api = FakeApi()
    api.fail_next["/api/Account/search"] = [httpx.Response(429)]
    slept = []

    async def fake_sleep(s):
        slept.append(s)

    async def go():
        monkeypatch.setattr(asyncio, "sleep", fake_sleep)
        async with client(api) as c:
            assert await c.search_accounts()
    run(go())
    assert slept and slept[0] >= 1


def test_success_false_raises_with_code():
    api = FakeApi()
    api.fail_next["/api/Order/cancel"] = [httpx.Response(200, json={"success": False, "errorCode": 5, "errorMessage": "nope"})]

    async def go():
        async with client(api) as c:
            with pytest.raises(ProjectXError) as info:
                await c.cancel_order(7, 1)
            assert info.value.code == 5
    run(go())


def test_order_placement_is_not_retried_on_network_error():
    api = FakeApi()
    api.fail_next["/api/Order/place"] = [httpx.ConnectError("boom")]

    async def go():
        async with client(api) as c:
            with pytest.raises(httpx.ConnectError):
                await c.place_order(7, "C", OrderType.MARKET, OrderSide.BUY, 1, custom_tag="x")
    run(go())
    assert sum(1 for p, _, _ in api.calls if p == "/api/Order/place") == 1


def test_place_order_body_and_brackets():
    api = FakeApi()

    async def go():
        async with client(api) as c:
            oid = await c.place_order(7, "C", OrderType.MARKET, OrderSide.SELL, 2, custom_tag="t1",
                                      stop_loss_ticks=10, take_profit_ticks=20)
            assert oid == 99
    run(go())
    body = [b for p, b, _ in api.calls if p == "/api/Order/place"][0]
    assert body["type"] == 2 and body["side"] == 1 and body["size"] == 2 and body["customTag"] == "t1"
    assert body["stopLossBracket"] == {"ticks": 10, "type": 4}
    assert body["takeProfitBracket"] == {"ticks": 20, "type": 1}


def test_bars_are_sorted_ascending():
    from datetime import datetime, timezone

    async def go():
        async with client(FakeApi()) as c:
            now = datetime.now(timezone.utc)
            bars = await c.retrieve_bars("C", now, now)
            assert bars[0].ts < bars[1].ts and bars[0].open == 1
    run(go())


def test_resolve_contract_picks_active_root_match():
    async def go():
        async with client(FakeApi()) as c:
            contract = await c.resolve_contract("MNQ")
            assert contract.id == "CON.F.US.MNQ.H26" and contract.root == "MNQ"
    run(go())


def test_parse_helpers():
    ts = parse.parse_ts("2025-04-21T19:45:52.1058087+00:00")
    assert ts.microsecond == 105808
    assert parse.root_from_name("ESZ5") == "ES" and parse.root_from_name("M2KH26") == "M2K"
    order = parse.order({"id": 1, "accountId": 2, "contractId": "C", "status": 2, "type": 4, "side": 1, "size": 3,
                         "stopPrice": 5138.0, "filledPrice": 5137.75, "customTag": "tag"})
    assert order.status == OrderStatus.FILLED and order.type == OrderType.STOP and order.side == OrderSide.SELL
    pos = parse.position({"accountId": 2, "contractId": "C", "type": 2, "size": 2, "averagePrice": 10})
    assert pos.size == -2
    assert list(parse.unwrap({"action": 1, "data": {"id": 5}})) == [{"id": 5}]
    assert list(parse.unwrap([{"price": 1}, {"price": 2}])) == [{"price": 1}, {"price": 2}]


# ------------------------------------------------------------------ SignalR

def test_signalr_framing_roundtrip():
    frame = encode({"type": 1, "target": "X", "arguments": [1]}) + encode({"type": 6})
    assert frame.count(RS) == 2
    assert decode(frame) == [{"type": 1, "target": "X", "arguments": [1]}, {"type": 6}]


def test_hub_handshake_subscribe_dispatch_and_reconnect():
    """Run the hub client against a real local WebSocket server speaking SignalR JSON."""

    async def go():
        received: list[dict] = []
        connections = 0
        events: list[tuple] = []

        async def server(ws):
            nonlocal connections
            connections += 1
            assert "access_token=tok" in ws.request.path
            hs = decode(await ws.recv())
            assert hs[0] == {"protocol": "json", "version": 1}
            await ws.send("{}" + RS)
            msg = decode(await ws.recv())[0]
            received.append(msg)
            await ws.send(encode({"type": 1, "target": "GatewayQuote", "arguments": ["CON.X", {"lastPrice": 101.5}]}))
            if connections == 1:
                await asyncio.sleep(0.05)
                await ws.close()  # force a reconnect
            else:
                await asyncio.sleep(5)

        async with websockets.serve(server, "127.0.0.1", 0) as srv:
            port = srv.sockets[0].getsockname()[1]

            async def token():
                return "tok"

            hub = HubConnection(f"http://127.0.0.1:{port}/hubs/market", token, reconnect_delays=(0.05,))
            hub.on("GatewayQuote", lambda cid, data: events.append((cid, data["lastPrice"])))
            hub.add_subscription("SubscribeContractQuotes", "CON.X")
            task = asyncio.create_task(hub.run())
            for _ in range(100):
                if connections >= 2 and len(events) >= 2:
                    break
                await asyncio.sleep(0.02)
            await hub.stop()
            task.cancel()
        assert connections >= 2
        assert all(m["target"] == "SubscribeContractQuotes" and m["arguments"] == ["CON.X"] for m in received)
        assert events[0] == ("CON.X", 101.5)

    run(go())


def test_stream_wrappers_parse_payloads():
    async def go():
        async def token():
            return "t"

        market = MarketStream("http://x/hubs/market", token, "CON.X")
        quotes, ticks = [], []
        market.on_quote = lambda q: quotes.append((q.last, q.bid, q.ask))
        market.on_tick = lambda t: ticks.append((t.price, t.size))
        await market._quote("CON.X", {"lastPrice": 10.0, "bestBid": 9.75})
        await market._quote("CON.X", {"bestAsk": 10.25})  # partial update merges
        await market._trade("CON.X", [{"price": 10.0, "volume": 2, "timestamp": "2026-03-03T15:00:00Z"}])
        await market._quote("OTHER", {"lastPrice": 1.0})  # ignored
        assert quotes == [(10.0, 9.75, None), (10.0, 9.75, 10.25)]
        assert ticks == [(10.0, 2.0)]

        user = UserStream("http://x/hubs/user", token, account_id=7)
        orders = []
        user.on_order = lambda o: orders.append(o.id)
        await user._order({"id": 1, "accountId": 7, "status": 1, "type": 2, "side": 0, "size": 1})
        await user._order({"action": 1, "data": {"id": 2, "accountId": 7, "status": 2}})
        await user._order({"id": 3, "accountId": 8, "status": 1})  # other account
        assert orders == [1, 2]

    run(go())
