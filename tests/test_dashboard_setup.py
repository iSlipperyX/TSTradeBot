"""Dashboard-first start: start.bat opens the server only, and the Setup tab does what the wizard did."""

import asyncio
import sys
from pathlib import Path

import httpx
import pytest
import yaml

from topstep_bot import cli
from topstep_bot import controller as controller_mod
from topstep_bot.api import rest
from topstep_bot.config import BotConfig, Secrets, load_config
from topstep_bot.config_edit import REMOVE, set_value, set_values
from topstep_bot.controller import Controller
from topstep_bot.models import Account
from topstep_bot.setup_service import ENV_KEYS, load_for_server
from topstep_bot.wizard import render_config

from .conftest import run
from .test_controller import until
from .test_telegram_control import FakeTelegram

FAKE = [sys.executable, str(Path(__file__).parent / "fake_worker.py")]


# ------------------------------------------------------------------------------ config.yaml editing

def test_values_change_in_place_and_comments_survive():
    text = render_config()
    new = set_values(text, {("account", "plan"): "100K", ("account", "account_id"): 4411,
                            ("risk", "risk_per_trade"): 125.5, (None, "mode"): "live"})
    old_lines, new_lines = text.splitlines(), new.splitlines()
    assert len(old_lines) == len(new_lines)  # nothing added or lost: the commented example was switched on
    changed = [(a, b) for a, b in zip(old_lines, new_lines, strict=True) if a != b]
    assert len(changed) == 4
    assert all("#" in b for a, b in changed if "#" in a)  # each line keeps its explanation
    cfg = BotConfig.model_validate(yaml.safe_load(new))
    assert (cfg.account.plan, cfg.account.account_id, cfg.risk.risk_per_trade, cfg.mode) == ("100K", 4411, 125.5, "live")


def test_new_settings_go_into_their_section_and_nested_blocks_are_replaced():
    text = ("strategy:\n  name: orb\n  params:\n    target_r: 1.5\n    range_minutes: 15\n"
            "  # a note at the end\n\n# next section\nrisk:  # mine\n  risk_per_trade: 100\n")
    new = set_values(text, {("strategy", "params"): {}, ("risk", "max_trades_per_day"): 6,
                            ("news", "enabled"): False, ("risk", "risk_per_trade"): REMOVE})
    data = yaml.safe_load(new)
    assert data == {"strategy": {"name": "orb", "params": {}}, "risk": {"max_trades_per_day": 6}, "news": {"enabled": False}}
    assert "# a note at the end" in new and "# next section" in new and "risk:  # mine" in new
    # a section written inline ("risk: {a: 1}") becomes a block it can add to
    assert yaml.safe_load(set_value("risk: {mll_buffer: 300}\n", "risk", "max_trades_per_day", 2)) == \
        {"risk": {"mll_buffer": 300, "max_trades_per_day": 2}}


# ------------------------------------------------------------------------------ the Setup tab, end to end

class FakeTopstepX:
    def __init__(self, username, api_key, *args, **kwargs):
        self.api_key = api_key

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def search_accounts(self, only_active=True):
        if self.api_key == "wrong":
            raise rest.ProjectXError("invalid credentials")
        return [Account(101, "50KTC-V2-11-22", 50_000.0, True, True, True),
                Account(102, "LFA-11", 52_000.0, True, True, False)]  # a Live Funded Account: never offered


@pytest.fixture
def clean_env(monkeypatch):
    for key in ENV_KEYS.values():
        monkeypatch.setenv(key, "")  # restored after the test, whatever the Setup tab writes
    monkeypatch.setattr(controller_mod, "notify", lambda *a: None)
    monkeypatch.setattr(rest, "ProjectXClient", FakeTopstepX)


def setup_scenario(tmp_path, body, config_text=None):
    async def go():
        path = tmp_path / "config.yaml"
        if config_text is not None:
            path.write_text(config_text, encoding="utf-8")
        cfg, problem = load_for_server(str(path))
        ctl = Controller(cfg, Secrets(), mode="paper", config_path=str(path), worker_command=FAKE, port=0,
                         poll_seconds=0.1, config_error=problem)
        await ctl.server.start()
        monitor = asyncio.create_task(ctl.bot.monitor())
        http = httpx.AsyncClient(base_url=f"http://127.0.0.1:{ctl.server.port}", headers={"X-Token": ctl.token})
        try:
            await body(ctl, http, path)
        finally:
            monitor.cancel()
            if ctl.bot.running:
                await ctl.bot.stop("test cleanup")
            await ctl.stop_telegram()
            await http.aclose()
            await ctl.server.stop()
            await ctl.bot.close()
    run(go())


FORM = {"plan": "50K", "stage": "combine", "goal": "pass", "topstep_dll": False, "payout_path": "standard",
        "symbol": "MNQ", "strategy": "adaptive", "risk_per_trade": "150", "daily_loss_limit": "500", "max_trades": "4"}


def test_first_run_from_an_empty_folder_to_a_running_bot(tmp_path, clean_env):
    async def body(ctl, http, path):
        s = (await http.get("/api/status")).json()
        assert not s["setup"]["configured"] and "Finish setup" in s["setup"]["blocker"]
        r = (await http.post("/api/bot/start")).json()  # nothing to run yet: it says why
        assert not r["ok"] and "Setup tab" in r["message"] and not ctl.bot.running

        r = (await http.post("/api/setup/login", json={"username": "me", "api_key": "wrong"})).json()
        assert not r["ok"] and "did not accept" in r["message"] and not (tmp_path / ".env").exists()
        r = (await http.post("/api/setup/login", json={"username": "me", "api_key": "secret-key"})).json()
        assert r["ok"] and [a["id"] for a in r["accounts"]] == [101] and r["excluded"] == ["LFA-11"]
        assert r["accounts"][0]["plan"] == "50K" and r["accounts"][0]["stage"] == "combine"
        assert "TOPSTEPX_API_KEY=secret-key" in (tmp_path / ".env").read_text()
        state = (await http.post("/api/setup/state")).json()
        assert "secret-key" not in str(state) and state["login"] == {"username": "me", "api_key_saved": True}

        r = (await http.post("/api/setup/save", json={**FORM, "account_id": 101, "goal": "learn"})).json()
        assert r["ok"], r
        cfg = load_config(path)
        assert cfg.account.account_id == 101 and cfg.account.account_name == "50KTC-V2-11-22"
        assert cfg.strategy.params == {"trade_unproven": True} and not cfg.risk.consistency_guard
        assert cfg.first_trade.enabled and cfg.first_trade.within_minutes == 15  # Teach the bot: first trade on
        assert path.read_text().startswith("# Goal: LEARN")
        s = (await http.get("/api/status")).json()
        assert s["setup"]["configured"] and s["setup"]["blocker"] is None
        assert ctl.cfg.account.account_id == 101  # the server uses it at once

        await http.post("/api/bot/start")
        await until(lambda: ctl.bot.state == "running")
        r = (await http.post("/api/setup/save", json={**FORM, "account_id": 101, "risk_per_trade": "100"})).json()
        assert r["ok"] and "Restart" in r["message"]
        assert (await http.get("/api/status")).json()["setup"]["restart_needed"]
    setup_scenario(tmp_path, body)


def test_saving_keeps_hand_edits_and_switching_goal_restores_the_profile(tmp_path, clean_env):
    text = render_config(goal="learn") + "\nnews:\n  minutes_before: 15   # my own setting\n"

    async def body(ctl, http, path):
        assert (await http.post("/api/setup/state")).json()["values"]["goal"] == "learn"
        r = (await http.post("/api/setup/save", json={**FORM, "symbol": "MES", "goal": "pass"})).json()
        assert r["ok"], r
        saved = path.read_text()
        cfg = load_config(path)
        assert cfg.instrument.symbol == "MES" and cfg.news.minutes_before == 15 and "# my own setting" in saved
        assert cfg.risk.consistency_guard and cfg.risk.max_consecutive_losses == 2 and cfg.risk.daily_profit_target is None
        assert cfg.strategy.params == {} and "# Goal: LEARN" not in saved
        assert not cfg.first_trade.enabled and "within_minutes: 15" in saved  # off, its other settings kept
        assert (tmp_path / "config.yaml.bak").read_text() == text

        # a setting that would break a Topstep rule is refused, and the file is left alone
        r = (await http.post("/api/setup/save", json={**FORM, "daily_loss_limit": "2500", "risk_per_trade": "100"})).json()
        assert not r["ok"] and "Maximum Loss Limit" in r["message"] and path.read_text() == saved
    setup_scenario(tmp_path, body, config_text=text)


def test_the_first_trade_after_starting_follows_the_teach_the_bot_goal(tmp_path, clean_env):
    async def body(ctl, http, path):
        r = (await http.post("/api/setup/save", json={**FORM, "goal": "learn", "first_trade": False})).json()
        assert r["ok"], r
        assert not load_config(path).first_trade.enabled  # unticked on the Setup tab
        state = (await http.post("/api/setup/state")).json()["values"]
        assert state["first_trade"] is False and state["first_trade_minutes"] == 15
        r = (await http.post("/api/setup/save", json={**FORM, "goal": "learn", "first_trade": True})).json()
        assert r["ok"] and load_config(path).first_trade.enabled
        r = (await http.post("/api/setup/save", json={**FORM, "goal": "pass", "first_trade": True})).json()
        assert r["ok"] and not load_config(path).first_trade.enabled  # never while protecting a Combine
    setup_scenario(tmp_path, body, config_text=render_config())


def test_a_broken_config_still_opens_the_dashboard_and_can_be_replaced(tmp_path, clean_env):
    async def body(ctl, http, path):
        s = (await http.get("/api/status")).json()
        assert "unknown setting" in s["setup"]["error"] and "problem" in s["setup"]["blocker"]
        assert (await http.get("/")).status_code == 200
        r = (await http.post("/api/setup/save", json=FORM)).json()  # editing in place keeps the typo...
        assert not r["ok"] and "risk_per_trad" in r["message"]
        r = (await http.post("/api/setup/save", json={**FORM, "fresh": True})).json()  # ...a fresh file fixes it
        assert r["ok"] and load_config(path).risk.risk_per_trade == 150
        assert "risk_per_trad:" in (tmp_path / "config.yaml.bak").read_text()
        assert (await http.get("/api/status")).json()["setup"]["error"] is None
    setup_scenario(tmp_path, body, config_text="risk:\n  risk_per_trad: 100\n")


def test_telegram_is_found_tested_and_saved_from_the_dashboard(tmp_path, clean_env):
    fake = FakeTelegram(backlog=[])

    def telegram(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/getUpdates"):  # the user has just messaged the new bot
            return httpx.Response(200, json={"ok": True, "result": [
                {"update_id": 5, "message": {"chat": {"id": 777, "type": "private", "username": "justyn"}}}]})
        return fake.handler(req)

    async def body(ctl, http, path):
        ctl.telegram_api = "http://telegram.test"
        real = httpx.AsyncClient

        def patched(*a, **kw):
            kw["transport"] = httpx.MockTransport(telegram)
            return real(*a, **kw)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(httpx, "AsyncClient", patched)
            r = (await http.post("/api/setup/telegram/find", json={"token": "123:ABC"})).json()
            assert r["chats"] == [{"id": "777", "label": "justyn (private)"}]
            r = (await http.post("/api/setup/telegram/save", json={"token": "123:ABC", "chat_id": "777",
                                                                    "control": False})).json()
        assert r["ok"] and "test message" in r["message"]
        assert any(m["chat_id"] == "777" and "connected" in m["text"] for m in fake.sent)
        env = (tmp_path / ".env").read_text()
        assert "TELEGRAM_BOT_TOKEN=123:ABC" in env and "TELEGRAM_CHAT_ID=777" in env
        state = (await http.post("/api/setup/state")).json()
        assert state["alerts"]["telegram"] and not state["alerts"]["telegram_control"] and "123:ABC" not in str(state)
        r = (await http.post("/api/setup/telegram/remove")).json()
        assert r["ok"] and not (await http.post("/api/setup/state")).json()["alerts"]["telegram"]
    setup_scenario(tmp_path, body)


# ------------------------------------------------------------------------------ start.bat with no arguments

def test_no_arguments_starts_the_server_without_the_bot_or_questions(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "_controller_running", lambda cfg: False)
    seen = {}

    def fake_run(cfg, secrets, **kw):
        seen.update(kw, mode=cfg.mode)
        return 0

    monkeypatch.setattr(controller_mod, "run_controller", fake_run)
    monkeypatch.setattr(cli.Prompt, "ask", lambda *a, **k: pytest.fail("start.bat must not ask anything"))
    controller_mod.save_state(load_for_server(None)[0], {"mode": "live"})  # even when live was used last
    (tmp_path / "config.yaml").write_text("risk:\n  risk_per_trad: 100\n")  # and config.yaml is broken
    assert cli.main([]) == 0
    assert seen["start_bot"] is False and seen["open_browser"] is True and seen["mode"] == "live"
    assert "risk_per_trad" in seen["config_error"]


def test_a_second_start_just_opens_the_dashboard(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    opened = []
    monkeypatch.setattr(cli, "_controller_running", lambda cfg: True)
    monkeypatch.setattr(cli.webbrowser, "open", opened.append)
    monkeypatch.setattr(controller_mod, "run_controller", lambda *a, **k: pytest.fail("a second server was started"))
    assert cli.main([]) == 0
    assert opened == ["http://127.0.0.1:8765/"] and "already running" in capsys.readouterr().out
