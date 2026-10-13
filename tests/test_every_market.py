"""v2.1: every market Topstep permits - the catalogue, their history, the lab on all of them, opportunities,
the market pick across all of them and switching an account's market for the day."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, time, timedelta

import httpx
import pytest

from topstep_bot import market_pick as mp
from topstep_bot import simlab
from topstep_bot.backtest.data import synthetic_bars
from topstep_bot.config import BotConfig
from topstep_bot.instruments import CLASSES, SPECS, expand_markets, markets, offline_contract
from topstep_bot.market_data import MarketData, coverage, parse_yahoo_bars
from topstep_bot.memory import MarketLibrary
from topstep_bot.models import MICRO_ROOTS, Bar

from .conftest import CT, ct, run


def cfg_for(tmp_path, **kw) -> BotConfig:
    return BotConfig.model_validate({"data_dir": str(tmp_path / "data"), "log_dir": str(tmp_path / "logs"), **kw})


# ------------------------------------------------------------------------------ the catalogue

def test_the_catalogue_holds_every_permitted_market_with_sane_specs():
    assert len(SPECS) == 51
    assert {s.asset_class for s in SPECS.values()} == set(CLASSES)
    for s in SPECS.values():
        assert s.tick_size > 0 and s.tick_value > 0 and s.fees_round_turn > 0, s.root
        assert s.rth_open < s.rth_close
        if s.mini:
            assert s.mini in SPECS and SPECS[s.mini].asset_class == s.asset_class
    assert markets("fx")[:2] == ["6A", "M6A"] and "ZN" in markets("rates") and "ZC" in markets("ags")
    assert SPECS["ZC"].market_close == "13:20" and SPECS["HE"].market_close == "13:05" and SPECS["ES"].market_close is None
    assert SPECS["ZN"].tick_size == 0.015625 and SPECS["6J"].tick_value == 6.25  # CME specs
    assert SPECS["MCL"].fees_round_turn == 1.72  # Topstep's table after the October 2026 CME increase


def test_candidates_accept_groups_and_keep_your_order():
    assert expand_markets(["ALL"]) == list(SPECS)
    assert expand_markets(["MES", "FX", "MES"])[:2] == ["MES", "6A"] and len(expand_markets(["MES", "FX"])) == 13
    assert set(expand_markets(["MICROS"])) == {s for s in SPECS if s in MICRO_ROOTS}
    cfg = BotConfig.model_validate({"instrument": {"auto_pick": True, "candidates": ["all"]}})
    assert cfg.instrument.candidates == ["ALL"]
    allowed = mp.candidates_for(cfg)
    assert len(allowed) == 48 and not {"SI", "HG", "PL"} & set(allowed)  # Topstep caps these at 0 for now
    with pytest.raises(ValueError, match="unknown market"):
        BotConfig.model_validate({"instrument": {"candidates": ["XYZ"]}})


def test_early_closing_markets_are_left_before_their_own_close():
    from topstep_bot.broker.paper import PaperBroker
    from topstep_bot.factory import build_core
    from topstep_bot.sessions import market_session

    cfg = BotConfig()
    zc = market_session(cfg.session, "ZC")
    assert (zc.flatten_at, zc.last_entry) == (time(13, 15), time(12, 45))
    assert market_session(cfg.session, "HE").flatten_at == time(13, 0)
    assert market_session(cfg.session, "ES") is cfg.session
    contract = offline_contract("ZC")
    core = build_core(cfg, contract, PaperBroker(contract, 50_000), clock=lambda: datetime.now(UTC), account_label="t")
    assert core.schedule.must_be_flat(ct(2026, 10, 6, 13, 16)) and not core.schedule.must_be_flat(ct(2026, 10, 6, 13, 0))


def test_a_grain_backtest_never_holds_past_its_close():
    from topstep_bot.backtest.runner import run_backtest

    cfg = BotConfig.model_validate({"strategy": {"name": "vwap_reversion"}})
    cfg.instrument.symbol = "ZC"
    bars = synthetic_bars("ZC", days=30, tick_size=0.25, seed=5)
    res = run(run_backtest(cfg, bars, offline_contract("ZC")))
    assert len(res.trades) >= 3
    assert any("ZC stops trading at 13:20 CT" in t.exit_reason for t in res.trades)
    for t in res.trades:
        assert t.closed_at.astimezone(CT).time() <= time(13, 20), t.closed_at
        assert t.opened_at.astimezone(CT).time() <= time(12, 50)


# ------------------------------------------------------------------------------ history

def _yahoo_json(start: datetime, n: int, base: float, minutes: int = 5, gap_at: int | None = None) -> dict:
    ts = [int((start + timedelta(minutes=minutes * i)).timestamp()) for i in range(n)]
    q = {k: [base + i * 0.001 for i in range(n)] for k in ("open", "high", "low", "close")}
    q["volume"] = [10] * n
    if gap_at is not None:
        q["close"][gap_at] = None
    return {"chart": {"result": [{"timestamp": ts, "indicators": {"quote": [q]}}]}}


def test_yahoo_bars_skip_gaps_live_quotes_and_the_forming_bar():
    start = datetime(2026, 10, 6, 14, 0, tzinfo=UTC)
    data = _yahoo_json(start, 6, 1.1, gap_at=2)
    data["chart"]["result"][0]["timestamp"][4] += 7  # an odd stamp: Yahoo's live quote
    bars = parse_yahoo_bars(data, 5, now=start + timedelta(minutes=27))
    assert [b.ts.minute for b in bars] == [0, 5, 15]  # gap, live quote and the still-forming 14:25 bar dropped
    assert parse_yahoo_bars({"chart": {"result": None}}, 5, start) == []


def test_every_market_downloads_into_the_library_without_overwriting_topstepx(tmp_path):
    cfg = cfg_for(tmp_path)
    now = datetime(2026, 10, 9, 20, 0, tzinfo=UTC)
    start = now - timedelta(days=2)
    asked: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        asked.append(request.url.path.rsplit("/", 1)[-1] + "?" + request.url.query.decode())
        if "interval=1d" in str(request.url):
            day = datetime(2025, 1, 1, tzinfo=UTC)
            return httpx.Response(200, json=_yahoo_json(day, 300, 100.0, minutes=1440))
        if "ZW=F" in str(request.url):
            return httpx.Response(500)
        return httpx.Response(200, json=_yahoo_json(start, 100, 1.2))

    lib = MarketLibrary(cfg.library_path)
    first = start.replace(second=0)
    lib.add("6E", 5, [Bar(first, 9.0, 9.0, 9.0, 9.0, 1)], "CON.F.US.EU6.Z26")  # what TopstepX delivered
    lib.close()
    data = MarketData(cfg, transport=httpx.MockTransport(handler))
    results = run(data.refresh(["6E", "M6E", "ZW"], now=now))
    assert [r.source for r in results] == ["Yahoo", "Yahoo", ""] and "ZW" in results[2].line()
    assert not any("M6E" in a for a in asked) and any(a.startswith("6E") for a in asked)  # the micro uses its mini's feed
    lib = MarketLibrary(cfg.library_path)
    try:
        kept = lib.bars("6E", 5, first, first + timedelta(minutes=1))
        assert kept[0].close == 9.0  # Yahoo only fills gaps
        summary = lib.summary(5)
    finally:
        lib.close()
    assert summary["6E"]["sources"] == ["TopstepX", "Yahoo"] and summary["M6E"]["sources"] == ["Yahoo"]
    rows = {r["symbol"]: r for r in coverage(cfg)}
    assert len(rows) == 51 and rows["6E"]["bars"] == 100 and rows["ES"]["bars"] == 0
    assert rows["6E"]["context"]["range_percentile"] is not None and rows["6E"]["daily_years"] > 1
    assert rows["SI"]["allowed"] is False and rows["6E"]["allowed"] is True and rows["ZC"]["early_close"] == "13:20"
    assert data.status()["last_run"] and not data.status()["running"]


# ------------------------------------------------------------------------------ the lab on every market

def _library(cfg: BotConfig, symbols: dict[str, float], days: int = 30) -> None:
    from topstep_bot.backtest.runner import prepare_bars

    lib = MarketLibrary(cfg.library_path)
    try:
        for i, (sym, tick) in enumerate(symbols.items()):
            lib.add(sym, 5, prepare_bars(synthetic_bars(sym, days=days, tick_size=tick, seed=11 + i), 5), f"yahoo:{sym}=F")
    finally:
        lib.close()


def test_the_lab_simulates_markets_known_only_from_the_library(tmp_path):
    cfg = cfg_for(tmp_path)
    _library(cfg, {"6E": 0.00005, "ZC": 0.25})
    assert simlab.available_markets(cfg) == ["6E", "ZC"]
    assert simlab.lab_symbols(cfg, ["6E", "ZC"]) == ["6E", "ZC"]  # lab.markets: all (the default)
    assert simlab.lab_symbols(cfg, ["6E", "ZC", "MNQ"], "tradable") == ["MNQ"]
    rep = run(simlab.run_lab(cfg, starts=1, strategies=["orb", "vwap_reversion"], workers=1, save=False))
    assert set(rep["markets"]) == {"6E", "ZC"} and rep["markets"]["ZC"]["class"] == "ags"
    assert rep["coverage"]["simulated"] == 2 and rep["coverage"]["permitted"] == 51 and "ES" in rep["coverage"]["missing"]
    assert {c["class"] for c in rep["classes"]} == {"fx", "ags"}
    assert {p["class"] for p in rep["playbooks"]} == {"fx", "ags"} and all(p["allowed"] for p in rep["playbooks"])
    opp = rep["opportunities"]
    assert opp["tested"] >= opp["found"] and "pockets" in opp["text"]
    only_fx = run(simlab.run_lab(cfg, starts=1, strategies=["orb"], classes=["fx"], workers=1, save=False))
    assert list(only_fx["markets"]) == ["6E"]


def _full(symbol: str, strategy: str, rows: list[tuple[str, str, float]]) -> simlab.SimRun:
    days = sorted({d for d, _, _ in rows})
    return simlab.SimRun(symbol, strategy, "", "full", daily=[(d, 0.0, 0.0, 1) for d in days],
                         trade_slots=[(d, slot, pnl / 100, pnl) for d, slot, pnl in rows])


def test_opportunities_need_both_halves_and_say_how_many_were_tested():
    days = [(date(2026, 8, 3) + timedelta(days=i)).isoformat() for i in range(20)]
    steady = [(d, "open", 120.0 if i % 4 else -60.0) for i, d in enumerate(days)]  # wins in both halves
    lucky = [(d, "midday", 400.0 if i < 10 else -100.0) for i, d in enumerate(days)]  # all of it in the first half
    runs = [_full("6E", "orb", steady + lucky)]
    playbooks = [{"symbol": "6E", "strategy": "orb", "class": "fx", "allowed": True, "verdict": "mixed"}]
    opp = simlab.find_opportunities(runs, playbooks)
    assert opp["tested"] == 2 and opp["found"] == 1
    best = opp["items"][0]
    assert best["label"] == "6E · orb · open" and best["halves"][0] > 0 and best["halves"][1] > 0
    assert best["strength"] in ("strong", "promising") and best["trades"] == 20
    assert str(opp["tested"]) in opp["text"] and "luck" in opp["text"]


def test_the_lab_never_suggests_a_market_the_plan_cannot_trade():
    pb = {"trades": 40, "days": 40, "halves": [500.0, 400.0], "profit_factor": 1.6, "net": 900.0, "verdict": "edge",
          "why": "", "resampled": {"pass": 0.5, "fail": 0.1}, "per_day": 22.0}
    playbooks = [{**pb, "symbol": "SI", "strategy": "orb", "label": "SI · orb", "allowed": False},
                 {**pb, "symbol": "MES", "strategy": "orb", "label": "MES · orb", "allowed": True, "verdict": "losing", "net": -50}]
    rec = simlab.recommendation(playbooks, None, BotConfig())
    assert rec["kind"] == "none" and "SI" not in rec["text"]
    assert simlab.holdout_check(playbooks)["symbol"] == "MES"


# ------------------------------------------------------------------------------ the market for the day

def test_a_market_switch_lasts_until_the_flat_by_deadline(tmp_path):
    cfg = cfg_for(tmp_path)
    tue_10 = ct(2026, 10, 6, 10, 0)
    data = mp.set_today(cfg, "6e", "dashboard", now=tue_10)
    assert data["symbol"] == "6E" and data["day"] == "2026-10-06"
    assert mp.load_today(cfg, tue_10 + timedelta(hours=5))["symbol"] == "6E"  # 15:00
    assert mp.load_today(cfg, ct(2026, 10, 6, 15, 11)) is None  # past 15:10: back to the usual market
    after = mp.set_today(cfg, "GC", "telegram", now=ct(2026, 10, 6, 15, 40))
    assert after["day"] == "2026-10-07"  # made after the deadline: it is for the coming day
    assert mp.load_today(cfg, ct(2026, 10, 7, 9, 0))["symbol"] == "GC"
    fri = mp.set_today(cfg, "GC", "telegram", now=ct(2026, 10, 9, 16, 0))
    assert fri["day"] == "2026-10-12"  # Friday afternoon: Monday
    assert mp.clear_today(cfg) and mp.load_today(cfg, ct(2026, 10, 12, 9, 0)) is None


def test_the_current_market_follows_todays_switch(tmp_path):
    cfg = cfg_for(tmp_path, instrument={"symbol": "M2K"})
    assert mp.current_symbol(cfg) == "M2K"
    mp.set_today(cfg, "MCL", "dashboard")
    assert mp.current_symbol(cfg) == "MCL"
    (cfg.account_dir / mp.TODAY_FILE).write_text(json.dumps({"symbol": "NOPE", "until": "2099-01-01T00:00:00+00:00"}))
    assert mp.current_symbol(cfg) == "M2K"  # a damaged or unknown entry is ignored


def test_picking_among_many_markets_also_asks_for_money_on_the_earlier_days(tmp_path, monkeypatch):
    cfg = cfg_for(tmp_path, instrument={"symbol": "MNQ", "auto_pick": True, "candidates": ["ALL"]},
                  strategy={"name": "orb"})

    async def fake_score(cfg, symbol, contract, bars):
        good = symbol in ("6E", "LUCKY")
        return mp.Candidate(symbol, days=40, test_days=13, trades=9, r_per_day=0.3 if good else -0.1, usd_per_day=10,
                            win_rate=0.5, sized=1.0, train_r_per_day=0.1 if symbol == "6E" else -0.2)

    monkeypatch.setattr(mp, "score_market", fake_score)
    monkeypatch.setattr(mp, "candidates_for", lambda cfg: ["LUCKY", "6E", *[f"X{i}" for i in range(9)]])

    async def load(symbol):
        return offline_contract("MNQ"), [object()]

    pick = run(mp.pick_market(cfg, load, now=datetime(2026, 10, 6, 12, tzinfo=UTC)))
    assert pick.symbol == "6E"
    lucky = next(c for c in pick.candidates if c.symbol == "LUCKY")
    assert not lucky.eligible and "likely luck" in lucky.note


# ------------------------------------------------------------------------------ the controller

def test_switching_the_market_happens_without_a_restart(tmp_path, monkeypatch):
    from .test_controller import scenario, until

    async def body(ctl, http):
        r = (await http.post("/api/markets", json={})).json()
        assert len(r["markets"]) == 51 and r["current"] == "MNQ" and r["today"] is None
        await ctl.bot.start("test")
        await until(lambda: ctl.bot.state == "running")
        bad = (await http.post("/api/market/switch", json={"symbol": "SI"})).json()
        assert bad["ok"] is False and "doesn't allow SI" in bad["message"]
        bad = (await http.post("/api/market/switch", json={"symbol": "XYZ"})).json()
        assert bad["ok"] is False and "Unknown market" in bad["message"]
        pid = ctl.bot.proc.pid
        r = (await http.post("/api/market/switch", json={"symbol": "6E"})).json()
        assert r["ok"] and "Now trading 6E" in r["message"] and "restarting" not in r["message"]
        assert ctl.bot.state == "running" and ctl.bot.proc.pid == pid  # no restart
        cfg = ctl.account_cfg(ctl.bot.account)
        mp.set_today(cfg, "6E", "test")  # what the running bot saves (the fake one doesn't)
        back = (await http.post("/api/market/switch", json={"symbol": "usual"})).json()
        assert back["ok"] and "Now trading MNQ" in back["message"] and mp.load_today(cfg) is None
        assert ctl.bot.proc.pid == pid
    scenario(tmp_path, body)


def test_a_switch_with_an_open_trade_lets_that_trade_finish(tmp_path, monkeypatch):
    from .test_controller import scenario, until

    monkeypatch.setenv("FAKE_POSITION", "1")

    async def body(ctl, http):
        await ctl.bot.start("test")
        await until(lambda: ctl.bot.state == "running")
        r = (await http.post("/api/market/switch", json={"symbol": "MES"})).json()
        assert r["ok"] and "dropped as soon as its open trade is closed" in r["message"]
    scenario(tmp_path, body)


def test_the_nightly_run_waits_for_a_weekday_after_the_close(tmp_path):
    from .test_controller import make

    ctl = make(tmp_path)
    lab = ctl.lab
    assert not lab.nightly_due(ct(2026, 10, 10, 18, 0))  # Saturday
    assert not lab.nightly_due(ct(2026, 10, 6, 15, 0))  # before 15:30 CT
    assert lab.nightly_due(ct(2026, 10, 6, 15, 31))
    lab.nightly_day = "2026-10-06"
    assert not lab.nightly_due(ct(2026, 10, 6, 18, 0))  # once a day
    assert lab.nightly_due(ct(2026, 10, 7, 16, 0))
    simlab.save_report(ctl.cfg, {"generated": ct(2026, 10, 7, 15, 45).isoformat(), "runs": []})
    assert not lab.nightly_due(ct(2026, 10, 7, 16, 0))  # already run since the close


def test_setup_offers_picking_from_every_market(tmp_path):
    from topstep_bot.config_edit import REMOVE
    from topstep_bot.setup_service import AUTO, AUTO_ALL, _candidates_change, market_key

    every = BotConfig.model_validate({"instrument": {"auto_pick": True, "candidates": ["ALL"]}})
    assert market_key(every.instrument) == AUTO_ALL
    assert market_key(BotConfig.model_validate({"instrument": {"auto_pick": True}}).instrument) == AUTO
    assert market_key(BotConfig.model_validate({"instrument": {"symbol": "6E"}}).instrument) == "6E"
    assert _candidates_change({"every_market": True}, []) == {("instrument", "candidates"): ["ALL"]}
    assert _candidates_change({"every_market": False}, ["ALL"]) == {("instrument", "candidates"): REMOVE}
    assert _candidates_change({"every_market": False}, ["MNQ", "MES"]) == {}  # your own list stays
