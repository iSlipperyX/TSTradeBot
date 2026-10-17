"""Version 4.2: the chart's trend reading, the trend filter's evidence test, and the day plan (goal + route)."""

import json
import random
from datetime import timedelta

import httpx
import pytest

from topstep_bot.config import BotConfig, TelegramConfig
from topstep_bot.day_plan import (
    DayPlanner,
    Frame,
    entry_for,
    goal_day,
    load_goals,
    parse_goals,
    plan_text,
    question,
    resolve,
    save_goal,
    suggest,
)
from topstep_bot.indicators import ADX
from topstep_bot.knowledge import KnowledgeBase, Observation
from topstep_bot.models import Bar, OrderSide, Signal
from topstep_bot.trend import TrendReader, against, view_text, with_trend
from topstep_bot.trend_lab import MIN_SKIPPED, blocks, choose, resolve_filter

from .conftest import bar, ct, run
from .test_forecast import TUE, make_core, past_days


def feed(reader: TrendReader, drift: float, n: int = 90, seed: int = 1, start=None, noise: float = 4.0,
         price: float = 20_000.0) -> list[Bar]:
    """``n`` 5-minute bars drifting ``drift`` points a bar, through ``reader``."""
    rnd = random.Random(seed)
    t, bars = start or ct(2026, 3, 3, 6, 0), []
    for _ in range(n):
        o, c = price, price + drift + rnd.gauss(0, noise)
        b = bar(t, o, max(o, c) + abs(rnd.gauss(0, 2)), min(o, c) - abs(rnd.gauss(0, 2)), c)
        reader.update(b, t + timedelta(minutes=5), t.date(), rth=True)
        bars.append(b)
        price, t = c, t + timedelta(minutes=5)
    return bars


def obs(day, time, side, net_r, tscore=None, tadx=None, strategy="orb") -> Observation:
    ctx = {} if tscore is None else {"tscore": tscore, "tadx": tadx}
    return Observation(day, time, strategy, side, "open", "calm", net_r, net_r * 150, "train", ctx=ctx, bars=6)


# ======================================================================== the trend on the chart

def test_adx_rises_in_a_steady_trend_and_says_which_way():
    adx = ADX(14)
    for i in range(60):
        adx.update(100 + 2 * i + 1, 100 + 2 * i - 1, 100 + 2 * i)
    assert adx.ready and adx.value > 40 and adx.plus_di > adx.minus_di
    down = ADX(14)
    for i in range(60):
        down.update(100 - 2 * i + 1, 100 - 2 * i - 1, 100 - 2 * i)
    assert down.minus_di > down.plus_di


def test_trend_reader_calls_up_down_and_sideways():
    up, down, flat = TrendReader(), TrendReader(), TrendReader()
    feed(up, 6)
    feed(down, -6)
    feed(flat, 0, noise=6, seed=4)
    assert up.last.direction == "up" and up.last.score > 0.6 and up.last.strong
    assert down.last.direction == "down" and down.last.score < -0.6
    assert flat.last.direction == "sideways" and abs(flat.last.score) < 0.35
    assert set(up.last.parts) == {"slope", "averages", "swings", "today"}
    # in plain words, with the reasons
    assert "up" in " ".join(up.last.words()).lower()
    # features saved with every signal's market snapshot
    f = up.features(20_400)
    assert f["tscore"] == pytest.approx(up.last.score, abs=0.01) and f["tadx"] > 25
    # the chart overlay: a fitted line and the swing points
    c = up.chart()
    assert c["direction"] == "up" and c["to"][1] > c["from"][1] and c["band"] >= 0
    assert all(kind in ("high", "low") for _, _, kind in c["swings"])


def test_trend_direction_needs_a_clear_move_to_flip():
    r = TrendReader()
    last = feed(r, 6, n=80)[-1]
    assert r.last.direction == "up"
    since = r.last.since
    feed(r, 0.5, n=3, seed=9, start=last.ts + timedelta(minutes=5), price=last.close)  # a short pause: still the same uptrend
    assert r.last.direction == "up" and r.last.since == since


def test_trend_calls_are_checked_an_hour_later():
    r = TrendReader()
    feed(r, 5, n=150)
    s = r.calls.summary()
    assert s["all"]["n"] > 20 and s["all"]["followed"] > 0.8


def test_with_and_against_the_trend():
    assert against(-0.7, "LONG", 0.6) and against(0.7, "SHORT", 0.6)
    assert not against(-0.5, "LONG", 0.6) and not against(0.7, "LONG", 0.6) and not against(None, "LONG", 0.6)
    assert with_trend({"tscore": 0.5}, "LONG") == "with" and with_trend({"tscore": 0.5}, "SHORT") == "against"
    assert with_trend({"tscore": 0.1}, "LONG") == "none" and with_trend({}, "LONG") == "unknown"


def test_view_text_is_honest_without_a_reading():
    assert "not enough bars" in view_text({"reading": None, "calls_text": "", "history": {}, "filter": {}})
    assert "doesn't" not in view_text(None) and "isn't running" in view_text(None)


# ======================================================================== the trend filter and its test

def test_filter_blocks_only_against_a_strong_trend():
    assert blocks(obs("2026-03-02", "09:00", "LONG", -1, tscore=-0.8, tadx=30))
    assert not blocks(obs("2026-03-02", "09:00", "LONG", -1, tscore=-0.8, tadx=15))  # not strong
    assert not blocks(obs("2026-03-02", "09:00", "SHORT", -1, tscore=-0.8, tadx=30))  # with the trend
    assert not blocks(obs("2026-03-02", "09:00", "LONG", -1))  # no reading saved: never blocked


def test_trend_test_turns_the_filter_on_only_with_evidence():
    good = {"days": 80, "diff_r": 12.0, "p_better": 0.98}
    assert choose({}, good, days_with_reading=80, skipped=40)[0] is True
    on, why = choose({}, good, days_with_reading=40, skipped=40)
    assert on is False and "too little" in why
    on, why = choose({}, good, days_with_reading=80, skipped=MIN_SKIPPED - 1)
    assert on is False and "skipped only" in why
    on, why = choose({}, {"days": 80, "diff_r": 3.0, "p_better": 0.80}, days_with_reading=80, skipped=40)
    assert on is False and "not convincing" in why
    assert choose({}, {"days": 80, "diff_r": -5.0, "p_better": 0.99}, 80, 40)[0] is False


def test_filter_setting_overrides_the_test(tmp_path):
    cfg = BotConfig.model_validate({"data_dir": str(tmp_path)})
    on, why = resolve_filter(cfg, None)
    assert on is False and "hasn't run yet" in why  # auto, no test yet: off
    assert resolve_filter(cfg, {"ok": True, "on": True, "why": "made more"}) == (True, "automatic: made more")
    cfg.trend.filter = "on"
    assert resolve_filter(cfg, {"ok": True, "on": False, "why": "x"})[0] is True
    cfg.trend.filter = "off"
    assert resolve_filter(cfg, {"ok": True, "on": True, "why": "x"})[0] is False


def test_engine_skips_a_signal_against_a_strong_trend_only_when_the_filter_is_on(tmp_path):
    core, now = make_core(tmp_path, at=(2, 0), trend={"min_score": 0.5})  # overnight: no VWAP part in the score
    t, price, rnd = ct(2026, 3, 3, 2, 0), 20_000.0, random.Random(2)
    while t < ct(2026, 3, 3, 8, 25):  # a strong downtrend, before the open (no strategy trades it)
        now[0] = t + timedelta(minutes=5)
        c = price - 7 + rnd.gauss(0, 12)  # with pullbacks, as real trends have
        run(core.on_bar(bar(t, price, max(price, c) + 2, min(price, c) - 2, c)))
        price, t = c, t + timedelta(minutes=5)
    assert core.trend.last.direction == "down" and core.trend.last.score < -0.5 and core.trend.last.strength > 25
    assert core.trend_filter[0] is False and core.trend_block(OrderSide.BUY) is None  # auto: off until proven
    snap = core.market_snapshot(price)
    assert snap["tscore"] < -0.5 and "tadx" in snap  # every signal's snapshot carries the reading
    core.set_trend_filter(True, "test")
    assert "trend filter" in core.trend_block(OrderSide.BUY) and core.trend_block(OrderSide.SELL) is None
    t = now[0] = ct(2026, 3, 3, 8, 45)
    b = bar(t, price, price + 3, price - 3, price)
    core.last_price = price
    assert core.orders.trade is None
    run(core._handle_entry(Signal("long", stop_price=price - 40, reason="t"), b, core.context(b)))
    assert core.orders.trade is None and any("trend filter" in e["message"] for e in core.events)
    view = core.trend_view()
    assert view["reading"]["direction"] == "down" and view["filter"]["on"] is True
    assert core.chart()["trend"]["direction"] == "down"


# ======================================================================== goals

def test_parse_goals():
    accounts = ["c1", "c2"]
    assert parse_goals("400", accounts) == {None: 400.0}
    assert parse_goals("$1.2k", []) == {None: 1200.0}
    assert parse_goals("c1 400 c2 $1,200", accounts) == {"c1": 400.0, "c2": 1200.0}
    assert parse_goals("c1: 400, c2: none", accounts) == {"c1": 400.0, "c2": None}
    assert parse_goals("400 300", accounts) == {"c1": 400.0, "c2": 300.0}  # in account order
    assert parse_goals("no goal", []) == {None: None}
    assert parse_goals("all 250", accounts) == {None: 250.0}
    for text in ("why did you buy?", "400 c2 300", "c1", "-50", "", "c1 400 c2 300 c3 9"):
        assert parse_goals(text, accounts) is None, text


def frame(**kw) -> Frame:
    base = {"stage": "combine", "plan": "50K", "path": "standard", "profit_target": 3000.0, "remaining": 1500.0,
            "cap": 1200.0, "cap_why": "the bot's Combine day cap", "risk_usd": 150.0, "daily_loss": 500.0,
            "max_trades": 4, "max_losses": 2, "cooldown": 10, "passed": False, "made": 1500.0}
    base.update(kw)
    return Frame(**base)


def test_suggested_goal_spreads_what_is_left_and_respects_the_cap():
    s = suggest(frame(), 10)
    assert s["suggested"] == 150 and s["options"][1] == 150 and "1,500 left" in s["why"]
    assert all(o <= 1200 for o in s["options"])
    tight = suggest(frame(remaining=3000.0, cap=200.0), 3)
    assert tight["suggested"] <= 200 and "capped" in tight["why"]
    assert suggest(frame(remaining=0.0, passed=True), 10)["suggested"] is None
    xfa = suggest(frame(stage="express", remaining=None, cap=None), 10)
    assert xfa["suggested"] >= 200  # a winning day ($150+) with a cushion


def test_goals_are_saved_per_day_and_the_last_one_carries_over(tmp_path):
    cfg = BotConfig.model_validate({"data_dir": str(tmp_path)})
    f, sug = frame(), suggest(frame(), 10)
    g = resolve(cfg, load_goals(cfg), "2026-03-03", f, sug)
    assert (g.kind, g.amount) == ("suggested", 150)
    save_goal(cfg, "2026-03-02", 400, "test")
    g = resolve(cfg, load_goals(cfg), "2026-03-03", f, sug)
    assert (g.kind, g.amount, g.from_day) == ("last", 400, "2026-03-02")
    cfg.day_plan.unanswered = "suggested"
    assert resolve(cfg, load_goals(cfg), "2026-03-03", f, sug).kind == "suggested"
    save_goal(cfg, "2026-03-03", 5000, "test")
    g = resolve(cfg, load_goals(cfg), "2026-03-03", f, sug)
    assert (g.kind, g.amount, g.asked, g.capped) == ("set", 1200, 5000, True)  # never above what a day may make
    save_goal(cfg, "2026-03-03", None, "test")
    assert resolve(cfg, load_goals(cfg), "2026-03-03", f, sug).kind == "off"
    with pytest.raises(ValueError):
        save_goal(cfg, "2026-03-03", -5, "test")
    assert json.loads((tmp_path / "day_goals.json").read_text())["days"]["2026-03-03"]["goal"] is None


def test_goal_day_moves_to_the_next_trading_day_after_the_close(tmp_path):
    cfg = BotConfig.model_validate({"data_dir": str(tmp_path)})
    assert goal_day(cfg, ct(2026, 3, 3, 9, 0)) == "2026-03-03"
    assert goal_day(cfg, ct(2026, 3, 3, 16, 0)) == "2026-03-04"
    assert goal_day(cfg, ct(2026, 3, 6, 16, 0)) == "2026-03-09"  # Friday evening: Monday


# ======================================================================== the goal in the risk manager

def test_a_goal_lowers_the_day_target_never_raises_it_and_stops_new_trades(tmp_path):
    core, now = make_core(tmp_path, at=(9, 0))
    risk = core.risk
    base = risk.daily_profit_target
    assert base == 1200  # the 50K Combine's day cap
    core.risk.book.goal = 5000
    assert risk.daily_profit_target == base  # a goal never raises it
    core.risk.book.goal, core.risk.book.goal_text = 300, "your goal"
    assert risk.daily_profit_target == 300 and risk.day_cap() == 1200
    core.balance = 50_310
    reason = risk.entry_block_reason(now[0], core.balance)
    assert "today's goal $300 reached" in str(reason)
    assert risk.lock_reason and risk.lock_reason.startswith("today's goal")


def test_the_planner_applies_the_goal_and_a_new_goal_lifts_the_old_stop(tmp_path):
    core, now = make_core(tmp_path, at=(9, 0))
    planner = DayPlanner(core.cfg, "T")
    core.planner = planner
    save_goal(core.cfg, "2026-03-03", 300, "test")
    planner.reload()
    goal = planner.apply(core, core.risk.book)
    assert goal.amount == 300 and core.risk.book.goal == 300
    assert any("Today's goal: $300" in e["message"] for e in core.events)
    core.risk.lock("today's goal $300 reached (your goal) - done for the day, keeping it")
    save_goal(core.cfg, "2026-03-03", 600, "test")
    planner.reload()
    planner.apply(core, core.risk.book)
    assert core.risk.book.goal == 600 and core.risk.lock_reason is None
    # stop_at_goal off: the goal is planned but stops nothing
    core.cfg.day_plan.stop_at_goal = False
    planner.reload(force=True)
    planner.apply(core, core.risk.book)
    assert core.risk.book.goal is None and core.risk.daily_profit_target == 1200


# ======================================================================== the route

def plan_core(tmp_path, at=(7, 50), goal=400):
    core, now = make_core(tmp_path, at=at)
    rnd = random.Random(3)
    kb = KnowledgeBase(None, min_samples=2)
    for d in past_days(40):
        for t in rnd.sample(["08:45", "09:10", "09:40", "10:30", "11:15", "13:20", "14:05"], 3):
            r = rnd.choice([-1, -1, 1.5, 2.0, -1, 1.2])
            kb.record(obs(d.isoformat(), t, rnd.choice(["LONG", "SHORT"]), r, tscore=rnd.uniform(-1, 1), tadx=30),
                      save=False)
    core.attach_knowledge(kb)
    core.planner = DayPlanner(core.cfg, "T")
    if goal is not None:
        save_goal(core.cfg, TUE.isoformat(), goal, "test")
    core.planner.reload()
    core.planner.apply(core, core.risk.book)
    return core, now


def test_the_plan_replays_past_days_under_todays_rules(tmp_path):
    core, now = plan_core(tmp_path)
    p = core.plan_view()
    assert p["goal"]["amount"] == 400 and p["goal"]["kind"] == "set" and p["answered"]
    assert p["goal_r"] == pytest.approx(400 / 150, abs=0.01)
    o = p["odds"]
    assert o["days"] >= 35 and 0 <= o["reach"] <= 1 and o["reach_n"] == round(o["reach"] * o["days"])
    assert 0 <= o["green"] <= 1 and 0 <= o["red"] <= 1
    assert [w["name"] for w in p["windows"]] == ["Open", "Midday", "Close"]
    assert sum(w["per_day"] for w in p["windows"]) <= 4 + 1e-9  # never more than the day's trade limit
    titles = [s["title"] for s in p["steps"]]
    assert titles[0].startswith("Goal: $400") and titles[-1] == "Finish"
    assert any("Nothing is sized bigger" in s for s in p["stops"])
    assert "Goal $400" in p["headline"] and "past days" in p["headline"]
    text = plan_text(p)
    assert "Today's plan" in text and "not a promise" in text and "Stops:" in text


def test_a_higher_goal_is_reached_on_fewer_past_days(tmp_path):
    low = plan_core(tmp_path / "a", goal=150)[0].plan_view()["odds"]["reach"]
    high = plan_core(tmp_path / "b", goal=1000)[0].plan_view()["odds"]["reach"]
    assert low >= high


def test_without_history_the_plan_has_no_odds_but_still_the_stops(tmp_path):
    core, now = make_core(tmp_path, at=(7, 50))
    core.planner = DayPlanner(core.cfg, "T")
    core.planner.apply(core, core.risk.book)
    p = core.plan_view()
    assert p["odds"] is None and p["stops"] and "Not enough history" in p["headline"]
    assert p["goal"]["kind"] == "suggested" and not p["answered"]


def test_the_assistant_sees_the_plan_and_the_trend(tmp_path):
    core, now = plan_core(tmp_path)
    brief = core.plan_brief()
    assert brief["goal_usd"] == 400 and "made_today_usd" in brief


# ======================================================================== the morning question

def test_morning_question_has_buttons_per_account_and_says_the_goal_never_raises_risk(tmp_path):
    cfg = BotConfig.model_validate({"data_dir": str(tmp_path)})
    e1 = entry_for(cfg, "c1", None, "2026-03-03")
    e2 = {**e1, "account": "c2"}
    text, kb = question([e1, e2], multi=True, ask_by="08:30", unanswered="last")
    assert "each account" in text and "never makes a trade bigger" in text and "not a promise" in text
    data = [b["callback_data"] for row in kb["inline_keyboard"] for b in row]
    assert "goal:c1:none" in data and "goal:c2:none" in data and "goal:*:suggested" in data
    assert any(d.startswith("goal:c1:") and d[8:].replace(".", "").isdigit() for d in data)
    assert all(len(d.encode()) <= 64 for d in data)  # Telegram's limit for callback data


def test_goal_from_telegram_and_dashboard_with_the_bots_stopped(tmp_path):
    from .test_controller import until  # noqa: F401 - same helpers as the controller tests
    from .test_multi_account import multi_scenario
    from .test_telegram_control import CHAT, FakeTelegram, button, msg

    async def body(ctl, http):
        from topstep_bot.controller import ProxyActions
        from topstep_bot.telegram_control import TelegramController

        fake = FakeTelegram()
        tg = TelegramController("TOKEN", CHAT, ProxyActions(ctl), TelegramConfig(), transport=httpx.MockTransport(fake.handler))
        day = goal_day(ctl.account_cfg("c1"))
        await tg.handle_update(msg("/goal c1 400 c2 1200"))
        assert "c1" in fake.sent[-1]["text"] and "never makes a trade bigger" in fake.sent[-1]["text"]
        assert load_goals(ctl.account_cfg("c1"))["days"][day]["goal"] == 400
        assert load_goals(ctl.account_cfg("c2"))["days"][day]["goal"] == 1200
        await tg.handle_update(msg("/goal all 250"))
        assert all(load_goals(ctl.account_cfg(n))["days"][day]["goal"] == 250 for n in ctl.bots)
        await tg.handle_update(button("goal:c3:none", 1))
        assert load_goals(ctl.account_cfg("c3"))["days"][day]["goal"] is None
        await tg.handle_update(msg("/goal"))  # the question with buttons
        assert fake.sent[-1]["reply_markup"]["inline_keyboard"]
        await tg.handle_update(msg("/goal lots"))
        assert "Usage" in fake.sent[-1]["text"]
        # typed answers count only while the morning question is open
        ctl.plans.waiting = (day, ctl.plans.waiting[1] if ctl.plans.waiting else ct(2030, 1, 1, 0, 0))
        await tg.handle_update(msg("c1 500"))
        assert load_goals(ctl.account_cfg("c1"))["days"][day]["goal"] == 500
        ctl.plans.waiting = None
        # the dashboard: works with the bot stopped
        r = await http.post("/api/goal", json={"account": "c2", "goal": 700})
        assert r.status_code == 200 and "c2" in r.json()["message"]
        assert load_goals(ctl.account_cfg("c2"))["days"][day]["goal"] == 700
        goals = (await http.post("/api/goals", json={})).json()["goals"]
        assert {g["account"]: g["goal"] for g in goals}["c2"] == 700
        r = await http.post("/api/goal", json={"account": "c2", "goal": "lots"})
        assert r.json()["ok"] is False
        # /plan with the bot stopped: the goal, and that the route comes once it runs
        await tg.handle_update(msg("/plan c1"))
        assert "500" in fake.sent[-1]["text"] and "once the bot is running" in fake.sent[-1]["text"]
        await tg.close()

    multi_scenario(tmp_path, body)


def test_the_morning_question_is_sent_once_on_a_trading_morning(tmp_path):
    from .test_controller import scenario
    from .test_telegram_control import CHAT, FakeTelegram

    async def body(ctl, http):
        from topstep_bot.controller import ProxyActions
        from topstep_bot.telegram_control import TelegramController

        fake = FakeTelegram()
        ctl.telegram = TelegramController("TOKEN", CHAT, ProxyActions(ctl), TelegramConfig(),
                                          transport=httpx.MockTransport(fake.handler))
        assert await ctl.plans.ask_if_due(ct(2026, 3, 3, 7, 30)) is False  # before ask_at
        assert await ctl.plans.ask_if_due(ct(2026, 3, 7, 9, 0)) is False  # Saturday
        assert await ctl.plans.ask_if_due(ct(2026, 3, 3, 8, 5)) is True
        assert "Good morning" in fake.sent[-1]["text"] and ctl.plans.waiting[0] == "2026-03-03"
        assert await ctl.plans.ask_if_due(ct(2026, 3, 3, 8, 6)) is False  # once a day
        assert ctl.plans.awaiting(ct(2026, 3, 3, 8, 10)) and not ctl.plans.awaiting(ct(2026, 3, 3, 15, 0))
        save_goal(ctl.cfg, "2026-03-04", 300, "test")  # already answered for tomorrow: nothing to ask
        assert await ctl.plans.ask_if_due(ct(2026, 3, 4, 8, 5)) is False
        ctl.cfg.day_plan.ask = False
        assert await ctl.plans.ask_if_due(ct(2026, 3, 5, 8, 5)) is False
        await ctl.telegram.close()
        ctl.telegram = None

    scenario(tmp_path, body)


# ======================================================================== settings and answers

def test_setup_tab_saves_the_day_plan_and_trend_settings(tmp_path):
    from .test_controller import scenario

    async def body(ctl, http):
        r = await http.post("/api/setup/day_plan", json={"ask": True, "ask_at": "07:45", "unanswered": "suggested",
                                                         "stop_at_goal": False, "days_to_target": 15,
                                                         "trend_filter": "off"})
        assert r.status_code == 200, r.text
        assert "07:45" in r.json()["message"] and "trend filter off" in r.json()["message"]
        cfg = ctl.cfg
        assert (cfg.day_plan.ask_at.strftime("%H:%M"), cfg.day_plan.unanswered, cfg.day_plan.stop_at_goal,
                cfg.day_plan.days_to_target, cfg.trend.filter) == ("07:45", "suggested", False, 15, "off")
        assert "day_plan:" in (tmp_path / "config.yaml").read_text()
        bad = await http.post("/api/setup/day_plan", json={"days_to_target": 500})
        assert bad.json()["ok"] is False and "Not saved" in bad.json()["message"]
        state = (await http.post("/api/setup/state", json={})).json()
        assert state["values"]["day_plan"]["ask_at"] == "07:45" and state["values"]["trend"]["filter"] == "off"

    scenario(tmp_path, body)


def test_ask_answers_about_the_goal_and_the_trend(tmp_path):
    from topstep_bot.brain.facts import TOOLS, Facts

    assert "trend" in TOOLS and "day_plan" in TOOLS
    cfg = BotConfig.model_validate({"data_dir": str(tmp_path)})
    offline = Facts(cfg, offline="The bot is stopped.")
    assert "goal for" in offline.call("day_plan")["text"] and "start the bot" in offline.call("trend")["text"]
    core, now = plan_core(tmp_path / "running")
    from topstep_bot.control import BotActions
    from topstep_bot.live import Controls

    facts = Facts(core.cfg, core=core, actions=BotActions(core, Controls()))
    plan = facts.call("day_plan")
    assert plan["goal"]["amount"] == 400 and "Today's plan" in plan["text"]
    assert "Trend" in facts.call("trend")["text"]

