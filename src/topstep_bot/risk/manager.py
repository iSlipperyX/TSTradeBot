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
  target, and an open trade is closed before the day crosses it.
* Combine profit target - once reached, trading stops so the pass can't be given back.
* News - Topstep's maximum position size is never held into a scheduled major release.
"""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta

from topstep_bot.config import AccountConfig, RiskConfig
from topstep_bot.models import Contract
from topstep_bot.risk.topstep import (
    CombineProgress,
    LossLimitTracker,
    PlanSpec,
    combine_progress,
    max_contracts_allowed,
)
from topstep_bot.sessions import SessionSchedule

# Combine accounts default to capping each day at 40% of the profit target, which keeps
# the best day comfortably under Topstep's 55% Consistency Target.
DEFAULT_COMBINE_DAILY_CAP = 0.40
# An open Combine trade is closed once the day's P&L reaches this share of the profit target,
# safely before the 55% line that would raise the target.
CONSISTENCY_GUARD_FRACTION = 0.50
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
    ):
        self.cfg = cfg
        self.account_cfg = account
        self.plan = plan
        self.stage = account.stage
        self.contract = contract
        self.schedule = schedule
        self.tracker = tracker
        self.fees_round_turn = fees_round_turn
        self.day: date | None = None
        self.day_start_balance = 0.0
        self.trades_today = 0
        self.wins_today = 0
        self.consecutive_losses = 0
        self.last_loss_at: datetime | None = None
        self.lock_reason: str | None = None
        self.paused = False
        self.risk_scale = 1.0  # < 1 during the live ramp-up period
        # Best finished day of the Combine so far (for the Consistency Target); set by the engine.
        self.best_prior_day = 0.0
        # Live and paper trading turn on the guards that need a real account history or a news
        # calendar (news position-size cap, stop at the Combine target). Backtests replay many
        # Combine attempts from one run and have no calendar, so they leave these off.
        self.live_guards = False

    # ----------------------------------------------------------------- limits

    @property
    def daily_profit_target(self) -> float | None:
        if self.cfg.daily_profit_target is not None:
            return self.cfg.daily_profit_target
        if self.stage == "combine":
            return DEFAULT_COMBINE_DAILY_CAP * self.plan.profit_target
        return None

    @property
    def topstep_dll(self) -> float | None:
        return self.account_cfg.topstep_daily_loss_limit

    def max_contracts_topstep(self) -> int:
        """Topstep's own position limit for today, in contracts of this instrument."""
        return max_contracts_allowed(self.plan, self.stage, self.day_start_balance, self.contract.root, self.contract.is_micro)

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
        return max(1, self.max_contracts_topstep() // 2)

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
        return combine_progress(self.plan, balance + open_pnl - self.tracker.starting_balance, best)

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
        self.day = day
        self.day_start_balance = balance - realized_so_far
        self.trades_today = 0
        self.wins_today = 0
        self.consecutive_losses = 0
        self.last_loss_at = None
        self.lock_reason = None
        for ts, net_pnl in sorted(closed_today or []):
            self.record_trade(net_pnl, ts)

    def record_trade(self, net_pnl: float, ts: datetime) -> None:
        self.trades_today += 1
        if net_pnl < 0:
            self.consecutive_losses += 1
            self.last_loss_at = ts
        else:
            self.wins_today += 1
            self.consecutive_losses = 0

    def lock(self, reason: str) -> str:
        if self.lock_reason is None:
            self.lock_reason = reason
        return reason

    def day_pnl(self, balance: float, open_pnl: float = 0.0) -> float:
        return balance + open_pnl - self.day_start_balance

    # ---------------------------------------------------------------- sizing

    def risk_per_contract(self, entry: float, stop: float) -> float:
        """Worst-case loss per contract if the stop is hit: distance + stop slippage + fees."""
        ticks = self.contract.ticks(entry - stop) + self.cfg.slippage_ticks
        return ticks * self.contract.tick_value + self.fees_round_turn

    def position_size(self, entry: float, stop: float, balance: float, now: datetime | None = None) -> int:
        rpc = self.risk_per_contract(entry, stop)
        if rpc <= 0:
            return 0
        day_pnl = self.day_pnl(balance)
        budgets = [
            self.cfg.risk_per_trade * self.risk_scale,
            self.cfg.personal_daily_loss_limit + day_pnl,
            self.tracker.room(balance) - self.cfg.mll_buffer,
        ]
        if self.topstep_dll:
            budgets.append(0.9 * self.topstep_dll + day_pnl)
        budget = min(budgets)
        if budget <= 0:
            return 0
        return max(0, min(math.floor(budget / rpc), self.max_contracts(now)))

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
        if self.trades_today >= self.cfg.max_trades_per_day and not manual:
            return f"max trades per day ({self.cfg.max_trades_per_day}) reached"
        if self.consecutive_losses >= self.cfg.max_consecutive_losses:
            return f"{self.consecutive_losses} consecutive losses - done for the day"
        if self.last_loss_at and now - self.last_loss_at < timedelta(minutes=self.cfg.cooldown_minutes_after_loss):
            return "cooling down after a loss"
        day_pnl = self.day_pnl(balance, open_pnl)
        target = self.daily_profit_target
        if target is not None and day_pnl >= target:
            return self.lock(f"daily profit target ${target:,.0f} reached")
        if day_pnl <= -self.cfg.personal_daily_loss_limit:
            return self.lock(f"personal daily loss limit ${self.cfg.personal_daily_loss_limit:,.0f} reached")
        if self.tracker.room(balance + open_pnl) <= self.cfg.mll_buffer:
            return "too close to the Maximum Loss Limit"
        if self.topstep_dll and day_pnl <= -0.9 * self.topstep_dll:
            return self.lock("within 10% of Topstep daily loss limit")
        combine = self._combine_reason(balance, open_pnl)
        if combine:
            return self.lock(combine)
        return None

    def check_open_risk(self, balance: float, open_pnl: float) -> str | None:
        """Called on every price update. Returns a reason if the position must be flattened now."""
        equity = balance + open_pnl
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
        equity = balance + open_pnl
        progress = self.combine_progress(balance, open_pnl)
        return {
            "day": self.day.isoformat() if self.day else None,
            "day_pnl": round(self.day_pnl(balance, open_pnl), 2),
            "trades_today": self.trades_today,
            "max_trades": self.cfg.max_trades_per_day,
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
            "locked": self.lock_reason,
            "risk_per_trade": round(self.cfg.risk_per_trade * self.risk_scale, 2),
            "risk_scale": self.risk_scale,
            "paused": self.paused,
        }
