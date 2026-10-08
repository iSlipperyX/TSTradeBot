"""Pre-trade checks, position sizing and live risk monitoring.

The bot's own limits are intentionally stricter than Topstep's so that a rule violation
should never happen: every trade is sized so that hitting its stop cannot breach the
personal daily loss limit or come within ``mll_buffer`` of the Maximum Loss Limit.
"""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta

from topstep_bot.config import AccountConfig, RiskConfig
from topstep_bot.models import Contract
from topstep_bot.risk.topstep import LossLimitTracker, PlanSpec, max_minis_allowed
from topstep_bot.sessions import SessionSchedule

# Combine accounts default to capping each day at 40% of the profit target, which keeps
# the best day comfortably under Topstep's 50% consistency limit.
DEFAULT_COMBINE_DAILY_CAP = 0.40


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
        minis = max_minis_allowed(self.plan, self.stage, self.day_start_balance)
        return minis * 10 if self.contract.is_micro else minis

    def max_contracts(self) -> int:
        cap = self.max_contracts_topstep()
        if self.cfg.max_contracts is not None:
            cap = min(cap, self.cfg.max_contracts)
        return cap

    # ------------------------------------------------------------- day state

    def start_day(self, day: date, balance: float, realized_so_far: float = 0.0, trades_so_far: int = 0) -> None:
        self.day = day
        self.day_start_balance = balance - realized_so_far
        self.trades_today = trades_so_far
        self.wins_today = 0
        self.consecutive_losses = 0
        self.last_loss_at = None
        self.lock_reason = None

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

    def position_size(self, entry: float, stop: float, balance: float) -> int:
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
        return max(0, min(math.floor(budget / rpc), self.max_contracts()))

    # ---------------------------------------------------------------- checks

    def entry_block_reason(
        self, now: datetime, balance: float, open_pnl: float = 0.0, manual: bool = False
    ) -> str | None:
        """None if a new trade may be opened now, else the reason it may not.

        ``manual``: a trade you asked for yourself - allowed while auto-trading is paused and
        after the daily trade count, but every loss limit and session rule still applies.
        """
        if self.paused and not manual:
            return "paused from dashboard"
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
        return None

    def snapshot(self, balance: float, open_pnl: float) -> dict:
        equity = balance + open_pnl
        return {
            "day": self.day.isoformat() if self.day else None,
            "day_pnl": round(self.day_pnl(balance, open_pnl), 2),
            "trades_today": self.trades_today,
            "max_trades": self.cfg.max_trades_per_day,
            "wins_today": self.wins_today,
            "mll_size": self.plan.max_loss_limit,
            "consecutive_losses": self.consecutive_losses,
            "max_contracts": self.max_contracts(),
            "mll_floor": round(self.tracker.floor, 2),
            "mll_room": round(self.tracker.room(equity), 2),
            "daily_loss_limit": self.cfg.personal_daily_loss_limit,
            "daily_profit_target": self.daily_profit_target,
            "locked": self.lock_reason,
            "risk_per_trade": round(self.cfg.risk_per_trade * self.risk_scale, 2),
            "risk_scale": self.risk_scale,
            "paused": self.paused,
        }
