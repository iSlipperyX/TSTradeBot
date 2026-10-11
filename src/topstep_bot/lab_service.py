"""The dashboard's Simulations and Coach tabs, served by the controller.

LabService runs the Simulation Lab (simlab.py) in the background and applies a playbook you choose.
CoachService runs AI coaching sessions (coach.py), keeps the Claude API key in .env, and applies the
proposals you approve. Both change config.yaml only when you press a button, through the same checked,
in-place editor the Setup tab uses (the previous file is kept as config.yaml.bak), and a running bot
keeps its old settings until it is restarted.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import TYPE_CHECKING, Any

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
        self.ctl = ctl
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
        return {"running": self.running, "stage": self.stage, "done": done, "total": total, "error": self.error,
                "generated": rep["generated"] if rep else None, "totals": rep["totals"] if rep else None,
                "recommendation": rep["recommendation"] if rep else None}

    def state(self) -> dict[str, Any]:
        from topstep_bot.simlab import DEFAULT_STARTS, history_sources, load_report, report_view

        cfg = _shared_cfg(self.ctl)
        done, total = self.progress
        return {"running": self.running, "stage": self.stage, "done": done, "total": total, "error": self.error,
                "elapsed": round(time.time() - self.started) if self.running and self.started else None,
                "report": report_view(load_report(cfg), runs=1000), "taught": self.taught,
                "history": sorted(history_sources(cfg)), "default_starts": DEFAULT_STARTS,
                "current": {"symbol": cfg.instrument.symbol, "strategy": cfg.strategy.name, "auto_pick": cfg.instrument.auto_pick}}

    def run(self, body: dict, source: str) -> str:
        if self.running:
            return "The Simulation Lab is already running."
        try:
            starts = max(1, min(30, int(body.get("starts") or 4)))
        except (TypeError, ValueError):
            starts = 4
        teach = body.get("teach", True) is not False
        self.error, self.taught, self.progress, self.started = None, [], (0, 0), time.time()
        self.stage = "starting"
        self.task = asyncio.get_running_loop().create_task(self._run(starts, teach, source))
        return f"Simulation Lab started ({starts} Combine start days per playbook). This page shows its progress."

    async def _run(self, starts: int, teach: bool, source: str) -> None:
        from topstep_bot.simlab import run_lab
        from topstep_bot.simlab import teach as teach_memory

        cfg = _shared_cfg(self.ctl)

        def step(done: int, total: int, stage: str) -> None:
            self.progress, self.stage = (done, total), stage

        try:
            report = await run_lab(cfg, starts=starts, progress=step)
            if teach:
                self.stage = "teaching the long-run memory"
                self.taught = await teach_memory(cfg, progress=lambda d, t, s: setattr(self, "stage", s))
            t = report["totals"]
            self.ctl.bot._event("info", f"Simulation Lab ({source}): {t['simulations']} live-like Combines - {t['passed']} passed, "
                                        f"{t['failed']} failed, {t['open']} open. {report['recommendation']['text']}")
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
        from topstep_bot.coach import ENV_KEY, CoachError
        from topstep_bot.wizard import update_env_file

        key = str(body.get("key", "")).strip()
        if not key:
            raise ValueError("Paste your Claude API key first.")
        if not key.startswith("sk-ant-"):
            raise ValueError("That doesn't look like a Claude API key (they start with sk-ant-).")
        self._sync()
        try:
            message = await self.coach.test_key(key)
        except CoachError as exc:
            return {"ok": False, "message": str(exc)}
        update_env_file(self.ctl.setup.env_path, {ENV_KEY: key})
        os.environ[ENV_KEY] = key
        self.ctl.secrets.anthropic_api_key = key
        self.coach.set_key(key)
        from topstep_bot.logging_setup import add_secret

        add_secret(key)
        return {"ok": True, "message": f"{message} Saved in .env on this PC."}

    def remove_key(self) -> str:
        from topstep_bot.coach import ENV_KEY
        from topstep_bot.wizard import remove_env_keys

        remove_env_keys(self.ctl.setup.env_path, {ENV_KEY})
        os.environ.pop(ENV_KEY, None)
        self.ctl.secrets.anthropic_api_key = None
        self.coach.set_key(None)
        return "Claude API key removed from this PC."

    def settings(self, body: dict) -> str:
        from topstep_bot.coach import MODELS

        changes: dict[tuple[str | None, str], Any] = {}
        if "model" in body:
            if body["model"] not in MODELS:
                raise ValueError("Unknown model.")
            changes[("coach", "model")] = body["model"]
        if "effort" in body:
            if body["effort"] not in ("low", "medium", "high", "xhigh", "max"):
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
