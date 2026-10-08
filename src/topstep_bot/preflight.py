"""Preflight: everything the bot must know before its first live trade, checked automatically.

Run it before going live (menu "Start trading today", or `topstep-bot preflight`). It verifies
the connection and account, teaches the bot the account's current Maximum Loss Limit, checks
the account is flat, the contract, live data, your PC clock, today's calendar and news, alerts,
risk settings, and backtests every strategy on the most recent real market data.
"""

from __future__ import annotations

import asyncio
import email.utils
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from topstep_bot.config import BotConfig, Secrets
from topstep_bot.factory import starting_balance
from topstep_bot.instruments import SPECS, offline_contract
from topstep_bot.risk.guards import api_trading_block, hosting_warning
from topstep_bot.risk.topstep import PLANS, LossLimitTracker, max_contracts_allowed, product_limit

UTC = timezone.utc
OK, WARN, FAIL, INFO = "ok", "warn", "fail", "info"


@dataclass
class Check:
    status: str
    name: str
    detail: str


@dataclass
class PreflightReport:
    checks: list[Check] = field(default_factory=list)
    strategy_rows: list[dict] = field(default_factory=list)
    account_name: str | None = None

    def add(self, status: str, name: str, detail: str) -> None:
        self.checks.append(Check(status, name, detail))

    @property
    def failures(self) -> int:
        return sum(1 for c in self.checks if c.status == FAIL)

    @property
    def warnings(self) -> int:
        return sum(1 for c in self.checks if c.status == WARN)

    @property
    def verdict(self) -> str:
        if self.failures:
            return "NOT READY"
        return "READY WITH WARNINGS" if self.warnings else "READY"


def account_hints(name: str) -> tuple[str | None, str | None]:
    """Guess (plan, stage) from a TopstepX account name such as '50KTC-V2-123' or 'XFA-150K-...'."""
    upper = name.upper()
    plan = next((p for p in ("150K", "100K", "50K") if p in upper), None)
    stage = None
    if re.search(r"XFA|EXPRESS|\bEF\b|FUNDED", upper):
        stage = "express"
    elif re.search(r"PRAC", upper):
        stage = "practice"
    elif re.search(r"\d+KTC|\bTC\b|COMBINE", upper):
        stage = "combine"
    return plan, stage


def _money(v: float) -> str:
    return f"-${abs(v):,.2f}" if v < 0 else f"${v:,.2f}"


async def run_preflight(
    cfg: BotConfig,
    secrets: Secrets,
    *,
    ask_mll=None,
    backtest_days: int = 90,
    run_backtests: bool = True,
    progress=None,
    client=None,
) -> PreflightReport:
    """Run every check. ``ask_mll(prompt) -> str`` lets an interactive caller teach the bot the MLL."""
    from topstep_bot.api.rest import ProjectXClient
    from topstep_bot.journal import Journal
    from topstep_bot.live import SetupError, resolve_contract, select_account
    from topstep_bot.models import BarUnit
    from topstep_bot.sessions import SessionSchedule, session_open_for

    rep = PreflightReport()
    step = progress or (lambda msg: None)
    plan = PLANS[cfg.account.plan]
    schedule = SessionSchedule(cfg.session)
    now = datetime.now(UTC)

    # ---- risk settings (no network needed)
    mll = plan.max_loss_limit
    if cfg.risk.risk_per_trade > 0.15 * mll:
        rep.add(WARN, "Risk per trade", f"{_money(cfg.risk.risk_per_trade)} is over 15% of your {_money(mll)} MLL - a few losses could end the account")
    else:
        rep.add(OK, "Risk per trade", f"{_money(cfg.risk.risk_per_trade)} ({cfg.risk.risk_per_trade / mll:.0%} of the MLL)")
    if cfg.risk.personal_daily_loss_limit > 0.5 * mll:
        rep.add(WARN, "Daily loss limit", f"{_money(cfg.risk.personal_daily_loss_limit)} is over half your MLL")
    else:
        rep.add(OK, "Daily loss limit", f"{_money(cfg.risk.personal_daily_loss_limit)} per day")
    # ---- Topstep rules that depend on this computer and the settings
    hosted = hosting_warning()
    if hosted:
        rep.add(WARN, "Your computer", f"{hosted}. Topstep requires automated trading to run from your own personal "
                "computer - a VPS, VPN or remote server can get the account removed. Ignore this if it is your own PC")
    else:
        rep.add(OK, "Your computer", "looks like a personal computer (Topstep prohibits VPS/VPN/remote servers)")
    dll = cfg.account.topstep_daily_loss_limit
    if dll is not None:
        rep.add(OK, "Topstep Daily Loss Limit", f"{_money(dll)}: the bot stops new trades at 90% of it and closes trades "
                f"at 95%; your own limit {_money(cfg.risk.personal_daily_loss_limit)} comes first")
    else:
        rep.add(INFO, "Topstep Daily Loss Limit", "none configured. If you added one at checkout, set "
                "account.topstep_daily_loss_limit: true")
    if cfg.account.stage == "combine":
        cap = cfg.risk.daily_profit_target or 0.4 * plan.profit_target
        rep.add(OK if cap < plan.consistency_day_limit else WARN, "Consistency Target",
                f"best day must stay at or below {_money(plan.consistency_day_limit)} (55% of the target); the bot stops "
                f"opening trades at {_money(cap)}" + (" and closes out at 50%" if cfg.risk.consistency_guard else
                                                       " (consistency_guard is OFF)"))
    root = cfg.instrument.symbol
    spec = SPECS.get(root)
    if spec is None:
        rep.add(WARN, "Product", f"{root} has no built-in spec (fees, trading hours) - double-check it is allowed on Topstep")
    else:
        cap_now = max_contracts_allowed(plan, cfg.account.stage, starting_balance(cfg), root, offline_contract(root).is_micro)
        extra = f" (Topstep's product limit for {root})" if product_limit(root, plan) is not None else ""
        rep.add(OK, "Position limit", f"at most {cap_now} {root} contract(s){extra}"
                + (" - grows with the Scaling Plan" if cfg.account.stage == "express" else ""))

    if cfg.risk.ramp_up_days:
        rep.add(INFO, "Ramp-up", f"first {cfg.risk.ramp_up_days} live day(s) on a new account risk "
                f"{cfg.risk.ramp_up_risk_fraction:.0%} of normal ({_money(cfg.risk.risk_per_trade * cfg.risk.ramp_up_risk_fraction)}/trade)")
    else:
        rep.add(WARN, "Ramp-up", "off - the bot trades full size from its first live trade")

    if not secrets.has_credentials:
        rep.add(FAIL, "TopstepX credentials", "missing - run setup")
        return rep

    own_client = client is None
    client = client or ProjectXClient(secrets.username, secrets.api_key, cfg.api.base_url, cfg.api.timeout_seconds)
    try:
        # ---- login & account
        step("Logging in")
        try:
            account = await select_account(client, cfg)
        except SetupError as exc:
            rep.add(FAIL, "Account", str(exc))
            return rep
        except Exception as exc:  # noqa: BLE001
            rep.add(FAIL, "Login", str(exc))
            return rep
        rep.account_name = account.name
        rep.add(OK, "Login", f"connected as {secrets.username}")
        blocked = api_trading_block(account)
        if blocked:
            rep.add(FAIL, "Account type", blocked)
            return rep
        rep.add(OK if account.can_trade else FAIL, "Account", f"{account.name} (id {account.id}), balance {_money(account.balance)}"
                + ("" if account.can_trade else " - NOT allowed to trade right now"))
        hint_plan, hint_stage = account_hints(account.name)
        mismatch = []
        if hint_plan and hint_plan != cfg.account.plan:
            mismatch.append(f"plan looks like {hint_plan}")
        if hint_stage and hint_stage != cfg.account.stage:
            mismatch.append(f"type looks like {hint_stage}")
        if "DLL" in account.name.upper() and cfg.account.topstep_daily_loss_limit is None:
            rep.add(WARN, "Topstep Daily Loss Limit", f"the account name suggests a Topstep DLL of {_money(plan.daily_loss_limit)}; "
                    "the bot will enforce it - set account.topstep_daily_loss_limit: true to make that explicit")
        if mismatch:
            rep.add(WARN, "Account type", f"config says {cfg.account.plan} {cfg.account.stage}, but the account name suggests "
                    + " and ".join(mismatch) + " - fix account.plan / account.stage")
        else:
            rep.add(OK, "Account type", f"{cfg.account.plan} {cfg.account.stage}")

        # ---- teach the bot the Maximum Loss Limit
        start = starting_balance(cfg)
        label = account.name
        journal = Journal(Path(cfg.data_dir) / "journal_live.db")
        try:
            floor = cfg.account.mll_floor_override
            source = "config (mll_floor_override)"
            if floor is None:
                floor = journal.get_state(f"mll_floor:{label}")
                source = "learned earlier"
            if floor is None and abs(account.balance - start) < 0.01:
                floor = start - mll
                source = "fresh account"
            if floor is None and ask_mll is not None:
                answer = ask_mll(
                    f"This account's balance ({_money(account.balance)}) differs from the starting balance, so the bot "
                    f"can't work out the Maximum Loss Limit itself.\nEnter the MLL shown in your Topstep dashboard "
                    f"(e.g. {start - mll + 500:,.0f}), or press Enter to skip"
                )
                try:
                    floor = float(str(answer).replace("$", "").replace(",", "").strip()) if answer else None
                except ValueError:
                    floor = None
                if floor is not None:
                    journal.set_state(f"mll_floor:{label}", floor)
                    source = "entered now (saved)"
            if floor is None:
                rep.add(WARN, "Max Loss Limit", "unknown - the bot will estimate it from the balance. Set "
                        "account.mll_floor_override to the value in your Topstep dashboard to be safe")
            else:
                tracker = LossLimitTracker(start, mll, floor=floor)
                room = tracker.room(account.balance)
                status = OK if room > 2 * cfg.risk.mll_buffer else (WARN if room > 0 else FAIL)
                rep.add(status, "Max Loss Limit", f"floor {_money(floor)} ({source}); room {_money(room)}")
        finally:
            journal.close()

        # ---- account must be flat
        positions = await client.search_open_positions(account.id)
        orders = await client.search_open_orders(account.id)
        if positions or orders:
            rep.add(WARN, "Open positions/orders", f"{len(positions)} position(s), {len(orders)} order(s) open - with "
                    f"orphan_position_policy '{cfg.execution.orphan_position_policy}' the bot will act on them. "
                    "Close them first or use 'flatten'")
        else:
            rep.add(OK, "Open positions/orders", "account is flat")

        # ---- contract
        step("Checking the contract")
        try:
            contract = await resolve_contract(client, cfg)
        except Exception as exc:  # noqa: BLE001
            rep.add(FAIL, "Contract", str(exc))
            return rep
        spec = SPECS.get(contract.root.upper())
        if spec and (abs(spec.tick_size - contract.tick_size) > 1e-9 or abs(spec.tick_value - contract.tick_value) > 1e-9):
            rep.add(WARN, "Contract", f"{contract.name}: tick {contract.tick_size}/${contract.tick_value} differs from the "
                    "built-in spec - the API values will be used")
        else:
            rep.add(OK, "Contract", f"{contract.name} - {contract.description} (tick {contract.tick_size} = ${contract.tick_value})")

        # ---- PC clock (bars are timed by your clock)
        try:
            resp = await client._http.get("/")
            server = email.utils.parsedate_to_datetime(resp.headers["date"])
            drift = (datetime.now(UTC) - server).total_seconds()
            status = OK if abs(drift) <= 3 else (WARN if abs(drift) <= 30 else FAIL)
            rep.add(status, "PC clock", f"{drift:+.0f}s vs TopstepX" + ("" if status == OK else
                    " - turn on 'Set time automatically' and press 'Sync now' in Windows date & time settings"))
        except Exception as exc:  # noqa: BLE001
            rep.add(WARN, "PC clock", f"could not compare with the server ({exc})")

        # ---- live market data
        step("Checking market data")
        bars = await client.retrieve_bars(contract.id, now - timedelta(hours=72), now, BarUnit.MINUTE, 1, limit=10,
                                          live=cfg.data.live_market_data, include_partial=True)
        if not bars:
            rep.add(FAIL, "Market data", "no recent bars returned")
        elif schedule.market_open(now):
            age = (now - bars[-1].ts).total_seconds() / 60
            rep.add(OK if age <= 5 else WARN, "Market data", f"last price {bars[-1].close} ({age:.0f} min ago)")
        else:
            rep.add(OK, "Market data", f"market closed now; last price {bars[-1].close}")

        # ---- today's calendar
        day = schedule.trading_day(now)
        local = schedule.local(now)
        win_start, win_end = schedule.entry_window(day)
        if not schedule.is_trade_day(day):
            rep.add(INFO, "Calendar", f"trading day {day} is a no-trade day (holiday/weekend) - the bot will wait")
        elif local < win_start:
            rep.add(OK, "Calendar", f"entries open {win_start:%a %H:%M} CT ({(win_start - local).total_seconds() / 3600:.1f}h from now)")
        elif local < win_end:
            rep.add(OK, "Calendar", f"entry window open now until {win_end:%H:%M} CT")
        else:
            rep.add(INFO, "Calendar", f"today's entry window has closed; next trading day {day + timedelta(days=1)} "
                    f"(the session opens {session_open_for(day + timedelta(days=1), schedule.tz):%a %H:%M} CT)")

        # ---- news
        if cfg.news.enabled:
            from topstep_bot.news import NewsCalendar

            n = cfg.news
            cal = NewsCalendar(n.url, Path(cfg.data_dir) / "news_cache.json", n.impacts, n.currencies, n.minutes_before, n.minutes_after)
            if await cal.refresh() or cal.events:
                upcoming = cal.upcoming(now, hours=48)
                listing = "; ".join(f"{e.label} {e.time.astimezone(schedule.tz):%a %H:%M} CT" for e in upcoming[:6])
                rep.add(OK, "News blackouts", listing or "no high-impact releases in the next 48h")
            else:
                rep.add(WARN, "News blackouts", "economic calendar unavailable - add blackout_windows for big releases manually")
        else:
            rep.add(WARN, "News blackouts", "disabled (news.enabled: false)")

        # ---- strategy check on real recent data
        if run_backtests:
            step(f"Backtesting all strategies on the last {backtest_days} days of real data")
            await _strategy_check(cfg, client, contract, backtest_days, rep)
    finally:
        if own_client:
            await client.close()

    # ---- alerts / remote control
    if secrets.telegram_bot_token and secrets.telegram_chat_id:
        rep.add(OK, "Telegram", "alerts and remote control configured (test with: topstep-bot telegram-test)")
    else:
        rep.add(WARN, "Telegram", "not set up - you won't get alerts or be able to stop the bot from your phone")
    return rep


async def _strategy_check(cfg: BotConfig, client, contract, days: int, rep: PreflightReport) -> None:
    from topstep_bot.backtest.data import save_csv
    from topstep_bot.backtest.metrics import combine_statistics, compute_metrics
    from topstep_bot.backtest.runner import run_backtest
    from topstep_bot.models import BarUnit
    from topstep_bot.strategies import STRATEGIES

    end = datetime.now(UTC)
    try:
        bars = await client.retrieve_bars_range(contract.id, end - timedelta(days=days), end, BarUnit.MINUTE, 1,
                                                live=cfg.data.live_market_data)
    except Exception as exc:  # noqa: BLE001
        rep.add(WARN, "Strategy check", f"could not download history ({exc})")
        return
    if len(bars) < 1000:
        rep.add(WARN, "Strategy check", f"only {len(bars)} bars of history available - not enough to judge")
        return
    save_csv(bars, Path(cfg.data_dir) / f"{cfg.instrument.symbol}_1m.csv")
    plan = PLANS[cfg.account.plan]
    offline = offline_contract(contract.root) if contract.root.upper() in SPECS else contract
    for name, cls in STRATEGIES.items():
        trial = cfg.model_copy(deep=True)
        if name != cfg.strategy.name:
            trial.strategy.name, trial.strategy.params = name, {}
        try:
            res = await run_backtest(trial, bars, offline)
        except ValueError as exc:
            rep.strategy_rows.append({"name": name, "title": cls.title, "error": str(exc), "configured": name == cfg.strategy.name})
            continue
        m = compute_metrics(res.trades, res.days, res.starting_balance)
        cs = combine_statistics(res.days, plan)
        rep.strategy_rows.append({
            "name": name, "title": cls.title, "configured": name == cfg.strategy.name,
            "net": m["net_pnl"], "trades": m["trades"], "win_rate": m["win_rate"], "pf": m["profit_factor"],
            "max_dd": m["max_drawdown"], "pass_rate": cs["pass_rate"], "breaches": len(res.breaches),
        })
        await asyncio.sleep(0)
    mine = next((r for r in rep.strategy_rows if r["configured"] and "error" not in r), None)
    if mine is None:
        rep.add(WARN, "Strategy check", "the configured strategy could not be backtested")
        return
    detail = (f"{mine['title']}: {_money(mine['net'])} over {mine['trades']} trades, max drawdown {_money(mine['max_dd'])}"
              + (f", Combine pass rate {mine['pass_rate']:.0%}" if mine["pass_rate"] is not None else ""))
    good = mine["net"] > 0 and mine["breaches"] == 0
    rep.add(OK if good else WARN, "Strategy check", detail + ("" if good else " - it LOST money or hit the MLL on recent data"))
