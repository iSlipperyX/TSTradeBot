# Topstep Bot

An automated futures trading bot for **Topstep** accounts, using the official **TopstepX
(ProjectX Gateway) API**. It trains, backtests, paper trades and trades live with the same engine,
runs unattended 24/7 on your own PC, and its risk layer is built around Topstep's rules so a
strategy can't accidentally break them.

**→ New here? Read the [complete how-to-use guide](docs/HOW_TO_USE.md).**

## Quick start (Windows)

1. Install [Python 3.11+](https://www.python.org/downloads/) (tick "Add python.exe to PATH").
2. Double-click **`start.bat`**. The first run installs everything, then the dashboard opens in your
   browser. Nothing is asked in the black window; keep it open.
3. On the dashboard's **Setup** tab, enter your TopstepX API key, pick the account, strategy and risk
   (every field has a suggestion) and press **Save**.
4. Press **Start** with the **Paper | Live** switch on Paper to watch it on live prices with simulated
   orders. Switch to Live (type `LIVE`) when you're ready, and start small.
5. Optional extras live in the text menu, `start.bat menu`: **5 (train)** so the bot learns which
   strategy works at which time of day, **4 (backtest)**, and **2 (start trading today)**, which runs
   every safety check first.

Other systems: `pip install -e .` then run `topstep-bot` (it starts the dashboard).

No bot can guarantee passing the Combine or making a profit, and this one doesn't either.

## New in 4.1

- **An AI trading assistant** — a Claude model watches the market with the bot. Switch on **watch and
  manage open trades** (tighter stop, breakeven, early close; never wider or bigger) and **place its
  own trades** (small and few) separately, per account, from the Overview, the Setup tab or Telegram
  (`/assistant`). Both start off. Every action goes through the same risk rules and Topstep rules as the
  bot's own trades, every decision is recorded with why, its trades are tagged AI and kept out of the
  strategies' evidence, and without a Claude key or budget the bot trades as usual.
  ([guide](docs/HOW_TO_USE.md#46-the-ai-trading-assistant))

## New in 4.0

- **Several markets at once** — one account trades up to six markets side by side. Add, drop or switch
  them while the bot runs, **without a restart**, from the markets strip at the top of the dashboard or
  in Telegram (`/markets`, `/add`, `/drop`, `/switch`); an open trade keeps its stop and target. Every
  Topstep limit counts all of them together: one MLL, one daily loss limit, one position limit (10
  micros = 1 mini), one consistency line, and no opposite positions in related markets.
  ([guide](docs/HOW_TO_USE.md#41-several-markets-at-once))
- **A probability calculator** — every signal gets its P(win), average win and loss, expected value
  after costs and an 80% range, pooled from related markets down to this time of day and market mood.
  The bot skips a signal it is at least 90% sure loses, *Make money every day* sizes each trade by a
  quarter of the Kelly fraction, and every prediction is scored against its outcome so you can see how
  well it predicts. ([guide](docs/HOW_TO_USE.md#42-the-probability-calculator))
- **A knowledge network that grows by itself** — related markets inform each other's odds, and each
  night, while flat, the bot learns a few more related markets from the history saved on your PC.
  ([guide](docs/HOW_TO_USE.md#43-the-knowledge-network))
- **More aggressive** — in *Make money every day* each strategy may trade three times its usual count,
  each market brings its own 12 trades a day, a large, well-measured edge may risk up to 1.5 times the
  usual amount, and 35% of the day's profit presses the next trade (up to 2.5 times the risk per trade).
- **Charts of the current trade** — live candles with the entry, stop, target and today's fills on the
  Overview, and your planned stop and target on the trade ticket. ([guide](docs/HOW_TO_USE.md#44-the-trade-chart))
- **A simpler dashboard** — seven tabs instead of eleven. **Ask** explains anything in the bot from its
  records and its own guide; **Knowledge** puts real trades, signals watched live, real market history,
  simulations and related markets in one place. ([guide](docs/HOW_TO_USE.md#45-the-simpler-dashboard))

More markets and bigger size mean bigger swings both ways. A probability is a long-run frequency from
past signals, not a promise about the next trade.

## New in 3.0 — the first release

- **Make money every day** — a third goal on the Setup tab: the bot trades every signal its strategies
  find (each strategy up to twice its usual daily count, at most 12 trades a day), sizes unproven
  signals at half risk, adds part of the day's realized profit to the next trade, locks in a good day
  and stops at a daily profit goal. ([guide](docs/HOW_TO_USE.md#39-make-money-every-day-the-aggressive-mode))
- **Two risk gaps closed, in every goal** — no single trade can carry a Combine day past the
  consistency line (its target is moved to the line or the size comes down), and every loss limit
  allows for a sudden jump past the stop, measured per market.
- **The market network** — all 51 markets read together: what moves with what, which are stressed,
  the board's risk appetite, the next 24 hours' reports, and each market's jump risk. On the Market
  tab, `/network`, `topstep-bot network`, the Ask tab and the AI coach.
  ([guide](docs/HOW_TO_USE.md#40-the-market-network))
- **One Claude integration** — the AI coach and the Ask answers share one key, one model list with
  prices (Opus 5.5 by default), one spending log and the same plain-words errors.

Aggressive means bigger swings both ways. It can't break a Topstep rule by itself, but it can lose.

## New in 2.2

- **Ask the bot why** — the dashboard's **Ask** tab, or typing to the bot in Telegram (`/ask`), explains
  every signal it weighed: why it took it, skipped it or passed on it, and what the result taught it. It
  also answers from the Simulation Lab and the AI coach's lessons.
  ([guide](docs/HOW_TO_USE.md#38-the-central-intelligence-ask-the-bot-why))
- **Decision records** for every signal, and a **decision lab** (`topstep-bot decision-lab`) that compares
  the classic rule with a new evidence rule. The bot keeps the classic rule until the evidence rule wins
  clearly on enough replayed history.
- **One Claude API key** for the AI coach and the Ask tab's optional AI answers, with one monthly budget.

## New in 2.1

- **Every market Topstep permits** — all 51: equity indexes, crypto, currencies, energy, metals,
  interest rates, grains and livestock, with their contract specs, Topstep fees, busiest hours and what
  moves each one. Grains and livestock are closed out before their early market close.
- **All their history, no login needed** — one click (or `topstep-bot markets --download`) fetches
  every market: TopstepX when your login works, Yahoo Finance for the rest, plus ten years of daily
  prices for long-term context. Refreshed in the background about twice a day.
- **Simulation Lab on every market** — about 1,600 live-like Combines in a few minutes, a summary per
  asset class, and **Opportunities**: every market × strategy × time of day that made money in both
  halves of the history, with an honest note on how many would look that good by luck. It runs by
  itself every weekday after the close. ([guide](docs/HOW_TO_USE.md#36-every-market-topstep-permits))
- **Trade another market today** — from the trade ticket, the Simulations tab or Telegram `/switch`:
  the bot restarts on that market for the rest of the day, then goes back by itself.
  ([guide](docs/HOW_TO_USE.md#37-trading-another-market-today))
- **Auto market from every market** — the morning pick can choose from all 51 markets, with a stricter
  test when it compares that many.

## New in 2.0

- **A redesigned dashboard** — a trading-desk console: navigation rail, status bar, KPI strip,
  monospaced figures, and colour used only for state (P&L, risk, mode). Works on a phone too.
- **Simulation Lab** — replays the bot's real trading code through history as fresh Topstep Combines
  started on many different days (128 by default, every market × strategy), adds thousands of
  resampled Combine paths, splits the history in halves and runs a held-out check, then tells you
  plainly whether any playbook has an edge. One click makes a playbook the bot's.
  ([guide](docs/HOW_TO_USE.md#34-simulation-lab-hundreds-of-combines-from-history))
- **AI Coach** — Claude reads what the bot knows, runs its own simulations, records lessons the bot keeps,
  and proposes changes you approve. Your own Claude API key and prepaid credits, with per-session and
  monthly spending limits. It can never trade, raise risk or change a setting by itself.
  ([guide](docs/HOW_TO_USE.md#35-the-ai-coach-claude-teaches-the-bot))
- `topstep-bot simulate`, `topstep-bot coach`, and `/lab` and `/coach` in Telegram.

## Features

- **AI trading assistant** (new in 4.1) — optional, per account: manages open trades and places a few
  small trades of its own through every risk rule, with a recorded reason for every decision
  ([how](docs/HOW_TO_USE.md#46-the-ai-trading-assistant)).
- **Several markets at once** (new in 4.0) — up to six per account, added, dropped or switched while the
  bot runs without a restart, with every Topstep limit counted across all of them
  ([how](docs/HOW_TO_USE.md#41-several-markets-at-once)).
- **Probability calculator** (new in 4.0) — P(win), payoff and expected value after costs for every
  signal, with its range and a calibration record; it vetoes confident losers and sizes the aggressive
  mode by edge ([how](docs/HOW_TO_USE.md#42-the-probability-calculator)).
- **Knowledge network** (new in 4.0) — related markets inform each other's odds, and the bot learns new
  related markets by itself from saved history ([how](docs/HOW_TO_USE.md#43-the-knowledge-network)).
- **A central intelligence you can talk to** (new in 2.2) — ask the bot why it took or skipped a trade,
  what each trade taught it, how a strategy is doing or what's next, on the dashboard's **Ask** tab or by
  typing to it in Telegram. Every signal is recorded as a **decision record** (each strategy's verdict,
  the evidence, the market, your limits, then the result and the lesson), and every answer comes from
  those records. Optional AI answers with the same Claude API key as the AI coach; built-in answers without it. It can
  explain, never trade.
- **Decision lab** (new in 2.2) — a new evidence rule pools every level of what the bot knows, after
  costs and backed by the long-run memory. The lab replays it against the 1.2 rule without peeking, and
  the bot only switches when the new rule wins clearly on enough history.
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
  signal from every strategy — traded or not, around the clock — is followed to its outcome and added to
  the base, and it retrains on the last 60 days of real data after every session. Nothing unproven is traded.
- **Knowledge that survives anything** — each start gets the knowledge ready first: it retrains from the
  history saved on your PC (downloading only what's missing), and after a mid-day restart it replays the
  day so far so the morning's ideas are still learned from. Every live observation is also kept in a
  never-trimmed ledger, with daily backups, and a damaged knowledge file is repaired automatically.
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
- **Set up from the dashboard** — `start.bat` opens the dashboard and asks nothing in the console. The
  Setup tab fills in the login, account, strategy, risk, Telegram, Discord and Start with Windows, and
  edits `config.yaml` in place without losing your comments. Credentials stay in `.env` on your PC and
  are never shown back.
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
- **Knowledge tab** — everything the bot knows in one place (real trades, signals watched live, real
  market history, simulations, related markets), the odds of every strategy now, and **What works**:
  per strategy, time of day and regime, with a Retrain button; `/knowledge` and `/train` in Telegram.
- **Charts of the current trade** — live candles with entry, stop, target and fills on the Overview and
  the trade ticket ([how](docs/HOW_TO_USE.md#44-the-trade-chart)).
- **What the bot learned** — every signal and trade keeps a market snapshot, its price path (best and
  worst point), its costs and, for real fills, the slippage. Knowledge → What works, `/knowledge` and
  `topstep-bot insights` show results after costs, real fills against simulated ones and conditions
  worth testing; `--csv` exports everything for Excel.
- **What the bot knows and when it trades next** — the bot explains in plain sentences what it knows,
  what it would trade right now and why, and when its next trade is likely (an honest estimate from
  its own history and the rules, with what it's based on): Knowledge tab, a countdown on the
  dashboard, `/brief` and `/next` in Telegram.
- **First trade after starting** — with the *Teach the bot* goal, the bot makes one educated trade
  within 15 minutes of starting (or of the session opening): the setup its knowledge base supports
  best, at 1 contract, with every Topstep rule and risk limit applied, filed apart so it learns from
  it. `first-trade-test` compares its picks with a coin flip on your history. It is there to teach
  the bot, not a promise of profit.
- **Long-run memory** — every price bar the bot sees is kept, up to a year (or more) of history is
  backfilled, and all of it is replayed through every strategy daily, for a much bigger knowledge base
  to report on and compare against. It doesn't change how the bot trades.
- **Live settings** — change risk, limits, times, news pause and the auto-traded strategy from the
  dashboard or Telegram, within safe bounds, with confirmation and a full audit trail.
- **Logging** — daily logs, an errors-only file, a JSON event log, crash reports, secrets masked,
  and the reason for every shutdown (`topstep-bot logs`).
- **Telegram remote control** — `/status`, `/pause`, `/resume`, `/flatten`, `/ideas`, `/set`, `/stop` with tap buttons,
  owner-only, confirmations for dangerous actions; the dashboard's Setup tab finds your chat ID for you.
- **The dashboard on your phone, inside Telegram** — switch on phone access and `/dashboard` opens the
  full dashboard in Telegram's built-in browser when you're away. Only your Telegram account can open it,
  LIVE can't be switched on from the phone, and the bot keeps running on your PC: a secure Cloudflare
  link carries only the dashboard, with no router changes ([how](docs/HOW_TO_USE.md#open-the-dashboard-on-your-phone-inside-telegram)).
- **Several accounts at once** — one bot per Combine or funded account, from one dashboard and one
  Telegram chat. Accounts can take the same calls, their own calls, or copy another account's calls,
  each with its own risk and limits; a hedge guard stops opposite positions across accounts, which
  Topstep prohibits ([how](docs/HOW_TO_USE.md#29-trading-several-accounts)).
- **Accounts on the dashboard** — the Setup tab shows what TopstepX reports for every account, read
  with its API key (balance, can it trade, open positions and orders, today's trades, and anything
  that doesn't match your settings), with **+ Add account**, **Edit** and **Remove** buttons.
- **Several API keys** — one per account or per TopstepX login if you like, managed on the dashboard
  or with `topstep-bot keys`, and masked in every log ([how](docs/HOW_TO_USE.md#30-several-api-keys)).
- **The bot can pick its markets** — set the market to Auto and each day it trades the market (or up to
  six) where its strategy did best on recent days held back from training, after costs; if none shows
  an edge it stays on your market ([how](docs/HOW_TO_USE.md#letting-the-bot-pick-the-market)).
- **Market knowledge** — Knowledge → Market data and `/market`: the contract's specs and what moves it, key
  levels, the normal day's range, today's reports for your market and its headlines; a session journal
  of market conditions next to your trades ([how](docs/HOW_TO_USE.md#31-what-the-bot-knows-about-the-market-and-the-news)).
- **Funded-account goals** — progress to your Combine pass or next Express payout on either payout path
  (`/funded`), and goal rules that only make the bot more careful, e.g. keeping a winning day a winning
  day ([how](docs/HOW_TO_USE.md#32-funded-account-goals-and-topsteps-rules)).
- **Research that grows from the internet** — while it runs, the bot archives every economic calendar
  (so retraining knows past news days), headlines for your markets and ten years of daily prices, and
  watches Topstep's rule pages, alerting you when one changes. Information only: it never trades on it
  ([how](docs/HOW_TO_USE.md#33-research-knowledge-that-grows-from-the-internet)).
- **Entries inside the bid/offer** — entry orders are limit orders capped at the best bid/offer (and a
  few ticks of slippage at most), never through it.
- **Journal** (SQLite) and Telegram/Discord alerts.
- Realtime data via a built-in SignalR client with automatic reconnect and REST fallback.
- **Updates from GitHub** — the bot checks for a newer version every few hours and tells you on the
  dashboard and Telegram what changed. It installs only when you confirm, only while no trade or order
  is open (or after the close), test-starts the new version, restarts itself, and puts the previous
  version back if anything fails. Your settings and data are kept ([how](docs/HOW_TO_USE.md#updating)).
- 600+ automated tests, including end-to-end runs against a simulated TopstepX server and Telegram, and a test for every Topstep rule.

## Commands

```
topstep-bot                 start the dashboard (set up and start the bot there); same as 'server'
topstep-bot menu            the text menu
topstep-bot setup           question-and-answer setup in the console
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
topstep-bot decision-lab    decision lab: replay the classic and evidence rules without peeking and pick one
topstep-bot first-trade-test  backtest the first trade after starting against a coin flip (--days N)
topstep-bot update          check GitHub for a newer version and install it (--check, --token, --undo)
topstep-bot accounts        your accounts: what each trades, its key, which share the same calls (--check: ask TopstepX)
topstep-bot keys            list, add, remove or test stored API keys (add NAME, remove NAME, test)
topstep-bot markets         every market Topstep permits and its history (--download, --yahoo)
topstep-bot simulate        Simulation Lab on every market (--scope, --classes, --symbols, --starts)

With several accounts, add --account NAME to act on one (e.g. 'start --mode live --account c2').
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
  brain/          the central intelligence: lookups, built-in answers, optional AI answers, the guide search
  reasoning.py    a decision record for every signal: verdicts, evidence, market, limits, result, lesson
  evidence.py     the evidence rule (pooled, after costs);  policy.py  which rule decides;  lab.py  the decision lab
  fmt.py          shared number formatting
  forecast.py     when the next trade is likely;  briefing.py  what the bot knows, in plain words
  execution.py    order/trade lifecycle (entry, stop, target, OCO, reconciliation)
  controller.py   dashboard + Telegram + supervision of the bot processes (one per account)
  setup_service.py  the dashboard's Setup tab;  config_edit.py  edits config.yaml keeping comments
  worker_api.py   the bot's private local API (used by the controller)
  web.py          tiny local HTTP server shared by both
  live.py         live/paper runner (the bot process): every market of one account, added and dropped live
  training.py     walk-forward tuning ('tune');  remote.py  live settings changes
  logging_setup.py    log files, redaction, crash reports
  service.py      bot <-> controller contract;  autostart.py, keepawake.py
  preflight.py    same-day readiness checks;  news.py  economic calendar
  market_knowledge.py  each market's specs and what moves it;  market_intel.py  live levels and ranges
  research.py     the research library: what the bot gathers from the internet
  funded.py       progress to the Combine pass or next payout;  crossaccount.py  the hedge guard
  account_check.py  what TopstepX says about each account;  market_pick.py  the bot picks its market(s)
  probability.py  the probability calculator: P(win), payoff, expected value, Kelly sizing, calibration
  knowledge_network.py  related markets' knowledge, and learning new ones from saved history
  risk/book.py    one account's shared risk across every market it trades;  broker/shared.py  one connection, many markets
  control.py      pause/resume/flatten/stop actions shared by dashboard and Telegram
  manual.py       manual trades from the dashboard: trade ticket, suggestions, rule checks
  assistant.py    the AI trading assistant: when it thinks, what it sees, its guardrails and its records
  setups.py       the trades each strategy is building toward (dashboard's Getting ready to trade)
  telegram_control.py  Telegram bot remote control
  phone_access.py the dashboard inside Telegram on your phone (Telegram sign-in + Cloudflare tunnel)
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
