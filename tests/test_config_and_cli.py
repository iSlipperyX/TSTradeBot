"""Config loading, CLI error handling, secret redaction, alert delivery and news wording."""

import asyncio
from datetime import time, timedelta

import httpx
import pytest

from topstep_bot import cli
from topstep_bot.config import BotConfig, ConfigError, NotificationsConfig, Secrets, load_config
from topstep_bot.news import NewsCalendar, NewsEvent
from topstep_bot.notify import Notifier, redact
from topstep_bot.sessions import SessionSchedule

from .conftest import CT, ct


def write(tmp_path, text):
    path = tmp_path / "config.yaml"
    path.write_text(text, encoding="utf-8")
    return path


@pytest.mark.parametrize("value", ["off", "false", "no", '""', "null"])
def test_yaml_off_disables_daily_restart_instead_of_midnight(tmp_path, value):
    cfg = load_config(write(tmp_path, f"service:\n  daily_restart_time: {value}\n  check_in_time: {value}\n"))
    assert cfg.service.daily_restart_time is None and cfg.service.check_in_time is None


def test_service_times_still_parse(tmp_path):
    cfg = load_config(write(tmp_path, 'service:\n  daily_restart_time: "16:20"\n  check_in_time: 7:45\n'))
    assert cfg.service.daily_restart_time == time(16, 20) and cfg.service.check_in_time == time(7, 45)


@pytest.mark.parametrize("text,expected", [
    ("risk:\n  risk_per_trad: 100\n", "risk.risk_per_trad: unknown setting"),
    ("risk:\n  risk_per_trade: -5\n", "risk.risk_per_trade"),
    ("strategy:\n  name: orbb\n", "Unknown strategy 'orbb'"),
    ("strategy:\n  name: orb\n  params: {target: 2}\n", "Unknown parameter(s) for 'orb': target"),
    ("instrument:\n  timeframe_minutes: 10\nstrategy:\n  name: orb\n  params: {range_minutes: 15}\n", "multiple of the bar timeframe"),
    ("mode: paper\n  risk: 1\n", "not valid YAML"),
    ("- just\n- a list\n", "should contain settings"),
    ("risk:\n  risk_per_trade: 100\nmode: paper\nrisk:\n  risk_per_trade: 200\n", "risk: appears twice (lines 1 and 4)"),
    ("session:\n  trade_start: 8\n", "must look like"),
    ('session:\n  trade_start: "8"\n', "must look like"),
    ("session:\n  trade_start: 0800\n", "must look like"),
    ("session:\n  trade_start: 830\n", "must look like"),
])
def test_config_problems_are_explained(tmp_path, text, expected):
    with pytest.raises(ConfigError, match=None) as info:
        load_config(write(tmp_path, text))
    assert expected in str(info.value)


def test_cli_reports_config_problems_and_unexpected_errors(tmp_path, monkeypatch, capsys, restore_logging):
    monkeypatch.chdir(tmp_path)
    write(tmp_path, "risk:\n  risk_per_trad: 100\n")
    assert cli.main(["-c", "config.yaml", "journal"]) == 2
    assert "Configuration problem" in capsys.readouterr().out

    write(tmp_path, "mode: paper\n")

    def explode(args):
        raise RuntimeError("something broke")

    monkeypatch.setattr(cli, "cmd_journal", explode)
    args = cli.build_parser().parse_args(["-c", "config.yaml", "journal"])
    args.func = explode
    assert cli.dispatch(args) == 1
    out = capsys.readouterr().out
    assert "Unexpected error" in out and "errors.log" in out
    for name in ("errors.log", "commands.log"):  # never bot.log: a running bot may be writing that one
        log = (tmp_path / "logs" / name).read_text(encoding="utf-8")
        assert "RuntimeError: something broke" in log and "Traceback" in log
    assert not (tmp_path / "logs" / "bot.log").exists()


def test_redact_hides_tokens_and_webhook_secrets():
    text = ("Client error '400' for url 'https://api.telegram.org/bot123456:AAH-x_y9/sendMessage' and "
            "https://discord.com/api/webhooks/42/abc-DEF_1?wait=1 near /bottom")
    out = redact(text)
    assert "AAH-x_y9" not in out and "abc-DEF_1" not in out
    assert "/bot<secret>/sendMessage" in out and "/webhooks/42/<secret>" in out and "/bottom" in out


def test_failing_discord_does_not_silence_telegram_and_logs_no_secret(caplog):
    async def go():
        sent = []

        def handler(request):
            if "discord" in request.url.host:
                return httpx.Response(500)
            sent.append(request.url.path)
            return httpx.Response(200, json={"ok": True})

        secrets = Secrets(discord_webhook_url="https://discord.com/api/webhooks/1/SECRETHOOK",
                          telegram_bot_token="999:TOKENSECRET", telegram_chat_id="5")
        n = Notifier(NotificationsConfig(), secrets)
        n.start()
        await n._client.aclose()
        n._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        n.notify("entry", "hello")
        await asyncio.wait_for(n._queue.join(), 5)
        await n.stop()
        return sent

    sent = asyncio.run(go())
    assert sent == ["/bot999:TOKENSECRET/sendMessage"]
    assert "Discord alert failed" in caplog.text
    assert "SECRETHOOK" not in caplog.text and "TOKENSECRET" not in caplog.text


def test_news_blackout_reason_uses_central_time(tmp_path):
    cal = NewsCalendar("https://x", tmp_path / "n.json", ["High"], ["USD"], 5, 10)
    release = ct(2026, 3, 3, 7, 30)
    cal.events = [NewsEvent("CPI m/m", "USD", "High", release)]
    schedule = SessionSchedule(BotConfig().session.model_copy(update={"trade_start": time(7, 0)}))
    schedule.news = cal
    reason = schedule.entry_block_reason(release - timedelta(minutes=2))
    assert reason == "news blackout: USD CPI m/m at 07:30 CT"
    assert release.astimezone(CT).hour == 7


# ---------------------------------------------------------------- emergency flatten

FLATTEN_CONFIG = """accounts:
  - name: c1
    account_id: 111
  - name: c2
    account_id: 222
    login: second
"""


class FlattenBroker:
    """TopstepX as the emergency flatten sees it: per account, open positions and working orders."""

    def __init__(self, fail_close=0, fail_always=False):
        from topstep_bot.models import Order, OrderSide, OrderStatus, OrderType, Position

        self.positions = {111: [Position(111, "CON.MES", 1, 100.0)], 222: [Position(222, "CON.MNQ", -2, 200.0)]}
        self.orders = {111: [Order(7, 111, "CON.MES", OrderType.STOP, OrderSide.SELL, 1, OrderStatus.OPEN, stop_price=95.0)],
                       222: [Order(8, 222, "CON.MNQ", OrderType.STOP, OrderSide.BUY, 2, OrderStatus.OPEN, stop_price=210.0)]}
        self.fail_close, self.fail_always = fail_close, fail_always
        self.logins, self.calls = [], []


class FlattenClient:
    def __init__(self, broker, login):
        self.broker, self.login = broker, login

    async def __aenter__(self):
        self.broker.logins.append(self.login)
        return self

    async def __aexit__(self, *exc):
        return False

    async def search_accounts(self, only_active=True):
        from topstep_bot.models import Account

        return [Account(111, "C1-50K", 50_000.0), Account(222, "C2-100K", 100_000.0)]

    async def search_open_positions(self, account_id):
        return list(self.broker.positions[account_id])

    async def search_open_orders(self, account_id):
        return list(self.broker.orders[account_id])

    async def close_position(self, account_id, contract_id, *, quick=False):
        assert quick
        self.broker.calls.append(("close", account_id, contract_id))
        if self.broker.fail_always or self.broker.fail_close > 0:
            self.broker.fail_close -= 1
            raise httpx.ConnectError("getaddrinfo failed")
        self.broker.positions[account_id] = [p for p in self.broker.positions[account_id] if p.contract_id != contract_id]

    async def cancel_order(self, account_id, order_id, *, quick=False):
        assert quick
        self.broker.calls.append(("cancel", account_id, order_id))
        self.broker.orders[account_id] = [o for o in self.broker.orders[account_id] if o.id != order_id]


def flatten(tmp_path, monkeypatch, broker, capsys):
    path = write(tmp_path, FLATTEN_CONFIG)

    class FakeSecrets:
        def login_for(self, cfg):
            return cfg.account.login

    monkeypatch.setattr(cli, "load_secrets", lambda: FakeSecrets())
    monkeypatch.setattr(cli, "_client", lambda cfg: FlattenClient(broker, cfg.account.login))
    monkeypatch.setattr(cli, "FLATTEN_SETTLE_SECONDS", 0)
    monkeypatch.setattr(cli.console, "width", 300)
    rc = cli.cmd_flatten(cli.build_parser().parse_args(["-c", str(path), "flatten", "--yes"]))
    return rc, capsys.readouterr().out


def test_flatten_uses_each_accounts_key_and_closes_before_cancelling(tmp_path, monkeypatch, capsys):
    broker = FlattenBroker()
    rc, out = flatten(tmp_path, monkeypatch, broker, capsys)
    assert rc == 0 and "Flat." in out and "does not halt a running bot" in out
    assert sorted(broker.logins, key=str) == sorted([None, "second"], key=str)
    for account in (111, 222):
        mine = [c[0] for c in broker.calls if c[1] == account]
        assert mine == ["close", "cancel"]  # the stop stays until the position is gone


@pytest.mark.parametrize("fail_always", [False, True])
def test_flatten_keeps_going_and_reports_what_is_still_open(tmp_path, monkeypatch, capsys, fail_always):
    broker = FlattenBroker(fail_close=1, fail_always=fail_always)
    rc, out = flatten(tmp_path, monkeypatch, broker, capsys)
    assert "Could not close CON.MES" in out
    assert ("cancel", 222, 8) in broker.calls  # one failure did not stop the other account
    if fail_always:
        assert rc == 1 and "STILL OPEN: c1: 1 position(s) in CON.MES" in out and "STILL OPEN: c2" in out
    else:  # the second pass got it
        assert rc == 0 and "Flat." in out and broker.positions[111] == []


def test_flatten_says_which_account_it_could_not_check(tmp_path, monkeypatch, capsys):
    broker = FlattenBroker()

    def client(cfg):
        if cfg.account.login == "second":
            raise SystemExit(2)
        return FlattenClient(broker, None)

    path = write(tmp_path, FLATTEN_CONFIG)
    monkeypatch.setattr(cli, "load_secrets", lambda: type("S", (), {"login_for": lambda self, cfg: cfg.account.login})())
    monkeypatch.setattr(cli, "_client", client)
    monkeypatch.setattr(cli, "FLATTEN_SETTLE_SECONDS", 0)
    monkeypatch.setattr(cli.console, "width", 300)
    rc = cli.cmd_flatten(cli.build_parser().parse_args(["-c", str(path), "flatten", "--yes"]))
    out = capsys.readouterr().out
    assert rc == 1 and "NOT CHECKED: c2" in out and broker.positions[111] == [] and "Flat." not in out



# ---------------------------------------------------------------- config.yaml and .env as Windows leaves them

def test_a_section_written_twice_is_not_saved_into_the_first_copy():
    from topstep_bot.config_edit import set_value

    with pytest.raises(ValueError, match="^Not saved: risk: appears twice"):
        set_value("risk:\n  risk_per_trade: 100\nrisk:\n  risk_per_trade: 200\n", "risk", "risk_per_trade", 50)


def test_the_dashboard_opens_whatever_is_in_the_file(tmp_path, monkeypatch):
    from topstep_bot import setup_service

    cfg, problem = setup_service.load_for_server(str(write(tmp_path, 'session:\n  trade_start: "8"\n')))
    assert cfg.session.trade_start == BotConfig().session.trade_start and "must look like" in problem

    def boom(path):
        raise RuntimeError("boom")

    monkeypatch.setattr(setup_service, "load_config", boom)
    assert "boom" in setup_service.load_for_server(str(tmp_path / "config.yaml"))[1]


def test_a_utf16_config_says_how_to_fix_it_and_a_bom_env_still_reads(tmp_path, monkeypatch):
    from topstep_bot.config import load_env_file

    path = tmp_path / "config.yaml"
    path.write_bytes("mode: paper\n".encode("utf-16"))  # what PowerShell's > writes
    assert load_config(path).mode == "paper"
    path.write_bytes(b"mode: paper\n\xff\xfe\x00 bad")
    with pytest.raises(ConfigError, match="not saved as UTF-8"):
        load_config(path)
    monkeypatch.delenv("TOPSTEPX_USERNAME", raising=False)
    env = tmp_path / ".env"
    env.write_bytes("TOPSTEPX_USERNAME=me\n".encode("utf-8-sig"))
    load_env_file(env)
    import os
    assert os.environ["TOPSTEPX_USERNAME"] == "me"


def test_env_lines_with_export_and_comments(tmp_path, monkeypatch):
    import os

    from topstep_bot.config import load_env_file
    from topstep_bot.wizard import remove_env_keys, update_env_file

    env = tmp_path / ".env"
    env.write_text('export TOPSTEPX_API_KEY=a  # old\nTSB_QUOTED="a # b"\nTOPSTEPX_API_KEY=dup\n', encoding="utf-8")
    for key in ("TOPSTEPX_API_KEY", "TSB_QUOTED"):
        monkeypatch.delenv(key, raising=False)
    load_env_file(env)
    assert os.environ["TOPSTEPX_API_KEY"] == "a" and os.environ["TSB_QUOTED"] == "a # b"
    update_env_file(env, {"TOPSTEPX_API_KEY": "b"})
    lines = env.read_text(encoding="utf-8").splitlines()
    assert [ln for ln in lines if "API_KEY" in ln] == ["TOPSTEPX_API_KEY=b"]
    assert remove_env_keys(env, {"TSB_QUOTED"}) and "TSB_QUOTED" not in env.read_text(encoding="utf-8")


def test_atomic_write_survives_a_locked_moment_and_leaves_nothing_behind(tmp_path, monkeypatch):
    import os

    from topstep_bot.config import atomic_write_text

    path = tmp_path / "config.yaml"
    atomic_write_text(path, "mode: paper\n")
    assert path.read_text(encoding="utf-8") == "mode: paper\n" and not list(tmp_path.glob("*.part"))
    real, calls = os.replace, []

    def flaky(src, dst):
        calls.append(src)
        if len(calls) == 1:
            raise PermissionError(13, "Permission denied")
        return real(src, dst)

    monkeypatch.setattr(os, "replace", flaky)
    atomic_write_text(path, "mode: live\n")
    assert len(calls) == 2 and path.read_text(encoding="utf-8") == "mode: live\n" and not list(tmp_path.glob("*.part"))


def test_a_locked_file_is_a_plain_not_saved(tmp_path, monkeypatch):
    import os

    from topstep_bot.wizard import update_env_file

    def locked(src, dst):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(os, "replace", locked)
    monkeypatch.setattr("topstep_bot.config.clock.sleep", lambda s: None)
    with pytest.raises(ValueError, match=r"^Not saved: \.env is read-only"):
        update_env_file(tmp_path / ".env", {"TOPSTEPX_USERNAME": "me"})


def test_a_windows_file_keeps_its_lines_through_a_save(tmp_path):
    from topstep_bot.config import atomic_write_text, read_text_any
    from topstep_bot.training import save_strategy

    path = tmp_path / "config.yaml"
    path.write_bytes(b"# mine\r\nmode: paper\r\n\r\nrisk:\r\n  risk_per_trade: 120   # keep me\r\n")  # Notepad's line endings
    assert read_text_any(path) == "# mine\nmode: paper\n\nrisk:\n  risk_per_trade: 120   # keep me\n"
    save_strategy(path, "orb", {})
    text = read_text_any(path)
    assert "\r" not in text and "\n\n\n" not in text and "risk_per_trade: 120   # keep me" in text
    atomic_write_text(path, "a\nb\n")
    assert path.read_text(encoding="utf-8") == "a\nb\n"
