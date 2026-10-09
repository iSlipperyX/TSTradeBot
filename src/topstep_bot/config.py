"""Configuration: a YAML file for settings plus a .env file for secrets.

Every field has a safe default, so a minimal config only needs the plan, symbol and strategy.
"""

from __future__ import annotations

import os
from datetime import date, time
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_serializer, field_validator, model_validator

DEFAULT_CONFIG_PATH = Path("config.yaml")
DEFAULT_ENV_PATH = Path(".env")

# Days the bot will not open new trades (CME holidays and early closes). Edit freely.
DEFAULT_NO_TRADE_DATES = [
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25", "2026-06-19",
    "2026-07-03", "2026-09-07", "2026-11-26", "2026-11-27", "2026-12-24", "2026-12-25",
    "2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26", "2027-05-31", "2027-06-18",
    "2027-07-05", "2027-09-06", "2027-11-25", "2027-11-26", "2027-12-24",
]


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _parse_hhmm(value: Any) -> time:
    if isinstance(value, time):
        return value
    if isinstance(value, int):  # YAML 1.1 reads unquoted 08:30 as sexagesimal minutes
        return time(value // 60, value % 60)
    parts = str(value).strip().split(":")
    return time(int(parts[0]), int(parts[1]))


class TimeWindow(_Section):
    start: time
    end: time
    label: str = ""

    @field_validator("start", "end", mode="before")
    @classmethod
    def _parse_times(cls, v: Any) -> time:
        return _parse_hhmm(v)

    @field_serializer("start", "end")
    def _dump_times(self, v: time) -> str:
        return v.strftime("%H:%M")


class ApiConfig(_Section):
    base_url: str = "https://api.topstepx.com"
    user_hub_url: str = "https://rtc.topstepx.com/hubs/user"
    market_hub_url: str = "https://rtc.topstepx.com/hubs/market"
    timeout_seconds: float = 15.0


class AccountConfig(_Section):
    plan: Literal["50K", "100K", "150K"] = "50K"
    stage: Literal["combine", "express", "practice"] = "combine"
    account_id: int | None = None
    account_name: str | None = None
    starting_balance: float | None = None
    mll_floor_override: float | None = Field(
        default=None,
        description="Current Maximum Loss Limit floor shown in your Topstep dashboard. Set it to sync the bot.",
    )
    topstep_daily_loss_limit: float | None = Field(
        default=None,
        description="The optional Topstep Daily Loss Limit, if you added it at checkout: true (your plan's amount: "
        "$1,000 / $2,000 / $3,000) or a dollar amount. Leave empty if your account has none.",
    )
    payout_path: Literal["standard", "consistency"] = Field(
        default="standard",
        description="Express Funded Account payout path chosen at activation (only used for progress reports).",
    )

    @model_validator(mode="before")
    @classmethod
    def _dll_from_plan(cls, data: Any) -> Any:
        """``topstep_daily_loss_limit: true`` means "the DLL that comes with my plan"."""
        if isinstance(data, dict) and isinstance(data.get("topstep_daily_loss_limit"), bool):
            data = dict(data)
            if data["topstep_daily_loss_limit"]:
                from topstep_bot.risk.topstep import PLANS

                plan = PLANS.get(str(data.get("plan", "50K")).upper())
                data["topstep_daily_loss_limit"] = plan.daily_loss_limit if plan else None
            else:
                data["topstep_daily_loss_limit"] = None
        return data


class InstrumentConfig(_Section):
    symbol: str = "MNQ"
    contract_id: str | None = None
    timeframe_minutes: int = Field(default=5, ge=1, le=60)

    @field_validator("symbol")
    @classmethod
    def _upper(cls, v: str) -> str:
        return v.strip().upper()


class StrategyConfig(_Section):
    name: str = "adaptive"
    params: dict[str, Any] = Field(default_factory=dict)


class RiskConfig(_Section):
    risk_per_trade: float = Field(default=150.0, gt=0, description="Dollars risked per trade (stop distance x size).")
    max_contracts: int | None = Field(default=None, ge=1, description="Bot cap; never exceeds Topstep's cap.")
    personal_daily_loss_limit: float = Field(default=500.0, gt=0)
    daily_profit_target: float | None = Field(default=None, description="Stop trading for the day once reached.")
    max_trades_per_day: int = Field(default=4, ge=1)
    max_consecutive_losses: int = Field(default=2, ge=1)
    cooldown_minutes_after_loss: int = Field(default=10, ge=0)
    mll_buffer: float = Field(default=200.0, ge=0, description="Extra cushion kept above the MLL floor.")
    min_stop_ticks: int = Field(default=8, ge=1)
    min_stop_atr: float | None = Field(
        default=None, gt=0,
        description="Also widen stops to at least this many ATRs (14 bars) - stops inside normal noise get hit for no reason.",
    )
    max_stop_ticks: int = Field(default=400, ge=1)
    breakeven_at_r: float | None = Field(default=None, gt=0)
    breakeven_offset_ticks: int = 1
    trail_atr_multiple: float | None = Field(default=None, gt=0)
    fees_per_contract_round_turn: float | None = None
    slippage_ticks: float = Field(default=1.0, ge=0, description="Assumed slippage for paper fills and backtests.")
    ramp_up_days: int = Field(default=3, ge=0, description="First N live trading days use reduced risk.")
    ramp_up_risk_fraction: float = Field(default=0.5, gt=0, le=1)
    consistency_guard: bool = Field(
        default=True,
        description="Combine: close out for the day before today's profit reaches 55% of the profit target "
        "(Topstep's Consistency Target would otherwise raise the target).",
    )
    stop_at_profit_target: bool = Field(
        default=True, description="Combine: stop trading once the profit target is reached, so the pass can't be given back."
    )


class SessionConfig(_Section):
    timezone: str = "America/Chicago"
    trade_start: time = time(8, 30)
    last_entry: time = time(14, 30)
    flatten_at: time = time(15, 0)
    trade_weekdays: list[int] = Field(default_factory=lambda: [0, 1, 2, 3, 4])
    blackout_windows: list[TimeWindow] = Field(default_factory=list)
    no_trade_dates: list[date] = Field(default_factory=lambda: [date.fromisoformat(d) for d in DEFAULT_NO_TRADE_DATES])

    @field_validator("trade_start", "last_entry", "flatten_at", mode="before")
    @classmethod
    def _parse_times(cls, v: Any) -> time:
        return _parse_hhmm(v)

    @field_serializer("trade_start", "last_entry", "flatten_at")
    def _dump_times(self, v: time) -> str:
        return v.strftime("%H:%M")

    @model_validator(mode="after")
    def _check_order(self) -> SessionConfig:
        if not (self.trade_start < self.last_entry < self.flatten_at):
            raise ValueError("session times must satisfy trade_start < last_entry < flatten_at")
        if self.flatten_at > time(15, 8):
            raise ValueError("flatten_at must be 15:08 or earlier: Topstep requires being flat by 15:10 CT")
        return self


class ExecutionConfig(_Section):
    use_native_brackets: bool = Field(
        default=False,
        description="Use ProjectX server-side brackets (requires 'Auto OCO Brackets' in TopstepX risk settings).",
    )
    orphan_position_policy: Literal["flatten", "adopt", "ignore"] = "flatten"
    max_entry_slippage_ticks: int | None = Field(
        default=8, ge=0,
        description="Entries are limit orders at most this many ticks worse than the signal price (None = market).",
    )
    max_risk_overrun: float = Field(
        default=1.5, ge=1.0,
        description="Exit at once if a fill leaves the trade riskier than this multiple of its planned risk.",
    )
    reconcile_interval_seconds: float = 15.0
    entry_fill_timeout_seconds: float = 20.0
    flatten_on_shutdown: bool = True


class DataConfig(_Section):
    warmup_days: int = Field(default=20, ge=1, le=90)
    live_market_data: bool = False


class NotificationsConfig(_Section):
    enabled: bool = True
    events: list[str] = Field(
        default_factory=lambda: ["start", "stop", "entry", "exit", "risk", "error", "daily_summary"]
    )


class TelegramConfig(_Section):
    """Two-way control from Telegram (needs TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env)."""

    control_enabled: bool = True
    allowed_user_ids: list[int] = Field(
        default_factory=list,
        description="Optional: only these Telegram user IDs may send commands (useful in group chats).",
    )
    confirm_dangerous: bool = Field(default=True, description="Ask for confirmation before flatten / stop.")


class NewsConfig(_Section):
    """Automatic no-trade windows around economic releases (live and paper trading only)."""

    enabled: bool = True
    impacts: list[str] = Field(default_factory=lambda: ["High"])
    currencies: list[str] = Field(default_factory=lambda: ["USD"])
    minutes_before: int = Field(default=5, ge=0, le=120)
    minutes_after: int = Field(default=10, ge=0, le=240)
    flatten_before: bool = Field(default=False, description="Also close open trades just before the release.")
    url: str = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"


class RecommendationsConfig(_Section):
    """Trade ideas on the dashboard from every strategy (the bot only trades the configured one)."""

    enabled: bool = True
    strategies: list[str] = Field(default_factory=list, description="Which strategies to show ideas from (empty = all).")


class KnowledgeConfig(_Section):
    """What the bot learns about its strategies while it runs (drives the 'adaptive' strategy)."""

    enabled: bool = True
    auto_train: bool = Field(default=True, description="Retrain from recent history at startup when the base is stale.")
    history_days: int = Field(default=60, ge=10, le=120, description="Days of history to train on.")
    retrain_hours: float = Field(default=20, ge=1, description="Training older than this is refreshed at startup.")
    half_life_days: int = Field(default=20, ge=1, description="Observations lose half their weight after this many days.")
    min_samples: int = Field(default=8, ge=1, description="Evidence needed before a strategy may trade in a slot.")
    min_edge_r: float = Field(default=0.05, description="Minimum shrunk expectancy (in R) to keep trading a strategy.")
    real_trade_weight: float = Field(default=2.0, ge=1.0, description="How much more a real trade counts than an idea.")
    deep_learning: bool = Field(
        default=True,
        description="Long-run memory: keep every bar the bot sees, backfill up to deep_history_days of history and replay "
        "all of it through every strategy once a day. Feeds the reports only; it does not change how the bot trades.",
    )
    deep_history_days: int = Field(default=365, ge=30, le=3650, description="How far back the long-run memory reaches.")


class ServiceConfig(_Section):
    """Unattended 24/7 operation (the controller started by 'topstep-bot start')."""

    keep_awake: bool = Field(default=True, description="Stop Windows from sleeping while the bot runs.")
    daily_restart_time: time | None = Field(
        default=time(16, 5),
        description="Daily maintenance restart (CT) during the CME halt: picks up contract rolls and fresh connections.",
    )
    check_in_time: time | None = Field(default=time(8, 0), description="Weekday 'bot is alive' message (CT).")
    heartbeat_timeout_seconds: int = Field(default=180, ge=30)
    max_restarts_per_hour: int = Field(default=6, ge=1)

    @field_validator("daily_restart_time", "check_in_time", mode="before")
    @classmethod
    def _parse_times(cls, v: Any) -> time | None:
        # YAML reads an unquoted `off` / `no` / `false` as the boolean False - that means "disabled",
        # not midnight (False is an int in Python, so it must be caught before _parse_hhmm).
        if v is None or v is False or (isinstance(v, str) and v.strip().lower() in ("", "off", "no", "false", "none")):
            return None
        if v is True:
            raise ValueError("use a time like \"16:05\", or off to disable")
        return _parse_hhmm(v)

    @field_serializer("daily_restart_time", "check_in_time")
    def _dump_times(self, v: time | None) -> str | None:
        return v.strftime("%H:%M") if v else None


class UpdatesConfig(_Section):
    """Checks GitHub for newer versions of the bot. Nothing is installed until you confirm."""

    enabled: bool = Field(default=True, description="Check for updates automatically.")
    check_every_hours: float = Field(default=6, ge=1, le=168)
    notify: bool = Field(default=True, description="Tell you (Telegram/Discord) once when a new version appears.")
    repo: str = Field(default="iSlipperyX/TSTradeBot", pattern=r"^[\w.-]+/[\w.-]+$", description="GitHub owner/name.")
    branch: str = Field(default="main", min_length=1, description="The branch the bot follows.")


class DashboardConfig(_Section):
    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = 8765
    open_browser: bool = True


class BacktestConfig(_Section):
    data_file: str | None = None
    report_dir: str = "reports"


class BotConfig(_Section):
    mode: Literal["paper", "live"] = "paper"
    log_level: str = "INFO"
    log_dir: str = "logs"
    log_retention_days: int = Field(default=30, ge=1, le=365)
    data_dir: str = "data"
    api: ApiConfig = Field(default_factory=ApiConfig)
    account: AccountConfig = Field(default_factory=AccountConfig)
    instrument: InstrumentConfig = Field(default_factory=InstrumentConfig)
    strategy: StrategyConfig = Field(default_factory=StrategyConfig)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    session: SessionConfig = Field(default_factory=SessionConfig)
    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)
    data: DataConfig = Field(default_factory=DataConfig)
    notifications: NotificationsConfig = Field(default_factory=NotificationsConfig)
    telegram: TelegramConfig = Field(default_factory=TelegramConfig)
    service: ServiceConfig = Field(default_factory=ServiceConfig)
    news: NewsConfig = Field(default_factory=NewsConfig)
    recommendations: RecommendationsConfig = Field(default_factory=RecommendationsConfig)
    knowledge: KnowledgeConfig = Field(default_factory=KnowledgeConfig)
    updates: UpdatesConfig = Field(default_factory=UpdatesConfig)
    dashboard: DashboardConfig = Field(default_factory=DashboardConfig)
    backtest: BacktestConfig = Field(default_factory=BacktestConfig)

    @model_validator(mode="after")
    def _check_strategy(self) -> BotConfig:
        """Build the strategy once so a typo in its name or parameters is reported at load time."""
        from topstep_bot.instruments import SPECS, offline_contract
        from topstep_bot.models import Contract
        from topstep_bot.strategies import create_strategy

        symbol = self.instrument.symbol
        contract = offline_contract(symbol) if symbol in SPECS else Contract("CHECK", symbol, 0.25, 1.0, root=symbol)
        create_strategy(self.strategy.name, self.strategy.params, contract, self.instrument.timeframe_minutes)
        return self

    @model_validator(mode="after")
    def _check_topstep_rules(self) -> BotConfig:
        """Settings that would let the bot break a Topstep rule are refused at load time."""
        from topstep_bot.risk.topstep import PLANS, product_limit

        plan = PLANS[self.account.plan]
        risk = self.risk
        problems = []
        if risk.personal_daily_loss_limit >= plan.max_loss_limit:
            problems.append(f"risk.personal_daily_loss_limit (${risk.personal_daily_loss_limit:,.0f}) must be below the "
                            f"{plan.name} Maximum Loss Limit (${plan.max_loss_limit:,.0f})")
        dll = self.account.topstep_daily_loss_limit
        if dll is not None and risk.personal_daily_loss_limit >= dll:
            problems.append(f"risk.personal_daily_loss_limit (${risk.personal_daily_loss_limit:,.0f}) must be below your "
                            f"Topstep Daily Loss Limit (${dll:,.0f})")
        if risk.risk_per_trade > risk.personal_daily_loss_limit:
            problems.append(f"risk.risk_per_trade (${risk.risk_per_trade:,.0f}) can't be more than "
                            f"risk.personal_daily_loss_limit (${risk.personal_daily_loss_limit:,.0f})")
        if product_limit(self.instrument.symbol, plan) == 0:
            problems.append(f"Topstep does not currently allow trading {self.instrument.symbol}")
        if problems:
            raise ValueError("; ".join(problems))
        return self

    @property
    def data_path(self) -> Path:
        return Path(self.data_dir)

    @property
    def journal_path(self) -> Path:
        return self.data_path / f"journal_{self.mode}.db"

    @property
    def knowledge_path(self) -> Path:
        """One knowledge base per symbol and timeframe (shared by paper and live)."""
        return self.data_path / f"knowledge_{self.instrument.symbol}_{self.instrument.timeframe_minutes}m.json"

    @property
    def longrun_knowledge_path(self) -> Path:
        """What every strategy did on the whole market library (memory.py): reports only, never trading decisions."""
        return self.data_path / f"knowledge_{self.instrument.symbol}_{self.instrument.timeframe_minutes}m_longrun.json"

    @property
    def library_path(self) -> Path:
        """Every price bar the bot has downloaded or imported (memory.py), for all symbols and timeframes."""
        return self.data_path / "market_library.sqlite"


class Secrets(BaseModel):
    username: str | None = None
    api_key: str | None = None
    discord_webhook_url: str | None = None
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None
    github_token: str | None = None  # read-only token for update checks (only needed for a private repository)

    @property
    def has_credentials(self) -> bool:
        return bool(self.username and self.api_key)


def load_env_file(path: Path = DEFAULT_ENV_PATH) -> None:
    """Minimal .env loader: KEY=VALUE lines, '#' comments. Existing env vars win."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip().removeprefix("export ").strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def load_secrets() -> Secrets:
    return Secrets(
        username=os.environ.get("TOPSTEPX_USERNAME") or None,
        api_key=os.environ.get("TOPSTEPX_API_KEY") or None,
        discord_webhook_url=os.environ.get("DISCORD_WEBHOOK_URL") or None,
        telegram_bot_token=os.environ.get("TELEGRAM_BOT_TOKEN") or None,
        telegram_chat_id=os.environ.get("TELEGRAM_CHAT_ID") or None,
        github_token=os.environ.get("GITHUB_TOKEN") or None,
    )


class ConfigError(ValueError):
    """config.yaml can't be used as written. The message says where and why, in plain words."""


def _describe(exc: ValidationError) -> str:
    lines = []
    for err in exc.errors():
        where = ".".join(str(p) for p in err["loc"]) or "config"
        msg = err["msg"].removeprefix("Value error, ")
        if err["type"] == "extra_forbidden":
            msg = "unknown setting (check the spelling and indentation)"
        lines.append(f"  {where}: {msg}")
    return "\n".join(lines)


def load_config(path: Path | str | None = None) -> BotConfig:
    """Load config.yaml (or defaults if missing) and the .env file beside it."""
    cfg_path = Path(path) if path else DEFAULT_CONFIG_PATH
    load_env_file(cfg_path.parent / ".env" if path else DEFAULT_ENV_PATH)
    if not cfg_path.exists():
        if path:
            raise FileNotFoundError(f"Config file not found: {cfg_path}")
        return BotConfig()
    return anchor_folders(config_from_text(cfg_path.read_text(encoding="utf-8"), cfg_path), cfg_path)


def config_from_text(text: str, name: Path | str = DEFAULT_CONFIG_PATH) -> BotConfig:
    """Parse and check config.yaml's contents (folders are left relative). Raises ConfigError."""
    try:
        raw = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"{name} is not valid YAML (check indentation and quotes):\n  {exc}") from None
    if not isinstance(raw, dict):
        raise ConfigError(f"{name} should contain settings like 'mode: paper', one per line")
    try:
        return BotConfig.model_validate(raw)
    except ValidationError as exc:
        raise ConfigError(f"{name} has {exc.error_count()} problem(s):\n{_describe(exc)}") from None


def anchor_folders(cfg: BotConfig, config_path: Path | str) -> BotConfig:
    """Relative folders (data, logs, reports) live next to config.yaml, wherever the bot is started from."""
    base = Path(config_path).resolve().parent
    for owner, attr in ((cfg, "data_dir"), (cfg, "log_dir"), (cfg.backtest, "report_dir")):
        value = Path(getattr(owner, attr))
        if not value.is_absolute():
            setattr(owner, attr, str(base / value))
    return cfg


def save_config(cfg: BotConfig, path: Path | str = DEFAULT_CONFIG_PATH) -> None:
    data = cfg.model_dump(mode="json", exclude_none=True)
    Path(path).write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
