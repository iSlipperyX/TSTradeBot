# Topstep Bot

An automated futures trading bot for **Topstep** accounts, using the official **TopstepX
(ProjectX Gateway) API**. It trains, backtests, paper trades and trades live with the same engine,
runs unattended 24/7 on your own PC, and its risk layer is built around Topstep's rules so a
strategy can't accidentally break them.

**→ New here? Read the [complete how-to-use guide](docs/HOW_TO_USE.md).**

## Quick start (Windows)

1. Install [Python 3.11+](https://www.python.org/downloads/) (tick "Add python.exe to PATH").
2. Double-click **`start.bat`**. The first run installs everything.
3. Pick **1** to set up your TopstepX API key, account, strategy and risk (or **11** for a demo).
4. Pick **5 (train)** to find the strategy and settings that held up on real data they never saw.
5. Pick **2 (start trading today)**: every safety check runs first, and the first live days trade
   at reduced risk. Choose 24/7 to keep it running.

Other systems: `pip install -e .` then run `topstep-bot`.

## Features

- **Training** — walk-forward selection of strategy settings: chosen on one stretch of real
  history, judged only on the next stretch it never saw, with an HTML report and one-step saving
  to `config.yaml`. See [what 10½ years of real Nasdaq data says](docs/HOW_TO_USE.md#14-the-strategies).
- **Topstep-aware risk engine** — trailing Maximum Loss Limit (end-of-day, locks at the start
  balance, checked in real time with open P&L), contract caps incl. the XFA scaling plan, flat by
  15:10 CT, a 40% daily profit cap protecting the Combine consistency rule, plus your own daily loss
  limit, trade count, losing-streak and cooldown limits — all restored after a restart.
- **Risk-based position sizing** — every trade is sized from its stop so a stop-out costs about
  `risk_per_trade`, and never enough to breach your daily limit or approach the MLL; optional ATR
  floor on stop distance; reduced risk for the first live days (ramp-up).
- **Safe execution** — entries that fill immediately or are cancelled, a protective stop placed
  right after every fill (position flattened if that fails), stop/target one-cancels-other, stops
  only tighten, stops resized on partial fills, reconciliation with the account every 15 s,
  idempotent order tags, no blind retries of order placement, and re-adoption of the bot's own
  position after a crash.
- **6 strategies** — research-based 5-minute Opening Range Momentum (Zarattini/Aziz 2023), Intraday
  Momentum "noise area" (Zarattini/Aziz/Barbon 2024) and Late-Day Momentum (Gao/Han/Li/Zhou 2018),
  plus classic Opening Range Breakout, EMA trend and VWAP mean reversion; easy to add your own.
- **24/7 service** — auto-restart after crashes or hangs (heartbeat), daily maintenance restart,
  keeps the PC awake, start at Windows sign-in, morning check-in message.
- **Same-day start** — a preflight that checks the account, MLL, contract, data, PC clock,
  calendar, news and alerts and backtests every strategy on the latest data before going live.
- **News blackouts** — no new entries around high-impact USD releases, from a live economic calendar.
- **Backtester** using the exact live code path, a conservative fill model, TopstepX fees, and a
  Combine pass-rate simulation, with an interactive HTML report.
- **Paper trading** on live TopstepX prices with simulated fills.
- **Local dashboard** (http://127.0.0.1:8765) with guardrail meters and Pause / Flatten / Stop
  buttons; KILL-file kill switch; emergency flatten command.
- **Recommended trades** — every strategy's signals on the dashboard and in Telegram, sized with your
  risk rules, tracked to a result; take any of them with one tap (re-priced, never oversized).
- **Live settings** — change risk, limits, times, news pause and the auto-traded strategy from the
  dashboard or Telegram, within safe bounds, with confirmation and a full audit trail.
- **Logging** — daily logs, an errors-only file, a JSON event log, crash reports, secrets masked,
  and the reason for every shutdown (`topstep-bot logs`).
- **Telegram remote control** — `/status`, `/pause`, `/resume`, `/flatten`, `/ideas`, `/set`, `/stop` with tap buttons,
  owner-only, confirmations for dangerous actions; the setup wizard finds your chat ID for you.
- **Journal** (SQLite) and Telegram/Discord alerts.
- Realtime data via a built-in SignalR client with automatic reconnect and REST fallback.
- 170+ automated tests, including end-to-end runs against a simulated TopstepX server.

## Commands

```
topstep-bot                 interactive menu
topstep-bot setup           setup wizard
topstep-bot go-live         preflight checks, then live trading (24/7 or this session)
topstep-bot preflight       the checks alone
topstep-bot train           walk-forward training         (--days, --strategies, --save ...)
topstep-bot backtest        backtest + HTML report        (--download, --days, --strategy, --data ...)
topstep-bot run             start the bot                 (--mode paper|live)
topstep-bot service         run 24/7 with auto-restart
topstep-bot autostart on    start the service at Windows sign-in (off / status)
topstep-bot check           test connection, list accounts
topstep-bot download        save history to data/
topstep-bot flatten         EMERGENCY: close everything on the account
topstep-bot journal         recent trades and daily results
topstep-bot strategies      describe strategies
topstep-bot demo            demo backtest on synthetic data
topstep-bot telegram-test   send a test message to your Telegram bot
topstep-bot logs            recent errors and crash reports  (--open, --bundle for support)
```

## Project layout

```
src/topstep_bot/
  api/            REST client (auth, rate limits, retries), SignalR client, realtime streams
  broker/         live TopstepX broker and simulated paper broker
  strategies/     strategy base class + bundled strategies
  risk/           Topstep rules (MLL, caps, consistency) and the risk manager
  backtest/       runner, metrics, Combine simulation, HTML reports, data loading
  dashboard/      local web dashboard
  engine.py       trading core shared by backtests and live trading
  execution.py    order/trade lifecycle (entry, stop, target, OCO, reconciliation)
  live.py         live/paper runner
  training.py     walk-forward training
  recommendations.py  trade ideas from every strategy;  remote.py  live settings changes
  logging_setup.py    log files, redaction, crash reports
  service.py      24/7 supervisor;  autostart.py, keepawake.py
  preflight.py    same-day readiness checks;  news.py  economic calendar
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
- Trading futures involves substantial risk of loss. Backtests and training results are
  hypothetical. This software comes with no guarantee of profit; you are responsible for every
  order it places.
