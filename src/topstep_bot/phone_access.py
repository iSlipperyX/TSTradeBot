"""Phone access: the dashboard on your phone, inside Telegram (a Telegram Mini App).

    phone (Telegram) --HTTPS--> Cloudflare --secure tunnel--> phone link on your PC --> the same controller

How it works
  * Switched on from the dashboard's Settings tab (or /dashboard in Telegram, with a confirmation
    tap). It is off by default, and turning it off ends the link at once.
  * The controller starts a second, separate web server on 127.0.0.1 (the "phone link") and a
    Cloudflare quick tunnel (cloudflared) to it. The tunnel is an outgoing connection from your PC:
    no router ports are opened. Cloudflare gives it a random https://....trycloudflare.com address,
    which the bot puts behind an "Open dashboard" button in your Telegram chat.
  * The bot keeps running on your PC and still trades from your own internet connection. The tunnel
    only carries the dashboard to your phone - it is not a VPN or VPS (which Topstep forbids).

Security
  * Every request needs a sign-in. The page Telegram opens reads the sign-in data Telegram gives a
    Mini App (initData). It is signed with your bot's token, so the PC can check that Telegram issued
    it, for your bot, recently, and for your own Telegram account (TELEGRAM_CHAT_ID, or
    telegram.allowed_user_ids). Anyone else - including someone who learns the address - is refused.
  * A sign-in gives a short-lived session token that this page sends with every request.
  * The phone link serves only the dashboard's own routes (an allowlist), and it can't switch the
    bot to LIVE mode or turn phone access on: those stay on the PC. It can turn phone access off.
  * Cloudflare carries the traffic (its HTTPS ends at Cloudflare), like any website behind it.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import io
import json
import logging
import os
import platform
import re
import secrets as pysecrets
import shutil
import tarfile
import time
from collections import deque
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qsl, urlsplit

import httpx

from topstep_bot.logging_setup import spawn
from topstep_bot.notify import redact
from topstep_bot.web import ALLOWED_HOSTS, HttpServer, Request, Response, html_response, json_response

if TYPE_CHECKING:
    from topstep_bot.controller import Controller

log = logging.getLogger("topstep_bot.phone")

AUTH_MAX_AGE = 24 * 3600  # Telegram signs fresh sign-in data each time the dashboard is opened
SESSION_SECONDS = 12 * 3600
MAX_SESSIONS = 10
MAX_FAILURES = 20  # refused sign-ins per 10 minutes before the link stops trying to check more
START_TIMEOUT = 60  # seconds for cloudflared to get an address and connect
BACKOFF = (10, 30, 60, 120, 300)
HEALTHY_AFTER = 600
STATE_KEY = "phone_access"

# The quick-tunnel address cloudflared prints (never api.trycloudflare.com, which appears in its errors).
TUNNEL_URL = re.compile(r"https://(?!api\.)[a-z0-9]+(?:-[a-z0-9]+)+\.trycloudflare\.com")
CONNECTED = "Registered tunnel connection"

RELEASES_API = "https://api.github.com/repos/cloudflare/cloudflared/releases/latest"
RELEASE_DOWNLOAD = "https://github.com/cloudflare/cloudflared/releases/latest/download/"
WINDOWS_INSTALL_DIRS = (r"C:\Program Files (x86)\cloudflared", r"C:\Program Files\cloudflared")

# Routes of the PC dashboard that the phone link serves too. Anything not listed (including routes
# added later) stays PC-only until it is added here on purpose.
REMOTE_ROUTES = {
    ("GET", "/api/status"),
    ("GET", "/api/logs"),
    ("POST", "/api/bot/start"),
    ("POST", "/api/bot/stop"),
    ("POST", "/api/bot/restart"),
    ("POST", "/api/mode"),  # paper only: the controller refuses LIVE from the phone
    ("POST", "/api/action/*"),
    ("POST", "/api/updates/check"),
    ("POST", "/api/updates/install"),
    ("POST", "/api/updates/cancel"),
    ("POST", "/api/phone/off"),
}
OPEN_ROUTES = {("GET", "/"), ("POST", "/auth")}  # the sign-in page and the sign-in itself
# Telegram Web shows Mini Apps in a frame; the phone apps use their own browser view.
HEADERS = {
    "Content-Security-Policy": "frame-ancestors https://web.telegram.org https://*.telegram.org",
    "Referrer-Policy": "no-referrer",
}


# ------------------------------------------------------------------------------ Telegram sign-in

def verify_init_data(init_data: str, bot_token: str, *, max_age: float = AUTH_MAX_AGE, now: float | None = None) -> dict:
    """Check Telegram's signature on Mini App sign-in data and return its user; ValueError if it isn't valid.

    https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app
    """
    if not init_data or not bot_token:
        raise ValueError("no sign-in data")
    try:
        fields = dict(parse_qsl(init_data, keep_blank_values=True, strict_parsing=True))
    except ValueError:
        raise ValueError("malformed sign-in data") from None
    received = fields.pop("hash", "")
    check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not received or not hmac.compare_digest(expected, received.lower()):
        raise ValueError("the signature does not match this bot")
    try:
        signed_at = int(fields.get("auth_date", ""))
        user = json.loads(fields.get("user", ""))
    except ValueError:
        raise ValueError("incomplete sign-in data") from None
    now = time.time() if now is None else now
    if now - signed_at > max_age or signed_at - now > 300:
        raise ValueError("the sign-in is too old - reopen the dashboard from Telegram")
    if not isinstance(user, dict) or not isinstance(user.get("id"), int):
        raise ValueError("no Telegram user in the sign-in")
    return user


def sign_init_data(fields: dict[str, str], bot_token: str) -> str:
    """What Telegram does: the signed query string for ``fields`` (used by the tests and the demo)."""
    from urllib.parse import urlencode

    check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    return urlencode({**fields, "hash": hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()})


def owner_ids(chat_id: str | None, allowed_user_ids: list[int]) -> set[int]:
    """The Telegram accounts that may open the dashboard: allowed_user_ids, else the private chat's owner."""
    if allowed_user_ids:
        return set(allowed_user_ids)
    try:
        chat = int(str(chat_id))
    except ValueError:
        return set()
    return {chat} if chat > 0 else set()  # a private chat's ID is its user's ID; groups are negative


def dashboard_keyboard(url: str) -> dict:
    return {"inline_keyboard": [[{"text": "📊 Open dashboard", "web_app": {"url": url}}]]}


# ------------------------------------------------------------------------------ cloudflared

def asset_name(system: str | None = None, machine: str | None = None) -> str | None:
    """The cloudflared download for this computer (None if Cloudflare doesn't publish one)."""
    system = (system or platform.system()).lower()
    machine = (machine or platform.machine()).lower()
    arch = {"amd64": "amd64", "x86_64": "amd64", "arm64": "arm64", "aarch64": "arm64",
            "x86": "386", "i386": "386", "i686": "386", "armv7l": "arm"}.get(machine)
    if system == "windows":
        return "cloudflared-windows-386.exe" if arch == "386" else "cloudflared-windows-amd64.exe"
    if system == "linux" and arch:
        return f"cloudflared-linux-{arch}"
    if system == "darwin":
        return f"cloudflared-darwin-{'arm64' if arch == 'arm64' else 'amd64'}.tgz"
    return None


def exe_name() -> str:
    return "cloudflared.exe" if os.name == "nt" else "cloudflared"


def find_cloudflared(configured: str | None, tools_dir: Path) -> Path | None:
    """cloudflared from config, the bot's own copy, PATH, or where the Windows installer puts it."""
    candidates = [Path(configured).expanduser()] if configured else []
    candidates.append(tools_dir / exe_name())
    if found := shutil.which("cloudflared"):
        candidates.append(Path(found))
    if os.name == "nt":
        candidates += [Path(d) / "cloudflared.exe" for d in WINDOWS_INSTALL_DIRS]
    return next((p for p in candidates if p.is_file()), None)


def download_cloudflared(tools_dir: Path, *, transport: httpx.BaseTransport | None = None,
                         system: str | None = None, machine: str | None = None) -> Path:
    """Download Cloudflare's official cloudflared from its GitHub releases, checking its SHA-256."""
    name = asset_name(system, machine)
    if name is None:
        raise RuntimeError("Cloudflare has no cloudflared download for this computer - install it yourself and set "
                           "dashboard.cloudflared_path in config.yaml")
    with httpx.Client(timeout=httpx.Timeout(30, read=120), follow_redirects=True, transport=transport) as client:
        url, digest = RELEASE_DOWNLOAD + name, None
        try:
            release = client.get(RELEASES_API, headers={"Accept": "application/vnd.github+json"})
            release.raise_for_status()
            asset = next(a for a in release.json()["assets"] if a["name"] == name)
            url, digest = asset["browser_download_url"], asset.get("digest")
        except (httpx.HTTPError, ValueError, KeyError, StopIteration) as exc:
            log.warning("Could not read cloudflared's release details (%s); downloading the latest release directly", exc)
        if not url.startswith("https://github.com/cloudflare/cloudflared/"):
            raise RuntimeError(f"unexpected cloudflared download address: {url}")
        log.info("Downloading %s", url)
        data = client.get(url)
        data.raise_for_status()
    body = data.content
    if digest:
        algo, _, want = str(digest).partition(":")
        if algo != "sha256" or hashlib.sha256(body).hexdigest() != want.lower():
            raise RuntimeError("the cloudflared download did not match its published checksum - not installed")
    else:
        log.warning("cloudflared's release has no published checksum; installed it from github.com over HTTPS")
    if name.endswith(".tgz"):
        with tarfile.open(fileobj=io.BytesIO(body), mode="r:gz") as tar:
            member = next((m for m in tar.getmembers() if m.isfile() and Path(m.name).name == "cloudflared"), None)
            if member is None:
                raise RuntimeError("the cloudflared download has no program in it")
            body = tar.extractfile(member).read()
    tools_dir.mkdir(parents=True, exist_ok=True)
    target = tools_dir / exe_name()
    part = target.with_suffix(".part")
    part.write_bytes(body)
    part.chmod(0o755)
    os.replace(part, target)
    log.info("cloudflared installed at %s", target)
    return target


class Tunnel:
    """One cloudflared quick tunnel to a local port."""

    def __init__(self, exe: Path | str, port: int, config_file: Path, extra_args: list[str] | None = None):
        self.exe = str(exe)
        self.port = port
        self.config_file = config_file
        self.extra_args = extra_args or []
        self.proc: asyncio.subprocess.Process | None = None
        self.url: str | None = None
        self.lines: deque[str] = deque(maxlen=30)
        self._reader: asyncio.Task | None = None

    def command(self) -> list[str]:
        # an empty config file: a cloudflared set up for something else on this PC can't change this tunnel
        return [self.exe, *self.extra_args, "tunnel", "--no-autoupdate", "--config", str(self.config_file),
                "--url", f"http://127.0.0.1:{self.port}"]

    async def start(self, timeout: float = START_TIMEOUT) -> str:
        self.config_file.parent.mkdir(parents=True, exist_ok=True)
        self.config_file.write_text("", encoding="utf-8")
        self.proc = await asyncio.create_subprocess_exec(
            *self.command(), stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE)
        connected = asyncio.Event()
        self._reader = spawn(self._read(connected), name="cloudflared-log")
        try:
            await asyncio.wait_for(self._until_ready(connected), timeout)
        except TimeoutError:
            await self.stop()
            raise RuntimeError("Cloudflare did not answer in time. Is the PC online? A firewall may be blocking "
                               "cloudflared.") from None
        return self.url

    async def _until_ready(self, connected: asyncio.Event) -> None:
        exited = asyncio.ensure_future(self.proc.wait())
        ready = asyncio.ensure_future(connected.wait())
        try:
            await asyncio.wait([exited, ready], return_when=asyncio.FIRST_COMPLETED)
        finally:
            ready.cancel()
            if not exited.done():
                exited.cancel()
        if not connected.is_set():
            last = next((ln for ln in reversed(self.lines) if "ERR" in ln or "failed" in ln), self.lines[-1] if self.lines else "")
            raise RuntimeError(f"cloudflared stopped (exit code {self.proc.returncode}): {last[-300:]}".rstrip(": "))

    async def _read(self, connected: asyncio.Event) -> None:
        """Keep reading cloudflared's log (a full pipe would freeze it); note the address and the connection."""
        assert self.proc and self.proc.stderr
        while line := await self.proc.stderr.readline():
            text = line.decode("utf-8", "replace").rstrip()
            self.lines.append(text)
            log.debug("cloudflared: %s", text)
            if self.url is None and (m := TUNNEL_URL.search(text)):
                self.url = m.group(0)
            if CONNECTED in text and self.url:
                connected.set()

    async def wait(self) -> int:
        assert self.proc
        return await self.proc.wait()

    async def stop(self) -> None:
        proc = self.proc
        if proc is not None and proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), 5)
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                await proc.wait()
        if self._reader:
            await asyncio.wait([self._reader], timeout=2)


# ------------------------------------------------------------------------------ the phone link

class PhoneAccess:
    """Runs the phone link (its own web server + the tunnel) for the controller while it is switched on."""

    def __init__(self, ctl: Controller, *, tunnel_args: list[str] | None = None):
        from topstep_bot.controller import load_state

        self.ctl = ctl
        self.cfg = ctl.cfg
        self.secrets = ctl.secrets
        self.enabled = bool(load_state(self.cfg).get(STATE_KEY, self.cfg.dashboard.phone_access))
        self.state = "off"  # off | starting | on | error
        self.detail = ""
        self.error: str | None = None
        self.url: str | None = None
        self.up_since: float | None = None
        self.tunnel_args = tunnel_args  # tests: run a stand-in instead of the real cloudflared
        self.sessions: dict[str, list] = {}  # token -> [Telegram user id, expires, last request]
        self.refused: deque[float] = deque()
        self._task: asyncio.Task | None = None
        self._tunnel: Tunnel | None = None
        self._failures = 0
        self.tools_dir = Path(self.cfg.data_dir) / "tools"
        routes = [r for r in ctl.server.routes if (r[0], r[1]) in REMOTE_ROUTES]
        self.server = HttpServer(
            "127.0.0.1", 0,
            [("GET", "/", lambda r: html_response(SIGN_IN_PAGE)), ("POST", "/auth", self._sign_in), *routes],
            name="phone link", allowed_hosts=self._hosts, authorize=self._authorize, remote=True, headers=HEADERS,
        )
        self._serving = False

    # ---------------------------------------------------------------- who may use it

    def owners(self) -> set[int]:
        return owner_ids(self.secrets.telegram_chat_id, self.cfg.telegram.allowed_user_ids)

    def problem(self) -> str | None:
        """Why phone access can't work with the current setup (None if it can)."""
        if not (self.secrets.telegram_bot_token and self.secrets.telegram_chat_id):
            return ("Set up Telegram first on the Setup tab: the dashboard opens through your Telegram bot, "
                    "and Telegram's sign-in is what keeps everyone else out.")
        if not self.cfg.telegram.control_enabled:
            return "Turn on remote control for Telegram on the Setup tab: the Dashboard button is sent through it."
        if not self.owners():
            return ("Your Telegram chat is a group. Add your own Telegram user ID to telegram.allowed_user_ids in "
                    "config.yaml so only you can open the dashboard.")
        return None

    def _hosts(self) -> tuple[str, ...]:
        host = urlsplit(self.url).hostname if self.url else None
        return (*ALLOWED_HOSTS, host) if host else ALLOWED_HOSTS

    def _prune(self) -> None:
        now = time.time()
        for t in [t for t, (_, exp, _) in self.sessions.items() if exp < now]:
            del self.sessions[t]

    def _session(self, token: str) -> int | None:
        self._prune()
        entry = self.sessions.get(token) if token else None
        if entry is None:
            return None
        entry[2] = time.time()
        return entry[0]

    def _authorize(self, req: Request) -> Response | None:
        if (req.method, req.path) in OPEN_ROUTES:
            return None
        if self._session(req.headers.get("x-token", "")) is None:
            return json_response({"ok": False, "signin": True,
                                  "message": "Your phone session ended - reopen the dashboard from Telegram."}, 401)
        return None

    def _sign_in(self, req: Request) -> Response | dict:
        now = time.time()
        while self.refused and now - self.refused[0] > 600:
            self.refused.popleft()
        if len(self.refused) >= MAX_FAILURES:
            return json_response({"ok": False, "message": "Too many refused sign-ins - try again in a few minutes."}, 429)
        init_data = str(req.json().get("init_data", ""))[:8192]
        try:
            user = verify_init_data(init_data, self.secrets.telegram_bot_token or "", now=now)
        except ValueError as exc:
            self.refused.append(now)
            log.warning("Phone link: refused a sign-in (%s)", exc)
            message = str(exc) if "too old" in str(exc) else (
                "This page only opens from your Topstep bot in Telegram: send /dashboard there and tap Open dashboard.")
            return json_response({"ok": False, "message": message}, 403)
        if user["id"] not in self.owners():
            self.refused.append(now)
            name = user.get("username") or user.get("first_name") or "?"
            self.ctl.bot._event("warning", f"Phone link: refused Telegram user {user['id']} ({name}) - not your account")
            return json_response({"ok": False, "message": "Only the owner's Telegram account can open this dashboard."}, 403)
        token = pysecrets.token_urlsafe(32)
        self.sessions[token] = [user["id"], now + SESSION_SECONDS, now]
        while len(self.sessions) > MAX_SESSIONS:
            del self.sessions[next(iter(self.sessions))]
        log.info("Phone link: dashboard opened by Telegram user %s", user["id"])
        return {"token": token, "page": self.ctl.page(token, view="phone")}

    # ---------------------------------------------------------------- switching it on and off

    def status(self) -> dict:
        return {
            "enabled": self.enabled,
            "state": self.state,
            "detail": self.detail,
            "error": self.error,
            "url": self.url if self.state == "on" else None,
            "problem": self.problem(),
            "telegram": self.ctl.telegram is not None,
            "in_use": self.in_use(),
        }

    def in_use(self) -> bool:
        """Is the dashboard open on the phone right now (it asks for news every 2 seconds while open)?"""
        self._prune()
        return any(time.time() - seen < 15 for _, _, seen in self.sessions.values())

    def _save(self) -> None:
        from topstep_bot.controller import load_state, save_state

        state = load_state(self.cfg)
        state[STATE_KEY] = self.enabled
        save_state(self.cfg, state)

    async def start(self) -> None:
        """At controller start: bring the link back if it was left on; otherwise clear an old Telegram button."""
        if self.enabled and self.problem() is None:
            self._launch()
        elif self.ctl.telegram:
            await self.ctl.telegram.set_dashboard_button(None)

    def _launch(self) -> None:
        if self._task is None or self._task.done():
            self.state, self.detail, self.error = "starting", "Starting…", None
            self._task = spawn(self._run(), name="phone-link")

    async def enable(self, source: str) -> str:
        if problem := self.problem():
            raise ValueError(problem)
        if self.enabled and self._task and not self._task.done():
            return "Phone access is already on." if self.state == "on" else "Phone access is starting - the button follows in Telegram."
        self.enabled = True
        self._save()
        self.ctl.bot._event("warning", f"Phone access turned on by {source}")
        self._launch()
        return "Turning on phone access. The Open dashboard button arrives in Telegram in a moment."

    async def disable(self, source: str, wait: bool = True) -> str:
        """Turn the link off. From the phone itself it ends just after this answer is sent (wait=False)."""
        was_on = self.enabled or self._task is not None
        self.enabled = False
        self._save()
        self.sessions.clear()
        if was_on:
            self.ctl.bot._event("warning", f"Phone access turned off by {source}")
        if wait:
            await self._teardown()
        else:
            spawn(self._teardown(delay=0.5), name="phone-link-off")
        return "Phone access is off. The phone link no longer opens the dashboard."

    async def telegram_changed(self) -> None:
        """Telegram was set up again on the Setup tab: sign everyone in again under the new setup."""
        self.sessions.clear()
        if self.problem() is not None:
            await self._teardown()  # nobody could sign in; the on/off choice is kept for when it is fixed
        elif self.enabled and self.state == "on" and self.url:
            await self._announce(self.url)  # the (possibly new) bot gets the button too
        elif self.enabled:
            self._launch()

    async def close(self) -> None:
        """Controller shutdown: end the link but keep the on/off choice for next time."""
        self.sessions.clear()
        await self._teardown()

    async def _teardown(self, delay: float = 0) -> None:
        if delay:
            await asyncio.sleep(delay)
        task, self._task = self._task, None
        if task and not task.done():
            task.cancel()
            await asyncio.wait([task], timeout=10)
        await self._stop_tunnel()
        await self._stop_server()
        self.state, self.detail, self.error, self.url, self.up_since = "off", "", None, None, None
        if self.ctl.telegram:
            await self.ctl.telegram.set_dashboard_button(None)

    async def _stop_tunnel(self) -> None:
        tunnel, self._tunnel = self._tunnel, None
        if tunnel:
            await tunnel.stop()

    async def _stop_server(self) -> None:
        if self._serving:
            self._serving = False
            await self.server.stop()

    # ---------------------------------------------------------------- keeping it up

    async def _cloudflared(self) -> Path:
        if self.tunnel_args is not None:
            return Path(self.tunnel_args[0])
        found = find_cloudflared(self.cfg.dashboard.cloudflared_path, self.tools_dir)
        if found:
            return found
        if self.cfg.dashboard.cloudflared_path:
            raise RuntimeError(f"cloudflared was not found at {self.cfg.dashboard.cloudflared_path} (dashboard.cloudflared_path)")
        self.detail = "Downloading Cloudflare's tunnel program (once, about 60 MB)…"
        self.ctl.bot._event("info", "Phone access: downloading cloudflared from Cloudflare's GitHub releases (once)")
        try:
            return await asyncio.to_thread(download_cloudflared, self.tools_dir)
        except httpx.HTTPError as exc:
            raise RuntimeError(f"could not download cloudflared: {redact(exc)}") from None

    async def _run(self) -> None:
        while self.enabled:
            started = time.time()
            try:
                self.state, self.error = "starting", None
                exe = await self._cloudflared()
                if not self._serving:
                    await self.server.start()
                    self._serving = True
                self.detail = "Connecting to Cloudflare…"
                tunnel = Tunnel(exe, self.server.port, self.tools_dir / "cloudflared-empty.yml",
                                extra_args=self.tunnel_args[1:] if self.tunnel_args else None)
                self._tunnel = tunnel
                url = await tunnel.start()
                self.url, self.state, self.detail, self.up_since = url, "on", "", time.time()
                self.ctl.bot._event("info", "Phone access is on: open the dashboard from the button in Telegram")
                await self._announce(url)
                code = await tunnel.wait()
                if not self.enabled:
                    return
                raise RuntimeError(f"the secure link closed (cloudflared exit code {code})")
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - keep retrying while it is switched on
                await self._stop_tunnel()
                self.url, self.up_since = None, None
                self._failures = 0 if time.time() - started > HEALTHY_AFTER else self._failures + 1
                wait = BACKOFF[min(self._failures - 1, len(BACKOFF) - 1)] if self._failures else BACKOFF[0]
                self.state, self.error, self.detail = "error", str(exc), f"Trying again in {wait}s"
                self.ctl.bot._event("warning", f"Phone access: {exc} - trying again in {wait}s")
                if self.ctl.telegram:
                    await self.ctl.telegram.set_dashboard_button(None)
                await asyncio.sleep(wait)

    async def _announce(self, url: str) -> None:
        tg = self.ctl.telegram
        if tg is None:
            return
        await tg.set_dashboard_button(url)
        sent = await tg.send("📱 Phone access is on. Tap Open dashboard, or the Dashboard button beside the message box. "
                             "Only your Telegram account can open it. Turn it off any time with /dashboard off.",
                             dashboard_keyboard(url), silent=True)
        if sent is None:
            self.ctl.bot._event("warning", "Telegram would not show the Open dashboard button: phone access needs a private "
                                           "chat with your bot, not a group.")


# ------------------------------------------------------------------------------ the sign-in page

SIGN_IN_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>Topstep Bot</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
:root { color-scheme: light dark; --bg:#f5f5f3; --text:#111110; --muted:#6f6e69; --surface:#fff; --border:#e4e3df; --crit:#d03b3b; }
@media (prefers-color-scheme: dark) { :root { --bg:#111110; --text:#f5f5f3; --muted:#a3a29a; --surface:#1a1a19; --border:#2e2e2b; --crit:#ff8a8a; } }
body { margin:0; min-height:100vh; display:grid; place-items:center; background:var(--bg); color:var(--text);
  font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif; padding:16px; box-sizing:border-box; }
.card { max-width:380px; background:var(--surface); border:1px solid var(--border); border-radius:14px; padding:22px 20px; text-align:center; }
.brand { font-weight:750; font-size:18px; margin-bottom:6px; }
.msg { color:var(--muted); margin:0; } .msg.bad { color:var(--crit); }
.spin { width:22px; height:22px; margin:14px auto 0; border:3px solid var(--border); border-top-color:var(--muted);
  border-radius:50%; animation:s 0.8s linear infinite; } @keyframes s { to { transform:rotate(360deg); } }
</style>
</head>
<body>
<div class="card"><div class="brand">Topstep Bot</div><p class="msg" id="msg">Signing you in through Telegram…</p><div class="spin" id="spin"></div></div>
<script>
function fail(text) { const m = document.getElementById("msg"); m.textContent = text; m.className = "msg bad";
  document.getElementById("spin").hidden = true; }
(async () => {
  const tg = window.Telegram && window.Telegram.WebApp;
  let initData = tg && tg.initData;
  if (!initData) initData = new URLSearchParams(location.hash.slice(1)).get("tgWebAppData") || "";
  if (!initData) { fail("For your safety this dashboard only opens inside Telegram. Send /dashboard to your Topstep bot and tap Open dashboard."); return; }
  if (tg) { tg.ready(); tg.expand(); }
  let d;
  try {
    const r = await fetch("/auth", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({init_data:initData})});
    d = await r.json();
  } catch (e) { fail("Can't reach the bot. Is the PC on and the Topstep Bot window open? Send /dashboard in Telegram for a fresh link."); return; }
  if (!d.ok) { fail(d.message || "Sign-in refused."); return; }
  document.open(); document.write(d.page); document.close();
})();
</script>
</body>
</html>
"""


def describe(info: dict[str, Any]) -> str:
    """One line for Telegram about where phone access stands."""
    if info["state"] == "on":
        return "Phone access is on."
    if info["state"] == "error":
        return f"Phone access is having trouble: {info['error']}. {info['detail']}."
    if info["enabled"]:
        return f"Phone access is starting ({info['detail'] or 'connecting'}). The button follows in a moment."
    return "Phone access is off."
