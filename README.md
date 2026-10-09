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
4. Pick **5 (train)** so the bot learns which strategy works at which time of day from recent real data,
   then **4 (backtest)** and read the report.
5. Pick **6 (paper)** to watch it on live prices, or **2 (start trading today)**: every safety check runs
   first, and the first live days trade at reduced risk. The dashboard's **Paper | Live** switch flips
   between them later (start small).

Other systems: `pip install -e .` then run `topstep-bot`.

## Features

- **Topstep rules enforced in code** ([TOPSTEP_RULES.md](docs/TOPSTEP_RULES.md), checked Oct 2026) —
  trailing Maximum Loss Limit (end-of-day, locks, checked live with open P&L), the optional Daily
  Loss Limit, the Combine Consistency Target (55% of the profit target) and stop-at-target, position
  limits incl. the XFA Scaling Plan and metals/energy product caps (checked again right before every
  entry order), never holding the maximum size into news, flat by 15:10 CT, an order-rate breaker
  (no HFT), refusal of Live Funded accounts and a VPS/remote-host warning — plus your own daily loss
  limit, trade count, losing-streak and cooldown limits, all restored after a restart.
  `topstep-bot rules` shows the numbers for your account.
- **Risk-based position sizing** — every trade is sized from its stop so a stop-out costs about
  `risk_per_trade`, and never enough to breach your daily limit or approach the MLL; optional ATR
  floor on stop distance; reduced risk for the first live days (ramp-up).
- **Safe execution** — entries that fill immediately or are cancelled, a protective stop placed
  right after every fill (position flattened if that fails), stop/target one-cancels-other, stops
  only tighten, stops resized on partial fills, reconciliation with the account every 15 s,
  idempotent order tags, no blind retries of order placement, and re-adoption of the bot's own
  position after a crash.
- **Adaptive all-day trading** — the default `adaptive` strategy runs every strategy through the whole
  session and trades only the ones the bot's **knowledge base** shows to be working at that time of day
  (open / midday / close) in the current volatility regime. The bot **learns while it runs**: every
  signal from every strategy — traded or not — is followed to its outcome and added to the base, and it
  retrains on the last 60 days of real data each day. Nothing unproven is traded.
- **Tuning** — `tune` picks strategy settings walk-forward: chosen on one stretch of real
  history, judged only on the next stretch it never saw, with an HTML report and one-step saving
  to `config.yaml`. See [what 10½ years of real Nasdaq data says](docs/HOW_TO_USE.md#12-the-strategies).
- **7 building-block strategies** — research-based Intraday Momentum "noise area" (Zarattini/Aziz/Barbon
  2024), 5-minute Opening Range Momentum (Zarattini/Aziz 2023) and Late-Day Momentum (Gao/Han/Li/Zhou
  2018), plus classic Opening Range Breakout, EMA trend, VWAP mean reversion and VWAP trend pullback;
  each can also run alone. Easy to add your own.
- **24/7 operation** — the controller restarts the bot after crashes or hangs (heartbeat), does a daily
  maintenance restart, keeps the PC awake, starts at Windows sign-in and sends a morning check-in.
- **Same-day start** — a preflight that checks the account, MLL, contract, data, PC clock,
  calendar, news and alerts and backtests every strategy on the latest data before going live.
- **News blackouts** — no new entries around high-impact USD releases, from a live economic calendar.
- **Backtester** using the exact live code path, a conservative fill model, TopstepX fees, and a
  Combine pass-rate simulation, with an interactive HTML report.
- **Paper trading** on live TopstepX prices with simulated fills.
- **Separate controller** — the dashboard (http://127.0.0.1:8765) and Telegram run apart from the trading
  bot, so they stay online if it stops or crashes: see why, then start/restart it remotely; Paper/Live switch.
- **Local dashboard** with guardrail meters and Pause / Flatten / Stop
  buttons; KILL-file kill switch; emergency flatten command.
- **Manual trades from the dashboard** — a trade ticket that suggests a stop, target and size, checks
  every Topstep and risk rule first, shows what the knowledge base says, and records your results so the
  bot learns from them too.
- **Getting ready to trade** — watch each strategy build toward its next trade: which entry conditions
  are met, the planned entry/stop/target and size, whether a rule would block it, and a history of
  setups forming, firing and being cancelled.
- **Recommended trades** — every strategy's signals on the dashboard and in Telegram, sized with your
  risk rules, tracked to a result; take any of them with one tap (re-priced, never oversized).
- **Knowledge tab** — what works when, per strategy, time of day and regime, with a Retrain button;
  `/knowledge` and `/train` in Telegram.
- **What the bot learned** — every signal and trade keeps a market snapshot, its price path (best and
  worst point), its costs and, for real fills, the slippage. The Knowledge tab, `/knowledge` and
  `topstep-bot insights` show results after costs, real fills against simulated ones and conditions
  worth testing; `--csv` exports everything for Excel.
- **What the bot knows and when it trades next** — the bot explains in plain sentences what it knows,
  what it would trade right now and why, and when its next trade is likely (an honest estimate from
  its own history and the rules, with what it's based on): Knowledge tab, a countdown on the
  dashboard, `/brief` and `/next` in Telegram.
- **Long-run memory** — every price bar the bot sees is kept, up to a year (or more) of history is
  backfilled, and all of it is replayed through every strategy daily, for a much bigger knowledge base
  to report on and compare against. It doesn't change how the bot trades.
- **Live settings** — change risk, limits, times, news pause and the auto-traded strategy from the
  dashboard or Telegram, within safe bounds, with confirmation and a full audit trail.
- **Logging** — daily logs, an errors-only file, a JSON event log, crash reports, secrets masked,
  and the reason for every shutdown (`topstep-bot logs`).
- **Telegram remote control** — `/status`, `/pause`, `/resume`, `/flatten`, `/ideas`, `/set`, `/stop` with tap buttons,
  owner-only, confirmations for dangerous actions; the setup wizard finds your chat ID for you.
- **Journal** (SQLite) and Telegram/Discord alerts.
- Realtime data via a built-in SignalR client with automatic reconnect and REST fallback.
- **Updates from GitHub** — the bot checks for a newer version every few hours and tells you on the
  dashboard and Telegram what changed. It installs only when you confirm, only while no trade or order
  is open (or after the close), test-starts the new version, restarts itself, and puts the previous
  version back if anything fails. Your settings and data are kept ([how](docs/HOW_TO_USE.md#updating)).
- 300+ automated tests, including end-to-end runs against a simulated TopstepX server and Telegram, and a test for every Topstep rule.

## Commands

```
topstep-bot                 interactive menu
topstep-bot setup           setup wizard
topstep-bot go-live         preflight checks, then live trading (24/7 or this session)
topstep-bot preflight       the checks alone
topstep-bot start           dashboard + Telegram + bot, 24/7 (--mode paper|live)
topstep-bot run             the trading bot only (normally started by 'start')
topstep-bot train           learn which strategy works at which time of day from recent real data
topstep-bot tune            walk-forward test of each strategy's settings (--days, --strategies, --save ...)
topstep-bot backtest        backtest + HTML report        (--download, --days, --strategy, --data ...)
topstep-bot autostart on    start everything at Windows sign-in (off / status)
topstep-bot check           test connection, list accounts
topstep-bot download        save history to data/
topstep-bot flatten         EMERGENCY: close everything on the account
topstep-bot journal         recent trades and daily results
topstep-bot strategies      describe strategies
topstep-bot rules           Topstep rules for your account and how the bot enforces them
topstep-bot demo            demo backtest on synthetic data
topstep-bot telegram-test   send a test message to your Telegram bot
topstep-bot logs            recent errors and crash reports  (--open, --bundle for support)
topstep-bot insights        what the bot has learned: results after costs, real fills, conditions (--csv FILE, --longrun)
topstep-bot learn           grow the long-run memory: backfill history and replay it all (--import CSV, --days N)
topstep-bot update          check GitHub for a newer version and install it (--check, --token, --undo)
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
  knowledge.py    the knowledge base (what works when) and its training
  recommendations.py  every strategy's signals tracked to an outcome (feeds the knowledge base)
  memory.py       long-run memory: the market library and replaying all of it
  forecast.py     when the next trade is likely;  briefing.py  what the bot knows, in plain words
  execution.py    order/trade lifecycle (entry, stop, target, OCO, reconciliation)
  controller.py   dashboard + Telegram + supervision of the bot process
  worker_api.py   the bot's private local API (used by the controller)
  web.py          tiny local HTTP server shared by both
  live.py         live/paper runner (the bot process)
  training.py     walk-forward tuning ('tune');  remote.py  live settings changes
  logging_setup.py    log files, redaction, crash reports
  service.py      bot <-> controller contract;  autostart.py, keepawake.py
  preflight.py    same-day readiness checks;  news.py  economic calendar
  control.py      pause/resume/flatten/stop actions shared by dashboard and Telegram
  manual.py       manual trades from the dashboard: trade ticket, suggestions, rule checks
  setups.py       the trades each strategy is building toward (dashboard's Getting ready to trade)
  telegram_control.py  Telegram bot remote control
  updater.py      updates from GitHub (git or download), install and undo;  update_service.py  when, and the restart
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
