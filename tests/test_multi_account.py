"""Several accounts: per-account settings, the cross-account hedge guard, the controller and Telegram."""

import asyncio

import httpx
import pytest

from topstep_bot.broker.paper import PaperBroker
from topstep_bot.cli import call_groups
from topstep_bot.config import RESERVED_NAMES, BotConfig, Secrets, TelegramConfig
from topstep_bot.controller import Controller, ProxyActions, load_state, resolve_modes, save_state
from topstep_bot.crossaccount import PositionBook, market_key
from topstep_bot.factory import build_core
from topstep_bot.journal import Journal
from topstep_bot.models import Signal
from topstep_bot.remote import ALIASES, SPEC_BY_KEY
from topstep_bot.telegram_control import TelegramController

from .conftest import bar, ct, run
from .test_controller import FAKE, until
from .test_telegram_control import CHAT, FakeTelegram, button, msg

T0 = ct(2026, 3, 3, 9, 0)

ACCOUNTS = {
    "strategy": {"name": "orb", "params": {"target_r": 1.5}},
    "risk": {"risk_per_trade": 150},
    "accounts": [
        {"name": "c1", "account_id": 1},
        {"name": "c2", "account_id": 2, "plan": "100K", "risk": {"risk_per_trade": 300}},
        {"name": "c3", "account_id": 3, "strategy": {"name": "ema_trend"}, "instrument": {"symbol": "mes"}},
        {"name": "c4", "account_id": 4, "follow": "c3", "mode": "live"},
        {"name": "old", "account_id": 5, "enabled": False},
    ],
}


def multi_cfg(tmp_path=None, **extra) -> BotConfig:
    paths = {"data_dir": str(tmp_path / "data"), "log_dir": str(tmp_path / "logs")} if tmp_path else {}
    return BotConfig.model_validate({**ACCOUNTS, **paths, **extra})


# ------------------------------------------------------------------ settings per account

def test_accounts_inherit_shared_settings_and_override_what_differs(tmp_path):
    cfg = multi_cfg(tmp_path)
    assert cfg.multi and cfg.account_names() == ["c1", "c2", "c3", "c4"]
    c1, c2, c3, c4 = (cfg.for_account(n) for n in ("c1", "c2", "c3", "c4"))
    assert (c1.strategy.name, c1.strategy.params, c1.risk.risk_per_trade) == ("orb", {"target_r": 1.5}, 150)
    assert (c2.account.plan, c2.risk.risk_per_trade, c2.strategy.name) == ("100K", 300, "orb")
    # a different strategy doesn't inherit the shared strategy's parameters
    assert (c3.strategy.name, c3.strategy.params, c3.instrument.symbol) == ("ema_trend", {}, "MES")
    # 'follow' copies the calls (strategy + symbol) but keeps its own risk and mode
    assert (c4.strategy.name, c4.instrument.symbol, c4.risk.risk_per_trade, c4.mode) == ("ema_trend", "MES", 150, "live")
    # every account keeps its own files
    assert c1.journal_path != c2.journal_path and c1.journal_path.parent.name == "c1"
    assert c3.knowledge_path.name == "knowledge_MES_5m.json" and c3.knowledge_path.parent.name == "c3"
    assert c1.log_dir.endswith("c1") and c1.active_account == "c1"
    assert c1.for_account(None) is c1


def test_single_account_config_is_unchanged(tmp_path):
    cfg = BotConfig.model_validate({"data_dir": str(tmp_path)})
    assert not cfg.multi and cfg.for_account(None) is cfg and cfg.for_account("main") is cfg
    assert cfg.journal_path == tmp_path / "journal_paper.db"  # v1.1 files keep working


def test_ambiguous_or_unknown_accounts_are_refused():
    cfg = multi_cfg()
    with pytest.raises(ValueError, match="choose one with --account"):
        cfg.for_account(None)
    with pytest.raises(ValueError, match="No account named"):
        cfg.for_account("nope")


@pytest.mark.parametrize("accounts, error", [
    ([{"name": "c1"}, {"name": "C1"}], "unique"),
    ([{"name": "all"}], "reserved"),
    ([{"name": "risk"}], "reserved"),
    ([{"name": "c1", "account_id": 1}, {"name": "c2", "account_id": 1}], "same account_id"),
    ([{"name": "c1", "follow": "zz"}], "not in 'accounts'"),
    ([{"name": "c1"}, {"name": "c2", "follow": "c1", "strategy": {"name": "orb"}}], "can't also set strategy"),
    ([{"name": "c1", "follow": "c2"}, {"name": "c2", "follow": "c1"}], "no chains"),
    ([{"name": "bad name"}], "pattern"),
])
def test_account_list_validation(accounts, error):
    with pytest.raises(ValueError, match=error):
        BotConfig.model_validate({"accounts": accounts})


def test_bad_override_names_the_account():
    cfg = BotConfig.model_validate({"accounts": [{"name": "c1", "risk": {"risk_per_trade": -5}}]})
    with pytest.raises(ValueError, match="account c1"):
        cfg.for_account("c1")


def test_account_names_cannot_be_mistaken_for_settings():
    assert set(SPEC_BY_KEY) | set(ALIASES) <= RESERVED_NAMES


def test_call_groups_show_who_trades_the_same_calls():
    groups = call_groups(multi_cfg())
    assert groups["orb (target_r=1.5) on MNQ 5m"] == ["c1", "c2"]
    assert groups["ema_trend on MES 5m"] == ["c3", "c4"]


def test_modes_per_account(tmp_path):
    cfg = multi_cfg(tmp_path)
    assert resolve_modes(cfg) == {"c1": "paper", "c2": "paper", "c3": "paper", "c4": "live"}
    save_state(cfg, {"modes": {"c1": "live"}})
    assert resolve_modes(cfg)["c1"] == "live"
    assert resolve_modes(cfg, "paper", ["c4"]) == {"c1": "live", "c2": "paper", "c3": "paper", "c4": "paper"}
    assert set(resolve_modes(cfg, "live").values()) == {"live"}


# ------------------------------------------------------------------ hedge guard

def test_market_keys():
    assert market_key("MNQ", "same_product") == market_key("NQ", "same_product") == "NQ"
    assert market_key("MES", "same_product") != market_key("MNQ", "same_product")
    assert market_key("MES", "correlated") == market_key("MNQ", "correlated") == market_key("M2K", "correlated")
    assert market_key("MGC", "correlated") != market_key("MES", "correlated")


def test_hedge_guard_blocks_opposite_positions_across_accounts(tmp_path):
    clock = [1000.0]
    a = PositionBook(tmp_path, "c1", "live", "MNQ", clock=lambda: clock[0])
    b = PositionBook(tmp_path, "c2", "live", "NQ", clock=lambda: clock[0])
    c = PositionBook(tmp_path, "c3", "live", "MES", clock=lambda: clock[0])
    paper = PositionBook(tmp_path, "c4", "paper", "MNQ", clock=lambda: clock[0])
    assert a.claim(1) is None                       # c1 goes long MNQ
    assert "c1 is long MNQ" in b.claim(-1)          # short NQ on another account: hedging
    assert "prohibits" in c.claim(-1)               # short MES too (correlated index)
    assert b.claim(1) is None                       # same direction is fine
    assert paper.claim(-1) is None                  # paper accounts are checked among themselves
    a.sync(0)                                       # not filled yet: the claim is kept for a while
    assert c.conflict(-1) is not None
    clock[0] += 120
    a.sync(0)                                       # c1 is flat again
    b.sync(0)
    assert c.claim(-1) is None


def test_hedge_guard_scope_and_off(tmp_path):
    a = PositionBook(tmp_path, "c1", "live", "MNQ", scope="same_product")
    b = PositionBook(tmp_path, "c2", "live", "MES", scope="same_product")
    off = PositionBook(tmp_path, "c3", "live", "MNQ", scope="off")
    assert a.claim(1) is None
    assert b.claim(-1) is None                      # S&P vs Nasdaq allowed in same_product scope
    assert off.claim(-1) is None and not off.enabled


def test_stale_file_from_a_previous_run_is_cleared(tmp_path):
    PositionBook(tmp_path, "c1", "live", "MNQ").claim(1)
    restarted = PositionBook(tmp_path, "c1", "live", "MNQ")
    restarted.sync(0)                               # first sync after a restart: actually flat
    assert PositionBook(tmp_path, "c2", "live", "MNQ").claim(-1) is None


def test_engine_skips_a_call_that_would_hedge_another_account(tmp_path, mnq):
    async def go():
        cfg = BotConfig.model_validate({"risk": {"risk_per_trade": 100}})
        broker = PaperBroker(mnq, 50_000, slippage_ticks=0)
        core = build_core(cfg, mnq, broker, clock=lambda: T0, account_label="t", journal=Journal(":memory:"))
        core.balance = 50_000
        await core.begin_day(core.schedule.trading_day(T0), 50_000)
        PositionBook(tmp_path, "other", "paper", "NQ").claim(1)
        core.entry_guard = PositionBook(tmp_path, "me", "paper", "MNQ").claim
        b = bar(T0, 100, 100, 100, 100)
        core.last_price = 100.0
        await core._handle_entry(Signal("short", stop_price=110.0, reason="t"), b, core.context(b))
        assert core.orders.trade is None
        assert any("hedging" in e["message"] for e in core.events)
        await core._handle_entry(Signal("long", stop_price=90.0, reason="t"), b, core.context(b))
        assert core.orders.trade is not None        # same direction as the other account: allowed
    run(go())


# ------------------------------------------------------------------ controller with several accounts

def multi_scenario(tmp_path, body, **extra):
    async def go():
        cfg = multi_cfg(tmp_path, **extra)
        path = tmp_path / "config.yaml"  # setup finished, as after the dashboard's Setup tab
        path.write_text("mode: paper\n", encoding="utf-8")
        ctl = Controller(cfg, Secrets(username="me", api_key="key"), modes=resolve_modes(cfg), config_path=str(path),
                         worker_command=FAKE, port=0, poll_seconds=0.1)
        await ctl.server.start()
        monitors = [asyncio.create_task(b.monitor()) for b in ctl.bots.values()]
        http = httpx.AsyncClient(base_url=f"http://127.0.0.1:{ctl.server.port}", headers={"X-Token": ctl.token})
        try:
            await body(ctl, http)
        finally:
            for m in monitors:
                m.cancel()
            await asyncio.gather(*(b.stop("test cleanup") for b in ctl.bots.values() if b.running))
            await http.aclose()
            await ctl.server.stop()
            for b in ctl.bots.values():
                await b.close()
    run(go())


def all_running(ctl):
    return all(b.state == "running" for b in ctl.bots.values())


def test_controller_runs_one_bot_per_account(tmp_path):
    async def body(ctl, http):
        assert list(ctl.bots) == ["c1", "c2", "c3", "c4"] and ctl.bots["c4"].mode == "live"
        assert ctl.bots["c1"].command(False)[-4:] == ["--mode", "paper", "--account", "c1"]
        r = (await http.post("/api/bot/start", json={"account": "all"})).json()
        assert r["ok"] and "[c4]" in r["message"]
        await until(lambda: all_running(ctl))
        s = (await http.get("/api/status?account=c3")).json()
        assert s["multi"] and s["selected"] == "c3" and s["bot"]["account"] == "c3"
        assert [a["account"] for a in s["accounts"]] == ["c1", "c2", "c3", "c4"]
        assert s["accounts"][3]["summary"]["day_pnl"] == 12.5 and s["accounts"][3]["mode"] == "live"
        # an action needs a target; one account or (for safe actions) all of them
        r = (await http.post("/api/action/pause", json={})).json()
        assert r["ok"] is False and "Which account" in r["message"]
        r = (await http.post("/api/action/set_setting", json={"account": "c2", "key": "risk", "value": 5})).json()
        assert r["ok"] and r["payload"]["key"] == "risk"
        r = (await http.post("/api/action/pause", json={"account": "all"})).json()
        assert r["ok"] and r["message"].count("Done.") == 4
        r = (await http.post("/api/action/take_idea", json={"account": "all", "id": "R1"})).json()
        assert r["ok"] is False and "one account" in r["message"]
        # stopping one account leaves the others trading
        pid = ctl.bots["c1"].proc.pid
        await http.post("/api/bot/stop", json={"account": "c2"})
        assert ctl.bots["c2"].state == "stopped" and ctl.bots["c1"].running and ctl.bots["c1"].proc.pid == pid
        s = (await http.get("/api/status?account=c2")).json()
        assert s["bot"] is None and s["accounts"][0]["summary"] is not None
    multi_scenario(tmp_path, body)


def test_one_account_failing_does_not_affect_the_others(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_EXIT", "2")
    monkeypatch.setenv("FAKE_EXIT_ACCOUNT", "c3")

    async def body(ctl, http):
        await ctl.start_bots("test")
        await until(lambda: ctl.bots["c3"].state == "failed"
                    and all(ctl.bots[n].state == "running" for n in ("c1", "c2", "c4")))
        await asyncio.sleep(0.5)
        assert ctl.bots["c3"].state == "failed" and not ctl.bots["c3"].running  # not retried
        assert any(e["account"] == "c3" and "Bot c3 could not start" in e["message"] for e in ctl.events)
    multi_scenario(tmp_path, body, multi_account={"stagger_seconds": 0})


def test_mode_switch_per_account(tmp_path):
    async def body(ctl, http):
        r = (await http.post("/api/mode", json={"account": "c2", "mode": "live", "confirm": "LIVE"})).json()
        assert r["ok"] and ctl.bots["c2"].mode == "live" and ctl.bots["c1"].mode == "paper"
        assert load_state(ctl.cfg)["modes"]["c2"] == "live"
        r = (await http.post("/api/mode", json={"account": "all", "mode": "paper"})).json()
        assert r["ok"] and {b.mode for b in ctl.bots.values()} == {"paper"}
    multi_scenario(tmp_path, body)


def test_logs_are_per_account(tmp_path):
    async def body(ctl, http):
        folder = tmp_path / "logs" / "accounts" / "c2"
        folder.mkdir(parents=True)
        (folder / "bot.log").write_text("hello from c2\n", encoding="utf-8")
        d = (await http.get("/api/logs?account=c2")).json()
        assert "hello from c2" in d["bot"] and d["folder"].endswith("c2")
    multi_scenario(tmp_path, body)


def test_telegram_targets_accounts(tmp_path):
    async def body(ctl, http):
        await ctl.start_bots("test")
        await until(lambda: all_running(ctl))
        fake = FakeTelegram()
        tg = TelegramController("TOKEN", CHAT, ProxyActions(ctl), TelegramConfig(), transport=httpx.MockTransport(fake.handler))
        await tg.handle_update(msg("/status"))
        text = fake.sent[-1]["text"]
        assert text.startswith("Accounts:") and "c4: 🔴 LIVE" in text and "c1: 🔵 paper" in text
        await tg.handle_update(msg("/status c2"))
        assert "fake bot c2" in fake.sent[-1]["text"]
        # no account named: one button per account (plus All for control commands)
        await tg.handle_update(msg("/pause"))
        buttons = [b["callback_data"] for row in fake.sent[-1]["reply_markup"]["inline_keyboard"] for b in row]
        assert buttons == ["pick:pause:c1", "pick:pause:c2", "pick:pause:c3", "pick:pause:c4", "pick:pause:all"]
        await tg.handle_update(button("pick:pause:all", fake.sent[-1]["message_id"]))
        assert fake.sent[-1]["text"].count("Done.") == 4
        await tg.handle_update(msg("/trades"))
        assert "pick:trades:all" not in str(fake.sent[-1]["reply_markup"])
        await tg.handle_update(msg("/trades all"))
        assert "one account at a time" in fake.sent[-1]["text"]
        # dangerous commands name the target in the confirmation
        await tg.handle_update(msg("/stop c3"))
        assert "on c3?" in fake.sent[-1]["text"]
        await tg.handle_update(button("yes:stop", fake.sent[-1]["message_id"]))
        await until(lambda: ctl.bots["c3"].state == "stopped")
        assert ctl.bots["c1"].running
        await tg.handle_update(msg("/set risk 150"))
        assert "Usage: /set <account|all>" in fake.sent[-1]["text"]
        await tg.close()
    multi_scenario(tmp_path, body, multi_account={"stagger_seconds": 0})
