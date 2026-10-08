"""Logging: readable daily log files, an errors-only file, a machine-readable event log, crash
capture, secret redaction, and supervision of background tasks.

Files (in the log folder, default ``logs/`` next to config.yaml):
  bot.log        everything (DEBUG+), one file per day, kept ``retention_days`` days
  errors.log     warnings and errors only - the first place to look when something goes wrong
  events.jsonl   one JSON object per line (trades, risk events, recommendations...) for analysis
  crash_*.txt    full details of any crash that ends the program
  faults.log     low-level hang/crash dumps from Python itself
"""

from __future__ import annotations

import asyncio
import faulthandler
import json
import logging
import os
import platform
import re
import sys
import threading
import traceback
from collections import deque
from collections.abc import Coroutine, Iterable
from datetime import datetime
from logging.handlers import RotatingFileHandler, TimedRotatingFileHandler
from pathlib import Path
from typing import Any

log = logging.getLogger("topstep_bot")

TEXT_FORMAT = "%(asctime)s.%(msecs)03d %(levelname)-8s %(name)-30s %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
NOISY_LOGGERS = ("httpx", "httpcore", "websockets", "asyncio")

_fault_file = None  # kept open for faulthandler


# ------------------------------------------------------------------ redaction

class RedactingFilter(logging.Filter):
    """Masks API keys, tokens and webhook URLs anywhere in a log record (message, args, traceback)."""

    PATTERNS = [
        (re.compile(r"(bot)\d{6,}:[A-Za-z0-9_-]{20,}"), r"\1***"),  # Telegram bot token in URLs
        (re.compile(r"(access_token=)[^&\s'\"]+"), r"\1***"),
        (re.compile(r"(Bearer\s+)[A-Za-z0-9._-]+"), r"\1***"),
        (re.compile(r"(discord(?:app)?\.com/api/webhooks/)\S+"), r"\1***"),
        (re.compile(r"(\"?(?:apiKey|api_key|token|newToken)\"?\s*[:=]\s*\"?)[^\s\",}]+"), r"\1***"),
    ]

    def __init__(self, secrets: Iterable[str | None] = ()):
        super().__init__()
        self.secrets = sorted({s for s in secrets if s and len(s) >= 6}, key=len, reverse=True)

    def redact(self, text: str) -> str:
        for secret in self.secrets:
            text = text.replace(secret, "***")
        for pattern, repl in self.PATTERNS:
            text = pattern.sub(repl, text)
        return text

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - malformed args: log them raw
            message = str(record.msg)
        record.msg = self.redact(message)
        record.args = None
        if record.exc_info:
            record.exc_text = self.redact("".join(traceback.format_exception(*record.exc_info)).rstrip())
            record.exc_info = None
        return True


# ------------------------------------------------------------------ formatters

class JsonFormatter(logging.Formatter):
    """One JSON object per line. Extra fields passed via ``extra={"event": ..., "data": {...}}`` are kept."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key in ("event", "data"):
            if hasattr(record, key):
                payload[key] = getattr(record, key)
        if record.exc_text:
            payload["exc"] = record.exc_text
        return json.dumps(payload, default=str, ensure_ascii=False)


class LogStats(logging.Handler):
    """Counts warnings/errors and keeps the latest ones (shown on the dashboard)."""

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.warnings = 0
        self.errors = 0
        self.recent: deque[dict] = deque(maxlen=20)

    def emit(self, record: logging.LogRecord) -> None:
        if record.levelno >= logging.ERROR:
            self.errors += 1
        else:
            self.warnings += 1
        self.recent.appendleft({
            "ts": datetime.fromtimestamp(record.created).strftime("%H:%M:%S"),
            "level": record.levelname.lower(),
            "message": record.getMessage()[:300],
        })

    def snapshot(self) -> dict:
        return {"warnings": self.warnings, "errors": self.errors, "recent": list(self.recent)[:10]}


stats = LogStats()


# ------------------------------------------------------------------ setup

def setup_logging(
    log_dir: Path | str,
    level: str = "INFO",
    *,
    secrets: Iterable[str | None] = (),
    console: Any = None,
    retention_days: int = 30,
    file_prefix: str = "bot",
    shared_files: bool = True,
) -> Path:
    """Configure logging for the whole program. Returns the log folder.

    ``shared_files=False`` (the controller) writes only ``<file_prefix>.log`` so two processes never
    rotate the same file - on Windows that fails while the other process has it open.
    """
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    redact = RedactingFilter(secrets)

    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()
    root.setLevel(logging.DEBUG)
    text = logging.Formatter(TEXT_FORMAT, DATE_FORMAT)

    main_file = TimedRotatingFileHandler(
        log_dir / f"{file_prefix}.log", when="midnight", backupCount=retention_days, encoding="utf-8", delay=True
    )
    main_file.setLevel(logging.DEBUG)
    main_file.setFormatter(text)

    errors_file = RotatingFileHandler(log_dir / "errors.log", maxBytes=2_000_000, backupCount=5, encoding="utf-8", delay=True)
    errors_file.setLevel(logging.WARNING)
    errors_file.setFormatter(text)

    events_file = TimedRotatingFileHandler(
        log_dir / "events.jsonl", when="midnight", backupCount=retention_days, encoding="utf-8", delay=True
    )
    events_file.setLevel(logging.INFO)
    events_file.setFormatter(JsonFormatter())

    if console is not None:
        from rich.logging import RichHandler

        screen: logging.Handler = RichHandler(console=console, show_path=False, rich_tracebacks=True, markup=False)
    else:
        screen = logging.StreamHandler()
        screen.setFormatter(logging.Formatter("%(asctime)s %(levelname)-8s %(message)s", DATE_FORMAT))
    screen.setLevel(level.upper())

    handlers = [main_file, errors_file, events_file, screen, stats] if shared_files else [main_file, screen, stats]
    for handler in handlers:
        handler.addFilter(redact)
        root.addHandler(handler)
    for name in NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)

    install_crash_handlers(log_dir)
    return log_dir


def install_crash_handlers(log_dir: Path) -> None:
    """Make sure no crash goes unrecorded, even if the console window closes."""
    global _fault_file

    def excepthook(exc_type, exc, tb):  # noqa: ANN001
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc, tb)
            return
        log.critical("Unhandled exception - the program is closing", exc_info=(exc_type, exc, tb))
        write_crash_report(log_dir, exc_type, exc, tb)
        sys.__excepthook__(exc_type, exc, tb)

    def thread_excepthook(args: threading.ExceptHookArgs) -> None:
        log.critical("Unhandled exception in thread %s", args.thread.name if args.thread else "?",
                     exc_info=(args.exc_type, args.exc_value, args.exc_traceback))

    sys.excepthook = excepthook
    threading.excepthook = thread_excepthook
    if _fault_file is None:
        try:
            _fault_file = open(log_dir / "faults.log", "a", encoding="utf-8")  # noqa: SIM115 - must stay open
            faulthandler.enable(file=_fault_file)
        except OSError:
            _fault_file = None


def write_crash_report(log_dir: Path, exc_type, exc, tb) -> Path | None:  # noqa: ANN001
    """Save a self-contained crash file (redacted) next to the logs."""
    try:
        from topstep_bot import __version__

        path = Path(log_dir) / f"crash_{datetime.now():%Y%m%d_%H%M%S}.txt"
        body = "".join(traceback.format_exception(exc_type, exc, tb))
        redact = next((f for h in logging.getLogger().handlers for f in h.filters if isinstance(f, RedactingFilter)), None)
        if redact:
            body = redact.redact(body)
        path.write_text(
            f"Topstep Bot {__version__} crashed at {datetime.now():%Y-%m-%d %H:%M:%S}\n"
            f"Python {platform.python_version()} on {platform.platform()}\npid {os.getpid()}  cwd {os.getcwd()}\n\n{body}",
            encoding="utf-8",
        )
        return path
    except Exception:  # noqa: BLE001
        return None


# ------------------------------------------------------------------ asyncio

def asyncio_exception_handler(loop: asyncio.AbstractEventLoop, context: dict) -> None:
    exc = context.get("exception")
    message = context.get("message", "asyncio error")
    if exc is not None:
        log.error("Unhandled error in background task: %s", message, exc_info=(type(exc), exc, exc.__traceback__))
    else:
        log.error("asyncio: %s", message)


def _report_task(task: asyncio.Task) -> None:
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        log.error("Background task '%s' failed and stopped", task.get_name(), exc_info=(type(exc), exc, exc.__traceback__))


def spawn(coro: Coroutine, name: str | None = None) -> asyncio.Task:
    """create_task that logs the failure if the task dies (instead of failing silently)."""
    task = asyncio.create_task(coro, name=name)
    task.add_done_callback(_report_task)
    return task


def log_startup(cfg: Any, command: str) -> None:
    from topstep_bot import __version__

    log.info("=" * 70)
    log.info("Topstep Bot %s starting: %s (pid %s)", __version__, command, os.getpid())
    log.info("Python %s on %s; working folder %s", platform.python_version(), platform.platform(), os.getcwd())
    log.info(
        "Config: mode=%s plan=%s %s symbol=%s %sm strategy=%s risk/trade=$%s daily loss=$%s",
        cfg.mode, cfg.account.plan, cfg.account.stage, cfg.instrument.symbol, cfg.instrument.timeframe_minutes,
        cfg.strategy.name, cfg.risk.risk_per_trade, cfg.risk.personal_daily_loss_limit,
    )


def tail(path: Path, lines: int = 40) -> list[str]:
    """Last ``lines`` lines of a text file (empty list if missing)."""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return list(deque(fh, maxlen=lines))
    except OSError:
        return []
