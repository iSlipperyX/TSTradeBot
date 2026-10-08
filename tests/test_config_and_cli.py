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
