"""The Setup tab's Accounts panel (check, add, edit, remove, API keys) and the bot picking its own market."""

import asyncio
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import yaml

from topstep_bot import account_check
from topstep_bot import market_pick as mp
from topstep_bot.api import rest
from topstep_bot.config import BotConfig, ConfigError, Secrets, config_from_text, load_config
from topstep_bot.config_edit import REMOVE, set_section
from topstep_bot.knowledge import Observation
from topstep_bot.models import Account, Fill, Order, OrderSide, OrderStatus, OrderType, Position

from .conftest import run
from .test_dashboard_setup import FORM, setup_scenario
from .test_dashboard_setup import clean_env as clean_env  # noqa: F401 - the fixture, by name

# ------------------------------------------------------------------------------ a fake TopstepX with two logins

ACCOUNTS = {
    "main-key": [Account(101, "50KTC-V2-11-22", 51_200.0, True, True, True),
                 Account(102, "100KTC-V2-33", 100_000.0, True, True, True),
                 Account(103, "LFA-11", 52_000.0, True, True, False)],  # Live Funded: never offered
    "partner-key": [Account(201, "PRAC-V2-77", 150_300.0, False, True, True)],
}


class FakeTopstepX:
    def __init__(self, username, api_key, *args, **kwargs):
        self.api_key = api_key

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def search_accounts(self, only_active=True):
        if self.api_key not in ACCOUNTS:
            raise rest.ProjectXError("invalid credentials")
        return ACCOUNTS[self.api_key]

    async def search_open_positions(self, account_id):
        return [Position(account_id, "CON.F.US.MNQ.Z26", 2, 20_100.25)] if account_id == 101 else []

    async def search_open_orders(self, account_id):
        return [Order(9, account_id, "CON.F.US.MNQ.Z26", OrderType.STOP, OrderSide.SELL, 2, OrderStatus.OPEN,
                      stop_price=20_050.0)] if account_id == 101 else []

    async def search_trades(self, account_id, start, end=None):
        now = datetime.now(UTC)
        return [Fill(1, 1, "CON.F.US.MNQ.Z26", OrderSide.BUY, 1, 20_000.0, now),
                Fill(2, 2, "CON.F.US.MNQ.Z26", OrderSide.SELL, 1, 20_010.0, now, pnl=20.0, fees=1.0),
                Fill(3, 3, "CON.F.US.MNQ.Z26", OrderSide.SELL, 1, 20_010.0, now, pnl=99.0, fees=1.0, voided=True)]


@pytest.fixture
def topstepx(monkeypatch, clean_env):  # noqa: F811 - clean_env is the imported fixture
    monkeypatch.setattr(rest, "ProjectXClient", FakeTopstepX)
    for var in ("TOPSTEPX_API_KEY_PARTNER", "TOPSTEPX_USERNAME_PARTNER", "TOPSTEPX_API_KEY_C2", "TOPSTEPX_USERNAME_C2"):
        monkeypatch.delenv(var, raising=False)
    yield


def secrets(**logins):
    from topstep_bot.config import Login

    return Secrets(username="me", api_key="main-key", logins={k: Login(**v) for k, v in logins.items()})


# ------------------------------------------------------------------------------ the account check

def test_the_check_reads_everything_topstepx_knows(topstepx):
    cfg = BotConfig.model_validate({"account": {"account_id": 101, "plan": "100K"}})
    report = run(account_check.check(cfg, secrets(partner={"username": "pal", "api_key": "partner-key"})))
    assert [(k["name"], k["ok"], k["accounts"]) for k in report["keys"]] == [("main", True, 3), ("partner", True, 1)]
    (row,) = report["accounts"]
    assert row["topstepx"]["name"] == "50KTC-V2-11-22" and row["topstepx"]["balance"] == 51_200.0
    assert row["positions"] == ["long 2 MNQ Z26 @ 20,100.25"] and row["orders"] == ["sell 2 MNQ Z26 @ 20,050.00"]
    assert row["today"] == {"trades": 1, "pnl": 19.0, "fees": 1.0}  # the voided fill doesn't count
    assert any("50K account, but the settings say 100K" in i for i in row["issues"]) and row["status"] == "error"
    # ready to add: the other accounts both keys can see; the Live Funded Account is left out
    assert {a["id"] for a in report["available"]} == {102, 201} and report["excluded"] == ["LFA-11"]
    practice = next(a for a in report["available"] if a["id"] == 201)
    assert (practice["key"], practice["plan"], practice["stage"], practice["can_trade"]) == ("partner", "150K", "practice", False)
    assert "Not set up yet" in account_check.text(report)


def test_a_wrong_key_or_a_missing_account_is_reported_not_raised(topstepx):
    cfg = BotConfig.model_validate({"accounts": [{"name": "c1", "account_id": 101},
                                                 {"name": "c2", "account_id": 999, "login": "partner"},
                                                 {"name": "c3", "account_id": 102, "login": "gone"}]})
    report = run(account_check.check(cfg, secrets(partner={"api_key": "wrong"})))
    rows = {r["name"]: r for r in report["accounts"]}
    assert rows["c1"]["status"] == "ok" and rows["c1"]["progress"]["profit"] == 1_200.0
    assert "could not log in" in rows["c2"]["issues"][0] and "isn't stored" in rows["c3"]["issues"][0]
    assert not report["keys"][1]["ok"] and "did not accept" in report["keys"][1]["error"]


def test_plan_comes_from_the_balance_when_the_name_doesnt_say():
    assert account_check.account_hints(Account(1, "S1MAR0912345", 100_400.0)) == ("100K", None)
    assert account_check.account_hints(Account(1, "XFA-123", 1_000.0)) == (None, "express")


# ------------------------------------------------------------------------------ adding, editing and removing accounts

ONE = ("mode: paper  # keep me\naccount:\n  plan: 50K\n  stage: combine\n  account_id: 101\n"
       "instrument:\n  symbol: MNQ\nrisk:\n  risk_per_trade: 150\n  personal_daily_loss_limit: 500\n")


def test_adding_a_second_account_from_the_dashboard(tmp_path, topstepx):
    data = tmp_path / "data"
    data.mkdir()
    (data / "journal_paper.db").write_text("trades")
    (data / "knowledge_MNQ_5m.json").write_text("{}")
    (data / "knowledge_MNQ_5m_longrun.json").write_text("{}")  # shared: stays where it is
    (data / "controller.json").write_text(json.dumps({"modes": {"main": "live"}}))

    async def body(ctl, http, path):
        ctl.secrets.username, ctl.secrets.api_key = "me", "main-key"
        r = (await http.post("/api/setup/accounts/check", json={})).json()
        assert r["ok"] and r["accounts"][0]["name"] == "main" and [a["id"] for a in r["available"]] == [102]
        new = {"name": "c2", "account_id": 102, "account_name": "100KTC-V2-33", "plan": "100K", "stage": "combine",
               "calls": "own", "strategy": "orb", "market": "AUTO", "risk_per_trade": "300", "daily_loss_limit": "500",
               "max_trades": "4", "key": "main"}
        r = (await http.post("/api/setup/accounts/save", json={**new, "first_name": "main"})).json()
        assert not r["ok"] and "reserved" in r["message"]
        r = (await http.post("/api/setup/accounts/save", json={**new, "first_name": "c1"})).json()
        assert r["ok"] and "your first account is c1" in r["message"], r
        text = path.read_text(encoding="utf-8")
        assert "# keep me" in text  # the rest of the file is untouched
        entries = yaml.safe_load(text)["accounts"]
        assert entries[0] == {"name": "c1", "account_id": 101}
        assert entries[1] == {"name": "c2", "account_id": 102, "plan": "100K", "stage": "combine",
                              "account_name": "100KTC-V2-33", "instrument": {"auto_pick": True},
                              "strategy": {"name": "orb"}, "risk": {"risk_per_trade": 300}, "mode": "paper"}
        # the daily limit equals the shared one, so it isn't copied: a change there reaches c2 too
        assert list(ctl.bots) == ["c1", "c2"] and ctl.multi and ctl.bots["c2"].mode == "paper"
        assert ctl.bots["c1"].mode == "live"  # the mode chosen on the dashboard moved with the account
        assert (data / "accounts" / "c1" / "journal_paper.db").read_text() == "trades"
        assert (data / "accounts" / "c1" / "knowledge_MNQ_5m.json").exists()
        assert not (data / "accounts" / "c1" / "knowledge_MNQ_5m_longrun.json").exists()
        assert (data / "journal_paper.db").exists()  # copied, not moved
        c2 = ctl.account_cfg("c2")
        assert c2.instrument.auto_pick and c2.strategy.name == "orb" and c2.risk.risk_per_trade == 300

        st = (await http.post("/api/setup/state")).json()
        assert st["multi"] and [p["name"] for p in st["profiles"]] == ["c1", "c2"]
        assert st["profiles"][1]["market"] == "AUTO" and st["profiles"][1]["calls"] == "own"

        # edit: copy c1's calls instead - the market and strategy go, the risk stays
        r = (await http.post("/api/setup/accounts/save", json={**new, "original": "c2", "calls": "follow",
                                                                  "follow": "c1"})).json()
        assert r["ok"], r
        c2 = load_config(path).profile("c2")
        assert c2.follow == "c1" and not c2.instrument and not c2.strategy and c2.mode == "paper"
        r = (await http.post("/api/setup/accounts/remove", json={"name": "c1"})).json()
        assert not r["ok"] and "copy c1's calls" in r["message"]
        r = (await http.post("/api/setup/accounts/remove", json={"name": "c2"})).json()
        assert r["ok"] and list(ctl.bots) == ["c1"]
        r = (await http.post("/api/setup/accounts/remove", json={"name": "c1"})).json()
        assert not r["ok"] and "only account" in r["message"]

    setup_scenario(tmp_path, body, ONE)


def test_a_running_bot_blocks_the_switch_to_an_accounts_list(tmp_path, topstepx):
    async def body(ctl, http, path):
        ctl.secrets.username, ctl.secrets.api_key = "me", "main-key"
        await ctl.bot.start("test")
        r = (await http.post("/api/setup/accounts/save", json={
            "name": "c2", "account_id": 102, "plan": "100K", "stage": "combine", "first_name": "c1"})).json()
        assert not r["ok"] and "Stop the bot first" in r["message"]
        assert "accounts" not in yaml.safe_load(path.read_text(encoding="utf-8"))

    setup_scenario(tmp_path, body, ONE)


def test_api_keys_are_tested_before_they_are_saved(tmp_path, topstepx):
    async def body(ctl, http, path):
        ctl.secrets.username, ctl.secrets.api_key = "me", "main-key"
        r = (await http.post("/api/setup/keys/add", json={"name": "partner", "username": "pal", "api_key": "wrong"})).json()
        assert not r["ok"] and "Nothing was saved" in r["message"] and "partner" not in ctl.secrets.logins
        r = (await http.post("/api/setup/keys/add", json={"name": "Partner", "username": "pal", "api_key": "partner-key"})).json()
        assert r["ok"] and "sees 1 account" in r["message"]
        env = (tmp_path / ".env").read_text(encoding="utf-8")
        assert "TOPSTEPX_API_KEY_PARTNER=partner-key" in env and "TOPSTEPX_USERNAME_PARTNER=pal" in env
        r = (await http.post("/api/setup/accounts/check", json={})).json()
        assert {a["id"] for a in r["available"]} == {102, 201}
        st = (await http.post("/api/setup/state")).json()
        assert st["keys"] == ["main", "partner"] and "partner-key" not in json.dumps(st)  # never sent back
        # an account that uses the key keeps it from being removed
        r = (await http.post("/api/setup/accounts/save", json={
            "name": "p1", "account_id": 201, "plan": "150K", "stage": "practice", "key": "partner", "first_name": "c1"})).json()
        assert r["ok"], r
        assert load_config(path).profile("p1").login == "partner"
        r = (await http.post("/api/setup/keys/remove", json={"name": "partner"})).json()
        assert not r["ok"] and "p1 use this key" in r["message"]

    setup_scenario(tmp_path, body, ONE)


def test_the_main_form_can_let_the_bot_pick_the_market(tmp_path, topstepx):
    async def body(ctl, http, path):
        ctl.secrets.username, ctl.secrets.api_key = "me", "main-key"
        r = (await http.post("/api/setup/save", json={**FORM, "account_id": 101, "symbol": "AUTO", "fallback": "MES"})).json()
        assert r["ok"], r
        cfg = load_config(path)
        assert cfg.instrument.auto_pick and cfg.instrument.symbol == "MES"
        st = (await http.post("/api/setup/state")).json()
        assert st["values"]["symbol"] == "AUTO" and st["values"]["fallback"] == "MES"
        r = (await http.post("/api/setup/save", json={**FORM, "account_id": 101, "symbol": "MNQ"})).json()
        assert r["ok"] and not load_config(path).instrument.auto_pick

    setup_scenario(tmp_path, body, ONE)


def test_sections_are_rewritten_whole_and_everything_else_is_kept():
    text = "mode: paper  # top\naccounts:  # mine\n  - name: c1\n# about the next one\nmulti_account:\n  stagger_seconds: 3\n"
    new = set_section(text, "accounts", [{"name": "c1", "account_id": 1}, {"name": "c2", "risk": {"risk_per_trade": 300}}])
    assert yaml.safe_load(new)["accounts"][1] == {"name": "c2", "risk": {"risk_per_trade": 300}}
    assert "accounts:  # mine" in new and "# about the next one" in new and "mode: paper  # top" in new
    assert "accounts" not in yaml.safe_load(set_section(text, "accounts", REMOVE))
    assert yaml.safe_load(set_section("mode: paper\n", "accounts", [{"name": "c1"}]))["accounts"] == [{"name": "c1"}]


# ------------------------------------------------------------------------------ the bot picks its market

def test_auto_pick_settings_are_checked():
    with pytest.raises(ConfigError, match="can't both be set"):
        config_from_text("instrument: {auto_pick: true, contract_id: CON.F.US.MNQ.Z26}\n")
    with pytest.raises(ConfigError, match="unknown market"):
        config_from_text("instrument: {auto_pick: true, candidates: [MNQ, XYZ]}\n")
    cfg = BotConfig.model_validate({"instrument": {"auto_pick": True, "candidates": ["mes", "MNQ", "mes"]}})
    assert mp.candidates_for(cfg) == ["MES", "MNQ"]


def obs(day, strategy, r, usd=10.0, slot="open", time="09:00"):
    return Observation(day=day, time=time, strategy=strategy, side="LONG", slot=slot, regime="calm", r=r, usd=usd,
                       source="train")


def days(n):
    start = datetime(2026, 6, 1)
    return [(start + timedelta(days=i)).date().isoformat() for i in range(n)]


def test_markets_are_scored_only_on_days_held_back_from_learning():
    cfg = BotConfig.model_validate({"knowledge": {"min_samples": 3}})
    d = days(30)
    # orb works all month; ema_trend worked early (learned) and fails on the held-back days
    o = [obs(x, "orb_momentum", 1.0) for x in d] + [obs(x, "ema_trend", 1.0 if x < d[20] else -1.0, time="10:00") for x in d]
    cand = mp.score_observations(cfg, "MNQ", o, d)
    assert cand.test_days == 10 and cand.trades == 20  # both pass what was learned earlier; both are counted
    assert cand.r_per_day == 0.0 and not cand.eligible and "no edge" in cand.note
    # a strategy that never worked before the split is not taken later, however well it does then
    o = [obs(x, "orb_momentum", 1.0) for x in d] + [obs(x, "ema_trend", -1.0 if x < d[20] else 3.0, time="10:00") for x in d]
    cand = mp.score_observations(cfg, "MNQ", o, d)
    assert cand.trades == 10 and cand.r_per_day == 1.0 and cand.eligible and cand.win_rate == 1.0


def test_signals_too_big_for_the_risk_or_too_few_are_not_picked():
    cfg = BotConfig.model_validate({"knowledge": {"min_samples": 3}, "strategy": {"name": "orb_momentum"}})
    d = days(30)
    cand = mp.score_observations(cfg, "NQ", [obs(x, "orb_momentum", 1.0, usd=None) for x in d], d)
    assert cand.trades == 0 and "too big" in cand.note and not cand.eligible
    cand = mp.score_observations(cfg, "MES", [obs(x, "orb_momentum", 1.0) for x in d[-4:]], d)
    assert cand.trades == 4 and "too few" in cand.note


def test_choosing_keeps_the_current_market_on_a_close_call_and_falls_back_without_an_edge():
    def c(symbol, r):
        return mp.Candidate(symbol, days=30, test_days=10, trades=12, r_per_day=r, usd_per_day=r * 150, sized=1.0)

    assert mp.choose([c("MNQ", 0.30), c("MES", 0.32)], "MNQ", "MNQ")[0:3:2] == ("MNQ", True)
    assert mp.choose([c("MNQ", 0.10), c("MES", 0.32)], "MNQ", "MNQ")[0] == "MES"
    symbol, reason, _ = mp.choose([c("MNQ", -0.1), c("MES", 0.0)], "MES", "M2K")
    assert symbol == "M2K" and "your set market" in reason


def test_a_pick_is_saved_reused_and_kept_for_an_open_position(tmp_path):
    from topstep_bot.backtest.data import synthetic_bars
    from topstep_bot.instruments import offline_contract

    cfg = BotConfig.model_validate({"data_dir": str(tmp_path), "instrument": {"auto_pick": True, "candidates": ["MNQ", "MES"]}})
    loaded = []

    async def load(symbol):
        loaded.append(symbol)
        return offline_contract(symbol), synthetic_bars(symbol, days=45, seed=7)

    now = datetime(2026, 10, 12, 12, tzinfo=UTC)
    pick = run(mp.pick_market(cfg, load, now=now))
    assert loaded == ["MNQ", "MES"] and pick.symbol in ("MNQ", "MES") and len(pick.candidates) == 2
    assert all(c.test_days >= 9 for c in pick.candidates) and pick.text().startswith("Market picked")
    assert mp.load_pick(cfg).symbol == pick.symbol == mp.current_symbol(cfg)
    assert mp.fresh(mp.load_pick(cfg), cfg, now + timedelta(hours=5))
    assert not mp.fresh(mp.load_pick(cfg), cfg, now + timedelta(hours=25))
    cfg.risk.risk_per_trade = 300  # what the pick depends on changed: pick again
    assert not mp.fresh(mp.load_pick(cfg), cfg, now)
    held = run(mp.pick_market(cfg, load, now=now, held="MES"))
    assert held.symbol == "MES" and held.kept and loaded == ["MNQ", "MES"]  # nothing scored


def test_the_bot_picks_its_market_when_it_starts(tmp_path):
    from topstep_bot.instruments import SPECS

    from .test_live_runner import FakeTopstepX as FakeServer
    from .test_live_runner import make_runner

    class Server(FakeServer):
        def handler(self, request):
            if request.url.path == "/api/Contract/search":
                sym = json.loads(request.content)["searchText"]
                spec = SPECS[sym]
                return httpx.Response(200, json={"success": True, "errorCode": 0, "contracts": [{
                    "id": f"CON.F.US.{sym}.Z26", "name": f"{sym}Z6", "tickSize": spec.tick_size, "tickValue": spec.tick_value,
                    "activeContract": True}]})
            return super().handler(request)

    async def go():
        import websockets

        fake = Server()
        async with websockets.serve(fake.hub, "127.0.0.1", 0) as srv:
            port = srv.sockets[0].getsockname()[1]
            runner = make_runner(fake, port, "paper", tmp_path,
                                 instrument={"auto_pick": True, "candidates": ["MNQ", "MES"], "symbol": "MNQ"})
            ready = asyncio.Event()

            async def on_ready(core):
                ready.set()

            task = asyncio.create_task(runner.run(on_ready))
            await asyncio.wait_for(ready.wait(), 60)
            pick = runner.market_pick
            assert pick is not None and pick.symbol in ("MNQ", "MES")
            assert runner.contract.root == pick.symbol == runner.cfg.instrument.symbol
            assert runner.core.snapshot()["market_pick"]["symbol"] == pick.symbol
            assert (tmp_path / "market_pick.json").exists()
            runner.controls.stop.set()
            await asyncio.wait_for(task, 20)

    run(go())
