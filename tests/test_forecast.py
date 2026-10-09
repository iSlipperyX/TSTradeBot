"""The next-trade forecast (forecast.py) and the "what the bot knows" briefing (briefing.py)."""

from datetime import date, datetime, timedelta

import pytest

from topstep_bot.briefing import brief_text, build_brief
from topstep_bot.broker.paper import PaperBroker
from topstep_bot.config import BotConfig
from topstep_bot.control import BotActions
from topstep_bot.factory import build_core
from topstep_bot.forecast import (
    SignalHistory,
    estimate,
    forecast,
    forecast_text,
    next_allowed,
    session_clear,
    signal_history,
    wait_text,
)
from topstep_bot.instruments import offline_contract
from topstep_bot.knowledge import KnowledgeBase, Observation
from topstep_bot.live import Controls
from topstep_bot.sessions import SessionSchedule

from .conftest import CT, ct, run

TUE = date(2026, 3, 3)


def make_core(tmp_path, strategy="orb", at=(8, 0), **cfg_extra):
    mnq = offline_contract("MNQ")
    cfg = BotConfig.model_validate({"data_dir": str(tmp_path), "strategy": {"name": strategy}, "news": {"enabled": False},
                                    **cfg_extra})
    now = [ct(2026, 3, 3, *at)]
    core = build_core(cfg, mnq, PaperBroker(mnq, 50_000), clock=lambda: now[0], account_label="T")
    core.balance = 50_000
    run(core.begin_day(TUE, 50_000))
    return core, now


def past_days(n: int, before: date = TUE) -> list[date]:
    days, d = [], before
    while len(days) < n:
        d -= timedelta(days=1)
        if d.weekday() < 5 and d != date(2026, 2, 16):  # Presidents' Day is a no-trade date
            days.append(d)
    return sorted(days)


def kb_with(times_by_day: dict[date, list[str]], strategy="orb", min_samples=2) -> KnowledgeBase:
    kb = KnowledgeBase(None, min_samples=min_samples)
    for d, times in times_by_day.items():
        for t in times:
            kb.record(Observation(d.isoformat(), t, strategy, "LONG", "open", "calm", 0.5, 25.0, "train"), save=False)
    return kb


# ------------------------------------------------------------------ the rules

def test_session_clear_walks_to_the_next_allowed_moment():
    cfg = BotConfig.model_validate({"session": {"blackout_windows": [{"start": "10:00", "end": "10:15", "label": "CPI"}]}})
    s = SessionSchedule(cfg.session)

    def local(t):
        return t.astimezone(CT).strftime("%a %Y-%m-%d %H:%M")

    assert local(session_clear(s, ct(2026, 3, 3, 7, 0))) == "Tue 2026-03-03 08:30"  # before the window
    assert local(session_clear(s, ct(2026, 3, 3, 9, 7))) == "Tue 2026-03-03 09:07"  # inside it: now
    assert local(session_clear(s, ct(2026, 3, 3, 10, 5))) == "Tue 2026-03-03 10:15"  # a blackout window ends
    assert local(session_clear(s, ct(2026, 3, 3, 14, 45))) == "Wed 2026-03-04 08:30"  # after the last entry
    assert local(session_clear(s, ct(2026, 3, 7, 12, 0))) == "Mon 2026-03-09 08:30"  # Saturday
    assert local(session_clear(s, ct(2026, 3, 6, 18, 0))) == "Mon 2026-03-09 08:30"  # Friday evening
    assert local(session_clear(s, ct(2026, 4, 2, 15, 0))) == "Mon 2026-04-06 08:30"  # Good Friday is a no-trade date


def test_next_allowed_follows_every_guard(tmp_path):
    core, now = make_core(tmp_path, at=(9, 0))
    assert next_allowed(core, now[0]) == (now[0], None)

    now[0] = ct(2026, 3, 3, 7, 45)
    at, why = next_allowed(core, now[0])
    assert at == ct(2026, 3, 3, 8, 30) and "before trade_start" in why

    now[0] = ct(2026, 3, 3, 9, 0)
    core.risk.last_loss_at = ct(2026, 3, 3, 8, 55)
    at, why = next_allowed(core, now[0])
    assert at == ct(2026, 3, 3, 9, 5) and why == "cooling down after a loss"
    core.risk.last_loss_at = None

    core.risk.trades_today = core.cfg.risk.max_trades_per_day
    at, why = next_allowed(core, now[0])
    assert at == ct(2026, 3, 4, 8, 30) and "max trades" in why  # the count resets tomorrow
    core.risk.trades_today = 0

    core.risk.lock("daily profit target reached")
    assert next_allowed(core, now[0])[0] == ct(2026, 3, 4, 8, 30)
    core.risk.lock_reason = None

    core.risk.paused = True
    at, why = next_allowed(core, now[0])
    assert at is None and "Resume" in why
    core.risk.paused = False
    core.halted = "KILL file found"
    at, why = next_allowed(core, now[0])
    assert at is None and "restart" in why


# ------------------------------------------------------------------ history

def test_estimate_from_first_signal_times():
    s = SessionSchedule(BotConfig().session)
    days = past_days(20)
    times = {d: ([9 * 60] if i % 2 else [13 * 60]) for i, d in enumerate(days)}  # half at 09:00, half at 13:00
    hist = SignalHistory(days, times, "test signals", 20)

    e = estimate(hist, s, ct(2026, 3, 3, 8, 30))
    assert e["p_today"] == 1.0 and e["q"] == 1.0 and e["per_day"] == 1.0
    assert e["at"] == ct(2026, 3, 3, 9, 0) and e["low"] == ct(2026, 3, 3, 9, 0) and e["high"] == ct(2026, 3, 3, 13, 0)

    e = estimate(hist, s, ct(2026, 3, 3, 10, 0))  # 09:00 has passed: only the 13:00 days are left today
    assert e["p_today"] == 0.5 and e["at"] == ct(2026, 3, 3, 13, 0)
    assert e["high"] == ct(2026, 3, 4, 9, 0)  # the rest spills into tomorrow

    sparse = SignalHistory(days, {d: [600] for d in days[:4]}, "rare", 4)  # 4 signal days in 20
    e = estimate(sparse, s, ct(2026, 3, 3, 11, 0))
    assert e["p_today"] == 0 and e["q"] == 0.2
    assert e["at"].astimezone(CT).date() > TUE and e["at"].astimezone(CT).time().hour == 10  # a few days out, at 10:00

    assert estimate(SignalHistory(days[:5], {}, "few", 0), s, ct(2026, 3, 3, 9, 0)) is None  # too little history
    assert estimate(SignalHistory(days, {}, "none", 0), s, ct(2026, 3, 3, 9, 0))["at"] is None  # never signals


def test_signal_history_counts_only_what_the_bot_would_trade(tmp_path):
    core, _ = make_core(tmp_path)
    days = past_days(12)
    kb = kb_with({d: ["08:00", "09:15", "14:45"] for d in days})  # 08:00 and 14:45 are outside the entry window
    for d in days:
        kb.record(Observation(d.isoformat(), "09:00", "ema_trend", "LONG", "open", "calm", 1.0, 50.0, "train"), save=False)
        kb.record(Observation(d.isoformat(), "09:05", "orb", "LONG", "open", "calm", 1.0, 50.0, "manual"), save=False)
    core.attach_knowledge(kb)
    hist = signal_history(core, TUE)
    assert hist.days == days and hist.signals == 12  # only orb's own 09:15 signals
    assert all(t == [9 * 60 + 15] for t in hist.times.values())
    assert signal_history(core, TUE) is hist  # cached until the knowledge base changes


def test_signal_history_for_the_adaptive_strategy_follows_the_knowledge_base(tmp_path):
    core, _ = make_core(tmp_path, strategy="adaptive")
    days = past_days(12)
    kb = KnowledgeBase(None, min_samples=4)
    for d in days:
        kb.record(Observation(d.isoformat(), "09:00", "orb", "LONG", "open", "calm", 1.0, 50.0, "train"), save=False)  # works
        kb.record(Observation(d.isoformat(), "08:45", "ema_trend", "LONG", "open", "calm", -1.0, -50.0, "train"), save=False)
    core.attach_knowledge(kb)
    hist = signal_history(core, TUE)
    assert hist.signals == 12 and all(t == [540] for t in hist.times.values())  # ema_trend loses: never counted
    assert "Adaptive" in hist.what


# ------------------------------------------------------------------ putting it together

def test_forecast_before_the_window_and_during_it(tmp_path):
    core, now = make_core(tmp_path, at=(7, 0))
    days = past_days(20)
    core.attach_knowledge(kb_with({d: ["09:00" if i % 2 else "13:00"] for i, d in enumerate(days)}))

    f = forecast(core)
    assert f["state"] == "closed" and f["earliest"] == ct(2026, 3, 3, 8, 30).isoformat()
    assert f["headline"] == "No trade before 08:30 CT. Next trade: most likely around 09:00 CT (in about 2 h 00 min), usually between 09:00 CT and 13:00 CT."
    assert f["estimate"]["days"] == 20 and f["estimate"]["at_local"] == "09:00 CT"
    assert any("20 trading days" in b for b in f["basis"]) and any("before trade_start" in b for b in f["basis"])

    now[0] = ct(2026, 3, 3, 10, 0)
    f = forecast(core)
    assert f["state"] == "waiting" and f["blocked"] is None
    assert f["headline"].startswith("Next trade: most likely around 13:00 CT (in about 3 h 00 min)")
    assert f["estimate"]["p_today"] == 0.5 and any("50% of those days" in b for b in f["basis"])
    text = forecast_text(f)
    assert text.startswith("⏳ Next trade") and "not a promise" in text

    now[0] = ct(2026, 3, 3, 15, 30)  # after the session: tomorrow
    f = forecast(core)
    assert f["earliest_local"] == "Wed 08:30 CT" and "Wed 09:00 CT" in f["headline"]


def test_forecast_in_a_trade_on_hold_and_without_history(tmp_path):
    core, now = make_core(tmp_path, at=(9, 0))
    f = forecast(core)
    assert f["estimate"] is None and "too little history" in f["headline"]
    assert any("knowledge base is turned off" in b for b in f["basis"])

    core.attach_knowledge(kb_with({d: ["10:00"] for d in past_days(3)}))
    f = forecast(core)
    assert any("Not enough history yet: 3 trading day(s)" in b for b in f["basis"])

    core.risk.paused = True
    f = forecast(core)
    assert f["state"] == "blocked" and "Resume" in f["headline"]
    core.risk.paused = False

    core.orders.position = 1
    assert forecast(core)["state"] == "in_trade"
    core.orders.position = 0


def test_forecast_mentions_the_setup_closest_to_firing(tmp_path):
    from topstep_bot.strategies.base import Setup

    core, now = make_core(tmp_path, at=(9, 50))
    core.attach_knowledge(kb_with({d: ["10:30"] for d in past_days(15)}))

    class Stub:
        name, title, subs = "orb", "Opening Range Breakout", None

        def setups(self, price, when):
            return [Setup("long", [("Price above the band", True), ("Still there at a checkpoint close (next 10:00 CT)", False)],
                          stop=90.0, at=datetime(2026, 3, 3, 10, 0).time())]

    core.strategy.setups = Stub().setups
    core.last_price = 100.0
    f = forecast(core)
    assert f["setup"]["title"] == "Opening Range Breakout" and f["setup"]["at_local"] == "10:00 CT"
    assert f["setup"]["waiting_for"] == ["Still there at a checkpoint close (next 10:00 CT)"]
    assert f["state"] == "ready" and "one condition away" in f["headline"]
    assert any("can fire at 10:00 CT" in b for b in f["basis"])


def test_wait_text():
    assert [wait_text(s) for s in (30, 8 * 60, 65 * 60, 2 * 86400 + 3 * 3600)] == ["now", "8 min", "1 h 05 min", "2 days 3 h"]


def test_snapshot_and_actions_carry_the_forecast_and_the_brief(tmp_path):
    core, now = make_core(tmp_path, strategy="adaptive", at=(9, 0))
    days = past_days(15)
    kb = KnowledgeBase(None, min_samples=4)
    for d in days:
        kb.record(Observation(d.isoformat(), "09:30", "orb", "LONG", "open", "calm", 0.8, 40.0, "train",
                              mfe_r=1.2, mae_r=-0.4, cost_r=0.05), save=False)
        kb.record(Observation(d.isoformat(), "09:40", "ema_trend", "SHORT", "open", "calm", -0.6, -30.0, "train",
                              mfe_r=1.1, mae_r=-1.0, cost_r=0.05), save=False)
    core.attach_knowledge(kb)
    snap = core.snapshot()
    assert snap["forecast"]["estimate"]["at_local"] == "09:30 CT" and snap["memory"] is None

    brief = build_brief(core)
    titles = [s["title"] for s in brief["sections"]]
    assert titles[0] == "My memory" and titles[1].startswith("Right now (open session, calm market)")
    assert titles[-1] == "My next trade"
    text = brief_text(brief)
    assert "I know the outcome of 30 trade signals" in text
    assert "I would trade: Opening Range Breakout" in text and "I'm staying out of: EMA Trend Crossover" in text
    assert "Opening Range Breakout: +0.75R per signal after costs over 15 (100% won)" in text
    assert "100% of EMA Trend Crossover's losing signals were 1R in profit first" in text
    assert "most likely around 09:30 CT" in text

    actions = BotActions(core, Controls())
    assert run(actions.handle("brief", {}))["brief"]["sections"][0]["title"] == "My memory"
    assert "What I know" in run(actions.handle("brief_text", {}))["text"]
    assert run(actions.handle("next_text", {}))["text"].startswith("⏳ Next trade: most likely around 09:30 CT")
    assert "⏳ Next trade" in run(actions.handle("status_text", {}))["text"]
    with pytest.raises(RuntimeError, match="only available while the bot is connected"):
        run(actions.handle("learn", {}))


def test_brief_without_knowledge(tmp_path):
    core, _ = make_core(tmp_path, at=(9, 0), knowledge={"enabled": False, "deep_learning": False})
    text = brief_text(build_brief(core))
    assert "turned off" in text and "I trade Opening Range Breakout." in text and "long-run memory is off" in text
