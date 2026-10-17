"""Version 4.0: the trade chart, the odds on every trade and setup, the knowledge network growing by itself, Ask
explaining anything in the bot, and several markets at once from the dashboard, Telegram and the Setup tab."""

import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from topstep_bot import knowledge_network as kn
from topstep_bot.backtest.data import synthetic_bars
from topstep_bot.bars import resample
from topstep_bot.brain import guide, intents
from topstep_bot.brain.facts import TOOLS, Facts
from topstep_bot.config import MAX_MARKETS, BotConfig, Secrets, TelegramConfig, load_config
from topstep_bot.control import BotActions
from topstep_bot.live import Controls, LiveRunner
from topstep_bot.memory import MarketLibrary
from topstep_bot.models import Signal
from topstep_bot.probability import network_paths

from .conftest import bar, ct, run
from .test_dashboard_setup import FORM, setup_scenario
from .test_dashboard_setup import clean_env as clean_env  # noqa: PLC0414 - the fixture, by its name
from .test_engine_and_dashboard import T0, make_core
from .test_probability import kb_core, outcomes, save_kb, signal

ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = ROOT / "src" / "topstep_bot" / "dashboard" / "index.html"


# ------------------------------------------------------------------------------ the trade chart

def test_the_chart_has_the_candles_the_bar_forming_now_and_the_trade(mnq):
    async def go():
        core, broker, now = make_core(mnq, risk={"risk_per_trade": 100, "slippage_ticks": 0})
        await core.begin_day(core.schedule.trading_day(T0), 50_000)
        for i in range(3):
            core.observe_bar(bar(T0 + timedelta(minutes=5 * i), 100 + i, 101 + i, 99 + i, 100.5 + i))
        chart = core.chart()
        assert chart["tf"] == 5 and chart["tz"] == "America/Chicago" and chart["decimals"] == 2
        assert [c[0] for c in chart["bars"]] == [int((T0 + timedelta(minutes=5 * i)).timestamp()) for i in range(3)]
        assert chart["bars"][0][1:] == [100, 101, 99, 100.5] and chart["forming"] is None and chart["trade"] is None

        # live prices build the bar that is still open, so the chart moves between closes
        start = T0 + timedelta(minutes=15)
        for second, price in ((5, 103.0), (40, 104.5), (90, 102.25), (200, 103.5)):
            await core.on_price(start + timedelta(seconds=second), price)
        assert core.chart()["forming"] == [int(start.timestamp()), 103.0, 104.5, 102.25, 103.5]
        await core.on_price(T0 + timedelta(minutes=14), 103.0)  # a late tick of a closed bar changes nothing
        assert core.chart()["forming"][0] == int(start.timestamp())
        core.observe_bar(bar(start, 103, 104.5, 102.25, 103.5))
        assert core.chart()["forming"] is None  # the closed bar replaced it

        # the open trade's levels, then its fill markers once closed
        core.last_price = 103.5
        b = bar(start, 103, 104.5, 102.25, 103.5)
        await core._handle_entry(Signal("long", stop_price=99.0, target_price=112.0, reason="test"), b, core.context(b))
        await broker.on_bar(bar(start + timedelta(minutes=5), 103.5, 104, 103, 103.5))
        t = core.chart()["trade"]
        assert t["side"] == "LONG" and t["stop"] == 99.0 and t["target"] == 112.0 and t["initial_stop"] == 99.0
        assert t["entry"] is not None and t["opened"] is not None and t["size"] >= 1
        assert core.snapshot()["chart"]["trade"]["entry"] == t["entry"]
        now[0] = start + timedelta(minutes=10)
        await core.manual.close("test")
        await broker.on_bar(bar(start + timedelta(minutes=10), 103.5, 104, 103, 103.75))
        fills = core.chart()["fills"]
        assert len(fills) == 1 and fills[0]["side"] == "LONG" and fills[0]["exit"] is not None
        assert core.chart()["trade"] is None

    run(go())


def test_the_chart_keeps_ten_hours_of_bars(mnq):
    from topstep_bot.engine import CHART_BARS

    core, _, _ = make_core(mnq)
    for i in range(CHART_BARS + 30):
        core.observe_bar(bar(T0 + timedelta(minutes=5 * i), 100, 101, 99, 100))
    bars = core.chart()["bars"]
    assert len(bars) == CHART_BARS == 120 and bars[-1][0] == int((T0 + timedelta(minutes=5 * (CHART_BARS + 29))).timestamp())


# ------------------------------------------------------------------------------ odds on every trade and setup

def test_a_trade_keeps_the_odds_it_was_taken_with(mnq):
    core = kb_core(mnq, outcomes(120, 80))
    b = bar(T0, 100, 100, 100, 100)
    run(core._handle_entry(signal(), b, core.context(b)))
    odds = core.snapshot()["trade"]["odds"]
    assert odds["n"] == 200 and 0.55 < odds["p_win"] < 0.65 and odds["ev_r"] > 0 and odds["p_lo"] < odds["p_win"] < odds["p_hi"]


def test_setups_and_the_trade_ticket_show_the_calculator(mnq):
    core = kb_core(mnq, outcomes(120, 80, slot="open", regime="calm") + outcomes(10, 40, strategy="ema_trend", win_r=1.0))
    odds = core.setups._odds("orb", "LONG")
    assert odds["n"] == 200 and odds["p_win"] > 0.5 and odds["p_lo"] < odds["p_win"] < odds["p_hi"] and odds["level"] == "cell"
    assert all("odds" in item for item in core.setups.view()["items"])
    rows = core.manual.ticket("long")["knowledge"]["odds"]
    assert rows and rows[0]["name"] == "orb" and rows[0]["ev_r"] > 0 and rows[0]["title"] == "Opening Range Breakout"
    assert [r["ev_r"] for r in rows] == sorted((r["ev_r"] for r in rows), reverse=True)
    assert all(r["level"] != "none" for r in rows)  # a strategy with no evidence at all isn't listed
    assert len(rows) <= 4


def test_ideas_carry_their_odds(mnq):
    from topstep_bot.recommendations import RecommendationBook

    core = kb_core(mnq, outcomes(120, 80))
    book = RecommendationBook(core, ["orb"])
    orb = next(s for s in book.shadows if s.name == "orb")
    book._add_idea(orb, signal(), bar(T0, 100, 100, 100, 100), T0 + timedelta(minutes=5))
    d = book.to_dict(next(r for r in book.items if r.strategy == "orb"))
    assert 0.5 < d["p_win"] < 0.7 and d["ev_r"] > 0


# ------------------------------------------------------------------------------ the knowledge network grows

def library_with(cfg: BotConfig, *symbols: str, days: int = 30) -> datetime:
    lib = MarketLibrary(cfg.library_path)
    last = None
    try:
        for i, s in enumerate(symbols):
            bars = list(resample(synthetic_bars(s, days=days, seed=5 + i), 5))
            lib.add(s, 5, bars)
            last = bars[-1].ts
    finally:
        lib.close()
    return last + timedelta(hours=2)


def test_the_network_learns_related_markets_from_saved_history(tmp_path):
    cfg = BotConfig.model_validate({"data_dir": str(tmp_path), "instrument": {"symbol": "MNQ"}})
    now = library_with(cfg, "MES", "GC")
    st = kn.status(cfg, "MNQ", traded=["MNQ"])
    assert st["learnable"] == ["MES"] and st["markets"] == [] and st["asset_class"] == "Equity index"  # gold isn't related
    assert "Saved history the bot can still learn from: MES" in kn.status_text(st)
    learned = run(kn.grow(cfg, ["MNQ"], now, limit=1))
    assert [x["symbol"] for x in learned] == ["MES"] and learned[0]["observations"] > 0 and learned[0]["days"] > 0
    st = kn.status(cfg, "MNQ", traded=["MNQ"])
    mes = st["markets"][0]
    assert mes["symbol"] == "MES" and mes["grown"] and mes["n"] == learned[0]["observations"] and not st["learnable"]
    assert "learned from saved history" in kn.status_text(st)
    assert run(kn.grow(cfg, ["MNQ"], now)) == []  # fresh knowledge isn't relearned...
    assert kn.candidates(cfg, ["MNQ"], now + kn.STALE + timedelta(hours=1), {"MES": {"days": 30}}) == ["MES"]  # ...until stale
    assert tmp_path / "knowledge_MES_5m.json" in network_paths(cfg, "MNQ")  # the calculator reads it
    assert kn.candidates(cfg, ["MNQ", "MES"], now, {"MES": {"days": 30}}) == []  # a traded market learns by trading


def test_too_little_history_or_switched_off_learns_nothing(tmp_path):
    cfg = BotConfig.model_validate({"data_dir": str(tmp_path), "probability": {"grow_per_day": 0}})
    assert run(kn.grow(cfg, ["MNQ"], datetime.now(ct(2026, 1, 1, 0, 0).tzinfo))) == []  # no library at all
    now = library_with(cfg, "MES", days=12)
    assert run(kn.grow(cfg, ["MNQ"], now)) == []  # turned off
    assert run(kn.grow(cfg, ["MNQ"], now, limit=3)) == []  # 12 days: too few to learn from
    off = BotConfig.model_validate({"data_dir": str(tmp_path), "probability": {"network": False}})
    assert "turned off" in kn.status_text(kn.status(off, "MNQ"))


def lane(flat=True, training=False, window=False):
    core = SimpleNamespace(orders=SimpleNamespace(is_flat=flat),
                           schedule=SimpleNamespace(entry_block_reason=lambda now: None if window else "outside the window"))
    return SimpleNamespace(core=core, _training=training)


def test_the_bot_grows_the_network_only_when_flat_and_outside_trading(tmp_path):
    runner = LiveRunner(BotConfig.model_validate({"data_dir": str(tmp_path)}), Secrets(username="u", api_key="k"), Controls())
    calls = []

    async def grow(source, limit=None):
        calls.append(source)
        return "learned"

    runner.grow_network = grow
    now = ct(2026, 10, 13, 17, 30)
    for lanes in ({"MNQ": lane(window=True)}, {"MNQ": lane(), "MES": lane(flat=False)}, {"MNQ": lane(training=True)}, {}):
        runner.lanes = lanes
        run(runner._grow_step(now))
    assert calls == []  # trading never waits on it
    runner.lanes = {"MNQ": lane(), "MES": lane()}
    run(runner._grow_step(now))
    run(runner._grow_step(now + timedelta(hours=3)))  # once a day
    assert calls == ["daily"]
    run(runner._grow_step(now + kn.STALE + timedelta(minutes=1)))
    assert calls == ["daily", "daily"]
    runner.cfg.probability.grow_network = False
    run(runner._grow_step(now + 3 * kn.STALE))
    assert len(calls) == 2


# ------------------------------------------------------------------------------ Ask explains anything

def test_the_guide_finds_sections_and_settings():
    found = guide.search("what is the consistency target")
    assert found["sections"] and any("onsistency" in s["title"] or "onsistency" in s["text"] for s in found["sections"])
    assert found["text"].startswith("From the ")
    cfg = BotConfig.model_validate({"aggressive": {"max_conviction": 1.8}})
    found = guide.search("what does max_conviction do", cfg)
    row = next(r for r in found["settings"] if r["key"] == "aggressive.max_conviction")
    assert row["value"] == 1.8 and "aggressive.max_conviction (yours: 1.8)" in found["text"]
    assert guide.search("")["sections"] == [] and "Name what" in guide.search("")["text"]
    assert "couldn't find" in guide.search("xylophone quasar")["text"]
    assert all(s.title.lower() != "contents" for s in guide.sections())


def test_guide_answers_read_as_plain_words():
    md = ("The **probability calculator** works out `P(win)` for every\nsignal, see [section 42](#42-x).\n\n"
          "- **Skips** sure losers\n  with enough evidence.\n- *Sizes* by edge.\n\n| Tab | Pages |\n|---|---|\n"
          "| **Ask** | Ask, Deep review |\n\n```yaml\nprobability:\n  veto: true\n```")
    assert guide.plain(md) == ("The probability calculator works out P(win) for every signal, see section 42.\n\n"
                               "• Skips sure losers with enough evidence.\n• Sizes by edge.\n\nTab · Pages\n"
                               "Ask · Ask, Deep review\n\nprobability:\n  veto: true")
    long = guide.search("how do I trade several markets at once")["text"]
    assert "**" not in long and "`" not in long and "](#" not in long
    assert "(More in the how-to guide.)" in long and len(long) < 3000
    assert [guide._value(v) for v in (True, None, [], ["MGC", "MCL"], 1.50, "MNQ")] == \
        ["true", "not set", "none", "[MGC, MCL]", "1.5", "MNQ"]


def test_the_lookups_answer_while_the_bot_is_stopped(tmp_path):
    cfg = BotConfig.model_validate({"data_dir": str(tmp_path), "instrument": {"symbol": "MNQ"}})
    save_kb(cfg.knowledge_path, outcomes(120, 80, day=datetime(2026, 10, 9).date()))
    save_kb(tmp_path / "knowledge_MES_5m.json", outcomes(40, 20))
    facts = Facts(cfg, offline="The bot is stopped.")
    for name in ("markets", "probability", "sources", "explain"):
        assert name in TOOLS
    m = facts.call("markets")
    assert m["markets"] == ["MNQ"] and "without a restart" in m["text"]
    p = facts.call("probability")
    assert p["rows"] and p["rows"][0]["title"] and "whole day" in p["where"] and p["network"] == ["MES"]
    assert facts.call("probability", {"strategy": "nonsense"})["error"].startswith("no strategy")
    s = facts.call("sources")
    assert s["knowledge"]["total"] == 200 and s["network"]["markets"][0]["symbol"] == "MES"
    assert "no source guarantees" in s["text"] and "Simulations: none yet" in s["text"]
    e = facts.call("explain", {"topic": "daily loss limit"})
    assert e["sections"] and "Settings" in e["text"]


@pytest.mark.parametrize("question,lookup", [
    ("which markets are you trading?", "markets"),
    ("what are the odds of each strategy now", "probability"),
    ("where does your knowledge come from", "sources"),
    ("what does max_conviction mean", "explain"),
    ("explain the consistency target", "explain"),
    ("how do I trade several markets at once?", "explain"),
    ("how do I pass the Combine", "explain"),
    ("can the bot trade gold and crude together", "explain"),
    ("how likely is a short to win now", "probability"),
    ("what markets are you trading now?", "markets"),
])
def test_questions_reach_the_right_lookup(question, lookup):
    asked = []

    async def fetch(name, args):
        asked.append(name)
        return {"text": f"[{name}]"}

    answer = run(intents.answer(question, fetch))
    assert asked and asked[0] == lookup and f"[{lookup}]" in answer


# ------------------------------------------------------------------------------ several markets: Telegram, controller, setup

class DeskActions(BotActions):
    def __init__(self, core, controls):
        super().__init__(core, controls)
        self.calls = []

    async def add_market(self, symbol, source, account=None):
        self.calls.append(("add", symbol))
        return f"Now trading {symbol}."

    async def remove_market(self, symbol, source, account=None):
        self.calls.append(("drop", symbol))
        return f"Stopped trading {symbol}."


def test_telegram_adds_and_drops_markets_after_confirmation():
    import httpx

    from topstep_bot.broker.paper import PaperBroker
    from topstep_bot.factory import build_core
    from topstep_bot.instruments import offline_contract
    from topstep_bot.telegram_control import COMMANDS, TelegramController

    from .test_telegram_control import FakeTelegram, button, msg

    for name in ("markets", "add", "drop"):
        assert name in dict(COMMANDS)
    contract = offline_contract("MNQ")
    core = build_core(BotConfig(), contract, PaperBroker(contract, 50_000), clock=lambda: T0, account_label="T")
    actions = DeskActions(core, Controls())
    fake = FakeTelegram()
    ctl = TelegramController("TOKEN", 555, actions, TelegramConfig(), transport=httpx.MockTransport(fake.handler))
    run(ctl.handle_update(msg("/markets", chat=555)))
    assert "Trading" in fake.sent[-1]["text"]
    run(ctl.handle_update(msg("/add", chat=555)))
    assert "Usage: /add" in fake.sent[-1]["text"] and actions.calls == []
    run(ctl.handle_update(msg("/add mes", chat=555)))
    assert "Trade MES alongside" in fake.sent[-1]["text"] and actions.calls == []  # asks first
    run(ctl.handle_update(button("yes:add", fake.sent[-1]["message_id"], chat=555)))
    run(ctl.handle_update(msg("/drop MES", chat=555)))
    run(ctl.handle_update(button("yes:drop", fake.sent[-1]["message_id"], chat=555)))
    assert actions.calls == [("add", "MES"), ("drop", "MES")]


def test_the_dashboard_switch_names_the_market_it_replaces(tmp_path):
    from .test_controller import scenario, until

    async def body(ctl, http):
        await ctl.bot.start("test")
        await until(lambda: ctl.bot.state == "running")
        r = (await http.post("/api/market/switch", json={"symbol": "MGC", "from": "MES"})).json()
        assert r["ok"] and "Now trading MGC instead of MES" in r["message"]
        r = (await http.post("/api/market/switch", json={"symbol": "MNQ", "from": "MES"})).json()
        assert r["ok"]  # the account's usual market can replace another one of its markets
    scenario(tmp_path, body)


@pytest.mark.usefixtures("clean_env")
def test_the_setup_tab_trades_several_markets_at_once(tmp_path):
    async def body(ctl, http, path):
        state = (await http.post("/api/setup/state")).json()
        assert state["choices"]["max_markets"] == MAX_MARKETS and state["values"]["count"] == 1
        r = (await http.post("/api/setup/save", json={**FORM, "count": "3", "markets": ["MGC", "MCL"]})).json()
        assert r["ok"], r
        cfg = load_config(path)
        assert cfg.instrument.symbol == "MNQ" and cfg.instrument.markets == ["MGC", "MCL"] and cfg.instrument.max_markets == 1
        values = (await http.post("/api/setup/state")).json()["values"]
        assert values["count"] == 3 and values["markets"] == ["MGC", "MCL"]
        for markets, why in ((["MGC"], "Pick 1 more"), (["NQ", "MGC"], "same product"), (["XYZ", "MGC"], "Unknown market")):
            r = (await http.post("/api/setup/save", json={**FORM, "count": "3", "markets": markets})).json()
            assert not r["ok"] and why in r["message"], r
        r = (await http.post("/api/setup/save", json={**FORM, "symbol": "AUTO", "count": "2"})).json()
        assert r["ok"], r
        cfg = load_config(path)
        assert cfg.instrument.auto_pick and cfg.instrument.max_markets == 2 and cfg.instrument.markets == []
        r = (await http.post("/api/setup/save", json={**FORM, "count": "1"})).json()
        assert r["ok"]
        cfg = load_config(path)
        assert cfg.instrument.markets == [] and cfg.instrument.max_markets == 1 and not cfg.instrument.auto_pick
        assert "markets:" not in path.read_text(encoding="utf-8").split("instrument:")[1].split("\n\n")[0]
    setup_scenario(tmp_path, body)


def test_the_overview_shows_what_a_strong_edge_may_risk(mnq):
    async def go(sizing):
        core, _broker, _now = make_core(mnq, risk={"risk_per_trade": 200}, aggressive={"enabled": True},
                                         probability={"sizing": sizing})
        await core.begin_day(core.schedule.trading_day(T0), 50_000)
        return core.risk.snapshot(core.balance, 0.0)

    view = run(go(True))
    assert view["next_trade_risk"] == 200 and view["aggressive"]["strong_trade_risk"] == 300  # 1.5x max_conviction
    assert run(go(False))["aggressive"]["strong_trade_risk"] is None


OLD_PROFIT = """aggressive:                     # Make money every day: as many trades as the rules allow, pressing on good days
  enabled: true
  unproven_risk: 0.5            # signals the bot hasn't proven yet trade at half size
  house_money: 0.25             # add a quarter of today's realized profit to the next trade's risk
  max_risk_multiple: 2          # ...never more than twice risk_per_trade
  lock_in_at: 0.5               # once the day has made half its goal, lock part of it in:
  give_back: 0.4                # never give back more than 40% of the day's best
"""


@pytest.mark.usefixtures("clean_env")
def test_make_money_every_day_moves_to_the_new_numbers_on_save(tmp_path):
    from topstep_bot.wizard import render_config

    fresh = render_config(goal="profit").replace("data_dir: data", f"data_dir: {tmp_path.as_posix()}")
    cfg = BotConfig.model_validate(__import__("yaml").safe_load(fresh))
    assert (cfg.aggressive.trades_multiple, cfg.aggressive.house_money, cfg.aggressive.max_risk_multiple,
            cfg.aggressive.max_conviction) == (3, 0.35, 2.5, 1.5)  # a new config leaves the defaults to the version
    start = fresh.index("\naggressive:") + 1
    end = fresh.index("\n\n", start)
    old = fresh[:start] + OLD_PROFIT.rstrip("\n") + fresh[end:]  # what 3.0's Setup tab wrote

    async def body(ctl, http, path):
        assert ctl.cfg.aggressive.house_money == 0.25 and ctl.cfg.aggressive.max_risk_multiple == 2
        r = (await http.post("/api/setup/save", json={**FORM, "goal": "profit", "risk_per_trade": "300",
                                                      "daily_loss_limit": "600", "max_trades": "12"})).json()
        assert r["ok"], r
        cfg = load_config(path)
        assert cfg.aggressive.house_money == 0.35 and cfg.aggressive.max_risk_multiple == 2.5
        assert cfg.aggressive.unproven_risk == 0.5 and cfg.aggressive.give_back == 0.4  # unchanged values stay
        text = path.read_text(encoding="utf-8")
        assert "house_money" not in text and "twice risk_per_trade" not in text and "give_back: 0.4" in text
    setup_scenario(tmp_path, body, config_text=old)

    async def chosen(ctl, http, path):  # a number you chose yourself is kept
        r = (await http.post("/api/setup/save", json={**FORM, "goal": "profit", "risk_per_trade": "300",
                                                      "daily_loss_limit": "600", "max_trades": "12"})).json()
        assert r["ok"] and load_config(path).aggressive.house_money == 0.1, r
    (tmp_path / "own").mkdir()
    setup_scenario(tmp_path / "own", chosen, config_text=old.replace("house_money: 0.25", "house_money: 0.1"))


# ------------------------------------------------------------------------------ the simpler dashboard

def test_the_dashboard_has_two_learning_tabs_with_pages():
    html = DASHBOARD.read_text(encoding="utf-8")
    rail = re.findall(r'<button data-tab="(\w+)"', html)
    assert rail == ["overview", "trade", "ask", "knowledge", "settings", "setup", "logs"]
    pages = dict(re.findall(r'id="tab-(\w+)" data-group="(\w+)"', html))
    assert pages == {"ask": "ask", "coach": "ask", "trade": "trade", "ideas": "trade", "knowledge": "knowledge",
                     "works": "knowledge", "lab": "knowledge", "market": "knowledge"}
    for needed in ('id="chart"', 'id="tk-chart"', 'id="mstrip"', 'id="prob-table"', 'id="cal-body"', 'id="kn-net"',
                   'id="kn-grow"', 'id="su-count"', 'id="tk-add"', "function drawChart", "function renderMarketsStrip",
                   "function loadHub"):
        assert needed in html
    names = re.findall(r"^(?:async )?function (\w+)", html, re.M)
    assert sorted({n for n in names if names.count(n) > 1}) == []
    consts = re.findall(r"^const (\w+)", html, re.M)
    assert sorted({n for n in consts if consts.count(n) > 1}) == []  # a second `const pct` stopped the whole page once


def test_the_desk_lists_markets_with_their_names(tmp_path):
    from topstep_bot.risk.book import AccountBook

    runner = LiveRunner(BotConfig.model_validate({"data_dir": str(tmp_path)}), Secrets(username="u", api_key="k"), Controls())
    runner.book = AccountBook(shared=True)
    view = runner.markets_view()
    assert view["max"] == MAX_MARKETS and view["lanes"] == [] and "MES" in view["available"]
    assert view["names"]["MES"] == ["Micro E-mini S&P 500", "Equity index"] and set(view["names"]) == set(view["available"])
    json.dumps(view)


# ------------------------------------------------------------------------------ the release itself


@pytest.mark.parametrize("question,section,setting", [
    ("how do I trade several markets at once", "Several markets at once", "instrument.max_markets"),
    ("what is the probability calculator", "The probability calculator", "probability.veto"),
    ("how does the bot size trades by kelly", "The probability calculator", "probability.sizing"),
    ("how does the knowledge network grow", "The knowledge network", "probability.grow_network"),
    ("what does the chart show", "The trade chart", None),
])
def test_ask_explains_the_new_parts_from_the_guide(question, section, setting):
    found = guide.search(question)
    assert section in found["sections"][0]["title"]
    if setting:
        assert setting in [s["key"] for s in found["settings"]]


def test_version_4_0_is_documented_honestly():
    from topstep_bot import __version__

    assert __version__.startswith("4.") and f'version = "{__version__}"' in (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    notes = re.search(r"^## 4\.0\.0\b.*?$\n(.*?)(?=^## |\Z)", changelog, re.M | re.S).group(1)
    assert "### Honest notes" in notes and "no bot can guarantee" in notes.lower()
    guide_text = (ROOT / "docs" / "HOW_TO_USE.md").read_text(encoding="utf-8")
    for n, title in enumerate(("Several markets at once", "The probability calculator", "The knowledge network",
                               "The trade chart", "The simpler dashboard"), start=41):
        assert f"## {n}. {title}" in guide_text and f"{n}. [{title}]" in guide_text  # the section and its contents line
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "## New in 4.0" in readme and "/add" in readme and "/drop" in readme
    # the old tabs are gone from the guide's directions (history notes and the table of where they went aside)
    for old in ("Ideas", "Market", "Simulations", "AI Coach"):
        assert not re.search(rf"\b{old} tab\b", guide_text.split("**New in 3.0")[1]), old


def test_version_4_0_1_is_documented_honestly():
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    notes = re.search(r"^## 4\.0\.1\b.*?$\n(.*?)(?=^## |\Z)", changelog, re.M | re.S).group(1)
    assert "### Settings to look at" in notes and "mll_floor_override" in notes
    assert "### Honest notes" in notes and "no bot can guarantee" in notes.lower()
