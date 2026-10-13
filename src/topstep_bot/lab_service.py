"""The dashboard's Simulations and Coach tabs, served by the controller.

LabService runs the Simulation Lab (simlab.py) in the background and applies a playbook you choose. It
also keeps every market's price history up to date (market_data.py: TopstepX on request, Yahoo in the
background about twice a day) and runs the lab on every market once each weekday after the close
(``lab.nightly``), so the Opportunities list is always fresh.
CoachService runs AI coaching sessions (coach.py), keeps the Claude API key in .env, and applies the
proposals you approve. Both change config.yaml only when you press a button, through the same checked,
in-place editor the Setup tab uses (the previous file is kept as config.yaml.bak), and a running bot
keeps its old settings until it is restarted.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from topstep_bot.config_edit import set_values

if TYPE_CHECKING:
    from topstep_bot.controller import Controller

log = logging.getLogger("topstep_bot.lab")


def _shared_cfg(ctl: Controller):
    """The settings the lab and the coach work with: the first account's (the shared ones with a single account)."""
    return ctl.account_cfg(ctl.bot.account)


def _apply_config(ctl: Controller, changes: dict[tuple[str | None, str], Any]) -> str:
    """Write ``changes`` into config.yaml (checked first; the old file becomes config.yaml.bak)."""
    setup = ctl.setup
    original = setup._require_file()
    setup._write(original, set_values(original, changes))
    if any(b.running for b in ctl.bots.values()):
        setup.saved_at = time.time()
        return " The bot is still running with the old settings: restart it to use the new ones."
    return " It takes effect when the bot starts."


class LabService:
    def __init__(self, ctl: Controller):
        from topstep_bot.market_data import MarketData

        self.ctl = ctl
        self.data = MarketData(_shared_cfg(ctl))
        self.data_task: asyncio.Task | None = None
        self.data_note = ""  # how the last download went, for the page
        self.nightly_day: str | None = None  # the CT date the nightly run last started
        self.task: asyncio.Task | None = None
        self.progress: tuple[int, int] = (0, 0)
        self.stage = ""
        self.error: str | None = None
        self.taught: list[dict] = []
        self.started: float | None = None

    @property
    def running(self) -> bool:
        return self.task is not None and not self.task.done()

    def summary(self) -> dict[str, Any]:
        from topstep_bot.simlab import load_report

        rep = load_report(_shared_cfg(self.ctl)) if not self.running else None
        done, total = self.progress
        opp = (rep or {}).get("opportunities") or {}
        return {"running": self.running, "stage": self.stage, "done": done, "total": total, "error": self.error,
                "generated": rep["generated"] if rep else None, "totals": rep["totals"] if rep else None,
                "recommendation": rep["recommendation"] if rep else None,
                "markets": len((rep or {}).get("markets") or {}), "opportunities": opp.get("found", 0),
                "top": (opp.get("items") or [None])[0]}

    def state(self) -> dict[str, Any]:
        from topstep_bot.instruments import CLASSES, SPECS
        from topstep_bot.market_pick import current_symbol
        from topstep_bot.simlab import DEFAULT_STARTS, available_markets, load_report, report_view

        cfg = _shared_cfg(self.ctl)
        done, total = self.progress
        return {"running": self.running, "stage": self.stage, "done": done, "total": total, "error": self.error,
                "elapsed": round(time.time() - self.started) if self.running and self.started else None,
                "report": report_view(load_report(cfg), runs=1000), "taught": self.taught,
                "history": available_markets(cfg), "permitted": len(SPECS), "default_starts": DEFAULT_STARTS,
                "scope": cfg.lab.markets, "classes": [{"key": k, "label": v} for k, v in CLASSES.items()],
                "data": {**self.data.status(), "note": self.data_note},
                "nightly": {"on": cfg.lab.nightly, "time": cfg.lab.nightly_time.strftime("%H:%M")},
                "current": {"symbol": cfg.instrument.symbol, "strategy": cfg.strategy.name, "auto_pick": cfg.instrument.auto_pick,
                            "today": current_symbol(cfg)}}

    def markets(self, body: dict | None = None) -> dict[str, Any]:
        """Every market Topstep permits: specs, the history on this PC, and whether the account may trade it."""
        from topstep_bot.market_data import coverage
        from topstep_bot.market_pick import current_symbol, load_today

        account = (body or {}).get("account")
        bot = self.ctl.bot
        if account and account != "all":
            bot = self.ctl.pick(account)[0]
        cfg = self.ctl.account_cfg(bot.account)
        return {"markets": coverage(cfg), "current": current_symbol(cfg), "usual": cfg.instrument.symbol,
                "today": load_today(cfg), "data": {**self.data.status(), "note": self.data_note}}

    # ---------------------------------------------------------------- price history

    def download(self, body: dict, source: str) -> str:
        """Download every market's history now: TopstepX when the login works (unless Yahoo is asked for), else Yahoo."""
        if self.data.running:
            return "Already downloading market history. This page shows its progress."
        cfg = _shared_cfg(self.ctl)
        self.data.cfg = cfg
        secrets = self.ctl.secrets.for_account(cfg)
        use_topstepx = body.get("source") != "yahoo" and secrets.has_credentials
        self.data_note = ""

        async def go() -> None:
            from topstep_bot.api.rest import ProjectXClient

            client = None
            if use_topstepx:
                client = ProjectXClient(secrets.username, secrets.api_key, cfg.api.base_url, cfg.api.timeout_seconds)
                try:
                    await client.login()
                except Exception as exc:  # noqa: BLE001 - Yahoo still works without a login
                    self.data_note = f"TopstepX login failed ({exc}), so the history came from Yahoo. "
                    await client.close()
                    client = None
            try:
                results = await self.data.refresh(client=client)
                got = sum(1 for r in results if r.source)
                new = sum(r.new_bars for r in results)
                self.data_note += f"{got} of {len(results)} markets updated, {new:,} new bars."
                self.ctl.bot._event("info", f"Market history ({source}): {self.data_note}")
            except Exception as exc:  # noqa: BLE001 - shown on the page
                log.exception("Market history download failed")
                self.data_note += f"The download stopped: {exc}"
            finally:
                if client is not None:
                    await client.close()

        self.data_task = asyncio.get_running_loop().create_task(go())
        where = "TopstepX (Yahoo for anything it can't supply)" if use_topstepx else "Yahoo Finance"
        return f"Downloading every market's history from {where}. It takes a minute or two; this page shows its progress."

    # ---------------------------------------------------------------- background

    def start_background(self) -> list[asyncio.Task]:
        """The controller's background jobs: refresh every market's history and the nightly lab run."""
        from topstep_bot.logging_setup import spawn

        cfg = _shared_cfg(self.ctl)
        tasks = []
        if cfg.lab.refresh_data:
            tasks.append(spawn(self.data.run(), name="market-data"))
        if cfg.lab.nightly:
            tasks.append(spawn(self._nightly_loop(), name="lab-nightly"))
        return tasks

    def nightly_due(self, now: datetime | None = None) -> bool:
        """A weekday, after lab.nightly_time CT, and no lab report since then."""
        from topstep_bot.simlab import load_report

        cfg = _shared_cfg(self.ctl)
        tz = ZoneInfo(cfg.session.timezone)
        local = (now or datetime.now(timezone.utc)).astimezone(tz)
        if local.weekday() >= 5 or local.time() < cfg.lab.nightly_time or self.nightly_day == local.date().isoformat():
            return False
        rep = load_report(cfg)
        if rep:
            try:
                made = datetime.fromisoformat(rep["generated"]).astimezone(tz)
                if made.date() == local.date() and made.time() >= cfg.lab.nightly_time:
                    return False  # already run since the close today
            except (KeyError, ValueError):
                pass
        return not self.running and not self.data.running

    async def _nightly_loop(self, check_every: float = 120.0) -> None:
        while True:
            await asyncio.sleep(check_every)
            if not self.nightly_due():
                continue
            cfg = _shared_cfg(self.ctl)
            self.nightly_day = datetime.now(timezone.utc).astimezone(ZoneInfo(cfg.session.timezone)).date().isoformat()
            try:
                self.data.cfg = cfg
                await self.data.refresh()
            except Exception as exc:  # noqa: BLE001 - simulate on what is already saved
                log.info("Nightly history refresh failed: %s", exc)
            if not self.running:
                self.run({"starts": 4, "teach": True}, "nightly run")

    def run(self, body: dict, source: str) -> str:
        if self.running:
            return "The Simulation Lab is already running."
        from topstep_bot.instruments import CLASSES

        try:
            starts = max(1, min(30, int(body.get("starts") or 4)))
        except (TypeError, ValueError):
            starts = 4
        teach = body.get("teach", True) is not False
        scope = body.get("scope") if body.get("scope") in ("all", "tradable") else None
        classes = [c for c in (body.get("classes") or []) if c in CLASSES] or None
        self.error, self.taught, self.progress, self.started = None, [], (0, 0), time.time()
        self.stage = "starting"
        self.task = asyncio.get_running_loop().create_task(self._run(starts, teach, source, scope, classes))
        if classes:
            which = "the markets you chose"
        elif (scope or _shared_cfg(self.ctl).lab.markets) == "all":
            which = "every market with history"
        else:
            which = "the markets this account can pick"
        return f"Simulation Lab started on {which} ({starts} Combine start days per playbook). This page shows its progress."

    async def _run(self, starts: int, teach: bool, source: str, scope: str | None = None,
                   classes: list[str] | None = None) -> None:
        from topstep_bot.simlab import run_lab
        from topstep_bot.simlab import teach as teach_memory

        cfg = _shared_cfg(self.ctl)

        def step(done: int, total: int, stage: str) -> None:
            self.progress, self.stage = (done, total), stage

        try:
            report = await run_lab(cfg, starts=starts, scope=scope, classes=classes, progress=step)
            if teach:
                self.stage = "teaching the long-run memory"
                self.taught = await teach_memory(cfg, list(report["markets"]), progress=lambda d, t, s: setattr(self, "stage", s))
            t = report["totals"]
            opp = report.get("opportunities") or {}
            top = (opp.get("items") or [None])[0]
            found = ""
            if opp:
                found = f" {opp.get('found', 0)} opportunities across {len(report['markets'])} markets" + (
                    f", strongest {top['label']} (${top['net']:,.0f}, {top['strength']})." if top else ".")
            self.ctl.bot._event("info", f"Simulation Lab ({source}): {t['simulations']} live-like Combines - {t['passed']} passed, "
                                        f"{t['failed']} failed, {t['open']} open.{found} {report['recommendation']['text']}")
        except Exception as exc:  # noqa: BLE001 - shown on the page and in the log
            log.exception("Simulation Lab failed")
            self.error = str(exc)
        finally:
            self.stage = ""

    def apply(self, body: dict, source: str) -> str:
        from topstep_bot.strategies import STRATEGIES

        symbol = str(body.get("symbol", "")).strip().upper()
        strategy = str(body.get("strategy", "")).strip()
        if strategy not in STRATEGIES:
            raise ValueError(f"Unknown strategy '{strategy}'.")
        note = _apply_config(self.ctl, {("instrument", "symbol"): symbol, ("instrument", "auto_pick"): False,
                                        ("strategy", "name"): strategy, ("strategy", "params"): {}})
        self.ctl.bot._event("info", f"Playbook set to {symbol} with {STRATEGIES[strategy].title} ({source}, from the Simulation Lab)")
        return f"Saved: the bot trades {symbol} with {STRATEGIES[strategy].title}.{note}"


class CoachService:
    def __init__(self, ctl: Controller):
        from topstep_bot.coach import Coach

        self.ctl = ctl
        self.coach = Coach(_shared_cfg(ctl), api_key=ctl.secrets.anthropic_api_key)
        self.task: asyncio.Task | None = None

    def _sync(self) -> None:
        self.coach.cfg = _shared_cfg(self.ctl)

    def summary(self) -> dict[str, Any]:
        pending = sum(1 for p in self.coach.proposals() if p.get("status") == "pending")
        return {"running": self.coach.running, "stage": self.coach.stage, "key_saved": bool(self.coach.api_key),
                "pending": pending}

    def state(self) -> dict[str, Any]:
        self._sync()
        return self.coach.status()

    async def save_key(self, body: dict) -> dict:
        from topstep_bot.claude import KEY_PREFIX
        from topstep_bot.coach import CoachError

        key = str(body.get("key", "")).strip()
        if not key:
            raise ValueError("Paste your Claude API key first.")
        if not key.startswith(KEY_PREFIX):
            raise ValueError(f"That doesn't look like a Claude API key (they start with {KEY_PREFIX}).")
        self._sync()
        try:
            message = await self.coach.test_key(key)
        except CoachError as exc:
            return {"ok": False, "message": str(exc)}
        self.ctl.setup.set_claude_key(key)  # one key: the Ask answers use it too
        return {"ok": True, "message": f"{message} Saved in .env on this PC."}

    def remove_key(self) -> str:
        self.ctl.setup.forget_claude_key()
        return "Claude API key removed from this PC (the Ask answers used it too)."

    def settings(self, body: dict) -> str:
        from topstep_bot.claude import EFFORTS, MODELS

        changes: dict[tuple[str | None, str], Any] = {}
        if "model" in body:
            if body["model"] not in MODELS:
                raise ValueError("Unknown model.")
            changes[("coach", "model")] = body["model"]
        if "effort" in body:
            if body["effort"] not in EFFORTS:
                raise ValueError("Effort must be low, medium, high, xhigh or max.")
            changes[("coach", "effort")] = body["effort"]
        for key, low, high in (("max_session_usd", 0.05, 50), ("monthly_budget_usd", 0.5, 1000)):
            if key in body:
                try:
                    value = round(float(body[key]), 2)
                except (TypeError, ValueError):
                    raise ValueError(f"{key.replace('_', ' ')} must be a number.") from None
                if not low <= value <= high:
                    raise ValueError(f"{key.replace('_', ' ')} must be between {low} and {high}.")
                changes[("coach", key)] = value
        if not changes:
            return "Nothing to change."
        setup = self.ctl.setup
        original = setup._require_file()
        setup._write(original, set_values(original, changes))  # coach settings don't need a bot restart
        self._sync()
        return "Coach settings saved."

    def run(self, body: dict, source: str) -> str:
        from topstep_bot.coach import CoachError

        self._sync()
        if self.coach.running:
            return "A coaching session is already running."
        question = " ".join(str(body.get("question", "")).split())[:600]
        if not self.coach.api_key:
            raise ValueError("Add your Claude API key first (Coach tab).")
        c = self.coach.cfg.coach
        if self.coach.spent() >= c.monthly_budget_usd:
            raise ValueError(f"This month's coaching budget (${c.monthly_budget_usd:.2f}) is used up. Raise it below to continue.")

        async def go() -> None:
            try:
                session = await self.coach.run(question)
                n_l, n_p = len(session["lessons"]), len(session["proposals"])
                self.ctl.bot._event("info", f"AI coach ({source}): session done - {n_l} lesson{'s' if n_l != 1 else ''}, "
                                            f"{n_p} proposal{'s' if n_p != 1 else ''}, cost ${session['cost_usd']:.2f}.")
            except CoachError as exc:
                self.ctl.bot._event("warning", f"AI coach: {exc}")
            except Exception as exc:  # noqa: BLE001
                log.exception("Coach session failed")
                self.coach.last_error = str(exc)
                self.ctl.bot._event("warning", f"AI coach failed: {exc}")

        self.coach.last_error = None
        self.coach.running = False
        self.task = asyncio.get_running_loop().create_task(go())
        return "Coaching session started. It usually takes a few minutes; this page shows what the coach is doing."

    def cancel(self) -> str:
        return "Stopping after the current step." if self.coach.cancel() else "No session is running."

    def decide(self, body: dict, source: str) -> str:
        from topstep_bot.strategies import STRATEGIES

        self._sync()
        pid, action = str(body.get("id", "")), str(body.get("action", ""))
        prop = next((p for p in self.coach.proposals() if p.get("id") == pid), None)
        if prop is None:
            raise ValueError("No such proposal (it may have been replaced by a newer one).")
        if prop.get("status") != "pending":
            raise ValueError(f"That proposal was already {prop.get('status')}.")
        if action == "dismiss":
            self.coach.set_proposal_status(pid, "dismissed")
            return f"Dismissed: {prop['title']}."
        if action != "apply":
            raise ValueError("action must be apply or dismiss")
        cfg = self.coach.cfg
        if prop["kind"] == "playbook":
            note = _apply_config(self.ctl, {("instrument", "symbol"): prop["symbol"], ("instrument", "auto_pick"): False,
                                            ("strategy", "name"): prop["strategy"], ("strategy", "params"): {}})
            what = f"the bot trades {prop['symbol']} with {STRATEGIES[prop['strategy']].title}"
        else:
            current = {"risk_per_trade": cfg.risk.risk_per_trade, "personal_daily_loss_limit": cfg.risk.personal_daily_loss_limit,
                       "max_trades_per_day": cfg.risk.max_trades_per_day}[prop["setting"]]
            if prop["value"] >= current:
                self.coach.set_proposal_status(pid, "dismissed")
                raise ValueError("That would no longer lower risk (the setting changed since): dismissed.")
            value = int(prop["value"]) if prop["setting"] == "max_trades_per_day" else prop["value"]
            note = _apply_config(self.ctl, {("risk", prop["setting"]): value})
            what = prop["title"]
        self.coach.set_proposal_status(pid, "applied")
        self.ctl.bot._event("info", f"Coach proposal applied ({source}): {prop['title']}")
        return f"Applied: {what}.{note}"

    def forget(self, body: dict) -> str:
        return "Lesson removed." if self.coach.forget_lesson(str(body.get("id", ""))) else "No such lesson."
