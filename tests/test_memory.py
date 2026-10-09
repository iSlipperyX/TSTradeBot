"""The long-run memory (memory.py): the market library and replaying all of it through every strategy."""

from datetime import datetime, timedelta, timezone

from topstep_bot.backtest.data import save_csv, synthetic_bars
from topstep_bot.bars import resample
from topstep_bot.briefing import brief_text, build_brief
from topstep_bot.broker.paper import PaperBroker
from topstep_bot.config import BotConfig
from topstep_bot.factory import build_core
from topstep_bot.instruments import offline_contract
from topstep_bot.memory import LongRunMemory, MarketLibrary, import_csv

from .conftest import run

UTC = timezone.utc
T0 = datetime(2026, 3, 2, 14, 30, tzinfo=UTC)


def bars5(days=40, seed=5, end=None):
    return list(resample(synthetic_bars("MNQ", days=days, seed=seed, end=end), 5))


def test_library_stores_bars_and_remembers_what_was_downloaded(tmp_path):
    lib = MarketLibrary(tmp_path / "lib.sqlite")
    bars = bars5(days=5)
    assert lib.add("MNQ", 5, bars, "CON.A") == len(bars)
    assert lib.add("MNQ", 5, bars[-10:], "CON.B") == 0  # a newer copy of the same bars replaces them: nothing new
    assert lib.add("MES", 5, bars[:3]) == 3  # other symbols and timeframes are kept apart
    back = lib.bars("MNQ", 5)
    assert len(back) == len(bars) and back[0].ts == bars[0].ts and back[-1].close == bars[-1].close
    assert len(lib.bars("MNQ", 5, bars[10].ts, bars[20].ts)) == 10

    st = lib.stats("MNQ", 5)
    assert st["bars"] == len(bars) and st["first"] == bars[0].ts.date().isoformat() and st["days"] >= 5
    assert lib.stats("MNQ", 5) is st  # cached until the next write

    day = timedelta(days=1)
    assert lib.missing("MNQ", 5, T0, T0 + 10 * day) == [(T0, T0 + 10 * day)]
    lib.note_span("MNQ", 5, T0 + 2 * day, T0 + 4 * day)
    lib.note_span("MNQ", 5, T0 + 3 * day, T0 + 6 * day)  # overlaps: merged into one span
    lib.note_span("MNQ", 5, T0 + 8 * day, T0 + 9 * day)
    assert lib.missing("MNQ", 5, T0, T0 + 10 * day) == [(T0, T0 + 2 * day), (T0 + 6 * day, T0 + 8 * day), (T0 + 9 * day, T0 + 10 * day)]
    assert lib.missing("MNQ", 5, T0 + 3 * day, T0 + 5 * day) == []
    assert lib.missing("MNQ", 5, T0 + 9 * day, T0 + 9 * day + timedelta(minutes=5), min_gap=timedelta(minutes=10)) == []
    lib.close()

    lib = MarketLibrary(tmp_path / "lib.sqlite")  # it's a file: everything is still there
    assert lib.stats("MNQ", 5)["bars"] == len(bars) and len(lib.missing("MNQ", 5, T0, T0 + 10 * day)) == 3
    lib.close()


def test_import_csv_resamples_into_the_library(tmp_path):
    cfg = BotConfig.model_validate({"data_dir": str(tmp_path)})
    path = tmp_path / "mnq_1m.csv"
    one_minute = synthetic_bars("MNQ", days=3, seed=2)
    save_csv(one_minute, path)
    read, added = import_csv(cfg, path)
    assert read == len(one_minute) and 0 < added <= len(one_minute) // 5 + 3
    assert import_csv(cfg, path) == (read, 0)  # importing again adds nothing


class FakeClient:
    """retrieve_bars_range over a fixed series, recording what was asked for."""

    def __init__(self, bars):
        self.bars = bars
        self.asked: list[tuple[datetime, datetime]] = []

    async def retrieve_bars_range(self, contract_id, start, end, unit, unit_number, live=False):
        self.asked.append((start, end))
        return [b for b in self.bars if start <= b.ts <= end]


def test_learn_backfills_once_and_replays_everything(tmp_path):
    cfg = BotConfig.model_validate({"data_dir": str(tmp_path), "knowledge": {"deep_history_days": 60}})
    contract = offline_contract("MNQ")
    series = bars5(days=35)
    now = series[-1].ts + timedelta(minutes=5)
    client = FakeClient(series)
    memory = LongRunMemory.open(cfg)
    result = run(memory.learn(cfg, contract, client, now))
    assert result.downloaded == len(series) and result.bars == len(series) and result.observations > 0
    assert result.days >= 20 and "Long-run memory updated" in result.text()
    assert memory.status()["observations"] == result.observations and memory.status()["running"] is False
    assert cfg.longrun_knowledge_path.exists() and cfg.library_path.exists()
    sources = {o.source for o in memory.knowledge.obs}
    assert sources == {"train"} and {o.strategy for o in memory.knowledge.obs} - {"adaptive"}

    client.asked.clear()
    again = run(memory.learn(cfg, contract, client, now))
    assert again.downloaded == 0 and client.asked == []  # nothing missing: TopstepX isn't asked again
    assert again.observations == result.observations  # the same bars give the same outcomes (replaced, not added)
    memory.close()

    reopened = LongRunMemory.open(cfg)  # all of it survives a restart
    assert len(reopened.knowledge.obs) == result.observations and reopened.status()["library"]["bars"] == len(series)
    assert not reopened.due(now + timedelta(hours=1), 20) and reopened.due(now + timedelta(hours=21), 20)
    reopened.close()


def test_learn_offline_replays_the_library_and_reports_an_empty_one(tmp_path):
    cfg = BotConfig.model_validate({"data_dir": str(tmp_path)})
    memory = LongRunMemory.open(cfg)
    try:
        run(memory.learn(cfg, offline_contract("MNQ"), None, datetime.now(UTC)))
    except ValueError as exc:
        assert "empty" in str(exc)
    else:
        raise AssertionError("an empty library has nothing to learn from")
    assert memory.status()["error"] and not memory.running
    memory.close()


def test_core_reports_on_the_long_run_memory(tmp_path):
    cfg = BotConfig.model_validate({"data_dir": str(tmp_path), "knowledge": {"deep_history_days": 60}})
    contract = offline_contract("MNQ")
    series = bars5(days=35)
    now = series[-1].ts + timedelta(minutes=5)
    memory = LongRunMemory.open(cfg)
    run(memory.learn(cfg, contract, FakeClient(series), now))
    core = build_core(cfg, contract, PaperBroker(contract, 50_000), clock=lambda: now, account_label="T")
    assert core.insights(longrun=True) is None  # no memory attached yet
    core.memory = memory
    rep = core.insights(longrun=True)
    assert rep["coverage"]["total"] == len(memory.knowledge.obs)
    assert core.insights(longrun=True) is rep  # cached until the memory changes
    assert core.snapshot()["memory"]["library"]["bars"] == len(series)
    text = brief_text(build_brief(core))
    assert f"My long-run memory holds {len(series):,} price bars" in text
    memory.close()


def test_cli_learn_imports_a_csv_and_reports_the_long_run(tmp_path, monkeypatch, capsys, restore_logging):
    from topstep_bot import cli

    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").write_text("mode: paper\nknowledge:\n  deep_history_days: 3650\n", encoding="utf-8")
    save_csv(synthetic_bars("MNQ", days=30, seed=4), tmp_path / "old.csv")
    assert cli.main(["-c", "config.yaml", "learn", "--offline", "--import", "old.csv"]) == 0
    out = capsys.readouterr().out
    assert "Imported old.csv" in out and "Long-run memory updated" in out and "Over the long run" in out
    assert cli.main(["-c", "config.yaml", "insights", "--longrun"]) == 0
    assert "What the bot learned over the long run" in capsys.readouterr().out
    assert cli.main(["-c", "config.yaml", "insights"]) == 1  # the recent knowledge base was never trained here
    assert "learn" in [name for name, _ in cli.MENU]
