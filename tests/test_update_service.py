"""Updates through the controller: never while a trade is open, 'after the close' scheduling, the
restart into the new version (and back if it fails), Telegram /update, the dashboard and the menu."""

import asyncio
from datetime import datetime

import httpx
import pytest

from topstep_bot import cli, wizard
from topstep_bot import update_service as svc_mod
from topstep_bot.config import BotConfig, Secrets, TelegramConfig
from topstep_bot.telegram_control import TelegramController
from topstep_bot.update_service import UPDATE_EXIT_CODE, UpdateService, relaunch
from topstep_bot.updater import Change, UpdateError, UpdateInfo, load_state, save_state

from .conftest import ct, run
from .test_controller import make, scenario, until
from .test_telegram_control import CHAT, FakeTelegram, button, msg

TRADING = ct(2026, 3, 3, 10, 0)  # a Tuesday morning, mid-session
EVENING = ct(2026, 3, 3, 18, 0)


def available(**kw) -> UpdateInfo:
    return UpdateInfo(method="download", branch="main", checked_at="2026-03-03T10:00:00", current="a" * 40, latest="b" * 40,
                      available=True, changes=[Change("b" * 40, "Dashboard: market clock (#4)", "Dev", "2026-03-02T12:00:00Z")],
                      change_count=1, files=["src/topstep_bot/web.py"], file_count=1, **kw)


class StubUpdater:
    method, token = "download", "t"

    def __init__(self, info: UpdateInfo | None = None, fail: str | None = None):
        self.info = info or available()
        self.fail = fail
        self.installed: list = []

    def check(self) -> UpdateInfo:
        return self.info

    def install(self, info, config_path=None, progress=None) -> dict:
        if self.fail:
            raise UpdateError(self.fail)
        self.installed.append((info.latest, config_path))
        return {"method": "download", "from": info.current, "to": info.latest, "status": "installed"}


class Clock:
    def __init__(self, now: datetime):
        self.now = now

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    monkeypatch.setattr(svc_mod, "RESTART_DELAY", 0.05)
    monkeypatch.setattr(svc_mod, "notify", lambda *a: None)
    monkeypatch.delenv(svc_mod.RELAUNCHED_ENV, raising=False)


def attach(ctl, stub=None, now=EVENING, broker=None) -> tuple[UpdateService, StubUpdater, Clock]:
    stub = stub or StubUpdater()
    clock = Clock(now)

    async def flat():
        if broker:
            raise ValueError(broker)
    ctl.updates = UpdateService(ctl, updater=stub, broker_check=flat, clock=clock, first_check_delay=0, poll_seconds=0.02)
    return ctl.updates, stub, clock


def test_quiet_hours_are_outside_the_trading_session(tmp_path):
    service, _, _ = attach(make(tmp_path))
    assert not service.quiet(TRADING) and not service.quiet(ct(2026, 3, 3, 8, 20))  # 10 minutes before the start
    assert service.quiet(ct(2026, 3, 3, 7, 0)) and service.quiet(ct(2026, 3, 3, 15, 15)) and service.quiet(EVENING)
    assert service.quiet(ct(2026, 3, 7, 10, 0))  # Saturday


def test_install_waits_while_a_trade_is_open(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_POSITION", "1")

    async def body(ctl, http):
        service, stub, _ = attach(ctl)
        await ctl.bot.start("test")
        await until(lambda: ctl.bot.state == "running")
        r = (await http.post("/api/updates/install", json={"when": "now"})).json()
        assert not r["ok"] and "A trade is open" in r["message"]
        assert stub.installed == [] and ctl.bot.state == "running" and ctl.exit_code == 0 and service.busy is None
    scenario(tmp_path, body)


def test_live_account_must_be_flat_at_topstepx_too(tmp_path):
    async def body(ctl, http):
        service, stub, _ = attach(ctl, broker="TopstepX shows 1 open position(s)")
        with pytest.raises(ValueError, match="TopstepX shows 1 open position"):
            await service.install("test")
        assert stub.installed == []
    scenario(tmp_path, body)


def test_install_stops_the_bot_then_restarts_into_the_new_version(tmp_path):
    async def body(ctl, http):
        service, stub, _ = attach(ctl)
        await ctl.bot.start("test")
        await until(lambda: ctl.bot.state == "running")
        r = (await http.post("/api/updates/install", json={"when": "now"})).json()
        assert r["ok"] and "Update installed. The bot and the dashboard restart" in r["message"]
        assert stub.installed == [("b" * 40, ctl.config_path)] and not ctl.bot.running
        assert (await http.get("/api/status")).json()["updates"]["busy"] == "restarting"
        await until(lambda: ctl._exit.is_set())
        assert ctl.exit_code == UPDATE_EXIT_CODE
        plan = load_state(tmp_path / "data")["restart"]
        assert plan["mode"] == "paper" and plan["bot"] is True
        assert plan["announce"] == "✅ Topstep Bot updated to bbbbbbb: Dashboard: market clock (#4)."
    scenario(tmp_path, body)


def test_failed_install_restarts_the_old_version_and_explains(tmp_path):
    async def body(ctl, http):
        service, stub, _ = attach(ctl, StubUpdater(fail="the new version did not start. Nothing changed: the previous "
                                                        "version was restored"))
        await ctl.bot.start("test")
        await until(lambda: ctl.bot.state == "running")
        with pytest.raises(ValueError, match="previous version was restored"):
            await service.install("test")
        assert ctl.exit_code == 0 and service.busy is None and "Update failed" in service.status()["message"]
        await until(lambda: ctl.bot.state == "running")  # the old version runs again
    scenario(tmp_path, body)


def test_after_the_close_waits_for_quiet_hours_then_installs(tmp_path):
    async def body(ctl, http):
        service, stub, clock = attach(ctl, now=TRADING)
        r = (await http.post("/api/updates/install", json={"when": "tonight"})).json()
        assert r["ok"] and "after today's trading" in r["message"] and stub.installed == []
        assert (await http.get("/api/status")).json()["updates"]["scheduled"]["by"] == "dashboard"
        await service._install_scheduled()  # still trading hours: nothing happens
        clock.now = EVENING
        await service._install_scheduled()
        assert stub.installed and service.scheduled is None and "scheduled" not in load_state(tmp_path / "data")
        await until(lambda: ctl._exit.is_set())
    scenario(tmp_path, body)


def test_scheduled_update_can_be_cancelled_and_up_to_date_needs_nothing(tmp_path):
    async def body(ctl, http):
        service, stub, _ = attach(ctl, now=TRADING)
        await service.install("Telegram", "tonight")
        assert (await http.post("/api/updates/cancel")).json()["message"].startswith("The scheduled update was cancelled")
        stub.info = UpdateInfo(method="download", branch="main", current="b" * 40, latest="b" * 40)
        assert await service.install("dashboard") == "Already up to date - nothing to install."
        assert (await http.post("/api/updates/check")).json()["message"].startswith("Topstep Bot is up to date")
    scenario(tmp_path, body)


def test_new_version_is_announced_once_and_the_restart_is_reported(tmp_path):
    async def go():
        ctl = make(tmp_path)
        sent = []

        class Tg:
            async def send(self, text, keyboard=None):
                sent.append((text, keyboard))
        ctl.telegram = Tg()
        service, _, _ = attach(ctl)
        task = asyncio.create_task(service.run())
        await until(lambda: sent)
        await asyncio.sleep(0.1)
        service.next_check = None  # force another check: same version, no second message
        await asyncio.sleep(0.1)
        task.cancel()
        assert len(sent) == 1 and "Update available" in sent[0][0]
        assert sent[0][1]["inline_keyboard"][0][0]["callback_data"] == "cmd:update"
        save_state(tmp_path / "data", {**load_state(tmp_path / "data"), "restart": {"announce": "✅ Topstep Bot updated"}})
        await service.start()
        assert sent[-1][0] == "✅ Topstep Bot updated" and "restart" not in load_state(tmp_path / "data")
        await ctl.bot.close()
    run(go())


def test_dashboard_status_carries_update_details_and_an_instance_id(tmp_path):
    async def body(ctl, http):
        service, _, _ = attach(ctl)
        await service.check("test")
        s = (await http.get("/api/status")).json()
        u = s["updates"]
        assert s["instance"] == ctl.instance and u["supported"] and u["quiet"] and u["branch"] == "main"
        assert u["info"]["changes"][0]["title"] == "Dashboard: market clock (#4)" and "1 change" in u["headline"]
        page = (await http.get("/")).text
        assert 'id="updates"' in page and "updates/install" in page
    scenario(tmp_path, body)


# ------------------------------------------------------------------------------ Telegram /update

class UpdateActions:
    def __init__(self, can_install=True, quiet=False):
        self.offer = {"text": "⬆️ Update available for Topstep Bot: 1 change.", "can_install": can_install, "quiet": quiet}
        self.installs: list = []

    async def check_update(self, source):
        return self.offer

    async def install_update(self, source, when="now"):
        self.installs.append(when)
        return "Update installed."


def telegram(actions):
    fake = FakeTelegram()
    return TelegramController("TOKEN", CHAT, actions, TelegramConfig(), transport=httpx.MockTransport(fake.handler)), fake


def test_telegram_update_offers_after_the_close_during_trading_hours():
    actions = UpdateActions(quiet=False)
    tg, fake = telegram(actions)
    run(tg.handle_update(msg("/update")))
    question = fake.sent[-1]
    labels = [b["text"] for row in question["reply_markup"]["inline_keyboard"] for b in row]
    assert labels == ["After the close", "Install now", "Not now"] and "Trading hours are on" in question["text"]
    run(tg.handle_update(button("yes:update_tonight", question["message_id"])))
    assert actions.installs == ["tonight"] and fake.edits[-1]["text"] == "✅ Update installed."


def test_telegram_update_outside_trading_hours_and_when_nothing_to_install():
    actions = UpdateActions(quiet=True)
    tg, fake = telegram(actions)
    run(tg.handle_update(msg("/update")))
    labels = [b["text"] for row in fake.sent[-1]["reply_markup"]["inline_keyboard"] for b in row]
    assert labels == ["Install now", "Not now"]
    run(tg.handle_update(button("yes:update", fake.sent[-1]["message_id"])))
    assert actions.installs == ["now"]
    run(tg.handle_update(button("yes:update", fake.sent[-1]["message_id"])))  # a second tap does nothing
    assert actions.installs == ["now"] and "expired" in fake.edits[-1]["text"]
    up_to_date = UpdateActions(can_install=False)
    tg, fake = telegram(up_to_date)
    run(tg.handle_update(msg("/update")))
    assert fake.sent[-1]["text"] == up_to_date.offer["text"] and "reply_markup" not in fake.sent[-1]


def test_telegram_update_needs_the_controller():
    tg, fake = telegram(object())
    run(tg.handle_update(msg("/update")))
    assert "only available when the controller is running" in fake.sent[-1]["text"]


# ------------------------------------------------------------------------------ the restart itself

def test_relaunch_starts_each_new_version_with_the_same_mode(tmp_path, monkeypatch):
    cfg = BotConfig.model_validate({"data_dir": str(tmp_path)})
    save_state(tmp_path, {"restart": {"mode": "live", "bot": False}})
    commands, codes = [], iter([UPDATE_EXIT_CODE, 0])
    monkeypatch.setattr(svc_mod, "_run_child", lambda cmd: commands.append(cmd) or next(codes))
    assert relaunch(cfg, "my.yaml", UPDATE_EXIT_CODE, out=lambda _: None) == 0
    assert len(commands) == 2 and commands[0][-6:] == ["start", "--yes", "--no-browser", "--mode", "live", "--no-bot"]
    assert commands[0][3:5] == ["-c", "my.yaml"]
    monkeypatch.setenv(svc_mod.RELAUNCHED_ENV, "1")  # a relaunched child hands the job back to its launcher
    assert relaunch(cfg, None, UPDATE_EXIT_CODE) == UPDATE_EXIT_CODE


def test_a_new_version_that_fails_to_start_is_rolled_back(tmp_path, monkeypatch):
    cfg = BotConfig.model_validate({"data_dir": str(tmp_path)})
    save_state(tmp_path, {"restart": {"mode": "paper", "bot": True}})
    rolled = []

    class Undo:
        def can_roll_back(self):
            return not rolled

        def rollback(self):
            rolled.append(1)
            return {"from": "c" * 40}

        def close(self):
            pass
    monkeypatch.setattr(svc_mod.Updater, "for_config", classmethod(lambda cls, cfg, **kw: Undo()))
    codes = iter([1, 1, 0])  # new version crashes; restored version... crashes too (not undone twice); then stops
    monkeypatch.setattr(svc_mod, "_run_child", lambda cmd: next(codes))
    messages = []
    assert relaunch(cfg, None, UPDATE_EXIT_CODE, out=messages.append) == 1
    assert rolled == [1] and any("previous version (ccccccc) was restored" in m for m in messages)
    assert "restored" in load_state(tmp_path)["restart"]["announce"]


# ------------------------------------------------------------------------------ the menu / CLI

def test_cli_update_shows_changes_and_leaves_a_running_bot_to_the_dashboard(tmp_path, monkeypatch, capsys, restore_logging):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").write_text("mode: paper\n")
    stub = StubUpdater()
    stub.close = lambda: None
    monkeypatch.setattr("topstep_bot.updater.Updater.for_config", classmethod(lambda cls, cfg, **kw: stub))
    monkeypatch.setattr(cli, "_controller_running", lambda cfg: True)
    assert cli.main(["update"]) == 0
    out = capsys.readouterr().out
    assert "Dashboard: market clock (#4)" in out and "Settings" in out and stub.installed == []
    monkeypatch.setattr(cli, "_controller_running", lambda cfg: False)
    assert cli.main(["update", "--yes"]) == 0
    assert stub.installed and "Updated to bbbbbbb" in capsys.readouterr().out


def test_cli_offers_a_github_token_only_when_the_repository_is_hidden(tmp_path, monkeypatch, capsys, restore_logging):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").write_text("mode: paper\n")
    stub = StubUpdater(UpdateInfo(method="download", branch="main", error="could not reach GitHub (ConnectError)"))
    stub.close = lambda: None
    monkeypatch.setattr("topstep_bot.updater.Updater.for_config", classmethod(lambda cls, cfg, **kw: stub))
    monkeypatch.setattr(cli.sys, "stdin", type("Tty", (), {"isatty": staticmethod(lambda: True)})())
    asked: list = []
    monkeypatch.setattr(cli.Confirm, "ask", lambda question, **k: asked.append(question) or False)
    assert cli.main(["update"]) == 1
    assert asked == [] and "could not reach GitHub" in capsys.readouterr().out  # a token wouldn't help
    stub.info = UpdateInfo(method="download", branch="main", error="GitHub did not show the repository", needs_token=True)
    assert cli.main(["update"]) == 1
    assert asked == ["Set up a GitHub token now?"]


def test_token_setup_has_nothing_to_do_for_a_public_repository(tmp_path, monkeypatch):
    monkeypatch.setattr(wizard, "repo_is_public", lambda repo: True)
    monkeypatch.setattr(wizard.Prompt, "ask", lambda *a, **k: pytest.fail("asked for a token"))
    assert wizard.setup_github_token(tmp_path / ".env", "owner/bot")
    assert not (tmp_path / ".env").exists()


@pytest.mark.parametrize(("answer", "public"), [
    (httpx.Response(200, json={"private": False}), True),
    (httpx.Response(200, json={"private": True}), False),  # visible only because of a sign-in
    (httpx.Response(404, json={"message": "Not Found"}), False),
    (httpx.ConnectError("offline"), False),
])
def test_repo_is_public_reads_githubs_answer(monkeypatch, answer, public):
    def get(url, **kw):
        assert url == "https://api.github.com/repos/owner/bot" and "Authorization" not in kw["headers"]
        if isinstance(answer, Exception):
            raise answer
        return answer
    monkeypatch.setattr(httpx, "get", get)
    assert wizard.repo_is_public("owner/bot") is public


def test_menu_mentions_an_available_update_without_going_online(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").write_text("mode: paper\n")
    save_state(tmp_path / "data", {"check": available().to_dict()})
    monkeypatch.setattr(cli.Prompt, "ask", lambda *a, **k: "0")
    assert cli.main(["menu"]) == 0
    out = capsys.readouterr().out
    number = [name for name, _ in cli.MENU].index("update") + 1
    assert f"Choose {number} to see what changed" in out and "Update available" in out


def test_updates_config_defaults_and_validation():
    cfg = BotConfig()
    assert cfg.updates.enabled and cfg.updates.branch == "main" and cfg.updates.repo == "iSlipperyX/TSTradeBot"
    with pytest.raises(ValueError):
        BotConfig.model_validate({"updates": {"repo": "not a repo"}})
    assert Secrets(github_token="x").github_token == "x"
