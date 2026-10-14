"""The Max Loss Limit override in config.yaml is a one-time sync, never a loosening on every restart."""

import httpx

from topstep_bot.api.rest import ProjectXClient
from topstep_bot.broker.paper import PaperBroker
from topstep_bot.config import BotConfig, Secrets
from topstep_bot.factory import build_core
from topstep_bot.journal import Journal
from topstep_bot.live import Controls, LiveRunner
from topstep_bot.preflight import run_preflight

from .conftest import ct, run
from .test_live_runner import FakeTopstepX

LABEL = "50KTC-1"


def _journal(tmp_path) -> Journal:
    return Journal(tmp_path / "journal.db")


# ------------------------------------------------------------------ the pure rule


def test_an_override_never_applied_is_used_once_and_recorded(tmp_path):
    from topstep_bot.live import mll_floor_for

    j = _journal(tmp_path)
    j.set_state(f"mll_floor:{LABEL}", 49_000)
    floor, note = mll_floor_for(j, LABEL, 48_500)
    assert floor == 48_500
    assert "48,500" in note and "one-time sync" in note
    assert j.get_state(f"mll_floor:{LABEL}") == 48_500
    assert j.get_state(f"mll_floor_override_applied:{LABEL}") == 48_500
    j.close()


def test_an_override_already_applied_does_not_replace_the_trailed_floor(tmp_path):
    from topstep_bot.live import mll_floor_for

    j = _journal(tmp_path)
    mll_floor_for(j, LABEL, 48_500)  # the first start applies it
    j.set_state(f"mll_floor:{LABEL}", 49_000)  # the floor trailed up at the end of a good day
    floor, note = mll_floor_for(j, LABEL, 48_500)  # the owner never removed the line from config.yaml
    assert floor == 49_000
    assert "journal" in note and "48,500" in note and "was applied" in note  # says why the override was not used
    assert j.get_state(f"mll_floor:{LABEL}") == 49_000
    j.close()


def test_a_changed_override_is_applied_once_more(tmp_path):
    from topstep_bot.live import mll_floor_for

    j = _journal(tmp_path)
    mll_floor_for(j, LABEL, 48_500)
    j.set_state(f"mll_floor:{LABEL}", 49_000)
    floor, note = mll_floor_for(j, LABEL, 49_300)  # the owner typed a new number to re-sync
    assert floor == 49_300 and "49,300" in note and "one-time sync" in note
    j.set_state(f"mll_floor:{LABEL}", 49_500)
    floor, _ = mll_floor_for(j, LABEL, 49_300)  # and then it is spent again
    assert floor == 49_500
    j.close()


def test_without_an_override_the_saved_floor_is_used_or_none(tmp_path):
    from topstep_bot.live import mll_floor_for

    j = _journal(tmp_path)
    assert mll_floor_for(j, LABEL, None)[0] is None  # nothing known: the tracker starts fresh
    assert j.get_state(f"mll_floor:{LABEL}") is None
    j.set_state(f"mll_floor:{LABEL}", 49_000)
    floor, note = mll_floor_for(j, LABEL, None)
    assert floor == 49_000 and "journal" in note
    j.close()


def test_an_override_of_zero_is_a_real_value(tmp_path):
    from topstep_bot.live import mll_floor_for

    j = _journal(tmp_path)  # an Express Funded Account after its first payout has an MLL of $0
    floor, _ = mll_floor_for(j, LABEL, 0)
    assert floor == 0
    j.set_state(f"mll_floor:{LABEL}", 100)
    assert mll_floor_for(j, LABEL, 0)[0] == 100
    j.close()


def test_an_override_with_nothing_saved_is_applied_and_recorded(tmp_path):
    from topstep_bot.live import mll_floor_for

    j = _journal(tmp_path)
    floor, _ = mll_floor_for(j, LABEL, 48_500)
    assert floor == 48_500 and j.get_state(f"mll_floor:{LABEL}") == 48_500
    j.close()


# ------------------------------------------------------------------ the three places that use it


def test_build_core_prefers_the_journal_floor_over_the_override(mnq):
    cfg = BotConfig.model_validate({"account": {"mll_floor_override": 48_500}})
    now = ct(2026, 3, 3, 9, 0)
    with_journal = build_core(cfg, mnq, PaperBroker(mnq, 50_000), clock=lambda: now, account_label="t", mll_floor=49_000)
    assert with_journal.tracker.floor == 49_000
    without = build_core(cfg, mnq, PaperBroker(mnq, 50_000), clock=lambda: now, account_label="t")
    assert without.tracker.floor == 48_500  # no journal value to prefer: the override still counts


def _preflight(tmp_path, override, *, saved=None, applied=None):
    fake = FakeTopstepX()
    original = fake.handler

    def handler(request):
        if request.url.path == "/api/Account/search":
            return httpx.Response(200, json={"success": True, "accounts": [
                {"id": 7, "name": LABEL, "balance": 50_900.0, "canTrade": True}]})
        return original(request)

    cfg = BotConfig.model_validate({"data_dir": str(tmp_path), "news": {"enabled": False},
                                    "account": {"mll_floor_override": override}})
    j = Journal(cfg.journal_path_for("live"))
    if saved is not None:
        j.set_state(f"mll_floor:{LABEL}", saved)
    if applied is not None:
        j.set_state(f"mll_floor_override_applied:{LABEL}", applied)
    j.close()
    client = ProjectXClient("u", "k", transport=httpx.MockTransport(handler))
    report = run(run_preflight(cfg, Secrets(username="u", api_key="k"), client=client, run_backtests=False))
    run(client.close())
    return next(c for c in report.checks if c.name == "Max Loss Limit")


def test_preflight_reports_the_journal_floor_when_the_override_was_already_applied(tmp_path):
    check = _preflight(tmp_path, 48_500, saved=49_000, applied=48_500)
    assert "49,000" in check.detail and "journal" in check.detail
    assert "48,500" in check.detail  # it says the override exists and was used earlier, so the owner knows why


def test_preflight_applies_a_new_override_once(tmp_path):
    check = _preflight(tmp_path, 48_500, saved=49_000)
    assert "48,500" in check.detail and "one-time sync" in check.detail
    j = Journal(tmp_path / "journal_live.db")
    assert j.get_state(f"mll_floor:{LABEL}") == 48_500
    j.close()


def _runner(tmp_path, override):
    cfg = BotConfig.model_validate({"data_dir": str(tmp_path), "log_dir": str(tmp_path), "news": {"enabled": False},
                                    "dashboard": {"enabled": False}, "account": {"mll_floor_override": override},
                                    "knowledge": {"deep_learning": False, "auto_train": False}})
    runner = LiveRunner(cfg, Secrets(username="u", api_key="k"), Controls())
    runner.client = ProjectXClient("u", "k", transport=httpx.MockTransport(FakeTopstepX().handler))
    return runner


def test_prepare_does_not_loosen_the_floor_on_a_restart(tmp_path):
    key = "mll_floor:PAPER-50KTC-1"
    first = _runner(tmp_path, 48_500)
    first.journal.set_state(key, 49_000)  # the floor trailed up to 49,000 after the override was spent
    first.journal.set_state("mll_floor_override_applied:PAPER-50KTC-1", 48_500)
    run(first.prepare())
    assert first.tracker.floor == 49_000  # the line is still in config.yaml, but it was used already
    assert first.journal.get_state(key) == 49_000
    run(first.client.close())
    first.journal.close()


def test_prepare_applies_the_override_once_and_remembers_it(tmp_path):
    key = "mll_floor:PAPER-50KTC-1"
    first = _runner(tmp_path, 48_500)
    first.journal.set_state(key, 49_000)
    run(first.prepare())
    assert first.tracker.floor == 48_500  # first start: the owner's number, once
    assert first.journal.get_state(key) == 48_500
    first.journal.set_state(key, 49_000)  # the floor trails up while the bot runs
    run(first.client.close())
    first.journal.close()

    second = _runner(tmp_path, 48_500)  # a restart with the same line still in config.yaml
    run(second.prepare())
    assert second.tracker.floor == 49_000
    run(second.client.close())
    second.journal.close()
