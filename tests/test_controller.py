"""The controller (dashboard + Telegram + supervision) against a real stand-in bot process."""

import asyncio
import json
import sys
from pathlib import Path

import httpx
import pytest

from topstep_bot import controller as controller_mod
from topstep_bot.config import BotConfig, Secrets, TelegramConfig
from topstep_bot.controller import Controller, ProxyActions, load_state, resolve_mode
from topstep_bot.telegram_control import TelegramController

from .conftest import run
from .test_telegram_control import CHAT, FakeTelegram, button, msg

FAKE = [sys.executable, str(Path(__file__).parent / "fake_worker.py")]


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(controller_mod, "BACKOFF", (0.1, 0.1, 0.1))
    monkeypatch.setattr(controller_mod, "notify", lambda *a: None)
    for var in ("FAKE_EXIT", "FAKE_HANG", "FAKE_POSITION", "FAKE_TRAIN_SECONDS"):
        monkeypatch.delenv(var, raising=False)


def make(tmp_path, **cfg_kw) -> Controller:
    cfg = BotConfig.model_validate({"data_dir": str(tmp_path / "data"), "log_dir": str(tmp_path / "logs"), **cfg_kw})
    return Controller(cfg, Secrets(), mode="paper", config_path=None, worker_command=FAKE, port=0, poll_seconds=0.1)


async def until(cond, timeout=15.0, step=0.05):
    end = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < end:
        result = cond()
        if asyncio.iscoroutine(result):
            result = await result
        if result:
            return True
        await asyncio.sleep(step)
    raise AssertionError("condition not met in time")


def scenario(tmp_path, body, **cfg_kw):
    """Run ``body(ctl, http)`` with the controller's web server and bot monitor running."""
    async def go():
        ctl = make(tmp_path, **cfg_kw)
        await ctl.server.start()
        monitor = asyncio.create_task(ctl.bot.monitor())
        http = httpx.AsyncClient(base_url=f"http://127.0.0.1:{ctl.server.port}", headers={"X-Token": ctl.token})
        try:
            await body(ctl, http)
        finally:
            monitor.cancel()
            if ctl.bot.running:
                await ctl.bot.stop("test cleanup")
            await http.aclose()
            await ctl.server.stop()
            await ctl.bot.close()
    run(go())


def test_start_status_and_stop_keep_dashboard_up(tmp_path):
    async def body(ctl, http):
        await http.post("/api/bot/start")
        await until(lambda: ctl.bot.state == "running")
        s = (await http.get("/api/status")).json()
        assert s["controller"]["state"] == "running" and s["bot"]["mode"] == "paper"
        r = (await http.post("/api/bot/stop")).json()
        assert r["ok"] and ctl.bot.state == "stopped"
        s = (await http.get("/api/status")).json()  # the dashboard still answers with the bot down
        assert s["bot"] is None and s["controller"]["last_exit"]["reason"] == "stop requested from dashboard"
        assert (await http.get("/")).status_code == 200
    scenario(tmp_path, body)


def test_restart_gives_a_new_process(tmp_path):
    async def body(ctl, http):
        await ctl.bot.start("test")
        await until(lambda: ctl.bot.state == "running")
        pid = ctl.bot.proc.pid
        await http.post("/api/bot/restart")
        await until(lambda: ctl.bot.state == "running" and ctl.bot.proc.pid != pid)
    scenario(tmp_path, body)


def test_crash_is_restarted_automatically(tmp_path):
    async def body(ctl, http):
        await ctl.bot.start("test")
        await until(lambda: ctl.bot.state == "running")
        pid = ctl.bot.proc.pid
        await ctl.bot.action("crash")
        await until(lambda: ctl.bot.state == "running" and ctl.bot.proc.pid != pid)
        assert any("crashed" in e["message"] for e in ctl.events)
    scenario(tmp_path, body)


def test_daily_maintenance_restart(tmp_path):
    async def body(ctl, http):
        await ctl.bot.start("test")
        await until(lambda: ctl.bot.state == "running")
        pid = ctl.bot.proc.pid
        await ctl.bot.action("maint")
        await until(lambda: ctl.bot.state == "running" and ctl.bot.proc.pid != pid)
        assert any("maintenance" in e["message"] for e in ctl.events)
    scenario(tmp_path, body)


def test_config_error_is_not_retried(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_EXIT", "2")

    async def body(ctl, http):
        await ctl.bot.start("test")
        await until(lambda: ctl.bot.state == "failed")
        assert ctl.bot.last_exit["reason"] == "fake configuration problem"
        await asyncio.sleep(0.5)
        assert ctl.bot.state == "failed" and not ctl.bot.running
    scenario(tmp_path, body)


def test_gives_up_after_too_many_crashes(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_EXIT", "3")

    async def body(ctl, http):
        ctl.cfg.service.max_restarts_per_hour = 2
        await ctl.bot.start("test")
        await until(lambda: ctl.bot.state == "failed")
        assert any("giving up" in e["message"] for e in ctl.events)
    scenario(tmp_path, body)


def test_hung_bot_is_killed_and_restarted(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_HANG", "1")

    async def body(ctl, http):
        ctl.cfg.service.heartbeat_timeout_seconds = 1
        await ctl.bot.start("test")
        pid = ctl.bot.proc.pid
        await until(lambda: any("has not responded" in e["message"] for e in ctl.events))
        await until(lambda: ctl.bot.running and ctl.bot.proc.pid != pid)
        ctl.cfg.service.heartbeat_timeout_seconds = 999  # let cleanup stop it normally
    scenario(tmp_path, body)


def test_mode_switch_requires_confirmation_and_restarts(tmp_path):
    async def body(ctl, http):
        await ctl.bot.start("test")
        await until(lambda: ctl.bot.state == "running")
        r = (await http.post("/api/mode", json={"mode": "live"})).json()
        assert r["ok"] is False and "Type LIVE" in r["message"]
        r = (await http.post("/api/mode", json={"mode": "live", "confirm": "live"})).json()
        assert r["ok"] and "LIVE" in r["message"]
        await until(lambda: ctl.bot.state == "running")
        await until(lambda: _bot_mode(ctl), timeout=5)
        assert (await ctl.bot.status())["bot"]["mode"] == "live"
        assert load_state(ctl.cfg)["mode"] == "live"
        r = (await http.post("/api/mode", json={"mode": "paper"})).json()  # back to paper needs no typing
        assert r["ok"]
        await until(lambda: ctl.bot.state == "running")
    scenario(tmp_path, body)


async def _bot_mode(ctl):
    s = await ctl.bot.status()
    return s and s["bot"]["mode"] == "live"


def test_mode_switch_refused_with_open_position(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_POSITION", "1")

    async def body(ctl, http):
        await ctl.bot.start("test")
        await until(lambda: ctl.bot.state == "running")
        r = (await http.post("/api/mode", json={"mode": "live", "confirm": "LIVE"})).json()
        assert r["ok"] is False and "Flatten" in r["message"] and ctl.bot.mode == "paper"
    scenario(tmp_path, body)


def test_mode_switch_while_stopped_just_sets_it(tmp_path):
    async def body(ctl, http):
        r = (await http.post("/api/mode", json={"mode": "live", "confirm": "LIVE"})).json()
        assert r["ok"] and "Press Start" in r["message"] and not ctl.bot.running
        assert ctl.bot.command(False)[-2:] == ["--mode", "live"]
    scenario(tmp_path, body)


def test_actions_are_relayed_to_the_bot(tmp_path):
    async def body(ctl, http):
        r = (await http.post("/api/action/pause")).json()
        assert r["ok"] is False and "not running" in r["message"]
        await ctl.bot.start("test")
        await until(lambda: ctl.bot.state == "running")
        r = (await http.post("/api/action/set_setting", json={"key": "risk", "value": 120})).json()
        assert r["ok"] and r["echo"] == "set_setting" and r["payload"]["source"] == "dashboard"
        r = (await http.post("/api/action/bad")).json()
        assert r["ok"] is False and r["message"] == "bad request from fake"
    scenario(tmp_path, body)


def test_dashboard_security(tmp_path):
    async def body(ctl, http):
        base = f"http://127.0.0.1:{ctl.server.port}"
        async with httpx.AsyncClient(base_url=base) as anon:
            assert (await anon.post("/api/bot/start")).status_code == 403  # no token
            assert (await anon.get("/api/status")).status_code == 200
            assert (await anon.get("/api/status", headers={"Host": "evil.example"})).status_code == 403
        assert not ctl.bot.running
        page = (await http.get("/")).text
        assert ctl.token in page
    scenario(tmp_path, body)


def test_telegram_works_while_bot_is_down(tmp_path):
    async def body(ctl, http):
        fake = FakeTelegram()
        tg = TelegramController("TOKEN", CHAT, ProxyActions(ctl), TelegramConfig(), transport=httpx.MockTransport(fake.handler))
        await tg.handle_update(msg("/status"))
        assert "STOPPED" in fake.sent[-1]["text"] and "/startbot" in fake.sent[-1]["text"]
        await tg.handle_update(msg("/pause"))
        assert fake.sent[-1]["text"].startswith("❌")
        await tg.handle_update(msg("/startbot"))
        await tg.handle_update(button("yes:startbot", fake.sent[-1]["message_id"]))
        await until(lambda: ctl.bot.state == "running")
        await tg.handle_update(msg("/status"))
        assert "fake bot in paper mode" in fake.sent[-1]["text"]
        await tg.handle_update(msg("/stop"))
        await tg.handle_update(button("yes:stop", fake.sent[-1]["message_id"]))
        await until(lambda: ctl.bot.state == "stopped")
        assert "Telegram" in ctl.bot.last_exit["reason"]
        await tg.close()
    scenario(tmp_path, body)


def test_slow_training_is_not_reported_as_a_bot_that_is_down(tmp_path, monkeypatch):
    """/train (and the dashboard's Retrain) used to time out after 8s and say the bot was 'not ready'."""
    monkeypatch.setattr(controller_mod, "ACTION_TIMEOUT", 0.3)
    monkeypatch.setenv("FAKE_TRAIN_SECONDS", "1")

    async def body(ctl, http):
        fake = FakeTelegram()
        tg = TelegramController("TOKEN", CHAT, ProxyActions(ctl), TelegramConfig(), transport=httpx.MockTransport(fake.handler))
        await ctl.bot.start("test")
        await until(lambda: ctl.bot.state == "running")
        await tg.handle_update(msg("/train"))
        await tg.handle_update(msg("/pause"))  # answered while the training is still running
        assert [m["text"] for m in fake.sent][1:] == ["Done."] and "Training" in fake.sent[0]["text"]
        await tg.idle()
        assert fake.sent[-1]["text"] == "trained"
        monkeypatch.setattr(controller_mod, "SLOW_ACTIONS", {})  # a request that really does time out...
        with pytest.raises(RuntimeError, match="busy"):  # ...says the bot is busy, not that it is down
            await ctl.bot.action("train")
        assert ctl.bot.state == "running"
        await tg.close()
    scenario(tmp_path, body)


def test_resolve_mode_precedence(tmp_path):
    cfg = BotConfig.model_validate({"data_dir": str(tmp_path), "mode": "paper"})
    assert resolve_mode(cfg, None) == "paper"
    controller_mod.save_state(cfg, {"mode": "live"})
    assert resolve_mode(cfg, None) == "live"
    assert resolve_mode(cfg, "paper") == "paper"
    assert json.loads((tmp_path / "controller.json").read_text())["mode"] == "live"
