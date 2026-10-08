"""SQLite trade journal: trades, daily summaries, events and small persistent state.

Persisted state lets the bot survive restarts mid-day (trade counts, the MLL floor, ...).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from topstep_bot.execution import ManagedTrade

UTC = timezone.utc

SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    tag TEXT PRIMARY KEY,
    trading_day TEXT,
    account TEXT,
    contract TEXT,
    strategy TEXT,
    side TEXT,
    size INTEGER,
    entry_time TEXT,
    entry_price REAL,
    exit_time TEXT,
    exit_price REAL,
    initial_stop REAL,
    target REAL,
    gross_pnl REAL,
    fees REAL,
    net_pnl REAL,
    r_multiple REAL,
    entry_reason TEXT,
    exit_reason TEXT
);
CREATE TABLE IF NOT EXISTS daily (
    trading_day TEXT,
    account TEXT,
    start_balance REAL,
    end_balance REAL,
    net_pnl REAL,
    trades INTEGER,
    mll_floor REAL,
    PRIMARY KEY (trading_day, account)
);
CREATE TABLE IF NOT EXISTS events (
    ts TEXT,
    level TEXT,
    message TEXT
);
CREATE TABLE IF NOT EXISTS state (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


class Journal:
    def __init__(self, path: Path | str):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # ------------------------------------------------------------------ writes

    def record_trade(self, t: ManagedTrade, trading_day: date, account: str, contract: str) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO trades VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                t.tag,
                trading_day.isoformat(),
                account,
                contract,
                t.strategy,
                t.side.label,
                t.filled_size,
                t.opened_at.isoformat() if t.opened_at else None,
                t.entry_price,
                t.closed_at.isoformat() if t.closed_at else None,
                t.exit_price,
                t.initial_stop,
                t.target_price,
                round(t.gross_pnl, 2),
                round(t.fees, 2),
                round(t.net_pnl, 2),
                t.r_multiple(),
                t.reason,
                t.exit_reason,
            ),
        )
        self.conn.commit()

    def record_day(
        self, day: date, account: str, start_balance: float, end_balance: float, trades: int, mll_floor: float
    ) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO daily VALUES (?,?,?,?,?,?,?)",
            (day.isoformat(), account, start_balance, end_balance, round(end_balance - start_balance, 2), trades, mll_floor),
        )
        self.conn.commit()

    def log_event(self, level: str, message: str) -> None:
        self.conn.execute(
            "INSERT INTO events VALUES (?,?,?)", (datetime.now(UTC).isoformat(timespec="seconds"), level, message)
        )
        self.conn.commit()

    def set_state(self, key: str, value: Any) -> None:
        self.conn.execute("INSERT OR REPLACE INTO state VALUES (?,?)", (key, json.dumps(value)))
        self.conn.commit()

    # ------------------------------------------------------------------- reads

    def get_state(self, key: str, default: Any = None) -> Any:
        row = self.conn.execute("SELECT value FROM state WHERE key = ?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def trades(self, limit: int = 50, account: str | None = None) -> list[dict]:
        if account:
            rows = self.conn.execute(
                "SELECT * FROM trades WHERE account = ? ORDER BY exit_time DESC LIMIT ?", (account, limit)
            )
        else:
            rows = self.conn.execute("SELECT * FROM trades ORDER BY exit_time DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows]

    def daily(self, account: str | None = None) -> list[dict]:
        if account:
            rows = self.conn.execute("SELECT * FROM daily WHERE account = ? ORDER BY trading_day", (account,))
        else:
            rows = self.conn.execute("SELECT * FROM daily ORDER BY trading_day")
        return [dict(r) for r in rows]

    def events(self, limit: int = 100) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM events ORDER BY rowid DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows]
