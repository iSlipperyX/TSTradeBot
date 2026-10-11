"""Configuration: a YAML file for settings plus a .env file for secrets.

Every field has a safe default, so a minimal config only needs the plan, symbol and strategy.
"""

from __future__ import annotations

import os
from datetime import date, time
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator, model_validator

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
        description="Only if your account has a Topstep DLL (legacy platforms / opt-in). New TopstepX accounts have none.",
    )
    login: str | None = Field(
        default=None, pattern=r"^[a-z0-9][a-z0-9_-]{0,19}$",
        description="Which stored API key to use (see 'topstep-bot keys'). Default: a key named like the account, "
                    "else the main key.",
    )

    @field_validator("login", mode="before")
    @classmethod
    def _lower_login(cls, v: Any) -> Any:
        return str(v).strip().lower() if v not in (None, "") else None


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
    max_stop_ticks: int = Field(default=400, ge=1)
    breakeven_at_r: float | None = Field(default=None, gt=0)
    breakeven_offset_ticks: int = 1
    trail_atr_multiple: float | None = Field(default=None, gt=0)
    fees_per_contract_round_turn: float | None = None
    slippage_ticks: float = Field(default=1.0, ge=0, description="Assumed slippage for paper fills and backtests.")
    ramp_up_days: int = Field(default=3, ge=0, description="First N live trading days use reduced risk.")
    ramp_up_risk_fraction: float = Field(default=0.5, gt=0, le=1)


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
        default=2, ge=0,
        description="Entries are limit orders at most this many ticks worse than the signal price, and never through the "
                    "best bid/offer (None = market orders).",
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
    per_instrument: bool = Field(
        default=True,
        description="Also pause around the reports that move what you trade (e.g. weekly crude inventories for CL/MCL).",
    )
    url: str = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
    headlines: bool = Field(default=True, description="Show recent headlines about what you trade (information only).")
    headline_refresh_minutes: int = Field(default=15, ge=5, le=240)
    headline_url: str = "https://feeds.finance.yahoo.com/rss/2.0/headline?s={symbol}&region=US&lang=en-US"


class FundedConfig(_Section):
    """Funded-account goals: Combine target and consistency, Express Funded payout progress."""

    payout_path: Literal["standard", "consistency"] = Field(
        default="standard",
        description="Your Express Funded Account payout path, chosen at activation (standard: winning days; "
                    "consistency: best day <= 40% of profit).",
    )
    last_payout_date: date | None = Field(default=None, description="Day of your last payout request (progress restarts after it).")
    winning_day_usd: float = Field(default=150, ge=0, description="Net profit that makes a 'winning day' on the standard path.")
    winning_days_needed: int = Field(default=5, ge=1)
    protect_winning_day: bool = Field(
        default=True,
        description="Express standard path: once today is a winning day, size new trades so a stop-out keeps it one.",
    )
    consistency_limit: float = Field(default=0.40, gt=0, le=1, description="Consistency path: largest day / total profit.")
    consistency_days_needed: int = Field(default=3, ge=1)
    cycle_profit_goal: float | None = Field(
        default=None, gt=0,
        description="Consistency path: profit you aim to build before requesting a payout. Each day is capped at "
                    "consistency_limit x this (default: the plan's Combine profit target).",
    )


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
        return None if v in (None, "", "off") else _parse_hhmm(v)

    @field_serializer("daily_restart_time", "check_in_time")
    def _dump_times(self, v: time | None) -> str | None:
        return v.strftime("%H:%M") if v else None


class DashboardConfig(_Section):
    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = 8765
    open_browser: bool = True


class BacktestConfig(_Section):
    data_file: str | None = None
    report_dir: str = "reports"


# Sections an account may override. Anything not overridden is inherited from the shared settings.
ACCOUNT_SECTIONS = ("account", "instrument", "strategy", "risk", "session", "execution", "data",
                    "notifications", "news", "recommendations", "knowledge", "funded")
# What 'follow' copies from the leader, so both accounts get the same trade calls.
CALL_SECTIONS = ("instrument", "strategy", "session", "knowledge", "news", "recommendations")
# Account names must not be confused with Telegram words (/pause all, /set risk 150, ...).
RESERVED_NAMES = {
    "all", "main", "on", "off", "none", "default", "help", "settings",
    # setting names and their short forms (see remote.py)
    "risk_per_trade", "daily_loss_limit", "max_trades", "max_losses", "max_contracts", "profit_target", "breakeven_r",
    "trail_atr", "trade_start", "last_entry", "news_filter", "news_flatten", "max_slippage", "strategy",
    "risk", "dailyloss", "dll", "maxtrades", "trades", "losses", "contracts", "maxcontracts", "target", "profit",
    "breakeven", "be", "trail", "start", "lastentry", "news", "newsflatten", "slippage",
}
ACCOUNT_SHORTCUTS = ("account_id", "account_name", "plan", "stage", "starting_balance", "mll_floor_override",
                     "topstep_daily_loss_limit", "login")
NAME_PATTERN = r"^[a-z0-9][a-z0-9_-]{0,19}$"


class AccountProfile(_Section):
    """One Topstep account the bot trades. Only what differs from the shared settings needs to be listed.

    Accounts that keep the shared strategy/symbol (or 'follow' another account) get the same trade
    calls at the same bar; each still sizes and risk-checks every trade with its own limits.
    """

    name: str = Field(pattern=NAME_PATTERN, description="Short name used on the dashboard and in Telegram, e.g. c1.")
    enabled: bool = True
    mode: Literal["paper", "live"] | None = Field(default=None, description="Default mode for this account (else the shared mode).")
    follow: str | None = Field(default=None, description="Trade the same calls as this account (copies its strategy and symbol).")
    # shortcuts for the account section
    account_id: int | None = None
    account_name: str | None = None
    plan: Literal["50K", "100K", "150K"] | None = None
    stage: Literal["combine", "express", "practice"] | None = None
    starting_balance: float | None = None
    mll_floor_override: float | None = None
    topstep_daily_loss_limit: float | None = None
    login: str | None = Field(default=None, description="Stored API key to use for this account (see 'topstep-bot keys').")
    # per-account overrides (validated when the account's settings are built)
    account: dict[str, Any] = Field(default_factory=dict)
    instrument: dict[str, Any] = Field(default_factory=dict)
    strategy: dict[str, Any] = Field(default_factory=dict)
    risk: dict[str, Any] = Field(default_factory=dict)
    session: dict[str, Any] = Field(default_factory=dict)
    execution: dict[str, Any] = Field(default_factory=dict)
    data: dict[str, Any] = Field(default_factory=dict)
    notifications: dict[str, Any] = Field(default_factory=dict)
    news: dict[str, Any] = Field(default_factory=dict)
    recommendations: dict[str, Any] = Field(default_factory=dict)
    knowledge: dict[str, Any] = Field(default_factory=dict)
    funded: dict[str, Any] = Field(default_factory=dict)

    @field_validator("name", mode="before")
    @classmethod
    def _lower(cls, v: Any) -> Any:
        return str(v).strip().lower() if v is not None else v

    @field_validator("follow", mode="before")
    @classmethod
    def _lower_follow(cls, v: Any) -> Any:
        return str(v).strip().lower() if v not in (None, "") else None

    def overrides(self) -> dict[str, dict[str, Any]]:
        out = {s: dict(getattr(self, s)) for s in ACCOUNT_SECTIONS if getattr(self, s)}
        shortcuts = {k: getattr(self, k) for k in ACCOUNT_SHORTCUTS if getattr(self, k) is not None}
        if shortcuts:
            out["account"] = {**out.get("account", {}), **shortcuts}
        return out


class MultiAccountConfig(_Section):
    """Rules that apply across all your accounts."""

    hedge_guard: Literal["correlated", "same_product", "off"] = Field(
        default="correlated",
        description="Topstep bans holding opposite positions across accounts. 'correlated' blocks e.g. long MES in one "
                    "account while short MNQ in another; 'same_product' only blocks the same market (NQ/MNQ).",
    )
    stagger_seconds: float = Field(default=3.0, ge=0, le=60, description="Pause between starting each account's bot.")


def _deep_merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in over.items():
        out[k] = _deep_merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


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
    funded: FundedConfig = Field(default_factory=FundedConfig)
    dashboard: DashboardConfig = Field(default_factory=DashboardConfig)
    backtest: BacktestConfig = Field(default_factory=BacktestConfig)
    multi_account: MultiAccountConfig = Field(default_factory=MultiAccountConfig)
    accounts: list[AccountProfile] = Field(default_factory=list)
    active_account: str | None = Field(default=None, exclude=True)  # set by for_account(); never saved

    @model_validator(mode="after")
    def _check_accounts(self) -> BotConfig:
        names = [a.name for a in self.accounts]
        dupes = {n for n in names if names.count(n) > 1}
        if dupes:
            raise ValueError(f"account names must be unique (repeated: {', '.join(sorted(dupes))})")
        reserved = [n for n in names if n in RESERVED_NAMES]
        if reserved:
            raise ValueError(f"account name(s) {', '.join(reserved)} are reserved words - pick another name, e.g. c1")
        ids = [a.account_id for a in self.accounts if a.account_id is not None]
        if len(ids) != len(set(ids)):
            raise ValueError("two accounts in 'accounts' use the same account_id")
        by_name = {a.name: a for a in self.accounts}
        for a in self.accounts:
            if a.follow is None:
                continue
            if a.follow not in by_name:
                raise ValueError(f"account {a.name} follows '{a.follow}', which is not in 'accounts'")
            if a.follow == a.name or by_name[a.follow].follow is not None:
                raise ValueError(f"account {a.name}: follow an account that picks its own calls (no chains)")
            clash = [s for s in CALL_SECTIONS if getattr(a, s)]
            if clash:
                raise ValueError(f"account {a.name} follows {a.follow}, so it can't also set {', '.join(clash)} "
                                 "(those decide the calls) - remove them or remove 'follow'")
        return self

    # ------------------------------------------------------------ accounts

    @property
    def multi(self) -> bool:
        """True when config.yaml lists accounts (each gets its own bot process)."""
        return bool(self.accounts)

    def account_names(self, enabled_only: bool = True) -> list[str]:
        return [a.name for a in self.accounts if a.enabled or not enabled_only]

    def profile(self, name: str) -> AccountProfile:
        for a in self.accounts:
            if a.name == name.strip().lower():
                return a
        raise ValueError(f"No account named '{name}' in config.yaml. Accounts: {', '.join(self.account_names(False)) or 'none'}")

    def for_account(self, name: str | None) -> BotConfig:
        """The effective settings of one account: the shared settings plus that account's overrides.

        ``None`` means 'the only account': the shared settings when no accounts are listed, or the
        single enabled account. Raises ValueError when the choice is ambiguous or unknown.
        """
        if self.active_account and name in (None, "", self.active_account):
            return self
        if not self.accounts:
            if name not in (None, "", "main"):
                raise ValueError(f"No account named '{name}': config.yaml has no 'accounts' list")
            return self
        if name in (None, ""):
            enabled = self.account_names()
            if len(enabled) != 1:
                raise ValueError(f"Several accounts are configured ({', '.join(enabled)}) - choose one with --account")
            name = enabled[0]
        prof = self.profile(name)
        base = self.model_dump(mode="python", exclude={"accounts", "active_account"})
        over = prof.overrides()
        if prof.follow:
            leader = self.profile(prof.follow).overrides()
            over = {**{s: leader[s] for s in CALL_SECTIONS if s in leader}, **over}
        merged = _deep_merge(base, over)
        strat = over.get("strategy", {})
        if strat.get("name", base["strategy"]["name"]) != base["strategy"]["name"]:
            merged["strategy"]["params"] = dict(strat.get("params", {}))  # shared params belong to another strategy
        if "symbol" in over.get("instrument", {}) and "contract_id" not in over["instrument"]:
            merged["instrument"]["contract_id"] = None
        if prof.mode:
            merged["mode"] = prof.mode
        try:
            cfg = BotConfig.model_validate(merged)
        except ValueError as exc:
            raise ValueError(f"account {prof.name}: {exc}") from None
        cfg.accounts = [a.model_copy() for a in self.accounts]
        cfg.active_account = prof.name
        cfg.log_dir = str(Path(self.log_dir) / "accounts" / prof.name)
        return cfg

    @property
    def account_dir(self) -> Path:
        """Where this account's own files live (journal, remote settings, knowledge, heartbeat)."""
        if self.active_account:
            return self.data_path / "accounts" / self.active_account
        return self.data_path

    @property
    def data_path(self) -> Path:
        return Path(self.data_dir)

    def journal_path_for(self, mode: str) -> Path:
        return self.account_dir / f"journal_{mode}.db"

    @property
    def journal_path(self) -> Path:
        return self.journal_path_for(self.mode)

    @property
    def knowledge_path(self) -> Path:
        """One knowledge base per symbol and timeframe (shared by paper and live), per account."""
        return self.account_dir / f"knowledge_{self.instrument.symbol}_{self.instrument.timeframe_minutes}m.json"


ENV_USERNAME = "TOPSTEPX_USERNAME"
ENV_API_KEY = "TOPSTEPX_API_KEY"


def login_env_names(name: str | None) -> tuple[str, str]:
    """.env variable names for a stored login: the main one, or TOPSTEPX_USERNAME_<NAME> / TOPSTEPX_API_KEY_<NAME>."""
    if not name:
        return ENV_USERNAME, ENV_API_KEY
    suffix = login_key(name).upper()
    return f"{ENV_USERNAME}_{suffix}", f"{ENV_API_KEY}_{suffix}"


def login_key(name: str) -> str:
    """How a login name is stored: lower case, '-' as '_' (so 'c-1' and 'C_1' are the same key)."""
    return name.strip().lower().replace("-", "_")


class Login(BaseModel):
    """One stored TopstepX login: a username and an API key."""

    username: str | None = None
    api_key: str | None = None


class Secrets(BaseModel):
    username: str | None = None
    api_key: str | None = None
    discord_webhook_url: str | None = None
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None
    logins: dict[str, Login] = Field(default_factory=dict)  # extra named API keys (lower-case names)
    login_name: str | None = None  # which named login username/api_key came from (None = the main key)

    @property
    def has_credentials(self) -> bool:
        return bool(self.username and self.api_key)

    def login_for(self, cfg: BotConfig) -> str | None:
        """The stored login an account uses: account.login, else a key named like the account, else the main key."""
        if cfg.account.login:
            return cfg.account.login
        if cfg.active_account and login_key(cfg.active_account) in self.logins:
            return cfg.active_account
        return None

    def for_login(self, name: str | None) -> Secrets:
        """These secrets with username/api_key taken from a named login (a key without a username uses the main one)."""
        if not name:
            return self
        login = self.logins.get(login_key(name))
        if login is None or not login.api_key:
            known = ", ".join(sorted(self.logins)) or "none"
            raise ValueError(f"No API key named '{name}' in .env (stored keys: {known}). Add it with 'topstep-bot keys add {name}'.")
        return self.model_copy(update={"username": login.username or self.username, "api_key": login.api_key,
                                       "login_name": login_key(name)})

    def for_account(self, cfg: BotConfig) -> Secrets:
        return self.for_login(self.login_for(cfg))

    def secret_values(self) -> list[str | None]:
        """Everything that must never appear in a log file."""
        return [self.api_key, self.telegram_bot_token, self.discord_webhook_url,
                *(login.api_key for login in self.logins.values())]


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
    logins: dict[str, Login] = {}
    for key, value in os.environ.items():
        for prefix, field in ((ENV_API_KEY + "_", "api_key"), (ENV_USERNAME + "_", "username")):
            if key.startswith(prefix) and value:
                name = login_key(key[len(prefix):])
                setattr(logins.setdefault(name, Login()), field, value)
    return Secrets(
        username=os.environ.get(ENV_USERNAME) or None,
        api_key=os.environ.get(ENV_API_KEY) or None,
        discord_webhook_url=os.environ.get("DISCORD_WEBHOOK_URL") or None,
        telegram_bot_token=os.environ.get("TELEGRAM_BOT_TOKEN") or None,
        telegram_chat_id=os.environ.get("TELEGRAM_CHAT_ID") or None,
        logins={k: v for k, v in logins.items() if v.api_key},
    )


def load_config(path: Path | str | None = None) -> BotConfig:
    """Load config.yaml (or defaults if missing) and the .env file beside it."""
    cfg_path = Path(path) if path else DEFAULT_CONFIG_PATH
    load_env_file(cfg_path.parent / ".env" if path else DEFAULT_ENV_PATH)
    if not cfg_path.exists():
        if path:
            raise FileNotFoundError(f"Config file not found: {cfg_path}")
        return BotConfig()
    raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    cfg = BotConfig.model_validate(raw)
    # Relative folders live next to config.yaml, wherever the bot is started from.
    base = cfg_path.resolve().parent
    for owner, attr in ((cfg, "data_dir"), (cfg, "log_dir"), (cfg.backtest, "report_dir")):
        value = Path(getattr(owner, attr))
        if not value.is_absolute():
            setattr(owner, attr, str(base / value))
    return cfg


def save_config(cfg: BotConfig, path: Path | str = DEFAULT_CONFIG_PATH) -> None:
    data = cfg.model_dump(mode="json", exclude_none=True)
    Path(path).write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
