"""The dashboard's Setups panel: trades each strategy is building toward, and their history."""

from datetime import date, timedelta

import pytest

from topstep_bot.broker.paper import PaperBroker
from topstep_bot.config import BotConfig
from topstep_bot.factory import build_core
from topstep_bot.instruments import offline_contract
from topstep_bot.journal import Journal
from topstep_bot.models import Bar
from topstep_bot.recommendations import RecommendationBook
from topstep_bot.strategies import STRATEGIES, create_strategy
from topstep_bot.strategies.base import Setup, StrategyContext

from .conftest import CT, ct, run

DAY = date(2026, 3, 3)  # a Tuesday


def orb_feed(strat):
    def feed(hh, mm, o, h, l, c):  # noqa: E741
        start = ct(2026, 3, 3, hh, mm)
        close = start + timedelta(minutes=5)
        strat.on_bar(Bar(start, o, h, l, c, 100), StrategyContext(close, close.astimezone(CT), DAY, 0, None, None))
        return close.astimezone(CT)
    return feed


def test_orb_reports_its_breakout_setups_as_the_range_forms(mnq):
    s = create_strategy("orb", {"max_trades_per_day": 2}, mnq, 5)
    s.on_new_day(DAY)
    feed = orb_feed(s)
    now = feed(8, 30, 100, 105, 99, 104)
    forming = {st.side: st for st in s.setups(104, now)}
    assert set(forming) == {"long", "short"} and forming["long"].progress == 0  # the range isn't set yet
    assert forming["long"].entry is None and "range forms until 08:45" in forming["long"].note

    feed(8, 35, 104, 106, 101, 102)
    now = feed(8, 40, 102, 104, 98, 100)  # the 15-minute range is 98-106
    ready = {st.side: st for st in s.setups(100, now)}
    long, short = ready["long"], ready["short"]
    assert long.entry == pytest.approx(106.5) and short.entry == pytest.approx(97.5)  # 2-tick buffer
    assert long.stop == pytest.approx(102) and long.target == pytest.approx(106.5 + 2 * 4.5)
    assert long.conditions[0][1] and not long.conditions[1][1] and long.progress == 0.5
    assert [c for c, _ in long.conditions][1].startswith("A bar closes above the range high (106.50)")
    assert {st.side: st.progress for st in s.setups(107, now)}["long"] == 1.0  # a close here would fire

    now = feed(8, 45, 100, 108, 100, 107.5)  # fires the long
    assert [st.side for st in s.setups(107.5, now)] == ["short"]  # one long a day: only the short is left
    late = now.replace(hour=11, minute=5)
    assert s.setups(100, late) == []  # past the 11:00 entry cutoff


@pytest.mark.parametrize("name", sorted(STRATEGIES))
def test_every_strategy_reports_setups_without_errors(name, mnq):
    from topstep_bot.backtest.data import synthetic_bars

    s = create_strategy(name, {}, mnq, 5)
    bars = [b for b in synthetic_bars("MNQ", days=20, seed=11, end=DAY)]
    day = None
    seen = 0
    for i in range(0, len(bars) - 5, 5):
        chunk = bars[i:i + 5]
        b = Bar(chunk[0].ts, chunk[0].open, max(x.high for x in chunk), min(x.low for x in chunk), chunk[-1].close,
                sum(x.volume for x in chunk))
        close = b.ts + timedelta(minutes=5)
        local = close.astimezone(CT)
        d = (local + timedelta(hours=7)).date()  # trading day rolls at 17:00 CT
        if d != day:
            s.on_new_day(d)
            day = d
        s.on_bar(b, StrategyContext(close, local, d, 0, None, None))
        for st in s.setups(b.close, local):
            seen += 1
            assert st.side in ("long", "short") and st.conditions and 0 <= st.progress <= 1
            ref = st.entry if st.entry is not None else b.close
            if st.stop is not None:
                assert (st.stop < ref) if st.side == "long" else (st.stop > ref), (st, ref)
    assert seen > 0  # every strategy builds toward something on a month of random bars


def make_core(tmp_path, strategy="orb"):
    mnq = offline_contract("MNQ")
    cfg = BotConfig.model_validate({"data_dir": str(tmp_path), "strategy": {"name": strategy}})
    now = [ct(2026, 3, 3, 8, 30)]
    broker = PaperBroker(mnq, 50_000, slippage_ticks=0, fees_round_turn=1.22, live=True)
    core = build_core(cfg, mnq, broker, clock=lambda: now[0], account_label="T", journal=Journal(tmp_path / "j.db"))
    core.balance = 50_000
    core.recommender = RecommendationBook(core, ["ema_trend"])
    core.setups.enabled = True
    run(core.begin_day(DAY, 50_000))
    return core, broker, now


def bar_at(core, broker, now, hh, mm, o, h, l, c):  # noqa: E741
    start = ct(2026, 3, 3, hh, mm)
    b = Bar(start, o, h, l, c, 100)
    now[0] = start + timedelta(minutes=5)
    run(broker.on_bar(b))
    run(core.on_price(now[0], c))
    run(core.on_bar(b))
    run(broker.drain())


def test_core_tracks_a_setup_from_forming_to_the_order(tmp_path):
    core, broker, now = make_core(tmp_path)
    bar_at(core, broker, now, 8, 30, 100, 105, 99, 104)
    view = core.setups.view()
    assert view["items"] == [] and "Opening Range Breakout long" in view["waiting"]  # nothing met yet

    bar_at(core, broker, now, 8, 35, 104, 106, 101, 102)
    bar_at(core, broker, now, 8, 40, 102, 104, 98, 100)
    view = core.setups.view()
    items = {i["side"]: i for i in view["items"] if i["strategy"] == "orb"}
    long = items["LONG"]
    assert long["role"] == "bot" and long["symbol"] == core.contract.name and long["met"] == 1 and long["total"] == 2
    assert long["entry"] == 106.5 and long["stop"] == 102 and long["target"] == 115.5
    assert long["plan"]["size"] >= 1 and long["plan"]["risk_usd"] <= core.cfg.risk.risk_per_trade
    assert long["plan"]["stop_ticks"] == 18 and long["plan"]["rr"] == 2.0
    assert long["blocked"] is None and view["blocked"] is None
    assert [c["met"] for c in long["conditions"]] == [True, False]
    forming = [e for e in view["events"] if e["kind"] == "forming"]
    assert {e["side"] for e in forming} == {"LONG", "SHORT"} and any("range high" in e["text"] for e in forming)

    bar_at(core, broker, now, 8, 45, 100, 108, 100, 107.5)  # the breakout: the bot enters
    assert core.orders.position > 0
    view = core.setups.view()
    fired = [e for e in view["events"] if e["kind"] == "fired"]
    assert fired and fired[0]["side"] == "LONG" and "placed a Long" in fired[0]["text"].replace("LONG", "Long")
    assert not any(i["strategy"] == "orb" for i in view["items"])  # in its trade: no new setups from it
    assert view["blocked"].startswith("a trade is already open")
    latest = core.snapshot()["setups"]["events"][0]  # the short can't happen any more
    assert latest["kind"] == "cancelled" and latest["side"] == "SHORT" and "took the long trade instead" in latest["text"]


class Stub:
    name, title = "stub", "Stub Strategy"

    def __init__(self):
        self.result = []

    def setups(self, price, now):
        return self.result


def test_setup_history_notes_cancelled_setups_and_ignores_flicker(tmp_path):
    core, _, now = make_core(tmp_path)
    stub = Stub()
    core.recommender.shadows = [stub]
    core.strategy = stub  # only the stub's setups
    t = core.setups
    close = ct(2026, 3, 3, 9, 0)

    def step(*met, minutes=5):
        nonlocal close
        close += timedelta(minutes=minutes)
        now[0] = close
        stub.result = [Setup("long", [(f"rule {i}", ok) for i, ok in enumerate(met)], stop=90.0)] if met else []
        t.on_bar_closed(close, 100.0)
        return [e["kind"] for e in t.events]

    assert step(True, False, False) == []  # a third met: not forming yet
    assert step(True, True, False) == ["forming"]
    assert step(True, False, False) == ["forming"]  # 1/3 is within the gap: no flicker
    assert step(False, False, False) == ["cancelled", "forming"]
    assert "no longer true: rule 0" in t.events[0]["text"]
    step(True, True, True)
    assert step() == ["cancelled", "forming", "cancelled", "forming"]  # gone entirely (window closed)
    assert "no longer possible" in t.events[0]["text"]

    t.enabled = False  # backtests and training skip the bookkeeping
    step(True, True, True)
    assert len(t.events) == 4
