# Topstep Bot

An automated futures trading bot for **Topstep** accounts, using the official **TopstepX
(ProjectX Gateway) API**. It backtests, paper trades and trades live with the same engine, and its
risk layer is built around Topstep's rules so a strategy can't accidentally break them.

**→ New here? Read the [complete how-to-use guide](docs/HOW_TO_USE.md).**

## Quick start (Windows)

1. Install [Python 3.11+](https://www.python.org/downloads/) (tick "Add python.exe to PATH").
2. Double-click **`start.bat`**. The first run installs everything.
3. Pick **9** for a demo backtest, or **1** to set up your TopstepX API key, account, strategy and risk.
4. Then: **3** backtest → **4** paper trade for a few weeks → **5** live (start with 1 micro contract).

Other systems: `pip install -e .` then run `topstep-bot`.

## Features

- **Topstep-aware risk engine** — trailing Maximum Loss Limit (end-of-day, locks at the start
  balance, checked in real time with open P&L), contract caps incl. the XFA scaling plan, flat by
  15:10 CT, a 40% daily profit cap protecting the Combine consistency rule, plus your own daily loss
  limit, trade count, losing-streak and cooldown limits.
- **Risk-based position sizing** — every trade is sized from its stop so a stop-out costs about
  `risk_per_trade`, and never enough to breach your daily limit or approach the MLL.
- **Safe execution** — protective stop placed immediately after every fill (position is flattened
  if that fails), stop/target one-cancels-other, stops only tighten, periodic reconciliation with
  the account, idempotent order tags, no blind retries of order placement.
- **4 strategies** — Opening Range Breakout, research-based Intraday Momentum "noise area"
  (Zarattini/Aziz/Barbon 2024), EMA trend, VWAP mean reversion; easy to add your own.
- **Backtester** using the exact live code path, conservative fill model, TopstepX fees, and a
  Combine pass-rate simulation, with an interactive HTML report.
- **Paper trading** on live TopstepX prices with simulated fills.
- **Local dashboard** (http://127.0.0.1:8765) with guardrail meters and Pause / Flatten / Stop
  buttons; KILL-file kill switch; emergency flatten command.
- **Telegram remote control** — `/status`, `/pause`, `/resume`, `/flatten`, `/stop` with tap buttons,
  owner-only, confirmations for dangerous actions; the setup wizard finds your chat ID for you.
- **Journal** (SQLite), rotating logs, Telegram/Discord alerts, restart-safe state.
- Realtime data via a built-in SignalR client with automatic reconnect and REST fallback.
- 87 automated tests, including an end-to-end run against a simulated TopstepX server.

## Commands

```
topstep-bot                 interactive menu
topstep-bot setup           setup wizard
topstep-bot check           test connection, list accounts
topstep-bot backtest        backtest + HTML report      (--download, --days, --strategy, --data ...)
topstep-bot run             start the bot               (--mode paper|live)
topstep-bot flatten         EMERGENCY: close everything on the account
topstep-bot journal         recent trades and daily results
topstep-bot strategies      describe strategies
topstep-bot demo            demo backtest on synthetic data
topstep-bot telegram-test   send a test message to your Telegram bot
```

## Project layout

```
src/topstep_bot/
  api/            REST client (auth, rate limits, retries), SignalR client, realtime streams
  broker/         live TopstepX broker and simulated paper broker
  strategies/     strategy base class + bundled strategies
  risk/           Topstep rules (MLL, caps, consistency) and the risk manager
  backtest/       runner, metrics, Combine simulation, HTML report, data loading
  dashboard/      local web dashboard
  engine.py       trading core shared by backtests and live trading
  execution.py    order/trade lifecycle (entry, stop, target, OCO, reconciliation)
  live.py         live/paper runner
  control.py      pause/resume/flatten/stop actions shared by dashboard and Telegram
  telegram_control.py  Telegram bot remote control
  cli.py, wizard.py
tests/            pytest suite (run: pytest)
docs/HOW_TO_USE.md
```

## Important

- Topstep requires automated trading to run **from your own computer** — no VPS or VPN — and
  prohibits high-frequency trading. API trading is available on Combine, Express Funded and
  Practice accounts (not Live Funded).
- Topstep's rules and fees change; verify them in your dashboard and help.topstep.com.
- Trading futures involves substantial risk of loss. Backtests are hypothetical. This software
  comes with no guarantee of profit; you are responsible for every order it places.
