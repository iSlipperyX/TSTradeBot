"""Pre-trade checks, position sizing and live risk monitoring.

The bot's own limits are intentionally stricter than Topstep's so that a rule violation
should never happen: every trade is sized so that hitting its stop cannot breach the
personal daily loss limit or come within ``mll_buffer`` of the Maximum Loss Limit.

Topstep rules enforced here (see docs/TOPSTEP_RULES.md):

* Maximum Loss Limit - sizing keeps a stop-out above the floor plus ``mll_buffer``; open trades
  are flattened at half the buffer.
* Daily Loss Limit (if the account has one) - entries stop at 90% of it, open trades are
  flattened at 95%.
* Position size - plan cap, XFA Scaling Plan and product caps (metals/energy).
* Combine Consistency Target - a daily profit cap keeps the best day below 55% of the profit
  target, an open trade is closed before the day crosses it, and no trade is sized or targeted
  so that one fast winner could carry the day past the line (``profit_ceiling``).
* Sudden moves - every hard limit (MLL, daily loss limits) is checked against the stop plus the
  market's jump allowance (network.py), so a stop that fills late can't carry the account past it.
* Combine profit target - once reached, trading stops so the pass can't be given back.
* News - Topstep's maximum position size is never held into a scheduled major release.
"""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta

from topstep_bot import funded
from topstep_bot.config import AccountConfig, AggressiveConfig, FundedConfig, RiskConfig
from topstep_bot.models import Contract
from topstep_bot.risk.book import MICROS_PER_MINI, AccountBook, Exposure
from topstep_bot.risk.topstep import (
    CombineProgress,
    LossLimitTracker,
    PlanSpec,
    combine_progress,
    max_contracts_allowed,
    max_minis_allowed,
)
from topstep_bot.sessions import SessionSchedule

# Combine accounts default to capping each day at 40% of the profit target, which keeps
# the best day comfortably under Topstep's 55% Consistency Target.
DEFAULT_COMBINE_DAILY_CAP = 0.40
# An open Combine trade is closed once the day's P&L reaches this share of the profit target,
# safely before the 55% line that would raise the target.
CONSISTENCY_GUARD_FRACTION = 0.50
# The aggressive daily goal by default: 45% of the profit target, under the consistency line.
AGGRESSIVE_GOAL_SHARE = 0.45
# Within this long before a scheduled major release (or when the calendar is unavailable)
# new trades use at most half of Topstep's maximum position size.
NEWS_SIZE_GUARD = timedelta(minutes=30)


class RiskManager:
    def __init__(
        self,
        cfg: RiskConfig,
        account: AccountConfig,
        plan: PlanSpec,
        contract: Contract,
        schedule: SessionSchedule,
        tracker: LossLimitTracker,
        fees_round_turn: float,
        funded_cfg: FundedConfig | None = None,
        aggressive_cfg: AggressiveConfig | None = None,
        book: AccountBook | None = None,
        lane: str | None = None,
    ):
        from topstep_bot.network import default_jump_ticks

        self.cfg = cfg
        self.funded = funded_cfg or FundedConfig()
        self.aggressive = aggressive_cfg or AggressiveConfig()
        self.edge_sizing = False  # the probability calculator sizes aggressive trades by their edge (factory.py)
        self.account_cfg = account
        self.plan = plan
        self.stage = account.stage
        self.contract = contract
        self.schedule = schedule
        self.tracker = tracker
        self.fees_round_turn = fees_round_turn
        # The day's state lives in the account book, shared with the other markets this account trades
        # (risk/book.py); a lane of its own (the usual single market) gets a private book.
        self.book = book if book is not None else AccountBook()
        self.lane = lane or contract.id or contract.name
        # Live and paper trading turn on the guards that need a real account history or a news
        # calendar (news position-size cap, stop at the Combine target). Backtests replay many
        # Combine attempts from one run and have no calendar, so they leave these off.
        self.live_guards = False
        # How far price can jump past a stop (ticks), added to every worst case checked against a hard
        # limit; the live bot refreshes it from the market network (network.py) each day.
        self.jump_ticks = default_jump_ticks(contract.root or contract.name)
        self.stressed = False  # the network marks this market as stressed (aggressive mode sizes down)
        self.network_note = ""

    # ------------------------------------------------- the shared day state

    def _shared(name: str):  # noqa: N805 - builds properties at class creation
        return property(lambda self: getattr(self.book, name), lambda self, v: setattr(self.book, name, v))

    day = _shared("day")
    day_start_balance = _shared("day_start_balance")
    trades_today = _shared("trades_today")
    wins_today = _shared("wins_today")
    consecutive_losses = _shared("consecutive_losses")
    last_loss_at = _shared("last_loss_at")
    lock_reason = _shared("lock_reason")
    paused = _shared("paused")
    risk_scale = _shared("risk_scale")
    best_prior_day = _shared("best_prior_day")
    realized_today = _shared("realized_today")
    day_peak = _shared("day_peak")
    del _shared

    def others(self) -> Exposure:
        """What the account's other markets have on right now (nothing when this is the only one)."""
        return self.book.others(self.lane)

    # ----------------------------------------------------------------- limits

    @property
    def daily_profit_target(self) -> float | None:
        """Where the day stops: the settings' daily target, lowered to today's goal from the day plan (never raised)."""
        base = self.configured_daily_target
        goal = self.book.goal
        if goal is None:
            return base
        return goal if base is None else min(base, goal)

    @property
    def configured_daily_target(self) -> float | None:
        """The daily profit target from the settings alone (the most a day plan's goal may be)."""
        if self.cfg.daily_profit_target is not None:
            return self.cfg.daily_profit_target
        if self.aggressive.enabled:
            return self.aggressive_goal
        if self.stage == "combine":
            return DEFAULT_COMBINE_DAILY_CAP * self.plan.profit_target
        return funded.daily_cap(self.funded, self.plan, self.stage, self.account_cfg.payout_path)

    def day_cap(self) -> float | None:
        """The most one day may make under the settings and the rules: the daily target or the profit ceiling."""
        caps = [c for c in (self.configured_daily_target, self.profit_ceiling()) if c is not None]
        return min(caps) if caps else None

    @property
    def aggressive_goal(self) -> float:
        """The aggressive mode's profit goal for one day (never above a funded payout path's daily cap)."""
        goal = self.aggressive.daily_goal or AGGRESSIVE_GOAL_SHARE * self.plan.profit_target
        cap = funded.daily_cap(self.funded, self.plan, self.stage, self.account_cfg.payout_path)
        return min(goal, cap) if cap is not None else goal

    def profit_ceiling(self) -> float | None:
        """The day P&L no trade may carry the day past: the Combine's consistency line (where the guard closes
        out, safely under Topstep's 55%) or an XFA consistency path's daily cap. None = no such line."""
        if self.stage == "combine" and self.cfg.consistency_guard:
            return CONSISTENCY_GUARD_FRACTION * self.plan.profit_target
        return funded.daily_cap(self.funded, self.plan, self.stage, self.account_cfg.payout_path)

    def reward_room(self, balance: float) -> float | None:
        """Dollars a new trade may still make today before the day reaches its profit ceiling."""
        ceiling = self.profit_ceiling()
        if ceiling is None or self.day is None:  # no trading day started yet: no day P&L to measure against
            return None
        # The other markets' open trades can still make their targets: that part of the room is theirs.
        return ceiling - self.day_pnl(balance) - self.others().upside

    def locked_floor(self) -> float | None:
        """Aggressive mode: the realized profit today that is locked in (None until the day has made enough)."""
        a = self.aggressive
        goal = self.daily_profit_target
        if not a.enabled or not goal or self.day_peak <= 0 or self.day_peak < a.lock_in_at * goal:
            return None
        return self.day_peak * (1 - a.give_back)

    def trade_risk(self, balance: float, conviction: float = 1.0) -> float:
        """Dollars the next trade may lose at its stop (before Topstep's and the daily limits are applied).

        Aggressive mode: ``conviction`` scales the risk (below 1 for signals the knowledge base hasn't proven or the
        probability calculator rates thin, up to ``max_conviction`` for a large, well-measured edge); part of today's
        realized profit is added (house money), up to ``max_risk_multiple``; a stressed market halves it;
        and it never exceeds ``cushion_share`` of the room above the Maximum Loss Limit."""
        base = self.cfg.risk_per_trade * self.risk_scale
        a = self.aggressive
        if not a.enabled:
            return base
        risk = base * min(a.max_conviction, max(0.0, conviction)) + a.house_money * max(0.0, self.realized_today)
        risk = min(risk, a.max_risk_multiple * base)
        if self.stressed:
            risk *= a.stressed_risk
        others = self.others()
        room = self.tracker.room(balance + others.open_pnl) - others.at_risk - self.cfg.mll_buffer
        return max(0.0, min(risk, a.cushion_share * room))

    @property
    def max_trades_today(self) -> int:
        """The account's trade allowance today: with several markets in aggressive mode, each market brings its own
        ``max_trades_per_day`` (aggressive.trades_per_market). Every loss limit still counts the whole account."""
        markets = len(self.book.lanes) if self.book.shared else 1
        if self.aggressive.enabled and self.aggressive.trades_per_market and markets > 1:
            return self.cfg.max_trades_per_day * markets
        return self.cfg.max_trades_per_day

    @property
    def topstep_dll(self) -> float | None:
        return self.account_cfg.topstep_daily_loss_limit

    def max_contracts_topstep(self) -> int:
        """Topstep's own position limit for today, in contracts of this instrument.

        Topstep's cap counts every open position in the account (10 micros = 1 mini), so what the other
        markets hold is taken off it first."""
        cap = max_contracts_allowed(self.plan, self.stage, self.day_start_balance, self.contract.root, self.contract.is_micro)
        used = self.others().minis
        if used <= 0:
            return cap
        return max(0, min(cap, self._contracts_in(max_minis_allowed(self.plan, self.stage, self.day_start_balance) - used)))

    def _contracts_in(self, minis: float) -> int:
        """How many contracts of this instrument fit in ``minis`` mini-contract units."""
        if minis <= 0:
            return 0
        return math.floor(minis * MICROS_PER_MINI + 1e-9) if self.contract.is_micro else math.floor(minis + 1e-9)

    def news_size_cap(self, now: datetime | None) -> int | None:
        """Half of Topstep's max when a scheduled major release is near (or the calendar is unknown).

        Topstep prohibits taking the maximum position size into a scheduled major news event.
        """
        if now is None or not self.live_guards:
            return None
        news = self.schedule.news
        calendar_known = news is not None and news.fetched_at is not None
        if calendar_known and not any(now <= e.time <= now + NEWS_SIZE_GUARD for e in news.events):
            return None
        full = max_contracts_allowed(self.plan, self.stage, self.day_start_balance, self.contract.root, self.contract.is_micro)
        return min(max(1, full // 2), self.max_contracts_topstep())

    def max_contracts(self, now: datetime | None = None) -> int:
        cap = self.max_contracts_topstep()
        if self.cfg.max_contracts is not None:
            cap = min(cap, self.cfg.max_contracts)
        news_cap = self.news_size_cap(now)
        if news_cap is not None:
            cap = min(cap, news_cap)
        return cap

    # ------------------------------------------------------------ the Combine

    def combine_progress(self, balance: float, open_pnl: float = 0.0) -> CombineProgress | None:
        """Combine accounts: profit so far against the (consistency-adjusted) profit target."""
        if self.stage != "combine":
            return None
        today = self.day_pnl(balance, open_pnl)
        best = max(self.best_prior_day, today)
        return combine_progress(self.plan, self.equity(balance, open_pnl) - self.tracker.starting_balance, best)

    def _combine_reason(self, balance: float, open_pnl: float) -> str | None:
        """Why the Combine rules say stop trading now (target reached / consistency line), or None."""
        if self.stage != "combine":
            return None
        progress = self.combine_progress(balance, open_pnl)
        if self.live_guards and self.cfg.stop_at_profit_target and progress and progress.passed:
            return "Combine profit target reached - trading stopped to protect the pass"
        if self.cfg.consistency_guard:
            day = self.day_pnl(balance, open_pnl)
            line = CONSISTENCY_GUARD_FRACTION * self.plan.profit_target
            if day >= line:
                return (f"today's profit ${day:,.0f} is near the Consistency Target "
                        f"(55% of the ${self.plan.profit_target:,.0f} profit target) - done for the day")
        return None

    # ------------------------------------------------------------- day state

    def start_day(
        self,
        day: date,
        balance: float,
        realized_so_far: float = 0.0,
        closed_today: list[tuple[datetime, float]] | None = None,
    ) -> None:
        """Begin a trading day. After a mid-day restart, pass today's closed trades as (time, net P&L)
        so the trade count, losing streak and post-loss cooldown carry on where they left off."""
        if self.book.shared and self.book.day == day:
            return  # the desk already started today for the whole account
        self.book.start_day(day, balance, realized_so_far, closed_today)

    def record_trade(self, net_pnl: float, ts: datetime) -> None:
        self.book.record_trade(net_pnl, ts)

    def lock(self, reason: str) -> str:
        return self.book.lock(reason)

    def day_pnl(self, balance: float, open_pnl: float = 0.0) -> float:
        """The account's P&L today: realized, this market's ``open_pnl`` and every other market's open P&L."""
        return balance + open_pnl + self.others().open_pnl - self.day_start_balance

    def equity(self, balance: float, open_pnl: float = 0.0) -> float:
        """The account's equity: the balance plus every open position (this market's ``open_pnl`` and the others')."""
        return balance + open_pnl + self.others().open_pnl

    # ---------------------------------------------------------------- sizing

    def risk_per_contract(self, entry: float, stop: float) -> float:
        """Expected loss per contract if the stop is hit: distance + stop slippage + fees."""
        ticks = self.contract.ticks(entry - stop) + self.cfg.slippage_ticks
        return ticks * self.contract.tick_value + self.fees_round_turn

    def worst_loss_per_contract(self, entry: float, stop: float) -> float:
        """Worst case per contract: the expected loss plus a sudden jump past the stop (the jump allowance)."""
        return self.risk_per_contract(entry, stop) + self.jump_ticks * self.contract.tick_value

    def position_size(self, entry: float, stop: float, balance: float, now: datetime | None = None,
                      conviction: float = 1.0) -> int:
        rpc = self.risk_per_contract(entry, stop)
        if rpc <= 0:
            return 0
        day_pnl = self.day_pnl(balance)
        # Hard limits: a stop-out, even one that fills a jump late, must not reach any of them.
        hard = [
            self.cfg.personal_daily_loss_limit + day_pnl,
            self.tracker.room(self.equity(balance)) - self.cfg.mll_buffer,
        ]
        if self.topstep_dll:
            hard.append(0.9 * self.topstep_dll + day_pnl)
        keep = funded.protected_floor(self.funded, self.stage, self.account_cfg.payout_path, day_pnl)
        if keep is not None:  # XFA standard path: a stop-out must not undo today's winning day
            hard.append(day_pnl - keep)
        locked = self.locked_floor()
        if locked is not None:  # aggressive mode: a stop-out must not give back the locked-in profit
            hard.append(day_pnl - locked)
        soft = self.trade_risk(balance, conviction)
        # Every other market's open trade could stop out too (and fill late): the budget left is what remains.
        budget = min(hard) - self.others().at_risk
        if soft <= 0 or budget <= 0:
            return 0
        size = min(math.floor(soft / rpc), math.floor(budget / self.worst_loss_per_contract(entry, stop)))
        return max(0, min(size, self.max_contracts(now)))

    # ---------------------------------------------------------------- checks

    def entry_block_reason(
        self, now: datetime, balance: float, open_pnl: float = 0.0, manual: bool = False
    ) -> str | None:
        """None if a new trade may be opened now, else the reason it may not.

        ``manual``: a trade you asked for yourself - allowed while auto-trading is paused and
        after the daily trade count, but every loss limit and session rule still applies.
        """
        if self.paused and not manual:
            return "new trades are paused"
        if self.lock_reason:
            return self.lock_reason
        session_reason = self.schedule.entry_block_reason(now)
        if session_reason:
            return session_reason
        if self.trades_today >= self.max_trades_today and not manual:
            return f"max trades per day ({self.max_trades_today}) reached"
        if self.consecutive_losses >= self.cfg.max_consecutive_losses:
            return f"{self.consecutive_losses} consecutive losses - done for the day"
        if self.last_loss_at and now - self.last_loss_at < timedelta(minutes=self.cfg.cooldown_minutes_after_loss):
            return "cooling down after a loss"
        day_pnl = self.day_pnl(balance, open_pnl)
        target = self.daily_profit_target
        if target is not None and day_pnl >= target:
            if self.book.goal is not None and target == self.book.goal:
                return self.lock(f"today's goal ${target:,.0f} reached ({self.book.goal_text or 'day plan'}) - done for the "
                                 "day, keeping it")
            return self.lock(f"daily profit target ${target:,.0f} reached")
        locked = self.locked_floor()
        if locked is not None and day_pnl <= locked:
            return self.lock(f"gave back {self.aggressive.give_back:.0%} of today's best (${self.day_peak:,.0f}) - "
                             f"keeping ${day_pnl:,.0f}")
        if day_pnl <= -self.cfg.personal_daily_loss_limit:
            return self.lock(f"personal daily loss limit ${self.cfg.personal_daily_loss_limit:,.0f} reached")
        if self.tracker.room(self.equity(balance, open_pnl)) <= self.cfg.mll_buffer:
            return "too close to the Maximum Loss Limit"
        if self.topstep_dll and day_pnl <= -0.9 * self.topstep_dll:
            return self.lock("within 10% of Topstep daily loss limit")
        combine = self._combine_reason(balance, open_pnl)
        if combine:
            return self.lock(combine)
        return None

    def check_open_risk(self, balance: float, open_pnl: float) -> str | None:
        """Called on every price update. Returns a reason if the position must be flattened now."""
        equity = self.equity(balance, open_pnl)
        day_pnl = equity - self.day_start_balance
        if day_pnl <= -self.cfg.personal_daily_loss_limit:
            return self.lock(f"personal daily loss limit hit (day P&L ${day_pnl:,.2f})")
        if self.tracker.room(equity) <= self.cfg.mll_buffer * 0.5:
            return self.lock(f"equity ${equity:,.2f} is within ${self.tracker.room(equity):,.0f} of the MLL")
        if self.topstep_dll and day_pnl <= -0.95 * self.topstep_dll:
            return self.lock("about to hit Topstep daily loss limit")
        combine = self._combine_reason(balance, open_pnl)
        if combine:
            return self.lock(combine)
        return None

    def news_flatten_reason(self, now: datetime, position: int) -> str | None:
        """A position at Topstep's maximum size must not be held into a scheduled major release."""
        news = self.schedule.news
        if position == 0 or news is None or abs(position) < self.max_contracts_topstep():
            return None
        event = news.releasing_soon(now)
        if event is None:
            return None
        return f"Topstep's maximum position size may not be held into news ({event.label})"

    def snapshot(self, balance: float, open_pnl: float) -> dict:
        equity = self.equity(balance, open_pnl)
        progress = self.combine_progress(balance, open_pnl)
        return {
            "day": self.day.isoformat() if self.day else None,
            "day_pnl": round(self.day_pnl(balance, open_pnl), 2),
            "trades_today": self.trades_today,
            "max_trades": self.max_trades_today,
            "wins_today": self.wins_today,
            "mll_size": self.plan.max_loss_limit,
            "consecutive_losses": self.consecutive_losses,
            "max_contracts": self.max_contracts(),
            "topstep_max_contracts": self.max_contracts_topstep(),
            "topstep_dll": self.topstep_dll,
            "combine": None if progress is None else {
                "profit_target": progress.profit_target,
                "remaining": round(progress.remaining, 2),
                "best_day": round(progress.best_day, 2),
                "target_raised": progress.target_raised,
                "passed": progress.passed,
                "summary": progress.describe(),
            },
            "mll_floor": round(self.tracker.floor, 2),
            "mll_room": round(self.tracker.room(equity), 2),
            "daily_loss_limit": self.cfg.personal_daily_loss_limit,
            "daily_profit_target": self.daily_profit_target,
            "day_goal": self.book.goal,
            "winning_day_floor": funded.protected_floor(self.funded, self.stage, self.account_cfg.payout_path,
                                                        self.day_pnl(balance)),
            "locked": self.lock_reason,
            "risk_per_trade": round(self.cfg.risk_per_trade * self.risk_scale, 2),
            "next_trade_risk": round(self.trade_risk(balance), 2),
            "profit_ceiling": self.profit_ceiling(),
            "jump_ticks": self.jump_ticks,
            "stressed": self.stressed,
            "network": self.network_note,
            "aggressive": None if not self.aggressive.enabled else {
                "goal": round(self.daily_profit_target or self.aggressive_goal, 2), "best_today": round(self.day_peak, 2),
                "realized_today": round(self.realized_today, 2),
                "locked_in": None if self.locked_floor() is None else round(self.locked_floor(), 2),
                "lock_in_from": round(self.aggressive.lock_in_at * (self.daily_profit_target or self.aggressive_goal), 2),
                # a large, well-measured edge may risk this much (probability.sizing); a thin one less than next_trade_risk
                "strong_trade_risk": round(self.trade_risk(balance, self.aggressive.max_conviction), 2)
                if self.edge_sizing else None,
            },
            "risk_scale": self.risk_scale,
            "paused": self.paused,
        }
