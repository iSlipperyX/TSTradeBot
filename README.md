# Topstep Bot

An automated futures trading bot for **Topstep** accounts, using the official **TopstepX
(ProjectX Gateway) API**. It backtests, paper trades and trades live with the same engine, and its
risk layer is built around Topstep's rules so a strategy can't accidentally break them.

**→ New here? Read the [complete how-to-use guide](docs/HOW_TO_USE.md).**

## Quick start (Windows)

1. Install [Python 3.11+](https://www.python.org/downloads/) (tick "Add python.exe to PATH").
2. Double-click **`start.bat`**. The first run installs everything.
3. Pick **11** for a demo backtest, or **1** to set up your TopstepX API key, account, strategy and risk.
4. Then: **5** train → **4** backtest → **6** paper → switch to **Live** on the dashboard when ready (start small).

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
- **Adaptive all-day trading** — the default `adaptive` strategy runs every strategy through the whole
  session and trades only the ones the bot's **knowledge base** shows to be working at that time of day
  (open / midday / close) in the current volatility regime. The bot **learns while it runs**: every
  signal from every strategy — traded or not — is followed to its outcome and added to the base, and it
  retrains on the last 60 days of real data each day. Nothing unproven is traded.
- **5 building-block strategies** — Opening Range Breakout, research-based Intraday Momentum "noise
  area" (Zarattini/Aziz/Barbon 2024), EMA trend, VWAP mean reversion, VWAP trend pullback; easy to
  add your own.
- **Backtester** using the exact live code path, conservative fill model, TopstepX fees, and a
  Combine pass-rate simulation, with an interactive HTML report.
- **Paper trading** on live TopstepX prices with simulated fills.
- **Separate controller** — the dashboard (http://127.0.0.1:8765) and Telegram run apart from the trading
  bot, so they stay online if it stops or crashes: see why, then start/restart it remotely; Paper/Live switch.
- **Local dashboard** with guardrail meters and Pause / Flatten / Stop
  buttons; KILL-file kill switch; emergency flatten command.
- **Recommended trades** — every strategy's signals on the dashboard and in Telegram, sized with your
  risk rules, tracked to a result; take any of them with one tap (re-priced, never oversized).
- **Knowledge tab** — what works when, per strategy, time of day and regime, with a Retrain button;
  `/knowledge` and `/train` in Telegram.
- **Live settings** — change risk, limits, times, news pause and the auto-traded strategy from the
  dashboard or Telegram, within safe bounds, with confirmation and a full audit trail.
- **Logging** — daily logs, an errors-only file, a JSON event log, crash reports, secrets masked,
  and the reason for every shutdown (`topstep-bot logs`).
- **24/7 service** — auto-restart, keep-awake, daily maintenance restart, crash recovery; preflight
  checks and ramp-up for a same-day start; automatic news blackouts.
- **Several accounts at once (v1.2)** — list your Combines/accounts in `config.yaml` and each gets its
  own bot: they take the **same calls** (each sized by its own risk) or **different calls** (own strategy,
  symbol or risk per account; `follow:` copies another account). A **hedge guard** stops two accounts
  from holding opposite positions (Topstep bans cross-account hedging). One dashboard and one Telegram
  chat control them all — per account or all at once.
- **Market knowledge** — for what you trade: key levels (prior day, overnight, open), a normal day's
  range in points and dollars, the reports that move that market (e.g. EIA crude inventories for CL)
  with automatic pauses around them, live headlines, and a contract profile (Market tab, `/brief`).
  The knowledge base also learns how each strategy does on news days vs quiet days.
- **Funded-account playbook** — Topstep's rules per stage (checked against the help center, with
  sources), Combine pass and XFA payout progress (winning days, consistency ratio, payout estimate,
  scaling limit), a winning-day protector and consistency cap, and preflight warnings for settings
  Topstep prohibits (`/funded`, `/rules`).
- **Several API keys** — one per account if you like, or keys for a second login (`topstep-bot keys`).
- **Telegram remote control** — `/status`, `/pause`, `/resume`, `/flatten`, `/stop` with tap buttons,
  owner-only, confirmations for dangerous actions; the setup wizard finds your chat ID for you.
- **Journal** (SQLite), rotating logs, Telegram/Discord alerts, restart-safe state.
- Realtime data via a built-in SignalR client with automatic reconnect and REST fallback.
- 189 automated tests, including an end-to-end run against a simulated TopstepX server.

## Commands

```
topstep-bot                 interactive menu
topstep-bot setup           setup wizard
topstep-bot check           test connection, list accounts
topstep-bot backtest        backtest + HTML report      (--download, --days, --strategy, --data ...)
topstep-bot train           learn which strategy works at which time of day from recent real data
topstep-bot start           dashboard + Telegram + bot  (--mode paper|live)
topstep-bot run             the trading bot only (normally started by 'start')
topstep-bot flatten         EMERGENCY: close everything on the account
topstep-bot journal         recent trades and daily results
topstep-bot strategies      describe strategies
topstep-bot demo            demo backtest on synthetic data
topstep-bot telegram-test   send a test message to your Telegram bot
topstep-bot preflight       check everything before trading live (go-live = preflight + start)
topstep-bot autostart on    start everything when you sign in to Windows
topstep-bot logs            recent errors; --open / --bundle
topstep-bot accounts        your accounts and which ones share the same calls
topstep-bot keys            list / add / remove / test stored API keys

With several accounts, add --account <name> (or -a) to run, backtest, train, preflight, flatten...
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
  knowledge.py    the knowledge base (what works when) and its training
  recommendations.py  every strategy's signals tracked to an outcome (feeds the knowledge base)
  execution.py    order/trade lifecycle (entry, stop, target, OCO, reconciliation)
  controller.py   dashboard + Telegram + supervision of the bot process(es), one per account
  crossaccount.py hedge guard: no opposite positions across accounts
  market_knowledge.py / market_intel.py   what each market is, its key reports, live levels and the brief
  news.py         economic calendar (per instrument) and headlines
  funded.py       Topstep rules per stage and progress to the pass / next payout
  worker_api.py   the bot's private local API (used by the controller)
  web.py          tiny local HTTP server shared by both
  live.py         live/paper runner (the bot process)
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
