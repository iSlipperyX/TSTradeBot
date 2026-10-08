"""Remote control of settings and trades (used by the dashboard and Telegram).

Settings
  A fixed list of settings can be changed while the bot runs. Each has safe bounds tied to your
  Topstep plan, takes effect immediately, is logged with who changed it, and is saved to
  data/remote_settings.json (config.yaml is never rewritten). `reset` returns to config.yaml.
  Never changeable remotely: mode (paper/live), account, symbol, credentials, and Topstep's own
  rules (Maximum Loss Limit, contract caps, flat by 15:10 CT).

Trades from recommendations
  An idea can be taken (optionally at a smaller size). It is re-priced at the current market,
  sized with the same risk rules as every bot trade (never larger), protected by its stop and
  target, and closed by the session flatten like any other trade.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from topstep_bot.models import Signal
from topstep_bot.strategies import STRATEGIES

if TYPE_CHECKING:
    from topstep_bot.engine import TradingCore

log = logging.getLogger("topstep_bot.remote")

IDEA_MAX_AGE = timedelta(minutes=15)


@dataclass
class SettingSpec:
    key: str
    label: str
    kind: str  # money | int | float | time | bool | choice | optional_int | optional_float | optional_money
    path: tuple[str, str]
    help: str
    bounds: Callable[[TradingCore], tuple[float, float]] | None = None
    choices: Callable[[TradingCore], list[str]] | None = None
    riskier: Callable[[Any, Any], bool] = lambda old, new: False


def _mll(core: TradingCore) -> float:
    return core.risk.plan.max_loss_limit


def _higher(old: Any, new: Any) -> bool:
    if old is None:
        return False
    return new is None or new > old


SPECS: list[SettingSpec] = [
    SettingSpec("risk_per_trade", "Risk per trade ($)", "money", ("risk", "risk_per_trade"),
                "Dollars lost if a trade hits its stop; position size is calculated from this.",
                bounds=lambda c: (10, round(0.15 * _mll(c))), riskier=_higher),
    SettingSpec("daily_loss_limit", "Daily loss limit ($)", "money", ("risk", "personal_daily_loss_limit"),
                "Stop trading for the day after losing this much (including open P&L).",
                bounds=lambda c: (50, round(min(0.5 * _mll(c), (c.risk.topstep_dll or 1e9) * 0.9))), riskier=_higher),
    SettingSpec("max_trades", "Max trades per day", "int", ("risk", "max_trades_per_day"),
                "No new trades after this many.", bounds=lambda c: (1, 20), riskier=_higher),
    SettingSpec("max_losses", "Max losses in a row", "int", ("risk", "max_consecutive_losses"),
                "Done for the day after this many consecutive losses.", bounds=lambda c: (1, 10), riskier=_higher),
    SettingSpec("max_contracts", "Max contracts (blank = Topstep cap)", "optional_int", ("risk", "max_contracts"),
                "Extra cap on position size. Topstep's own cap always applies.",
                bounds=lambda c: (1, c.risk.max_contracts_topstep()), riskier=_higher),
    SettingSpec("profit_target", "Daily profit target ($, blank = default)", "optional_money", ("risk", "daily_profit_target"),
                "No new trades after making this much in a day.",
                bounds=lambda c: (50, round(c.risk.plan.profit_target)), riskier=lambda o, n: False),
    SettingSpec("breakeven_r", "Move stop to breakeven at (R, blank = off)", "optional_float", ("risk", "breakeven_at_r"),
                "Once a trade is this many R in profit, the stop moves to entry.", bounds=lambda c: (0.3, 5)),
    SettingSpec("trail_atr", "ATR trailing stop (x ATR, blank = off)", "optional_float", ("risk", "trail_atr_multiple"),
                "Trail the stop this many ATRs behind price.", bounds=lambda c: (0.5, 10)),
    SettingSpec("trade_start", "First entry time (CT)", "time", ("session", "trade_start"),
                "No new trades before this time."),
    SettingSpec("last_entry", "Last entry time (CT)", "time", ("session", "last_entry"),
                "No new trades after this time.", riskier=_higher),
    SettingSpec("news_filter", "Pause around high-impact news", "bool", ("news", "enabled"),
                "No new trades from 5 min before to 10 min after big economic releases.",
                riskier=lambda o, n: bool(o) and not n),
    SettingSpec("news_flatten", "Close trades before news", "bool", ("news", "flatten_before"),
                "Also close open trades just before a big release."),
    SettingSpec("max_slippage", "Max entry slippage (ticks)", "int", ("execution", "max_entry_slippage_ticks"),
                "Entries can't fill more than this many ticks worse than the signal price.",
                bounds=lambda c: (0, 40), riskier=_higher),
    SettingSpec("strategy", "Auto-traded strategy", "choice", ("strategy", "name"),
                "Which strategy the bot trades by itself (switches when flat).",
                choices=lambda c: list(STRATEGIES), riskier=lambda o, n: o != n),
]
SPEC_BY_KEY = {s.key: s for s in SPECS}
ALIASES = {"risk": "risk_per_trade", "dailyloss": "daily_loss_limit", "dll": "daily_loss_limit",
           "maxtrades": "max_trades", "trades": "max_trades", "losses": "max_losses", "contracts": "max_contracts",
           "maxcontracts": "max_contracts", "target": "profit_target", "profit": "profit_target",
           "breakeven": "breakeven_r", "be": "breakeven_r", "trail": "trail_atr", "start": "trade_start",
           "lastentry": "last_entry", "news": "news_filter", "newsflatten": "news_flatten",
           "slippage": "max_slippage"}


class SettingError(ValueError):
    pass


class RemoteControl:
    def __init__(self, core: TradingCore, store: Path):
        self.core = core
        self.store = Path(store)
        self.overrides: dict[str, Any] = {}
        self.pending_strategy: str | None = None
        self.original = self.snapshot_original()  # config.yaml values, for reset

    # ------------------------------------------------------------ settings

    def _get(self, spec: SettingSpec) -> Any:
        section, attr = spec.path
        return getattr(getattr(self.core.cfg, section), attr)

    def _set(self, spec: SettingSpec, value: Any) -> None:
        section, attr = spec.path
        setattr(getattr(self.core.cfg, section), attr, value)

    @staticmethod
    def resolve_key(key: str) -> SettingSpec:
        k = key.strip().lower().replace("-", "_")
        k = ALIASES.get(k.replace("_", ""), ALIASES.get(k, k))
        spec = SPEC_BY_KEY.get(k)
        if spec is None:
            raise SettingError(f"Unknown setting '{key}'. Changeable: {', '.join(SPEC_BY_KEY)}")
        return spec

    def parse(self, spec: SettingSpec, raw: Any) -> Any:
        text = "" if raw is None else str(raw).strip().replace("$", "").replace(",", "")
        if spec.kind.startswith("optional") and text.lower() in ("", "none", "off", "default", "-"):
            return None
        try:
            if spec.kind in ("money", "optional_money", "float", "optional_float"):
                value: Any = float(text)
            elif spec.kind in ("int", "optional_int"):
                value = int(float(text))
            elif spec.kind == "bool":
                if text.lower() in ("1", "true", "on", "yes", "y"):
                    value = True
                elif text.lower() in ("0", "false", "off", "no", "n"):
                    value = False
                else:
                    raise ValueError
            elif spec.kind == "time":
                hh, mm = text.split(":")[:2]
                value = time(int(hh), int(mm))
            elif spec.kind == "choice":
                value = text.lower()
                if value not in spec.choices(self.core):
                    raise SettingError(f"Choose one of: {', '.join(spec.choices(self.core))}")
            else:
                raise ValueError
        except SettingError:
            raise
        except (ValueError, TypeError):
            raise SettingError(f"'{raw}' is not a valid value for {spec.label}") from None
        if spec.bounds and value is not None and spec.kind != "bool":
            lo, hi = spec.bounds(self.core)
            if not lo <= value <= hi:
                raise SettingError(f"{spec.label} must be between {lo:g} and {hi:g}")
        self._cross_check(spec, value)
        return value

    def _cross_check(self, spec: SettingSpec, value: Any) -> None:
        cfg = self.core.cfg
        if spec.key == "risk_per_trade" and value > cfg.risk.personal_daily_loss_limit:
            raise SettingError("Risk per trade can't be larger than the daily loss limit")
        if spec.key == "daily_loss_limit" and value < cfg.risk.risk_per_trade:
            raise SettingError("Daily loss limit can't be smaller than the risk per trade")
        s = cfg.session
        start = value if spec.key == "trade_start" else s.trade_start
        last = value if spec.key == "last_entry" else s.last_entry
        if spec.kind == "time" and not (start < last < s.flatten_at):
            raise SettingError(f"Times must satisfy first entry < last entry < flatten time ({s.flatten_at:%H:%M} CT)")

    def preview(self, key: str, raw: Any) -> dict:
        """Validate a change without applying it."""
        spec = self.resolve_key(key)
        old, new = self._get(spec), self.parse(spec, raw)
        return {"key": spec.key, "label": spec.label, "old": _show(old), "new": _show(new),
                "riskier": spec.riskier(old, new), "changed": old != new}

    def apply(self, key: str, raw: Any, source: str, persist: bool = True) -> str:
        spec = self.resolve_key(key)
        old, new = self._get(spec), self.parse(spec, raw)
        if old == new:
            return f"{spec.label} is already {_show(new)}."
        if spec.key == "strategy":
            message = self._switch_strategy(new, source)
        else:
            self._set(spec, new)
            message = f"{spec.label}: {_show(old)} -> {_show(new)}"
        self.overrides[spec.key] = _show(new) if new is not None else None
        if persist:
            self._save()
        self.core.event("warning", f"Setting changed from {source}: {message}", "risk")
        return message

    def _switch_strategy(self, name: str, source: str) -> str:
        if not self.core.orders.is_flat:
            self.pending_strategy = name
            return f"Will switch the auto-traded strategy to {STRATEGIES[name].title} once the current trade closes"
        old = self.core.strategy.title
        self.core.switch_strategy(name)
        self.pending_strategy = None
        return f"Auto-traded strategy: {old} -> {self.core.strategy.title}"

    def on_flat(self) -> None:
        """Called each bar: apply a strategy switch that was waiting for the position to close."""
        if self.pending_strategy and self.core.orders.is_flat:
            name, self.pending_strategy = self.pending_strategy, None
            self.core.switch_strategy(name)
            self.core.event("warning", f"Auto-traded strategy is now {self.core.strategy.title}", "risk")

    def reset(self, source: str) -> str:
        """Undo all remote changes (back to config.yaml)."""
        for key, value in self.original.items():
            spec = SPEC_BY_KEY[key]
            if self._get(spec) != value:
                if key == "strategy":
                    self._switch_strategy(value, source)
                else:
                    self._set(spec, value)
        self.overrides.clear()
        self._save()
        self.core.event("warning", f"All remote setting changes undone from {source} (back to config.yaml)", "risk")
        return "All settings are back to config.yaml."

    def snapshot_original(self) -> dict[str, Any]:
        return {s.key: self._get(s) for s in SPECS}

    def load_saved(self) -> list[str]:
        """Re-apply changes saved by earlier remote edits. Invalid ones are skipped and reported."""
        try:
            saved = json.loads(self.store.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        applied = []
        for key, value in saved.items():
            try:
                applied.append(self.apply(key, "" if value is None else value, "saved remote settings", persist=False))
            except SettingError as exc:
                log.warning("Ignoring saved remote setting %s=%s: %s", key, value, exc)
        return applied

    def _save(self) -> None:
        try:
            self.store.parent.mkdir(parents=True, exist_ok=True)
            self.store.write_text(json.dumps(self.overrides, indent=2), encoding="utf-8")
        except OSError as exc:
            log.warning("Could not save remote settings: %s", exc)

    def describe(self) -> list[dict]:
        out = []
        for spec in SPECS:
            value = self._get(spec)
            item = {"key": spec.key, "label": spec.label, "kind": spec.kind, "value": _show(value),
                    "help": spec.help, "changed": spec.key in self.overrides}
            if spec.bounds and spec.kind != "bool":
                lo, hi = spec.bounds(self.core)
                item["min"], item["max"] = lo, hi
            if spec.choices:
                item["choices"] = spec.choices(self.core)
            out.append(item)
        return out

    def settings_text(self) -> str:
        lines = ["Settings you can change (/set <name> <value>, /reset to undo):"]
        for d in self.describe():
            rng = f" [{d['min']:g}-{d['max']:g}]" if "min" in d else ""
            mark = " *" if d["changed"] else ""
            lines.append(f"{d['key']}: {d['value'] if d['value'] is not None else 'off'}{rng}{mark}")
        lines.append("* = changed remotely. Examples: /set risk 150, /set dailyloss 400, /set strategy orb, /set news off")
        return "\n".join(lines)

    # ------------------------------------------------------------ trades

    async def take_idea(self, rec_id: str, source: str, size: int | None = None) -> str:
        """Trade a recommendation now (re-priced, risk-sized; never bigger than the bot would trade)."""
        core = self.core
        book = core.recommender
        if book is None:
            raise SettingError("Recommendations are turned off")
        rec = next((r for r in book.items if r.id == rec_id), None)
        if rec is None:
            raise SettingError(f"Recommendation {rec_id} not found")
        if rec.status not in ("idea", "tracking", "skipped") or not rec.is_open:
            raise SettingError("That recommendation is no longer open")
        if core.clock() - rec.created > IDEA_MAX_AGE:
            raise SettingError("That idea is too old to take (over 15 minutes)")
        if not core.orders.is_flat:
            raise SettingError("Already in a trade - one position at a time")
        if core.halted:
            raise SettingError("The bot is halted; restart it to trade again")
        reason = core.risk.entry_block_reason(core.clock(), core.balance, manual=True)
        if reason:
            raise SettingError(f"Not allowed right now: {reason}")
        price = core.last_price if core.last_price is not None else rec.entry
        sig = Signal("long" if rec.side.sign > 0 else "short", rec.stop, rec.target, rec.reason)
        plan = core.plan_entry(sig, price)
        if isinstance(plan, str):
            raise SettingError(f"Can't take it at {price}: {plan}")
        if size is not None:
            if size < 1:
                raise SettingError("Size must be at least 1")
            plan.size = min(size, plan.size)
            plan.planned_risk = plan.size * core.risk.risk_per_contract(
                price + plan.side.sign * core.contract.price_offset(core.cfg.execution.max_entry_slippage_ticks or 0),
                plan.stop)
        trade = await core.orders.enter(
            plan.side, plan.size, plan.stop, plan.target,
            f"taken from {source}: {rec.title} idea {rec.id} - {rec.reason}", ref_price=price,
            limit_price=plan.limit, planned_risk=plan.planned_risk, strategy=rec.strategy,
        )
        if trade is None:
            raise SettingError("The order could not be placed - see the activity log")
        rec.status, rec.trade_tag, rec.hypothetical = "taken", trade.tag, False
        rec.size, rec.risk_usd, rec.entry, rec.stop, rec.target = plan.size, plan.planned_risk, price, plan.stop, plan.target
        book._store(rec)
        msg = (f"Took {rec.title} idea: {plan.side.label} {plan.size} {core.contract.name} @ ~{price}, stop {plan.stop}"
               + (f", target {plan.target}" if plan.target else "") + f" (risk ${plan.planned_risk:,.0f})")
        core.event("warning", f"{msg} - requested from {source}", "entry")
        return msg


def _show(value: Any) -> Any:
    if isinstance(value, time):
        return value.strftime("%H:%M")
    if isinstance(value, datetime):
        return value.isoformat()
    return value
