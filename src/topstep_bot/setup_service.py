"""The dashboard's Setup tab: everything the setup wizard asked for, filled in from the browser.

  * TopstepX login: tested against TopstepX before it is saved, then the accounts it can see
    are offered (Live Funded Accounts are left out: Topstep doesn't allow API trading on them).
  * Account, goal (pass the Combine or teach the bot), symbol, strategy and risk: written into
    config.yaml in place, so comments and hand edits elsewhere in the file are kept. The result
    is checked before it is saved, the previous file is kept as config.yaml.bak, and a change
    that would let the bot break a Topstep rule is refused with the reason.
  * Telegram and Discord alerts, update checks for a private copy, and starting with Windows.

Secrets (API key, bot tokens, webhooks) go to .env on this PC and are never sent back to the
browser: the page only learns whether each one is saved.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from topstep_bot.config import BotConfig, ConfigError, anchor_folders, config_from_text, load_config, load_env_file
from topstep_bot.config_edit import REMOVE, set_values
from topstep_bot.instruments import SPECS
from topstep_bot.risk.topstep import PLANS, product_limit
from topstep_bot.strategies import STRATEGIES
from topstep_bot.wizard import SYMBOL_CHOICES

if TYPE_CHECKING:
    from topstep_bot.controller import Controller

log = logging.getLogger("topstep_bot.setup")

STAGES = [("combine", "Trading Combine (evaluation)"), ("express", "Express Funded Account (XFA)"),
          ("practice", "Practice account")]
LEARN_MIN_TRADES = 8
ENV_KEYS = {"username": "TOPSTEPX_USERNAME", "api_key": "TOPSTEPX_API_KEY", "telegram_bot_token": "TELEGRAM_BOT_TOKEN",
            "telegram_chat_id": "TELEGRAM_CHAT_ID", "discord_webhook_url": "DISCORD_WEBHOOK_URL",
            "github_token": "GITHUB_TOKEN"}


def load_for_server(config_path: str | None) -> tuple[BotConfig, str | None]:
    """The configuration the server starts with, and what is wrong with config.yaml (if anything).

    The dashboard must open even when config.yaml is missing or broken, so a problem is returned
    (and shown on the Setup tab) instead of raised; the defaults stand in until it is fixed."""
    path = Path(config_path or "config.yaml")
    if not path.exists():  # not set up yet: that's what the Setup tab is for, not a problem
        load_env_file(path.parent / ".env")
        return anchor_folders(BotConfig(), path), None
    try:
        return load_config(path), None
    except ConfigError as exc:
        return anchor_folders(BotConfig(), path), str(exc)


def suggested_risk(plan: str, goal: str, topstep_dll: bool) -> dict:
    """The wizard's suggestions: risk about 7.5% of the Maximum Loss Limit per trade (5% when
    learning) and stop for the day after losing 25% of it (and below Topstep's DLL)."""
    p = PLANS[plan]
    ceiling = min(p.max_loss_limit, p.daily_loss_limit) if topstep_dll else p.max_loss_limit
    daily = min(round(p.max_loss_limit * 0.25), round(ceiling * 0.8))
    risk = min(round(p.max_loss_limit * (0.05 if goal == "learn" else 0.075)), daily)
    return {"risk_per_trade": risk, "daily_loss_limit": daily, "daily_ceiling": ceiling,
            "max_trades": LEARN_MIN_TRADES if goal == "learn" else 4}


def goal_of(cfg: BotConfig) -> str:
    """'learn' when config.yaml is set up to teach the bot (as the wizard writes it), else 'pass'."""
    return "learn" if cfg.strategy.name == "adaptive" and cfg.strategy.params.get("trade_unproven") is True else "pass"


class SetupService:
    def __init__(self, ctl: Controller, config_error: str | None = None):
        self.ctl = ctl
        self.config_path = Path(ctl.config_path or "config.yaml")
        self.env_path = self.config_path.parent / ".env"
        self.config_error = config_error
        self.accounts: list[dict] = []  # from the last successful login (for names and hints)
        self.saved_at: float | None = None  # last save while the bot was running (it needs a restart)

    # ---------------------------------------------------------------- state for the page

    @property
    def configured(self) -> bool:
        return self.config_path.exists() and self.config_error is None

    def blocker(self) -> str | None:
        """Why the bot can't be started yet, in plain words (None = it can)."""
        if self.config_error:
            return "config.yaml has a problem. Fix it on the Setup tab first."
        if not self.config_path.exists():
            return "Finish setup first: fill in the Setup tab and press Save."
        if not self.ctl.secrets.has_credentials:
            return "Add your TopstepX username and API key on the Setup tab first."
        return None

    def restart_needed(self) -> bool:
        bot = self.ctl.bot
        return bool(self.saved_at and bot.running and (bot.started_at or 0) < self.saved_at)

    def summary(self) -> dict:
        """The small part of the setup state sent with every status poll."""
        return {"configured": self.configured, "blocker": self.blocker(), "error": self.config_error,
                "restart_needed": self.restart_needed()}

    def state(self) -> dict:
        cfg, s = self.ctl.cfg, self.ctl.secrets
        a = cfg.account
        dll = a.topstep_daily_loss_limit is not None
        goal = goal_of(cfg)
        return {
            **self.summary(),
            "path": str(self.config_path.resolve()),
            "exists": self.config_path.exists(),
            "mode": self.ctl.bot.mode,
            "login": {"username": s.username or "", "api_key_saved": bool(s.api_key)},
            "accounts": self.accounts,
            "values": {
                "account_id": a.account_id, "account_name": a.account_name, "plan": a.plan, "stage": a.stage,
                "goal": goal, "topstep_dll": dll, "payout_path": a.payout_path,
                "symbol": cfg.instrument.symbol, "strategy": cfg.strategy.name,
                "risk_per_trade": cfg.risk.risk_per_trade, "daily_loss_limit": cfg.risk.personal_daily_loss_limit,
                "max_trades": cfg.risk.max_trades_per_day,
            },
            "alerts": {
                "telegram": bool(s.telegram_bot_token and s.telegram_chat_id),
                "telegram_chat": s.telegram_chat_id or "",
                "telegram_control": cfg.telegram.control_enabled,
                "telegram_running": self.ctl.telegram is not None,
                "discord": bool(s.discord_webhook_url),
            },
            "github_token_saved": bool(s.github_token),
            "autostart": self._autostart_state(),
            "choices": {
                "plans": [{"name": p.name, "profit_target": p.profit_target, "max_loss_limit": p.max_loss_limit,
                           "max_minis": p.max_minis, "daily_loss_limit": p.daily_loss_limit} for p in PLANS.values()],
                "stages": [{"key": k, "label": label} for k, label in STAGES],
                "symbols": [{"key": sym, "label": SPECS[sym].description, "tick_value": SPECS[sym].tick_value,
                             "blocked": [p for p in PLANS if product_limit(sym, PLANS[p]) == 0]}
                            for sym in SYMBOL_CHOICES if sym in SPECS],
                "strategies": [{"key": c.name, "title": c.title, "description": c.description} for c in STRATEGIES.values()],
                "suggested": {f"{p}|{g}|{int(d)}": suggested_risk(p, g, d)
                              for p in PLANS for g in ("pass", "learn") for d in (False, True)},
            },
        }

    @staticmethod
    def _autostart_state() -> dict:
        from topstep_bot import autostart

        try:
            return {"supported": True, "enabled": autostart.script_path().exists()}
        except OSError:  # not Windows
            return {"supported": False, "enabled": False}

    # ---------------------------------------------------------------- secrets

    def _set_secrets(self, values: dict[str, str | None]) -> None:
        """Write secrets to .env and make them live at once (the next bot start inherits them)."""
        from topstep_bot.wizard import update_env_file

        update_env_file(self.env_path, {ENV_KEYS[k]: (v or "") for k, v in values.items()})
        for k, v in values.items():
            os.environ[ENV_KEYS[k]] = v or ""
            setattr(self.ctl.secrets, k, v or None)

    # ---------------------------------------------------------------- TopstepX login

    async def login(self, body: dict) -> dict:
        from topstep_bot.api.rest import ProjectXClient, ProjectXError
        from topstep_bot.preflight import account_hints

        s = self.ctl.secrets
        username = str(body.get("username") or "").strip()
        api_key = str(body.get("api_key") or "").strip()
        if not username:
            raise ValueError("Type your TopstepX username (your TopstepX login name, not your email).")
        if not api_key:
            if not s.api_key:
                raise ValueError("Paste your API key (TopstepX > Settings > API).")
            api_key = s.api_key  # re-check with the saved key
        api = self.ctl.cfg.api
        try:
            async with ProjectXClient(username, api_key, api.base_url, api.timeout_seconds) as client:
                found = await client.search_accounts(only_active=True)
        except ProjectXError as exc:
            raise ValueError(f"TopstepX did not accept this login: {exc}. Check the username and the API key; "
                             "nothing was saved.") from None
        except Exception as exc:  # noqa: BLE001 - network trouble: keep what was typed, say so
            self._set_secrets({"username": username, "api_key": api_key})
            log.warning("Login check could not reach TopstepX: %s", exc)
            return {"message": f"Saved, but TopstepX could not be reached to check the login ({type(exc).__name__}). "
                               "Try Connect again when your internet is back.", "accounts": self.accounts, "excluded": []}
        self._set_secrets({"username": username, "api_key": api_key})
        excluded = [a.name for a in found if not a.simulated]
        self.accounts = []
        for a in found:
            if not a.simulated:
                continue
            plan, stage = account_hints(a.name)
            self.accounts.append({"id": a.id, "name": a.name, "balance": a.balance, "can_trade": a.can_trade,
                                  "plan": plan, "stage": stage, "dll": "DLL" in a.name.upper()})
        log.info("Setup: TopstepX login OK for %s, %d account(s)", username, len(found))
        n = len(self.accounts)
        msg = f"Login OK and saved. Found {n} account{'s' if n != 1 else ''} the bot can use."
        if excluded:
            msg += f" Not offered: {', '.join(excluded)} (Topstep doesn't allow Live Funded Accounts to trade through the API)."
        return {"message": msg, "accounts": self.accounts, "excluded": excluded}

    # ---------------------------------------------------------------- config.yaml

    def _changes(self, v: dict, old_goal: str | None, fresh: bool) -> dict:
        cfg = self.ctl.cfg
        plan, stage, goal = v["plan"], v["stage"], v["goal"]
        changes: dict[tuple[str | None, str], Any] = {
            ("account", "plan"): plan, ("account", "stage"): stage,
            ("account", "topstep_daily_loss_limit"): True if v["topstep_dll"] and stage != "practice" else REMOVE,
            ("account", "payout_path"): v["payout_path"],
            ("instrument", "symbol"): v["symbol"], ("strategy", "name"): v["strategy"],
            ("risk", "risk_per_trade"): v["risk_per_trade"], ("risk", "personal_daily_loss_limit"): v["daily_loss_limit"],
            ("risk", "max_trades_per_day"): v["max_trades"],
        }
        if v["account_id"] is not None:
            changes[("account", "account_id")] = v["account_id"]
            if v["account_name"]:
                changes[("account", "account_name")] = v["account_name"]
        # strategy parameters: kept while the strategy stays the same (e.g. after 'tune --save')
        params = dict(cfg.strategy.params) if not fresh and v["strategy"] == cfg.strategy.name else {}
        if v["strategy"] == "adaptive":
            if goal == "learn":
                params["trade_unproven"] = True
            else:
                params.pop("trade_unproven", None)
        changes[("strategy", "params")] = params
        if goal != old_goal:  # the goal's risk profile, as the wizard writes it
            if goal == "learn":
                changes.update({("risk", "daily_profit_target"): PLANS[plan].profit_target,
                                ("risk", "consistency_guard"): False, ("risk", "max_consecutive_losses"): 4,
                                ("risk", "cooldown_minutes_after_loss"): 5})
            else:
                changes.update({("risk", "daily_profit_target"): REMOVE, ("risk", "consistency_guard"): True,
                                ("risk", "max_consecutive_losses"): 2, ("risk", "cooldown_minutes_after_loss"): REMOVE})
        elif goal == "learn":
            changes[("risk", "daily_profit_target")] = PLANS[plan].profit_target  # follows a plan change
        return changes

    @staticmethod
    def _form(body: dict) -> dict:
        def number(key: str, label: str) -> float:
            raw = str(body.get(key, "")).replace("$", "").replace(",", "").strip()
            try:
                value = float(raw)
            except ValueError:
                raise ValueError(f"{label}: type a number.") from None
            if value <= 0:
                raise ValueError(f"{label} must be more than 0.")
            return value

        plan = str(body.get("plan", "")).upper()
        if plan not in PLANS:
            raise ValueError("Choose the account size (50K, 100K or 150K).")
        stage = str(body.get("stage", ""))
        if stage not in dict(STAGES):
            raise ValueError("Choose the account type.")
        symbol = str(body.get("symbol", "")).upper()
        if symbol not in SPECS:
            raise ValueError("Choose what to trade.")
        strategy = str(body.get("strategy", ""))
        if strategy not in STRATEGIES:
            raise ValueError("Choose a strategy.")
        goal = "learn" if body.get("goal") == "learn" and stage in ("combine", "practice") else "pass"
        account_id = body.get("account_id")
        try:
            account_id = int(account_id) if account_id not in (None, "") else None
        except (TypeError, ValueError):
            raise ValueError("Pick the account from the list.") from None
        max_trades = number("max_trades", "Max trades per day")
        if max_trades != int(max_trades) or not 1 <= max_trades <= 50:
            raise ValueError("Max trades per day must be a whole number from 1 to 50.")
        if goal == "learn":
            strategy = "adaptive"  # learning runs every strategy and learns from all of them
        return {
            "plan": plan, "stage": stage, "goal": goal, "symbol": symbol, "strategy": strategy,
            "account_id": account_id, "account_name": str(body.get("account_name") or "").strip() or None,
            "topstep_dll": bool(body.get("topstep_dll")),
            "payout_path": "consistency" if body.get("payout_path") == "consistency" and stage == "express" else "standard",
            "risk_per_trade": number("risk_per_trade", "Risk per trade"),
            "daily_loss_limit": number("daily_loss_limit", "Daily loss limit"),
            "max_trades": int(max_trades),
        }

    def _fresh_text(self, v: dict) -> str:
        from topstep_bot.wizard import render_config

        return render_config(
            mode=self.ctl.bot.mode, plan=v["plan"], stage=v["stage"], account_id=v["account_id"], symbol=v["symbol"],
            strategy=v["strategy"], risk_per_trade=v["risk_per_trade"], daily_loss=v["daily_loss_limit"],
            max_trades=v["max_trades"], topstep_dll=v["topstep_dll"] and v["stage"] != "practice",
            payout_path=v["payout_path"], goal=v["goal"],
        )

    async def save(self, body: dict) -> dict:
        v = self._form(body)
        if v["account_name"] is None and v["account_id"] is not None:
            v["account_name"] = next((a["name"] for a in self.accounts if a["id"] == v["account_id"]), None)
        path = self.config_path
        original = path.read_text(encoding="utf-8") if path.exists() else None
        fresh = original is None or bool(body.get("fresh"))
        if fresh:
            text = set_values(self._fresh_text(v), {("risk", "max_trades_per_day"): v["max_trades"],
                                                    ("account", "account_name"): v["account_name"] or REMOVE})
        else:
            old_goal = None if self.config_error else goal_of(self.ctl.cfg)
            text = set_values(original, self._changes(v, old_goal, fresh=False))
            text = _goal_header(text, v["goal"])
        try:
            config_from_text(text, path.name)
        except ConfigError as exc:
            raise ValueError(f"Not saved: {_problems(exc)}") from None
        if original is not None:
            path.with_name(path.name + ".bak").write_text(original, encoding="utf-8")
        path.write_text(text, encoding="utf-8")
        self._reload()
        log.info("Setup saved from the dashboard (%s %s, %s, %s, goal %s)", v["plan"], v["stage"], v["symbol"],
                 v["strategy"], v["goal"])
        self.ctl.bot._event("info", f"Setup saved from the dashboard: {v['plan']} {v['stage']}, {v['symbol']}, "
                                    f"{v['strategy']}, ${v['risk_per_trade']:,.0f} per trade")
        bot = self.ctl.bot
        if bot.running:
            self.saved_at = time.time()
            return {"message": "Saved. The bot is running with the old settings: press Restart bot to use the new ones "
                               "(an open trade is closed first)."}
        backup = " The previous version is in config.yaml.bak." if original is not None else ""
        return {"message": f"Saved.{backup} Press Start when you're ready."}

    def _reload(self) -> None:
        """Use the saved config.yaml in the server at once (the bot reads it when it starts)."""
        new = load_config(self.ctl.config_path)
        cfg = self.ctl.cfg
        old_telegram = cfg.telegram.control_enabled
        for name in type(cfg).model_fields:
            if name != "mode":
                setattr(cfg, name, getattr(new, name))
        self.config_error = None
        if cfg.telegram.control_enabled != old_telegram:
            asyncio.get_running_loop().create_task(self.ctl.restart_telegram())

    # ---------------------------------------------------------------- Telegram & Discord

    async def telegram_find(self, body: dict) -> dict:
        from topstep_bot.wizard import detect_telegram_chat

        token = str(body.get("token") or "").strip() or (self.ctl.secrets.telegram_bot_token or "")
        if not token:
            raise ValueError("Paste the bot token from @BotFather first.")
        if self.ctl.telegram is not None and token == self.ctl.secrets.telegram_bot_token:
            await self.ctl.stop_telegram()  # its polling would take the messages this looks for
        try:
            chats = await detect_telegram_chat(token, api_base=self.ctl.telegram_api)
        except ValueError as exc:
            raise ValueError(f"Telegram rejected the token: {exc}. Copy the whole token from @BotFather.") from None
        except httpx.HTTPError as exc:
            raise ValueError(f"Could not reach Telegram ({type(exc).__name__}). Check your internet and try again.") from None
        if not chats:
            return {"chats": [], "message": "No message found yet. Open your new bot in Telegram, press Start (or send it "
                                            "any message), then press Find my chat again."}
        return {"chats": [{"id": cid, "label": label} for cid, label in chats],
                "message": "Found your chat." if len(chats) == 1 else "Pick the chat that should control the bot."}

    async def telegram_save(self, body: dict) -> str:
        token = str(body.get("token") or "").strip() or (self.ctl.secrets.telegram_bot_token or "")
        chat = str(body.get("chat_id") or "").strip()
        if not token or not chat:
            raise ValueError("Paste the bot token and find your chat first.")
        control = bool(body.get("control", True))
        sent = await self._telegram_send(token, chat, "✅ Topstep Bot is connected to this chat. Alerts arrive here"
                                         + (", and you can control the bot with the buttons below or /help." if control else "."))
        if not sent:
            raise ValueError("Telegram could not deliver a test message to that chat, so nothing was saved. Message your "
                             "bot first, then try again.")
        self._set_secrets({"telegram_bot_token": token, "telegram_chat_id": chat})
        self._set_telegram_control(control)
        await self.ctl.restart_telegram()
        return "Telegram saved - a test message is in your chat." + ("" if control else " (Alerts only, no remote control.)")

    async def telegram_test(self) -> str:
        s = self.ctl.secrets
        if not (s.telegram_bot_token and s.telegram_chat_id):
            raise ValueError("Telegram is not set up yet.")
        if not await self._telegram_send(s.telegram_bot_token, s.telegram_chat_id, "✅ Test message from Topstep Bot."):
            raise ValueError("Telegram could not deliver the test message. Is the bot token still valid?")
        return "Test message sent - check Telegram."

    async def telegram_control(self, body: dict) -> str:
        self._set_telegram_control(bool(body.get("control")))
        await self.ctl.restart_telegram()
        return "Telegram remote control is on." if self.ctl.cfg.telegram.control_enabled else \
            "Telegram remote control is off (alerts still arrive)."

    def _set_telegram_control(self, on: bool) -> None:
        cfg = self.ctl.cfg
        if cfg.telegram.control_enabled == on and self.config_path.exists():
            return
        cfg.telegram.control_enabled = on
        if self.config_path.exists() and self.config_error is None:
            text = set_values(self.config_path.read_text(encoding="utf-8"), {("telegram", "control_enabled"): on})
            config_from_text(text, self.config_path.name)
            self.config_path.write_text(text, encoding="utf-8")

    async def telegram_remove(self) -> str:
        self._set_secrets({"telegram_bot_token": None, "telegram_chat_id": None})
        await self.ctl.restart_telegram()
        return "Telegram removed. Nothing more is sent there."

    async def _telegram_send(self, token: str, chat: str, text: str) -> bool:
        from topstep_bot.telegram_control import TelegramController

        tg = TelegramController(token, chat, None, self.ctl.cfg.telegram, api_base=self.ctl.telegram_api)  # type: ignore[arg-type]
        try:
            return await tg.send(text) is not None
        except Exception as exc:  # noqa: BLE001 - reported to the user as "could not deliver"
            log.warning("Telegram test message failed: %s", type(exc).__name__)
            return False
        finally:
            await tg.close()

    async def discord_save(self, body: dict) -> str:
        url = str(body.get("url") or "").strip()
        if not url:
            self._set_secrets({"discord_webhook_url": None})
            return "Discord alerts removed."
        if not url.startswith(("https://discord.com/api/webhooks/", "https://discordapp.com/api/webhooks/")):
            raise ValueError("That isn't a Discord webhook link. In Discord: channel settings > Integrations > Webhooks > "
                             "Copy Webhook URL.")
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.post(url, json={"content": "[Topstep Bot] ✅ Discord alerts are connected."})
        except httpx.HTTPError as exc:
            raise ValueError(f"Could not reach Discord ({type(exc).__name__}). Nothing was saved.") from None
        if r.status_code >= 300:
            raise ValueError(f"Discord refused the webhook ({r.status_code}). Copy it again; nothing was saved.")
        self._set_secrets({"discord_webhook_url": url})
        return "Discord saved - a test message is in the channel."

    # ---------------------------------------------------------------- updates and Windows start

    async def github_token(self, body: dict) -> str:
        from topstep_bot.wizard import check_github_token

        token = str(body.get("token") or "").strip()
        if not token:
            self._set_secrets({"github_token": None})
            return "GitHub token removed."
        problem = await asyncio.to_thread(check_github_token, token, self.ctl.cfg.updates.repo)
        if problem:
            raise ValueError(f"{problem[0].upper()}{problem[1:]}. Nothing was saved.")
        self._set_secrets({"github_token": token})
        updater = self.ctl.updates.updater
        if updater is not None:
            updater.token = token
            updater.close()  # the next check signs in with the new token
        return "GitHub token saved. Press Check now to look for updates."

    async def autostart(self, body: dict) -> str:
        from topstep_bot import autostart

        on = bool(body.get("enabled"))
        try:
            if not on:
                return "Topstep Bot no longer starts with Windows." if autostart.disable() else "It was not set to start with Windows."
            if self.blocker():
                raise ValueError(self.blocker())
            if self.ctl.bot.mode == "live" and str(body.get("confirm", "")).strip().upper() != "LIVE":
                raise ValueError("You're in LIVE mode: type LIVE to confirm it may start trading real orders by itself.")
            autostart.enable(self.config_path)
        except OSError as exc:
            raise ValueError(str(exc)) from None
        return (f"Topstep Bot now starts about 30 seconds after you sign in to Windows, and starts the bot in the mode "
                f"used last ({self.ctl.bot.mode.upper()} now).")


def _problems(exc: ConfigError) -> str:
    """'config.yaml has 2 problem(s):\n  risk.x: ...' -> 'risk.x: ...; ...' (one line, for a toast)."""
    lines = str(exc).splitlines()[1:] or [str(exc)]
    return "; ".join(line.strip().removeprefix("config: ") for line in lines)


def _goal_header(text: str, goal: str) -> str:
    """Keep the 'Goal: LEARN' note at the top of config.yaml in step with the goal."""
    lines = text.splitlines()
    while lines and lines[0].startswith("# Goal: LEARN"):
        lines.pop(0)
        while lines and lines[0].startswith("# ") and not lines[0].startswith("# Topstep Bot configuration"):
            lines.pop(0)
    if goal == "learn":
        lines[:0] = ["# Goal: LEARN - this account is for teaching the bot. It trades more often, tries unproven",
                     "# strategies, and doesn't stop early for the consistency rule. Every Topstep rule still applies."]
    return "\n".join(lines) + "\n"
