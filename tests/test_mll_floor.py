"""account.mll_floor_override is a one-time sync: the bot applies a new number once, then follows its own
trailing floor. A number left in config.yaml must never quietly give the bot more room on a later start."""

import asyncio
from datetime import date

import websockets

from topstep_bot.config import BotConfig
from topstep_bot.factory import build_core
from topstep_bot.journal import Journal
from topstep_bot.live import mll_floor_for

from .conftest import run
from .test_live_runner import FakeTopstepX, make_runner

LABEL = "50KTC-1"
DAY = date(2026, 10, 14)


def journal(tmp_path) -> Journal:
    return Journal(tmp_path / "journal.db")


def test_an_override_already_in_the_file_does_not_replace_a_learned_floor(tmp_path):
    j = journal(tmp_path)
    j.set_state(f"mll_floor:{LABEL}", 49_000)
    floor, note = mll_floor_for(j, LABEL, 48_500, DAY)
    assert floor == 49_000 and "already in config.yaml" in note and "change the number" in note
    assert j.get_state(f"mll_floor_override_applied:{LABEL}") == 48_500  # remembered as seen
    assert j.get_state(f"mll_floor:{LABEL}") == 49_000
    j.close()


def test_an_override_with_no_learned_floor_is_applied_once(tmp_path):
    j = journal(tmp_path)
    floor, note = mll_floor_for(j, LABEL, 48_500, DAY)
    assert floor == 48_500 and "applied once" in note
    assert j.get_state(f"mll_floor:{LABEL}") == 48_500
    assert j.get_state(f"mll_floor_override_applied_at:{LABEL}") == DAY.isoformat()
    # The floor then trails up; the same number in config.yaml no longer pulls it back down.
    j.set_state(f"mll_floor:{LABEL}", 49_000)
    floor, note = mll_floor_for(j, LABEL, 48_500, DAY)
    assert floor == 49_000 and DAY.isoformat() in note
    j.close()


def test_a_new_number_is_applied_once_and_a_lower_one_says_so(tmp_path):
    j = journal(tmp_path)
    mll_floor_for(j, LABEL, 48_500, DAY)
    j.set_state(f"mll_floor:{LABEL}", 49_000)
    floor, note = mll_floor_for(j, LABEL, 49_300, DAY)  # the owner typed a new number
    assert floor == 49_300 and "LOWERED" not in note
    assert mll_floor_for(j, LABEL, 49_300, DAY)[0] == 49_300
    floor, note = mll_floor_for(j, LABEL, 48_000, DAY)  # e.g. after a Topstep reset
    assert floor == 48_000 and "LOWERED" in note and "$49,300.00" in note
    assert mll_floor_for(j, LABEL, 48_000, DAY)[0] == 48_000
    j.close()


def test_no_override_and_nothing_learned_lets_the_tracker_work_it_out(tmp_path):
    j = journal(tmp_path)
    assert mll_floor_for(j, LABEL, None, DAY)[0] is None
    j.close()


def test_build_core_prefers_the_journal_floor_over_the_override(mnq):
    from topstep_bot.broker.paper import PaperBroker

    from .conftest import ct

    cfg = BotConfig.model_validate({"account": {"mll_floor_override": 48_500}})
    core = build_core(cfg, mnq, PaperBroker(mnq, 50_000), clock=lambda: ct(2026, 3, 3, 9, 0), account_label="T",
                      mll_floor=49_000)
    assert core.tracker.floor == 49_000


def test_a_second_start_never_loosens_the_floor(tmp_path):
    async def start_once() -> float:
        fake = FakeTopstepX()
        async with websockets.serve(fake.hub, "127.0.0.1", 0) as srv:
            runner = make_runner(fake, srv.sockets[0].getsockname()[1], "paper", tmp_path,
                                 account={"mll_floor_override": 48_500})
            ready = asyncio.Event()

            async def on_ready(core):
                ready.set()

            task = asyncio.create_task(runner.run(on_ready))
            await asyncio.wait_for(ready.wait(), 30)
            floor = runner.tracker.floor
            if floor == 48_500:  # the floor trails up while it runs (as the engine saves it at the end of a day)
                runner.journal.set_state("mll_floor:PAPER-50KTC-1", 49_000)
            runner.controls.stop.set()
            await asyncio.wait_for(task, 20)
            return floor

    assert run(start_once()) == 48_500  # the first start syncs it
    assert run(start_once()) == 49_000  # the second keeps the trailed floor
