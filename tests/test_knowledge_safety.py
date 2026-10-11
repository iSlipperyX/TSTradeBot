"""Keeping the knowledge base safe (backups, ledger, recovery) and learning from the whole day (restart catch-up)."""

import json
import os
from datetime import date, datetime, timedelta, timezone

import topstep_bot.knowledge as knowledge
from topstep_bot.backtest.data import synthetic_bars
from topstep_bot.bars import resample
from topstep_bot.broker.paper import PaperBroker
from topstep_bot.config import BotConfig
from topstep_bot.factory import build_core
from topstep_bot.instruments import offline_contract
from topstep_bot.knowledge import BACKUP_DIR, KnowledgeBase, Observation, training_fingerprint
from topstep_bot.recommendations import RecommendationBook
from topstep_bot.sessions import SessionSchedule

from .conftest import CT, ct, run

UTC = timezone.utc
DAY = date(2026, 10, 8)


def ob(at="09:00", strategy="ema_trend", r=0.5, source="shadow", day=DAY, side="LONG"):
    return Observation(day.isoformat(), at, strategy, side, "open", "calm", r, r * 50, source)


def trained(kb, to=DAY, at=None):
    kb.replace_training([ob("09:00", "orb", 1.0, "train", day=to)], first_day=to - timedelta(days=30), last_day=to, days=30,
                        bars=1000, symbol="MNQ", timeframe=5, at=at or datetime(2026, 10, 8, 22, 0, tzinfo=UTC))


def ledger_lines(kb):
    return [json.loads(line) for line in kb.ledger_path.read_text(encoding="utf-8").splitlines()]


# ------------------------------------------------------------------ saving

def test_every_live_observation_goes_to_the_ledger_and_survives_trimming(tmp_path):
    kb = KnowledgeBase(tmp_path / "k.json", max_observations=3)
    trained(kb)
    for i in range(4):
        kb.record(ob(f"10:0{i}", day=DAY + timedelta(days=1)))
    kb.record(ob("11:00", source="real", r=-1.0))
    assert [o["source"] for o in ledger_lines(kb)] == ["shadow"] * 4 + ["real"]  # training is not ledgered
    assert len(kb.obs) == 3 and kb.counts()["real"] == 1  # the base trims old ideas, never real trades
    again = KnowledgeBase(tmp_path / "k.json", max_observations=3)
    assert len(again.obs) == 3 and again.recovery == []  # the trimmed ideas aren't forced back in


def test_a_damaged_file_is_set_aside_and_the_backup_plus_ledger_restore_everything(tmp_path):
    path = tmp_path / "k.json"
    kb = KnowledgeBase(path)
    trained(kb)
    kb.record(ob("10:00", source="real", r=2.0))
    backup = tmp_path / BACKUP_DIR / f"k-{datetime.now().date().isoformat()}.json"
    assert backup.exists()  # first save of the day copies the file before overwriting it
    backup.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")  # the backup holds the 10:00 trade
    kb.record(ob("11:00", source="manual", strategy="manual", r=-1.0))  # only in the file and the ledger
    kb.record(ob("12:00", day=DAY + timedelta(days=1)))  # an idea newer than the training

    path.write_text('{"version": 2, "observations": [{"day": "2026-10', encoding="utf-8")  # cut off mid-write
    back = KnowledgeBase(path)
    assert any(p.name.startswith("k.json.damaged-") for p in tmp_path.iterdir())
    assert back.trained and back.trained["to"] == DAY.isoformat()
    c = back.counts()
    assert c["real"] == 1 and c["manual"] == 1 and c["shadow"] == 1 and c["train"] == 1
    assert any("damaged" in n for n in back.recovery) and any("backup" in n for n in back.recovery)
    assert any("ledger" in n for n in back.recovery)
    assert json.loads(path.read_text(encoding="utf-8"))["observations"]  # repaired file written again


def test_damaged_file_without_backup_still_keeps_real_trades_from_the_ledger(tmp_path):
    path = tmp_path / "k.json"
    kb = KnowledgeBase(path, backups=0)
    kb.record(ob("10:00", source="real", r=1.5))
    path.write_bytes(b"\x00\x00\x00")
    back = KnowledgeBase(path, backups=0)
    assert back.counts()["real"] == 1 and "no good backup was found" in back.recovery


def test_a_line_cut_short_in_the_ledger_is_skipped(tmp_path):
    path = tmp_path / "k.json"
    kb = KnowledgeBase(path)
    kb.record(ob("10:00", source="real"))
    with open(kb.ledger_path, "a", encoding="utf-8") as f:
        f.write('{"day": "2026-10-08", "time": "10:')
    path.unlink()
    back = KnowledgeBase(path)
    assert back.counts()["real"] == 1


def test_backups_are_daily_and_only_the_newest_are_kept(tmp_path):
    kb = KnowledgeBase(tmp_path / "k.json", backups=2)
    kb.record(ob("10:00"))
    folder = tmp_path / BACKUP_DIR
    folder.mkdir()
    for d in ("2026-01-01", "2026-01-02", "2026-01-03"):
        (folder / f"k-{d}.json").write_text("{}", encoding="utf-8")
    kb._backed_up = ""
    kb.record(ob("10:05"))
    assert [p.name for p in kb.backup_files()] == ["k-2026-01-03.json", f"k-{datetime.now().date().isoformat()}.json"]


def test_saving_retries_when_windows_holds_the_file(tmp_path, monkeypatch):
    real_replace, calls = os.replace, []

    def flaky(src, dst):
        calls.append(dst)
        if len(calls) < 3:
            raise PermissionError("in use by another process")
        real_replace(src, dst)

    monkeypatch.setattr(knowledge.os, "replace", flaky)
    kb = KnowledgeBase(tmp_path / "k.json")
    kb.record(ob("10:00"))
    assert len(calls) == 3 and json.loads((tmp_path / "k.json").read_text(encoding="utf-8"))["observations"]


def test_training_saved_by_another_program_is_kept(tmp_path):
    """`topstep-bot train` while the bot runs: the bot's next save keeps the new training and its own ideas."""
    path = tmp_path / "k.json"
    bot = KnowledgeBase(path)
    trained(bot, at=datetime(2026, 10, 7, 22, 0, tzinfo=UTC))
    bot.record(ob("10:00", day=DAY + timedelta(days=1)))
    cli = KnowledgeBase(path)
    cli.replace_training([ob("09:00", "noise_breakout", 2.0, "train"), ob("09:05", "orb", -1.0, "train")],
                         first_day=DAY - timedelta(days=60), last_day=DAY, days=60, bars=9000, symbol="MNQ", timeframe=5,
                         at=datetime(2026, 10, 8, 23, 0, tzinfo=UTC))
    bot.record(ob("10:30", source="real", r=1.0, day=DAY + timedelta(days=1)))
    saved = KnowledgeBase(path)
    assert saved.trained["days"] == 60 and saved.counts() == {"train": 2, "shadow": 1, "real": 1, "manual": 0}
    assert bot.trained["days"] == 60  # the running bot uses it from now on


# ------------------------------------------------------------------ never counted twice

def test_an_idea_seen_again_is_not_counted_twice(tmp_path):
    kb = KnowledgeBase(tmp_path / "k.json")
    trained(kb, to=DAY - timedelta(days=1))
    kb.replace_training([ob("09:00", "orb", 1.0, "train")], first_day=DAY - timedelta(days=30), last_day=DAY, days=30,
                        bars=1, symbol="MNQ", timeframe=5)
    assert kb.record(ob("09:00", "orb", 1.0)) is False  # training already has this signal
    assert kb.record(ob("09:30")) is True and kb.record(ob("09:30")) is False
    assert kb.record(ob("09:30", side="SHORT")) is True  # another signal
    assert kb.record(ob("09:30", source="real", r=1.0)) is True  # real trades are always recorded
    assert len(ledger_lines(kb)) == 3


# ------------------------------------------------------------------ when to retrain

def test_training_is_refreshed_after_each_session_and_after_code_changes(tmp_path, monkeypatch):
    sched = SessionSchedule(BotConfig().session)
    kb = KnowledgeBase(tmp_path / "k.json")
    assert kb.training_due_reason(datetime.now(UTC), 20) == "it has not been trained yet"
    at = ct(2026, 10, 8, 16, 10)  # trained after Thursday's close
    trained(kb, at=at)
    assert kb.trained["code"] == training_fingerprint()
    mid_friday = ct(2026, 10, 9, 11, 0)
    assert kb.training_due_reason(mid_friday, 20, session_end=sched.last_session_end(mid_friday)) is None
    after_friday = ct(2026, 10, 9, 16, 5)
    assert "session finished" in kb.training_due_reason(after_friday, 20, session_end=sched.last_session_end(after_friday))
    assert "hours old" in kb.training_due_reason(ct(2026, 10, 9, 13, 0), 20)
    monkeypatch.setattr(knowledge, "training_fingerprint", lambda: "different")
    assert "code changed" in kb.training_due_reason(mid_friday, 20)


def test_last_session_end_skips_weekends():
    sched = SessionSchedule(BotConfig().session)
    assert sched.last_session_end(ct(2026, 10, 9, 15, 59)) == datetime(2026, 10, 8, 16, 0, tzinfo=CT)
    assert sched.last_session_end(ct(2026, 10, 9, 16, 0)) == datetime(2026, 10, 9, 16, 0, tzinfo=CT)
    assert sched.last_session_end(ct(2026, 10, 11, 18, 0)) == datetime(2026, 10, 9, 16, 0, tzinfo=CT)  # Sunday evening


# ------------------------------------------------------------------ restart catch-up

def restarted_core(tmp_path, now):
    cfg = BotConfig.model_validate({"data_dir": str(tmp_path)})
    mnq = offline_contract("MNQ")
    core = build_core(cfg, mnq, PaperBroker(mnq, 50_000), clock=lambda: now, account_label="T")
    core.recommender = RecommendationBook(core)
    core.attach_knowledge(KnowledgeBase.from_config(cfg, cfg.knowledge_path))
    return core


def test_a_restart_replays_today_and_learns_each_idea_once(tmp_path):
    now = ct(2026, 10, 8, 13, 0)
    bars = [b for b in resample(synthetic_bars("MNQ", days=12, seed=4, end=now.date()), 5) if b.ts + timedelta(minutes=5) <= now]

    def start():
        core = restarted_core(tmp_path, now)
        today = core.schedule.trading_day(now)
        for b in bars:
            if core.schedule.trading_day(b.ts) < today:
                core.warmup_bar(b)
        run(core.begin_day(today, 50_000))
        for b in bars:
            if core.schedule.trading_day(b.ts) == today:
                core.catch_up_bar(b)
        return core

    first = start()
    ideas = list(first.recommender.items)
    learned = [o for o in first.knowledge.obs if o.source == "shadow"]
    assert ideas and learned and len(learned) == sum(1 for r in ideas if not r.is_open and r.outcome_r is not None)
    assert all(o.day == DAY.isoformat() for o in learned)
    assert first.orders.is_flat and first.last_price == bars[-1].close  # replaying never trades

    second = start()  # the same morning replayed again by another restart
    assert len(second.recommender.items) == len(ideas)
    assert len([o for o in second.knowledge.obs if o.source == "shadow"]) == len(learned)  # nothing counted twice
    assert any(r.is_open for r in second.recommender.items) or all(not r.is_open for r in ideas)
