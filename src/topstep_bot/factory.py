"""Wires configuration into a ready-to-run TradingCore (shared by backtests and live trading)."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime

from topstep_bot.broker.base import Broker
from topstep_bot.config import BotConfig
from topstep_bot.engine import TradingCore
from topstep_bot.execution import OrderManager
from topstep_bot.instruments import SPECS
from topstep_bot.journal import Journal
from topstep_bot.models import Contract
from topstep_bot.notify import Notifier
from topstep_bot.risk.guards import OrderGuard
from topstep_bot.risk.manager import RiskManager
from topstep_bot.risk.topstep import PLANS, LossLimitTracker, starting_balance_for
from topstep_bot.sessions import SessionSchedule
from topstep_bot.strategies import create_strategy

log = logging.getLogger(__name__)


def fees_for(cfg: BotConfig, contract: Contract) -> float:
    if cfg.risk.fees_per_contract_round_turn is not None:
        return cfg.risk.fees_per_contract_round_turn
    spec = SPECS.get(contract.root.upper())
    return spec.fees_round_turn if spec else 0.0


def starting_balance(cfg: BotConfig) -> float:
    if cfg.account.starting_balance is not None:
        return cfg.account.starting_balance
    return starting_balance_for(PLANS[cfg.account.plan], cfg.account.stage)


def mll_floor_for(cfg: BotConfig, saved: float | None) -> float | None:
    """The Maximum Loss Limit floor to start from: the saved floor, raised (never lowered) by the override.

    Topstep's floor only moves up, so an ``account.mll_floor_override`` left in config.yaml from an
    earlier sync must not undo the higher floor the bot has worked out and saved since.
    """
    override = cfg.account.mll_floor_override
    if override is None or saved is None:
        return saved if override is None else override
    if override < saved:
        log.warning("Not using account.mll_floor_override ($%s) from config.yaml: the Maximum Loss Limit floor the bot "
                    "saved is higher ($%s), and Topstep's floor never goes down, so the bot keeps $%s. You can delete "
                    "mll_floor_override from config.yaml.", f"{override:,.0f}", f"{saved:,.0f}", f"{saved:,.0f}")
        return saved
    return override


def build_core(
    cfg: BotConfig,
    contract: Contract,
    broker: Broker,
    *,
    clock: Callable[[], datetime],
    account_label: str,
    mll_floor: float | None = None,
    journal: Journal | None = None,
    notifier: Notifier | None = None,
) -> TradingCore:
    plan = PLANS[cfg.account.plan]
    schedule = SessionSchedule(cfg.session)
    schedule.news_pauses = lambda: cfg.news.enabled  # follows the setting when changed from the dashboard/Telegram
    tracker = LossLimitTracker(starting_balance(cfg), plan.max_loss_limit, floor=mll_floor_for(cfg, mll_floor))
    fees = fees_for(cfg, contract)
    risk = RiskManager(cfg.risk, cfg.account, plan, contract, schedule, tracker, fees)
    strategy = create_strategy(cfg.strategy.name, cfg.strategy.params, contract, cfg.instrument.timeframe_minutes)
    orders = OrderManager(
        broker,
        contract,
        fees_round_turn=fees,
        clock=clock,
        use_native_brackets=cfg.execution.use_native_brackets and cfg.mode == "live",
        orphan_policy=cfg.execution.orphan_position_policy,
        entry_timeout=cfg.execution.entry_fill_timeout_seconds,
        strategy_name=strategy.name,
        max_risk_overrun=cfg.execution.max_risk_overrun,
    )
    orders.guard = OrderGuard(risk.max_contracts_topstep)
    return TradingCore(
        cfg=cfg,
        contract=contract,
        broker=broker,
        strategy=strategy,
        risk=risk,
        orders=orders,
        schedule=schedule,
        tracker=tracker,
        clock=clock,
        account_label=account_label,
        journal=journal,
        notifier=notifier,
    )
