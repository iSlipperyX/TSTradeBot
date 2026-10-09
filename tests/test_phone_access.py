"""Phone access: the dashboard inside Telegram, through a secure tunnel, for the owner's Telegram account only."""

import asyncio
import hashlib
import io
import json
import sys
import tarfile
import time
from pathlib import Path

import httpx
import pytest

from topstep_bot import controller as controller_mod
from topstep_bot import phone_access as phone_mod
from topstep_bot.config import BotConfig, Secrets, TelegramConfig
from topstep_bot.controller import Controller, ProxyActions, load_state
from topstep_bot.phone_access import (
    PhoneAccess,
    asset_name,
    download_cloudflared,
    find_cloudflared,
    owner_ids,
    sign_init_data,
    verify_init_data,
)
from topstep_bot.telegram_control import KEYBOARD, TelegramController

from .conftest import run
from .test_controller import FAKE, until
from .test_telegram_control import CHAT, FakeTelegram, button, msg

BOT_TOKEN = "123456:TEST-token"
OWNER = 4242
FAKE_TUNNEL = [sys.executable, str(Path(__file__).parent / "fake_cloudflared.py")]
URL = "https://seasonal-deck-organisms-sf.trycloudflare.com"


def init_data(user_id=OWNER, token=BOT_TOKEN, age=0, **extra):
    fields = {"auth_date": str(int(time.time() - age)), "query_id": "AAHdF6IQ",
              "user": json.dumps({"id": user_id, "first_name": "Justyn", "username": "justyn"}), **extra}
    return sign_init_data(fields, token)


# ------------------------------------------------------------------------------ Telegram's signature

def test_valid_sign_in_returns_the_user():
    assert verify_init_data(init_data(), BOT_TOKEN)["id"] == OWNER
    # Telegram adds fields over time (e.g. signature); they are part of what it signs
    assert verify_init_data(init_data(signature="abc", chat_type="sender"), BOT_TOKEN)["id"] == OWNER


@pytest.mark.parametrize("data, why", [
    (init_data(token="999:other-bot"), "signature"),  # signed for another bot
    (init_data().replace("Justyn", "Mallory"), "signature"),  # changed after signing
    (init_data().replace("4242", "4243"), "signature"),
    (init_data(age=25 * 3600), "too old"),
    (init_data(age=-3600), "too old"),  # from the future
    ("auth_date=1&user=x", "signature"),  # no hash at all
    ("", "no sign-in"),
    ("%%%&&&==", "malformed"),
])
def test_forged_old_or_broken_sign_ins_are_refused(data, why):
    with pytest.raises(ValueError, match=why):
        verify_init_data(data, BOT_TOKEN)


def test_sign_in_needs_a_user():
    with pytest.raises(ValueError, match="incomplete"):
        verify_init_data(sign_init_data({"auth_date": str(int(time.time()))}, BOT_TOKEN), BOT_TOKEN)
    with pytest.raises(ValueError, match="no Telegram user"):
        verify_init_data(sign_init_data({"auth_date": str(int(time.time())), "user": '{"id": "1"}'}, BOT_TOKEN), BOT_TOKEN)


def test_owner_is_the_private_chat_or_the_allowed_users():
    assert owner_ids("4242", []) == {4242}
    assert owner_ids("4242", [7, 8]) == {7, 8}
    assert owner_ids("-100123", []) == set()  # a group: nobody until allowed_user_ids names someone
    assert owner_ids("-100123", [7]) == {7}
    assert owner_ids("@channel", []) == set()


# ------------------------------------------------------------------------------ cloudflared

def test_picks_the_right_download():
    assert asset_name("Windows", "AMD64") == "cloudflared-windows-amd64.exe"
    assert asset_name("Windows", "ARM64") == "cloudflared-windows-amd64.exe"  # runs under emulation
    assert asset_name("Linux", "x86_64") == "cloudflared-linux-amd64"
    assert asset_name("Linux", "aarch64") == "cloudflared-linux-arm64"
    assert asset_name("Darwin", "arm64") == "cloudflared-darwin-arm64.tgz"
    assert asset_name("FreeBSD", "amd64") is None


def release_transport(name, body, digest=None, api_ok=True):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.host == "api.github.com":
            if not api_ok:
                return httpx.Response(403, json={"message": "rate limited"})
            asset = {"name": name, "browser_download_url": f"https://github.com/cloudflare/cloudflared/releases/download/2026.10.0/{name}"}
            if digest is not None:
                asset["digest"] = digest
            return httpx.Response(200, json={"assets": [{"name": "other", "browser_download_url": "x"}, asset]})
        return httpx.Response(200, content=body)
    return httpx.MockTransport(handler), seen


def test_download_checks_the_published_checksum(tmp_path):
    body = b"MZ fake cloudflared"
    transport, _ = release_transport("cloudflared-windows-amd64.exe", body, "sha256:" + hashlib.sha256(body).hexdigest())
    exe = download_cloudflared(tmp_path / "tools", transport=transport, system="Windows", machine="AMD64")
    assert exe.read_bytes() == body and exe.parent == tmp_path / "tools"

    transport, _ = release_transport("cloudflared-windows-amd64.exe", body, "sha256:" + "0" * 64)
    with pytest.raises(RuntimeError, match="checksum"):
        download_cloudflared(tmp_path / "bad", transport=transport, system="Windows", machine="AMD64")
    assert not (tmp_path / "bad").exists()  # nothing half-installed


def test_download_falls_back_to_the_release_link_and_unpacks_mac_archives(tmp_path):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = b"\xcf\xfa\xed\xfe mac binary"
        info = tarfile.TarInfo("cloudflared")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    transport, seen = release_transport("cloudflared-darwin-arm64.tgz", buf.getvalue(), api_ok=False)
    exe = download_cloudflared(tmp_path, transport=transport, system="Darwin", machine="arm64")
    assert exe.read_bytes().endswith(b"mac binary")
    assert seen[-1] == "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-darwin-arm64.tgz"


def test_finds_cloudflared_where_it_is(tmp_path, monkeypatch):
    monkeypatch.setattr(phone_mod.shutil, "which", lambda _: None)
    assert find_cloudflared(None, tmp_path) is None
    own = tmp_path / phone_mod.exe_name()
    own.write_text("x")
    assert find_cloudflared(None, tmp_path) == own
    configured = tmp_path / "elsewhere" / "cf.exe"
    configured.parent.mkdir()
    configured.write_text("x")
    assert find_cloudflared(str(configured), tmp_path) == configured


# ------------------------------------------------------------------------------ the phone link, for real

@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(controller_mod, "notify", lambda *a: None)
    monkeypatch.setattr(phone_mod, "BACKOFF", (0.2, 0.2))
    for var in ("FAKE_TUNNEL", "FAKE_EXIT", "FAKE_POSITION"):
        monkeypatch.delenv(var, raising=False)


def make(tmp_path, secrets=None, state=None, **cfg_kw) -> Controller:
    cfg = BotConfig.model_validate({"data_dir": str(tmp_path / "data"), "log_dir": str(tmp_path / "logs"), **cfg_kw})
    if state is not None:
        controller_mod.save_state(cfg, state)
    secrets = secrets or Secrets(telegram_bot_token=BOT_TOKEN, telegram_chat_id=str(OWNER))
    ctl = Controller(cfg, secrets, mode="paper", config_path=None, worker_command=FAKE, port=0, poll_seconds=0.1)
    ctl.phone = PhoneAccess(ctl, tunnel_args=FAKE_TUNNEL)
    return ctl


def scenario(tmp_path, body, **kw):
    async def go():
        ctl = make(tmp_path, **kw)
        await ctl.server.start()
        monitor = asyncio.create_task(ctl.bot.monitor())
        pc = httpx.AsyncClient(base_url=f"http://127.0.0.1:{ctl.server.port}", headers={"X-Token": ctl.token})
        try:
            await body(ctl, pc)
        finally:
            monitor.cancel()
            await ctl.phone.close()
            if ctl.bot.running:
                await ctl.bot.stop("test cleanup")
            await pc.aclose()
            await ctl.server.stop()
            await ctl.bot.close()
    run(go())


def phone_client(ctl, token=None, host="seasonal-deck-organisms-sf.trycloudflare.com"):
    headers = {"Host": host}
    if token:
        headers["X-Token"] = token
    return httpx.AsyncClient(base_url=f"http://127.0.0.1:{ctl.phone.server.port}", headers=headers)


async def turn_on(ctl, pc):
    r = (await pc.post("/api/phone/on")).json()
    assert r["ok"], r
    await until(lambda: ctl.phone.state == "on")


async def sign_in(phone, data=None):
    return await phone.post("/auth", json={"init_data": data or init_data()})


def test_off_by_default_and_the_switch_is_remembered(tmp_path):
    async def body(ctl, pc):
        s = (await pc.get("/api/status")).json()
        assert s["phone"]["enabled"] is False and s["phone"]["state"] == "off" and s["viewer"] == "pc"
        await turn_on(ctl, pc)
        s = (await pc.get("/api/status")).json()["phone"]
        assert s["url"] == URL and s["problem"] is None
        assert load_state(ctl.cfg)["phone_access"] is True
        assert "--no-autoupdate" in " ".join(ctl.phone._tunnel.lines)  # cloudflared got the safe arguments
        r = (await pc.post("/api/phone/off")).json()
        assert r["ok"] and "off" in r["message"]
        assert ctl.phone.state == "off" and ctl.phone.url is None and not ctl.phone._serving
        assert load_state(ctl.cfg)["phone_access"] is False
        events = " ".join(e["message"] for e in ctl.events)
        assert "turned on by dashboard" in events and "turned off by dashboard" in events
    scenario(tmp_path, body)


def test_comes_back_on_after_a_restart_when_it_was_left_on(tmp_path):
    async def body(ctl, pc):
        await ctl.phone.start()
        await until(lambda: ctl.phone.state == "on")
    scenario(tmp_path, body, state={"phone_access": True})


def test_only_the_owner_signs_in_and_every_request_needs_the_session(tmp_path):
    async def body(ctl, pc):
        await turn_on(ctl, pc)
        async with phone_client(ctl) as phone:
            page = await phone.get("/")  # the sign-in page itself is public and holds nothing secret
            assert page.status_code == 200 and "telegram-web-app.js" in page.text and ctl.token not in page.text
            assert "frame-ancestors https://web.telegram.org" in page.headers["content-security-policy"]
            assert "x-frame-options" not in page.headers

            for path in ("/api/status", "/api/logs"):
                r = await phone.get(path)
                assert r.status_code == 401 and r.json()["signin"] is True
            assert (await phone.post("/api/bot/stop", headers={"X-Token": ctl.token})).status_code == 401  # the PC's token is no use

            assert (await sign_in(phone, init_data(user_id=999))).status_code == 403  # someone else's Telegram
            assert (await sign_in(phone, init_data(token="1:another-bot"))).status_code == 403
            assert (await sign_in(phone, "user=%7B%7D&hash=00")).status_code == 403
            assert any("refused Telegram user 999" in e["message"] for e in ctl.events)

            r = await sign_in(phone)
            d = r.json()
            assert r.status_code == 200 and d["ok"]
            assert f'const TOKEN = "{d["token"]}"' in d["page"] and '"phone" === "phone"' in d["page"]
            assert ctl.token not in d["page"]  # the phone never sees the PC's token

        async with phone_client(ctl, d["token"]) as phone:
            s = (await phone.get("/api/status")).json()
            assert s["viewer"] == "phone" and s["phone"]["in_use"] is True
            assert (await phone.get("/api/logs")).json()["ok"]
            r = await phone.post("/api/mode", json={"mode": "live", "confirm": "LIVE"})
            assert not r.json()["ok"] and "only be done on the PC" in r.json()["message"] and ctl.bot.mode == "paper"
            assert (await phone.post("/api/phone/on")).status_code == 404  # turning it on stays on the PC
            assert (await phone.get("/")).status_code == 200
        async with phone_client(ctl, d["token"], host="evil.example.com") as phone:
            assert (await phone.get("/api/status")).status_code == 403  # DNS rebinding
    scenario(tmp_path, body)


def test_phone_can_run_the_bot_and_its_actions_are_labelled(tmp_path):
    async def body(ctl, pc):
        await turn_on(ctl, pc)
        async with phone_client(ctl) as phone:
            token = (await sign_in(phone)).json()["token"]
        async with phone_client(ctl, token) as phone:
            assert (await phone.post("/api/bot/start")).json()["ok"]
            await until(lambda: ctl.bot.state == "running")
            r = (await phone.post("/api/action/pause", json={"source": "forged"})).json()
            assert r["payload"]["source"] == "phone (Telegram)"
            assert any("requested by phone (Telegram)" in e["message"] for e in ctl.events)
    scenario(tmp_path, body)


def test_turning_it_off_from_the_phone_ends_the_link(tmp_path):
    async def body(ctl, pc):
        await turn_on(ctl, pc)
        async with phone_client(ctl) as phone:
            token = (await sign_in(phone)).json()["token"]
        async with phone_client(ctl, token) as phone:
            r = (await phone.post("/api/phone/off")).json()
            assert r["ok"] and not ctl.phone.sessions  # the answer still reached the phone
        await until(lambda: ctl.phone.state == "off" and not ctl.phone._serving)
        assert not (await pc.get("/api/status")).json()["phone"]["enabled"]
        assert any("turned off by phone (Telegram)" in e["message"] for e in ctl.events)
    scenario(tmp_path, body)


def test_sessions_expire(tmp_path):
    async def body(ctl, pc):
        await turn_on(ctl, pc)
        async with phone_client(ctl) as phone:
            token = (await sign_in(phone)).json()["token"]
        ctl.phone.sessions[token][1] = time.time() - 1
        async with phone_client(ctl, token) as phone:
            assert (await phone.get("/api/status")).status_code == 401
    scenario(tmp_path, body)


def test_too_many_refused_sign_ins_are_slowed_down(tmp_path, monkeypatch):
    monkeypatch.setattr(phone_mod, "MAX_FAILURES", 3)

    async def body(ctl, pc):
        await turn_on(ctl, pc)
        async with phone_client(ctl) as phone:
            for _ in range(3):
                assert (await sign_in(phone, init_data(user_id=1))).status_code == 403
            assert (await sign_in(phone)).status_code == 429  # even a good one waits
    scenario(tmp_path, body)


def test_needs_telegram_and_a_known_owner(tmp_path):
    async def body(ctl, pc):
        s = (await pc.get("/api/status")).json()["phone"]
        assert "Set up Telegram first" in s["problem"]
        r = (await pc.post("/api/phone/on")).json()
        assert not r["ok"] and "Set up Telegram first" in r["message"] and ctl.phone.state == "off"
    scenario(tmp_path, body, secrets=Secrets())

    async def group(ctl, pc):
        r = (await pc.post("/api/phone/on")).json()
        assert not r["ok"] and "allowed_user_ids" in r["message"]
    scenario(tmp_path / "g", group, secrets=Secrets(telegram_bot_token=BOT_TOKEN, telegram_chat_id="-100555"))


def test_a_failing_tunnel_is_retried_and_reported(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_TUNNEL", "fail")

    async def body(ctl, pc):
        await pc.post("/api/phone/on")
        await until(lambda: ctl.phone.state == "error")
        s = (await pc.get("/api/status")).json()["phone"]
        assert "cloudflared stopped" in s["error"] and "no such host" in s["error"] and "Trying again" in s["detail"]
        monkeypatch.setenv("FAKE_TUNNEL", "")  # the internet is back
        await until(lambda: ctl.phone.state == "on")
    scenario(tmp_path, body)


def test_a_dropped_link_comes_back(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_TUNNEL", "drop")

    async def body(ctl, pc):
        await pc.post("/api/phone/on")
        await until(lambda: any("secure link closed" in e["message"] for e in ctl.events))
        monkeypatch.setenv("FAKE_TUNNEL", "")
        await until(lambda: ctl.phone.state == "on")
    scenario(tmp_path, body)


# ------------------------------------------------------------------------------ Telegram

def telegram_for(ctl, fake):
    tg = TelegramController(BOT_TOKEN, CHAT, ProxyActions(ctl), TelegramConfig(), transport=httpx.MockTransport(fake.handler))
    ctl.telegram = tg
    return tg


def test_dashboard_command_offers_to_turn_it_on_then_sends_the_button(tmp_path):
    async def body(ctl, pc):
        fake = FakeTelegram()
        tg = telegram_for(ctl, fake)
        try:
            await tg.handle_update(msg("/dashboard"))
            ask = fake.sent[-1]
            assert "Turn on phone access?" in ask["text"] and ctl.phone.state == "off"
            await tg.handle_update(button("yes:phone", ask["message_id"]))
            await until(lambda: ctl.phone.state == "on")
            await until(lambda: any(m.get("reply_markup", {}).get("inline_keyboard", [[{}]])[0][0].get("web_app") for m in fake.sent))
            announced = next(m for m in fake.sent if "Phone access is on" in m["text"])
            assert announced["reply_markup"]["inline_keyboard"][0][0]["web_app"]["url"] == URL
            assert announced["disable_notification"] is True
            assert "setChatMenuButton" in fake.calls

            await tg.handle_update(msg("/dashboard"))
            assert fake.sent[-1]["reply_markup"]["inline_keyboard"][0][0] == {"text": "📊 Open dashboard", "web_app": {"url": URL}}

            await tg.handle_update(msg("/dashboard off"))
            assert "Phone access is off" in fake.sent[-1]["text"] and ctl.phone.state == "off"
            assert any("Telegram (me)" in e["message"] for e in ctl.events)
        finally:
            await tg.close()
            ctl.telegram = None
    scenario(tmp_path, body)


def test_dashboard_command_explains_what_is_missing(tmp_path):
    async def body(ctl, pc):
        fake = FakeTelegram()
        tg = telegram_for(ctl, fake)
        try:
            await tg.handle_update(msg("/dashboard"))
            assert "allowed_user_ids" in fake.sent[-1]["text"]
        finally:
            await tg.close()
            ctl.telegram = None
    scenario(tmp_path, body, secrets=Secrets(telegram_bot_token=BOT_TOKEN, telegram_chat_id="-100555"))


def test_dashboard_button_is_on_the_keyboard():
    assert {"text": "📱 Dashboard", "callback_data": "cmd:dashboard"} in KEYBOARD["inline_keyboard"][0]
