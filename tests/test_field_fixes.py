"""Fixes found in the first live week's data (Oct 2026): realtime hubs stuck on a refused session token,
days not recorded when the bot was stopped before the close, ideas scored from a stop price already passed,
the bot's own trade row overwritten by its idea row, and the hand-versus-plan report."""

import asyncio
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

import httpx
import websockets

from topstep_bot import hand_review
from topstep_bot.api import signalr
from topstep_bot.api.parse import format_ts, parse_ts
from topstep_bot.api.rest import ProjectXClient
from topstep_bot.api.signalr import RS, HubConnection, decode, encode
from topstep_bot.broker.paper import PaperBroker
from topstep_bot.config import BotConfig
from topstep_bot.factory import build_core
from topstep_bot.instruments import offline_contract
from topstep_bot.journal import Journal
from topstep_bot.knowledge import KnowledgeBase
from topstep_bot.models import Bar, OrderSide
from topstep_bot.recommendations import Recommendation, RecommendationBook
from topstep_bot.sessions import trading_day_of

from .conftest import ct, run
from .test_live_runner import FakeTopstepX, make_runner

CT = ZoneInfo("America/Chicago")

# ----------------------------------------------------------------- realtime hubs


def test_a_hub_signs_in_again_when_the_server_keeps_closing_new_connections():
    """Live: once a second bot signed in with the same API key, TopstepX closed every new connection made with
    the first bot's token right after the handshake, every 30 s for hours, until a restart signed in again."""

    async def go():
        tokens: list[str] = []
        renewals: list[str] = []
        events: list[float] = []

        async def server(ws):
            token = ws.request.path.split("access_token=")[1]
            tokens.append(token)
            decode(await ws.recv())
            await ws.send("{}" + RS)
            if token == "old":
                await ws.send(encode({"type": 7}))  # SignalR close, no reason: what TopstepX sent
                return
            await ws.send(encode({"type": 1, "target": "GatewayQuote", "arguments": ["CON.X", {"lastPrice": 1.5}]}))
            await asyncio.sleep(5)

        async with websockets.serve(server, "127.0.0.1", 0) as srv:
            port = srv.sockets[0].getsockname()[1]

            async def token():
                return "old"

            async def renew(refused):
                renewals.append(refused)
                return "fresh"

            hub = HubConnection(f"http://127.0.0.1:{port}/hubs/user", token, reconnect_delays=(0.02,),
                                token_renewer=renew)
            hub.on("GatewayQuote", lambda cid, data: events.append(data["lastPrice"]))
            task = asyncio.create_task(hub.run())
            for _ in range(200):
                if events:
                    break
                await asyncio.sleep(0.02)
            await hub.stop()
            task.cancel()
        assert events == [1.5]
        assert renewals == ["old"]  # signed in once, after the quick closes
        assert tokens[:signalr.QUICK_CLOSES_BEFORE_NEW_TOKEN] == ["old"] * signalr.QUICK_CLOSES_BEFORE_NEW_TOKEN
        assert tokens[-1] == "fresh"

    run(go())


def test_a_connection_that_worked_for_a_while_reconnects_quickly_again(monkeypatch):
    """The delay before reconnecting used to keep growing for the life of the bot: after six drops ever, every
    later drop waited 30 s without prices or fills, even after hours of a healthy connection."""
    monkeypatch.setattr(signalr, "HEALTHY_SECONDS", 0.05)

    async def go():
        opened: list[float] = []
        loop = asyncio.get_running_loop()

        async def server(ws):
            opened.append(loop.time())
            decode(await ws.recv())
            await ws.send("{}" + RS)
            await asyncio.sleep(0.1)  # healthy for longer than HEALTHY_SECONDS, then dropped
            await ws.close()

        async with websockets.serve(server, "127.0.0.1", 0) as srv:
            port = srv.sockets[0].getsockname()[1]

            async def token():
                return "t"

            hub = HubConnection(f"http://127.0.0.1:{port}/hubs/market", token, reconnect_delays=(0.01, 5, 5))
            task = asyncio.create_task(hub.run())
            for _ in range(300):
                if len(opened) >= 4:
                    break
                await asyncio.sleep(0.01)
            await hub.stop()
            task.cancel()
        assert len(opened) >= 4  # without the reset the second reconnect would wait 5 s

    run(go())


def test_renew_token_signs_in_once_for_both_hubs():
    logins = []

    def handler(request: httpx.Request) -> httpx.Response:
        logins.append(request.url.path)
        return httpx.Response(200, json={"success": True, "token": f"tok{len(logins)}"})

    async def go():
        client = ProjectXClient("u", "k", transport=httpx.MockTransport(handler))
        first = await client.get_token()
        a, b = await asyncio.gather(client.renew_token(first), client.renew_token(first))
        assert a == b == "tok2" and len(logins) == 2
        assert await client.renew_token("someone else's old token") == "tok2" and len(logins) == 2
        await client.close()

    run(go())


# ----------------------------------------------------------------- missed day records


def weekdays_back(day, n):
    while n:
        day -= timedelta(days=1)
        if day.weekday() < 5:
            n -= 1
    return day


class FakeWithHistory(FakeTopstepX):
    """TopstepX with closed trades on earlier days (Trade/search honours its start time)."""

    def __init__(self, fills):
        super().__init__()
        self.fills = fills

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/Trade/search":
            import json

            start = parse_ts(json.loads(request.content)["startTimestamp"])
            trades = [f for f in self.fills if parse_ts(f["creationTimestamp"]) >= start]
            return httpx.Response(200, json={"success": True, "errorCode": 0, "trades": trades})
        return super().handler(request)


def test_days_missed_at_the_close_are_recorded_and_raise_the_floor(tmp_path):
    """Live: the bot was often stopped after the session, before its end of day, so most days had no record:
    the Consistency Target and Combine progress missed their profit and the trailing MLL never saw their close."""
    today = trading_day_of(datetime.now(UTC), CT)
    day_a, day_b, before = weekdays_back(today, 3), weekdays_back(today, 2), weekdays_back(today, 4)

    def fill(fid, day, pnl, fees=1.0):
        ts = datetime.combine(day, time(10, 0), tzinfo=CT).astimezone(UTC)
        return {"id": fid, "orderId": fid, "contractId": "CON.F.US.MNQ.Z26", "side": 1, "size": 1, "price": 1.0,
                "creationTimestamp": format_ts(ts), "profitAndLoss": pnl, "fees": fees}

    fake = FakeWithHistory([fill(1, day_a, 351.0), fill(2, day_a, 251.0), fill(3, day_b, -299.0)])

    async def go():
        async with websockets.serve(fake.hub, "127.0.0.1", 0) as srv:
            runner = make_runner(fake, srv.sockets[0].getsockname()[1], "live", tmp_path)
            label = "50KTC-1"
            pre = Journal(runner.cfg.journal_path)
            pre.set_state(f"last_eod:{label}", before.isoformat())
            pre.close()
            ready = asyncio.Event()

            async def on_ready(core):
                ready.set()

            task = asyncio.create_task(runner.run(on_ready))
            await asyncio.wait_for(ready.wait(), 30)
            daily = {r["trading_day"]: r for r in runner.journal.daily(label)}
            floor, best = runner.tracker.floor, runner.book.best_prior_day
            last_eod = runner.journal.get_state(f"last_eod:{label}")
            runner.controls.stop.set()
            await asyncio.wait_for(task, 20)
            return daily, floor, best, last_eod

    daily, floor, best, last_eod = run(go())
    a, b = daily[day_a.isoformat()], daily[day_b.isoformat()]
    # Walked back from the current balance (50,000): day B lost 300, so day A closed at 50,300 after making 600.
    assert (b["end_balance"], b["net_pnl"], b["trades"]) == (50_000, -300, 1)
    assert (a["end_balance"], a["start_balance"], a["net_pnl"], a["trades"]) == (50_300, 49_700, 600, 2)
    assert floor == 48_300  # day A's close raised the trailing floor (50,300 - 2,000)
    assert best == 600  # and it is the best day for the Consistency Target
    assert last_eod == day_b.isoformat()


# ----------------------------------------------------------------- ideas and their records


def core_with_book(tmp_path):
    mnq = offline_contract("MNQ")
    cfg = BotConfig.model_validate({"data_dir": str(tmp_path), "strategy": {"name": "adaptive"}})
    now = [ct(2026, 10, 14, 8, 50)]
    core = build_core(cfg, mnq, PaperBroker(mnq, 50_000), clock=lambda: now[0], account_label="T",
                      journal=Journal(":memory:"))
    core.recommender = RecommendationBook(core)
    core.attach_knowledge(KnowledgeBase(tmp_path / "kb.json", min_samples=5))
    return core, now


def test_an_idea_whose_stop_was_already_passed_is_not_scored(tmp_path):
    """Live (Oct 14): an opening-range long signalled at 7836 was checked at 7821.5, under its 7826.75 stop. It
    was "stopped out" at a higher price than its entry and saved as a +1R win in the knowledge ledger."""
    core, now = core_with_book(tmp_path)
    book = core.recommender
    rec = Recommendation(id="R1", created=now[0], strategy="orb", title="Opening Range Breakout", active=True,
                         side=OrderSide.BUY, entry=7821.5, stop=7826.75, target=7854.5, size=0, risk_usd=None,
                         reason="close broke opening-range high", status="skipped", note="stop is on the wrong side")
    book.items.appendleft(rec)
    before = len(core.knowledge.obs)
    book._track(Bar(now[0], 7821.5, 7830.0, 7818.0, 7825.0))
    assert rec.result == "void" and rec.outcome_r is None and "not scored" in rec.note
    assert len(core.knowledge.obs) == before  # nothing learned from it
    assert "not scored" in book.text()
    assert all(s["wins"] == 0 and s["closed"] == 0 for s in book.snapshot()["summary"])


def test_the_bots_trade_and_its_idea_keep_separate_journal_rows(tmp_path):
    core, now = core_with_book(tmp_path)
    book = core.recommender
    common = dict(created=now[0], strategy="vwap_pullback", title="VWAP Trend Pullback", side=OrderSide.SELL,
                  entry=7782.5, stop=7800.5, target=7746.75, size=1, risk_usd=90.0, reason="pullback")
    book._store(Recommendation(id="R1", active=False, status="idea", **common), new=True)
    book._store(Recommendation(id="R2", active=True, status="taken", **common), new=True)
    rows = core.journal.recommendations()
    assert sorted(r["active"] for r in rows) == [0, 1]


def add_trade(j, tag, day, entry_time, strategy, side, net, r, mfe, exit_reason, entry_reason="", fees=2.44):
    j.conn.execute(
        "INSERT INTO trades (tag, trading_day, account, contract, strategy, side, size, entry_time, exit_time, "
        "gross_pnl, fees, net_pnl, r_multiple, mfe_r, entry_reason, exit_reason) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (tag, day, "A", "MESZ6", strategy, side, 2, entry_time, entry_time, net + fees, fees, net, r, mfe,
         entry_reason, exit_reason))
    j.conn.commit()


def test_hand_review_scores_closing_by_hand_against_the_plan(tmp_path):
    j = Journal(tmp_path / "j.db")
    # The bot's VWAP pullback short, closed from the dashboard at +0.2R; its plan went on to its target (+2R).
    add_trade(j, "t1", "2026-10-16", "2026-10-16T14:55:30+00:00", "adaptive", "SHORT", 16.0, 0.2, 0.29,
              "closed from dashboard", "VWAP Trend Pullback: pullback to VWAP")
    j.record_decision({"id": "d1", "day": "2026-10-16", "created": "2026-10-16T14:55:00+00:00", "status": "taken",
                       "strategy": "vwap_pullback", "tag": "t1"})
    j.record_recommendation({"id": "20261016-1455-vwap_pullback-SHORT", "created": "2026-10-16T14:55:00+00:00",
                             "strategy": "vwap_pullback", "active": False, "side": "SHORT", "entry": 7782.5,
                             "stop": 7800.5, "target": 7746.75, "size": 1, "risk_usd": 90.0, "reason": "",
                             "status": "closed", "note": "target hit", "result": "won", "exit_price": 7746.75,
                             "outcome_usd": 177.53, "outcome_r": 2.0})
    # One bot trade left alone, and two of your own: a winner closed early and a loser that ran to its stop.
    add_trade(j, "t2", "2026-10-16", "2026-10-16T13:35:00+00:00", "adaptive", "SHORT", 130.0, 1.96, 1.96, "profit target")
    add_trade(j, "t3", "2026-10-15", "2026-10-15T14:26:00+00:00", "manual", "LONG", 20.0, 0.3, 0.9, "closed from dashboard")
    add_trade(j, "t4", "2026-10-15", "2026-10-15T15:59:00+00:00", "manual", "LONG", -85.0, -1.0, 0.3, "stop loss")
    rep = hand_review.review(j, "A")
    assert rep["bot_alone"]["n"] == 1 and rep["bot_alone"]["net_usd"] == 130.0
    (row,) = rep["stepped_in"]
    assert row["plan_r"] == 2.0 and row["diff_r"] == -1.8 and row["strategy"] == "vwap_pullback"
    assert row["diff_usd"] == round(-1.8 * (18.44 / 0.2), 2)
    own = rep["own"]
    assert (own["n"], own["wins"], own["avg_win_r"], own["avg_loss_r"]) == (2, 1, 0.3, -1.0)
    assert (own["hand_win_closed_r"], own["hand_win_best_r"]) == (0.3, 0.9)
    text = hand_review.text(rep)
    assert "closing them cost 1.8R" in text and "closed at +0.30R on average, after reaching +0.90R" in text
    assert hand_review.review(Journal(tmp_path / "empty.db"), "A") is None
    j.close()
