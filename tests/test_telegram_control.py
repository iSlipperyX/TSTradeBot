"""Telegram remote control, tested against a fake Telegram Bot API."""

import json
import time

import httpx
import pytest

from topstep_bot.broker.paper import PaperBroker
from topstep_bot.config import BotConfig, TelegramConfig
from topstep_bot.control import BotActions
from topstep_bot.factory import build_core
from topstep_bot.live import Controls
from topstep_bot.telegram_control import CONFIRM_SECONDS, TelegramController

from .conftest import ct, run

T0 = ct(2026, 3, 3, 9, 0)
CHAT = 555
OWNER = 42


class FakeTelegram:
    def __init__(self, backlog=None):
        self.sent: list[dict] = []
        self.edits: list[dict] = []
        self.calls: list[str] = []
        self.backlog = backlog or []
        self.next_message_id = 1000

    def handler(self, request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content or b"{}")
        self.calls.append(method)
        result: object = True
        if method == "getMe":
            result = {"id": 1, "username": "my_topstep_bot"}
        elif method == "getUpdates":
            result = self.backlog if body.get("offset") == -1 else []
        elif method == "sendMessage":
            self.next_message_id += 1
            self.sent.append({**body, "message_id": self.next_message_id})
            result = {"message_id": self.next_message_id}
        elif method == "editMessageText":
            self.edits.append(body)
        return httpx.Response(200, json={"ok": True, "result": result})


def make(cfg: TelegramConfig | None = None, backlog=None):
    contract_cfg = BotConfig()
    from topstep_bot.instruments import offline_contract

    contract = offline_contract("MNQ")
    broker = PaperBroker(contract, 50_000)
    core = build_core(contract_cfg, contract, broker, clock=lambda: T0, account_label="PAPER-TEST")
    core.balance = 50_000
    run(core.begin_day(core.schedule.trading_day(T0), 50_000))
    controls = Controls()
    fake = FakeTelegram(backlog)
    ctl = TelegramController(
        "TOKEN", CHAT, BotActions(core, controls), cfg or TelegramConfig(), transport=httpx.MockTransport(fake.handler)
    )
    return ctl, fake, core, controls


def msg(text, chat=CHAT, user=OWNER):
    return {"update_id": 1, "message": {"message_id": 1, "chat": {"id": chat}, "from": {"id": user, "username": "me"}, "text": text}}


def button(data, message_id, chat=CHAT, user=OWNER):
    return {"update_id": 2, "callback_query": {"id": "cb", "data": data, "from": {"id": user, "username": "me"},
                                               "message": {"message_id": message_id, "chat": {"id": chat}}}}


def test_start_registers_commands_and_drops_backlog():
    ctl, fake, _, _ = make(backlog=[{"update_id": 77, "message": {"text": "/flatten"}}])
    run(ctl.start())
    assert "setMyCommands" in fake.calls
    assert ctl.offset == 78  # the old /flatten is skipped, never executed
    assert "online" in fake.sent[-1]["text"]


def test_status_command_replies_with_account_state():
    ctl, fake, _, _ = make()
    run(ctl.handle_update(msg("/status")))
    text = fake.sent[-1]["text"]
    assert "PAPER" in text and "Balance: $50,000.00" in text and "Position: flat" in text
    assert fake.sent[-1]["reply_markup"]["inline_keyboard"]


def test_unauthorized_chats_and_users_are_ignored():
    ctl, fake, core, controls = make(TelegramConfig(allowed_user_ids=[OWNER]))
    run(ctl.handle_update(msg("/pause", chat=999)))
    run(ctl.handle_update(msg("/pause", user=7)))
    run(ctl.handle_update(button("cmd:stop", 5, chat=999)))
    assert fake.sent == [] and not core.risk.paused and not controls.stop.is_set()


def test_pause_and_resume():
    ctl, fake, core, _ = make()
    run(ctl.handle_update(msg("/pause@my_topstep_bot")))
    assert core.risk.paused and "Paused" in fake.sent[-1]["text"]
    assert "Telegram (me)" in core.events[0]["message"]
    run(ctl.handle_update(button("cmd:resume", 1)))
    assert not core.risk.paused


def test_flatten_requires_confirmation():
    ctl, fake, core, controls = make()
    run(ctl.handle_update(msg("/flatten")))
    assert not controls.flatten_requested
    prompt = fake.sent[-1]
    assert "Yes, flatten" in json.dumps(prompt["reply_markup"])
    run(ctl.handle_update(button("yes:flatten", prompt["message_id"])))
    assert controls.flatten_requested and "Telegram" in controls.flatten_reason
    assert fake.edits[-1]["text"].startswith("✅")


def test_cancel_and_forged_or_expired_confirmations_do_nothing():
    ctl, fake, _, controls = make()
    run(ctl.handle_update(msg("/stop")))
    mid = fake.sent[-1]["message_id"]
    run(ctl.handle_update(button("no", mid)))
    run(ctl.handle_update(button("yes:stop", mid)))  # already cancelled
    run(ctl.handle_update(button("yes:flatten", 12345)))  # never asked
    assert not controls.stop.is_set() and not controls.flatten_requested
    run(ctl.handle_update(msg("/stop")))
    mid = fake.sent[-1]["message_id"]
    ctl.pending[mid] = ("stop", time.monotonic() - CONFIRM_SECONDS - 1)
    run(ctl.handle_update(button("yes:stop", mid)))
    assert not controls.stop.is_set()
    assert "expired" in fake.edits[-1]["text"]


def test_stop_after_confirmation():
    ctl, fake, _, controls = make()
    run(ctl.handle_update(msg("/stop")))
    run(ctl.handle_update(button("yes:stop", fake.sent[-1]["message_id"])))
    assert controls.stop.is_set()


def test_resume_refused_when_halted():
    ctl, fake, core, _ = make()
    core.halted = "flatten requested from dashboard"
    core.risk.paused = True
    run(ctl.handle_update(msg("/resume")))
    assert core.risk.paused and "Restart" in fake.sent[-1]["text"]


@pytest.mark.parametrize("command,expected", [("/trades", "No closed trades"), ("/log", "activity"), ("/foo", "/flatten")])
def test_info_commands(command, expected):
    ctl, fake, _, _ = make()
    run(ctl.handle_update(msg(command)))
    assert expected in fake.sent[-1]["text"]


def test_config_template_has_telegram_section():
    import yaml

    from topstep_bot.wizard import render_config

    cfg = BotConfig.model_validate(yaml.safe_load(render_config()))
    assert cfg.telegram.control_enabled and cfg.telegram.confirm_dangerous


def test_polling_processes_updates_and_stops_on_revoked_token():
    ctl, fake, core, _ = make()
    replies = iter([
        {"ok": True, "result": [msg("/pause") | {"update_id": 10}]},
        {"ok": False, "error_code": 401, "description": "Unauthorized"},
    ])

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("getUpdates"):
            return httpx.Response(200, json=next(replies))
        return fake.handler(request)

    ctl._client = httpx.AsyncClient(base_url="https://api.telegram.org/botTOKEN/", transport=httpx.MockTransport(handler))
    run(ctl.run())  # returns instead of looping forever
    assert core.risk.paused and ctl.offset == 11


def test_polling_survives_unexpected_errors():
    """An unexpected error while polling used to end the poller silently: Telegram stopped answering for good."""
    ctl, fake, core, _ = make()
    replies = iter([
        RuntimeError("something unexpected"),
        {"ok": True, "result": [msg("/pause") | {"update_id": 10}]},
        {"ok": False, "error_code": 401, "description": "Unauthorized"},
    ])

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("getUpdates"):
            reply = next(replies)
            if isinstance(reply, Exception):
                raise reply
            return httpx.Response(200, json=reply)
        return fake.handler(request)

    ctl._client = httpx.AsyncClient(base_url="https://api.telegram.org/botTOKEN/", transport=httpx.MockTransport(handler))
    run(ctl.run())
    assert core.risk.paused and ctl.offset == 11


def test_brief_next_and_learn_commands():
    ctl, fake, core, _ = make()
    run(ctl.handle_update(msg("/next")))
    assert fake.sent[-1]["text"].startswith("⏳ ")
    run(ctl.handle_update(button("cmd:brief", 1)))
    assert fake.sent[-1]["text"].startswith("🧠 What I know") and "My memory:" in fake.sent[-1]["text"]
    async def learn():
        await ctl.handle_update(msg("/learn"))
        await ctl.idle()  # learning answers in the background, so Telegram keeps working meanwhile
    run(learn())
    assert "Learning from the long-run memory" in fake.sent[-2]["text"]
    assert "only available while the bot is connected" in fake.sent[-1]["text"]  # this test has no TopstepX connection
    buttons = [b["callback_data"] for row in ctl_keyboard() for b in row]
    assert "cmd:brief" in buttons and "cmd:next" in buttons


def ctl_keyboard():
    from topstep_bot.telegram_control import KEYBOARD

    return KEYBOARD["inline_keyboard"]
