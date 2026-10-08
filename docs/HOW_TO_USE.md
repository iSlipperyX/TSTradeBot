# Topstep Bot — Complete How-To-Use Guide

This guide takes you from zero to a running bot, explains every screen and setting, and covers
what to do when something goes wrong. If you only read one section, read
[The path to live trading](#7-the-path-to-live-trading).

---

## Contents

1. [What the bot does (and doesn't do)](#1-what-the-bot-does-and-doesnt-do)
2. [What you need](#2-what-you-need)
3. [Install](#3-install)
4. [Get your TopstepX API key](#4-get-your-topstepx-api-key)
5. [Run the setup wizard](#5-run-the-setup-wizard)
6. [The main menu](#6-the-main-menu)
7. [The path to live trading](#7-the-path-to-live-trading)
8. [Training: let the bot find what works](#8-training-let-the-bot-find-what-works)
9. [Backtesting](#9-backtesting)
10. [Paper trading and the dashboard](#10-paper-trading-and-the-dashboard)
11. [Live trading](#11-live-trading)
12. [Running 24/7](#12-running-247)
13. [Stopping the bot and emergency controls](#13-stopping-the-bot-and-emergency-controls)
14. [The strategies](#14-the-strategies)
15. [Risk settings explained](#15-risk-settings-explained)
16. [News blackouts](#16-news-blackouts)
17. [Topstep rules: what the bot enforces and what is still on you](#17-topstep-rules-what-the-bot-enforces-and-what-is-still-on-you)
18. [Telegram control and alerts on your phone](#18-telegram-control-and-alerts-on-your-phone)
19. [Recommended trades](#19-recommended-trades)
20. [Changing settings from the dashboard or Telegram](#20-changing-settings-from-the-dashboard-or-telegram)
21. [Daily routine](#21-daily-routine)
22. [Full configuration reference](#22-full-configuration-reference)
23. [Command reference](#23-command-reference)
24. [Files the bot creates](#24-files-the-bot-creates)
25. [Logs: finding out what happened](#25-logs-finding-out-what-happened)
26. [Troubleshooting](#26-troubleshooting)
27. [Writing your own strategy](#27-writing-your-own-strategy)

---
## 1. What the bot does (and doesn't do)

**It does:**

- Watch one futures contract (for example MNQ, the Micro Nasdaq) on 1–60 minute bars.
- Ask a strategy, once per closed bar, whether to buy, sell or exit.
- Size every trade from the dollars you are willing to lose on it.
- Place the entry, then immediately a **protective stop** and (optionally) a **profit target**.
  When one of those fills, it cancels the other.
- Enforce Topstep's rules *and* stricter personal limits: daily loss limit, distance from the
  Maximum Loss Limit, contract caps, trade counts, and being flat before 15:10 CT.
- Pause automatically around high-impact economic news.
- Run unattended 24/7 on your PC, restart itself after a crash, and recover an open position
  after a restart.
- **Train**: test every strategy and setting on real history the way it would have traded,
  judged only on data the choice never saw, and tell you what held up.
- Show everything on a dashboard in your browser and on Telegram, keep a trade journal, and send
  alerts.

**It doesn't:**

- Guarantee profits. Backtests are hypothetical; markets change. On 10½ years of real Nasdaq
  futures data, two of the bundled strategies lost money and the classic opening-range breakout
  roughly broke even (see [section 14](#14-the-strategies)). Treat every result as evidence, not
  a promise.
- Run on a VPS or behind a VPN. Topstep's terms require automated orders to come from **your own
  computer**. Run the bot on your PC.
- Trade Live Funded accounts. Topstep only allows API trading on Combine, Express Funded and
  Practice accounts.
- Hold positions overnight. It is a day-trading bot.

---

## 2. What you need

| Item | Notes |
|---|---|
| Windows 10/11 PC (macOS/Linux also work) | Must stay on while the bot trades. The bot keeps Windows awake itself (see [section 12](#12-running-247)), but a closed laptop lid or a Windows Update restart still stops it. |
| Python 3.11 or newer | [python.org/downloads](https://www.python.org/downloads/). During install, tick **"Add python.exe to PATH"**. |
| A Topstep account | Trading Combine, Express Funded Account, or Practice account on **TopstepX**. |
| TopstepX API access | A separate subscription (about $29/month; Topstep traders get 50% off with code `topstep`). Not needed for the demo. |
| A stable internet connection | The bot reconnects automatically after drops, but frequent outages are risky. |

---

## 3. Install

### Easiest (Windows)

1. Open the bot folder.
2. Double-click **`start.bat`**.
3. The first time, it creates a private Python environment and installs everything (about a minute).
   After that it opens the menu instantly.

### Manual (any OS)

```bash
python -m venv .venv
```

Then activate it (`.venv\Scripts\activate` on Windows, `source .venv/bin/activate` on macOS/Linux) and install:

```bash
pip install -e .
```

Now `topstep-bot` is available as a command. Running it with no arguments opens the menu.

### Updating

After you download a new version of the bot into the same folder, open a Command Prompt in the
folder and run `.venv\Scripts\python -m pip install -e .` once, so any new dependencies are
installed. Your `config.yaml`, `.env` and `data/` are kept.

### Try it before setting anything up

From the menu choose **11 (demo)**, or run:

```bash
topstep-bot demo
```

This backtests the default strategy on randomly generated data and opens a report in your
browser — a quick way to see what the bot produces. Random data says nothing about whether a
strategy works.

---

## 4. Get your TopstepX API key

1. Subscribe to API access from TopstepX (Settings → API, or Topstep's "API Access" help article).
2. In the ProjectX/TopstepX dashboard go to **Settings → API** and click **Add API key**.
   Copy the whole key.
3. Note your **username** — the name you log in to TopstepX with. It is *not* your email address
   and *not* an account name like `50KTC-V2-...`.

> Your API key can place trades on **every** account on your login. Treat it like a password.
> If it ever leaks, delete it in TopstepX and create a new one.

---

## 5. Run the setup wizard

Choose **1 (setup)** in the menu, or run `topstep-bot setup`. It walks you through:

1. **API credentials.** Enter your username and API key (the key is hidden as you type). They are
   saved in a file called `.env` in the bot folder, which only lives on your computer. The wizard
   then logs in to check them and lists your accounts.
2. **Which account** the bot should use (if you have several). The wizard reads the account name
   (e.g. `50KTC-...`, `XFA-150K-...`) and pre-selects the matching size and type below.
3. **Account size** — 50K, 100K or 150K. This sets the Maximum Loss Limit, profit target and
   contract cap the bot works with.
4. **Account type** — `combine` (evaluation), `express` (Express Funded Account) or `practice`.
5. **What to trade.** Start with a **micro** contract (MNQ, MES, M2K, MYM, MGC, MCL). Micros are
   1/10th the size of the minis, so mistakes cost 10× less.
6. **Strategy.** See [section 14](#14-the-strategies) — or let [training](#8-training-let-the-bot-find-what-works) pick.
7. **Risk.** Dollars to risk per trade and your personal daily loss limit. The wizard suggests
   7.5% and 25% of your Maximum Loss Limit (for a 50K account: $150 per trade, $500 per day).
8. **Telegram and alerts** (optional) — control the bot from your phone and get alerts; see
   [section 18](#18-telegram-control-and-alerts-on-your-phone).

It writes **`config.yaml`**. You can edit that file in Notepad any time; it is commented. Re-run
the wizard to start over.

Then choose **3 (check)** in the menu. It logs in, lists your accounts (the selected one is marked
◀), and shows the exact contract the bot will trade, its tick value and estimated fees.

---

## 6. The main menu

Double-click `start.bat` (or run `topstep-bot`):

| # | Option | What it does |
|---|---|---|
| 1 | setup | The setup wizard |
| 2 | go-live | **Start trading today**: runs every preflight check, then starts live trading |
| 3 | check | Tests the connection, shows accounts and the contract |
| 4 | backtest | Tests the configured strategy on history and opens a report |
| 5 | train | **Training**: tests every strategy on unseen real data and picks the settings that held up |
| 6 | paper | Runs the bot on live prices with **simulated** orders |
| 7 | live | Runs the bot with **real** orders on your TopstepX account |
| 8 | flatten | **Emergency:** cancels all orders and closes all positions on the account |
| 9 | journal | Recent trades and daily results |
| 10 | strategies | Describes each strategy and its parameters |
| 11 | demo | Backtest on synthetic data, no account needed |
| 12 | telegram-test | Sends a test message with control buttons to your Telegram |
| 13 | service | Runs the bot 24/7 (auto-restart, keeps the PC awake, daily maintenance restart) |
| 14 | autostart | Starts the 24/7 service automatically when you sign in to Windows |
| 15 | logs | Shows recent errors and where the log files are ([section 25](#25-logs-finding-out-what-happened)) |

---

## 7. The path to live trading

You can start the same day — the bot protects a new account with reduced risk while it proves
itself. In order:

1. **Set up** (menu 1) and **check** the connection (menu 3).
2. **Train** (menu 5). The bot downloads a year of real history and tests every strategy the honest
   way (see [section 8](#8-training-let-the-bot-find-what-works)). If one held up on data it never
   saw, it offers to save that strategy and its settings to `config.yaml`. If none did, don't go
   live with these settings.
3. **Start trading today** (menu 2). The preflight checks the login, the account (and whether it
   is allowed to trade), its current Maximum Loss Limit (it asks you for it if it can't work it out),
   that the account is flat, the contract, live data, your PC clock, today's calendar and news, your
   alerts and risk settings — and backtests every strategy on the last 90 days. Fix anything marked
   ✘, then type `LIVE`. Choose **24/7** to run it as a service.
4. **The first live days run at reduced risk** (ramp-up): by default the first 3 trading days on a
   new account risk 50% of your normal amount per trade. Only days on which the bot actually traded
   count.
5. **Watch the first trades closely.** Keep TopstepX open alongside the dashboard (or Telegram) and
   confirm every order shows up there with its stop.
6. Only then consider raising size, one step at a time.

Prefer to be extra careful? Paper trade first (menu 6): real prices, simulated orders, no risk.
Start on a Combine or Practice account — never on an account you can't afford to lose — and
consider `max_contracts: 1` in `config.yaml` for the first week.

---

## 8. Training: let the bot find what works

Training is how the bot "learns" which strategy and settings to use. It is deliberately strict,
because tuning settings until a backtest looks perfect is the classic way to build a strategy
that loses money live: with enough combinations, something always looks great on the past by luck.

### How it works

1. Every candidate — a strategy plus one combination of its settings (about 140 in total) — is
   backtested over the whole history using the bot's real trading code, your risk settings and
   realistic fills and fees.
2. The history is split into rolling **windows**. In each one, the bot *chooses* the best settings
   for each strategy using only the **train** part, then *scores* them on the **test** part that
   comes right after — data the choice never saw. Then it rolls forward and repeats.
3. Only those out-of-sample (test) results count. A strategy is recommended only if it made money
   on them with a profit factor of at least 1.05 and enough trades to judge. The settings it
   recommends are the ones chosen on the most recent window.

One honest caveat: picking the best of several strategies by their out-of-sample results flatters
the winner a little, so expect live results to be somewhat below the report's numbers even when
nothing else changes.

### Run it

Menu **5**, or:

```bash
topstep-bot train
```

| Option | Meaning |
|---|---|
| `--days 730` | Download more history (default 365). More history gives more reliable results. |
| `--strategies orb_momentum,noise_breakout` | Train only these strategies. |
| `--folds 6` | Number of walk-forward windows (default 4). |
| `--data file.csv` | Use your own 1-minute data instead of downloading. |
| `--save` / `--no-save` | Save the recommendation to `config.yaml` without asking / never ask. |
| `--workers 2` | Parallel processes (default: number of CPUs − 1, max 4). |

It takes a few minutes. The result appears in the window and as a report in your browser:

- **Out-of-sample P&L by strategy** — one line per strategy over the test windows only.
- **Results on unseen data** — net P&L, trades, profit factor, win rate, average R, Sharpe,
  max drawdown, Combine pass rate and a verdict for each strategy, plus the settings it would
  trade now.
- **Settings chosen in each window** — if a strategy keeps choosing similar settings, its edge is
  more likely to be real.

Your current strategy is always included unchanged, so you can see how it compares.

When you save, only the `strategy:` block of `config.yaml` changes (a training note is added
above it); everything else, including your comments, stays as it was, and the previous file is
kept as `config.yaml.bak`. Re-train every month or two, and after big changes in the market.

> Training on random demo data (`--synthetic`) only shows how the process works; its result is
> never saved.

---

## 9. Backtesting

### Run one

Menu **4**, or:

```bash
topstep-bot backtest
```

Where the price history comes from, in order:

1. `--data yourfile.csv` if you give one.
2. `data/<SYMBOL>_1m.csv` if it exists (a previous download).
3. A fresh download from TopstepX if your API key is set up (90 days by default).
4. Otherwise synthetic random data, with a clear warning.

Useful options:

```bash
topstep-bot backtest --download --days 180
```

```bash
topstep-bot backtest --strategy noise_breakout
```

```bash
topstep-bot backtest --symbol MES --timeframe 15
```

`--strategy` swaps in another strategy with its default parameters; `--no-open` skips opening the
report. The first few trading days only warm up indicators and are not traded (2 days for most
strategies, 15 for `noise_breakout` and the ATR version of `orb_momentum`).

### Download history separately

```bash
topstep-bot download --days 365
```

This saves `data/MNQ_1m.csv` (1-minute bars). Backtests and training resample it to whatever
timeframe your strategy uses. How far back TopstepX lets you go can vary; if you get fewer days
than asked for, that is the available history.

### Use your own data

Any CSV with a time column (`timestamp`, `datetime`, `time` or `date`) and `open, high, low,
close` (plus optional `volume`). Each time must be the bar's **open** time. If the times have no
timezone, say which one with `--tz`:

```bash
topstep-bot backtest --data mydata.csv --tz America/Chicago
```

### Reading the report

The report opens in your browser and is saved in `reports/`.

- **Verdict banner** — would a Combine started on the first day have passed, failed, or not finished?
- **Cards** — net P&L (after fees and slippage), trade count, win rate, profit factor
  (gross wins ÷ gross losses; above 1.0 means profitable), average R (profit per trade measured in
  units of the risk taken), max drawdown, Sharpe ratio.
- **Combine pass rate** — the backtest's daily results are replayed through Topstep's Combine rules
  starting on *every* day in the test. "62% of 40 simulated starts" means 62% of those start dates
  would have reached the profit target (with the 50% consistency rule) before touching the Maximum
  Loss Limit. It is a rough guide, not a probability promise.
- **Balance vs. Maximum Loss Limit chart** — hover to see the balance, the trailing floor, and the
  room between them each day.
- **Daily P&L** — one bar per day.
- **How trades ended** — stop loss, profit target, strategy exit, session flatten, etc.
- **Trade list** — every trade with entry, exit, P&L, R multiple and reason.

How fills are simulated (deliberately pessimistic): within each bar, anything that trades at the
open fills first (market orders fill at the open plus 1 tick of slippage; entry limit orders fill
at the open only if the open is within their limit, otherwise they are cancelled); the stop an
entry creates is then checked against the rest of that same bar; stops fill at the stop plus
slippage (or at the open if the market gaps through); profit targets only fill if price trades
*through* them; and if a bar touches both the stop and the target, the stop is assumed to fill
first. Fees default to TopstepX's round-turn costs (about $1.22 for micros and $3.78 for minis);
change them with `risk.fees_per_contract_round_turn`.

---

## 10. Paper trading and the dashboard

Menu **6**, or:

```bash
topstep-bot run --mode paper
```

What happens:

1. Logs in, finds your account and the current front-month contract.
2. Downloads recent history so the strategy's indicators are warmed up.
3. Connects to TopstepX's live price stream.
4. After each bar closes it fetches that bar, runs the strategy, and simulates any orders using
   live bid/ask prices. **No orders are sent to TopstepX in paper mode.**
5. Opens the dashboard at **http://127.0.0.1:8765**.

The paper account's balance carries over between runs (stored in `data/journal_paper.db`), and so
do the day's trades after a restart. To start the paper account fresh, stop the bot and delete
that file.

### The dashboard

- **Header** — PAPER (blue) or LIVE (red) badge, connection status, a **log badge** (warnings and
  errors since start — click it for details), account, plan, contract and time (CT).
- **Status bar** — "Trading normally", "Paused", "Done for today: <reason>", or "Halted".
- **Balance / Today's P&L / Open P&L / Position.**
- **Guardrails:**
  - *Room above Max Loss Limit* — dollars between your equity and Topstep's MLL floor. Turns
    yellow below 50% and red below 25% of the MLL size.
  - *Daily loss limit used* — how much of your personal daily loss limit today has used.
  - *Combine profit target* — progress toward the target (Combine accounts).
  - *Trades today* — against your daily maximum.
- **Current trade** — side, size, entry, stop, target and the reason it was taken.
- **Strategy** — the levels the strategy is watching (opening range, bands, VWAP, ...).
- **Recommended trades right now** — live ideas from every strategy with **Take** buttons — see
  [section 19](#19-recommended-trades).
- **Recommendation history & results** — every recommendation today, what happened to it, and a
  per-strategy scoreboard.
- **Settings** — change risk, limits, times, the news pause and the auto-traded strategy — see
  [section 20](#20-changing-settings-from-the-dashboard-or-telegram).
- **Controls** — see [section 13](#13-stopping-the-bot-and-emergency-controls).
- **Activity** — everything the bot did, newest first.

The dashboard only accepts connections from your own computer.

---

## 11. Live trading

Before going live, in TopstepX:

- Make sure the account is active and allowed to trade.
- Keep TopstepX open so you can see orders appear.
- You do **not** need to enable "Auto OCO Brackets"; the bot manages its own stop and target.

The recommended way in is **menu 2 (Start trading today)**, which runs the preflight first. Menu
**7** starts live trading directly (after you type `LIVE`); `mode: live` in `config.yaml` does the
same for every run.

Live mode works like paper mode, except orders really go to your account, and the bot also:

- listens to TopstepX's account stream for order, fill and position updates;
- double-checks every 15 seconds that its view matches the account (a missing stop is re-placed,
  a stop that covers the wrong size is resized, and a position it didn't open is handled per
  `execution.orphan_position_policy` — by default it is **closed**, so **don't trade manually on
  the account the bot is using**);
- refreshes the balance every minute;
- enters with limit orders that fill immediately or are cancelled (never worse than
  `execution.max_entry_slippage_ticks` from the signal price), and exits at once if a fill leaves
  the trade much riskier than planned (`execution.max_risk_overrun`).

**Restarts during the day are safe.** On start-up the bot reads today's closed trades from
TopstepX and carries on where it left off: daily P&L, trade count, losing streak and post-loss
cooldown. If it finds a position protected by one of its own stops (for example after a crash), it
**re-adopts** it with that stop and target instead of closing it.

---

## 12. Running 24/7

For unattended running, use the **service** (menu 13, or `topstep-bot service`; `start.bat service`
works too). It runs the bot as a child process and:

- **restarts it after a crash**, waiting a little longer after each crash (10 s, 30 s, 1 min, ...),
  and gives up — with an alert — after `service.max_restarts_per_hour` crashes in an hour;
- **restarts it if it hangs**: the bot writes a heartbeat every 10 seconds, and if it goes quiet for
  `service.heartbeat_timeout_seconds` (default 3 minutes) the service restarts it. Protective stops
  stay at TopstepX meanwhile;
- **restarts it every day at 16:05 CT**, during the CME maintenance halt, so contract rolls, login
  tokens and connections are always fresh (only when flat; quietly — no "bot started" alert);
- **stops for good** when you stop the bot on purpose (Ctrl+C, the dashboard's Stop, Telegram `/stop`);
- sends Telegram/Discord alerts about crashes and restarts.

While the bot runs it also:

- **keeps the PC awake** (`service.keep_awake`) — the same request a video player makes; it ends
  when the bot exits. A closed laptop lid, the power button or a Windows Update restart still
  stop the PC: set Windows Update's active hours to cover the trading day;
- **turns off the console's QuickEdit mode** while running. With QuickEdit on (the Windows
  default), one click inside the bot's window freezes the whole program until you press a key;
- sends a **good-morning check-in** at 08:00 CT on weekdays (`service.check_in_time`) with the
  balance and MLL room, so silence tells you something is wrong.

To start the service automatically whenever you sign in to Windows, choose **autostart** (menu 14),
or run `topstep-bot autostart on`. It adds a small script to your personal Startup folder (no
administrator rights needed) that starts the service about 30 seconds after you sign in, minimized.
Check with `topstep-bot autostart status`; remove with `topstep-bot autostart off`.

---

## 13. Stopping the bot and emergency controls

| Want to... | Do this |
|---|---|
| Stop new trades but let the open trade finish | Dashboard → **Pause new trades** (then **Resume**), or Telegram `/pause`. You can still take recommended trades yourself while paused. |
| Close everything and stop trading | Dashboard → **Flatten & halt**, or Telegram `/flatten` |
| Shut the bot down | Dashboard → **Stop bot**, type `/stop` in Telegram, or press **Ctrl+C** in the bot window. This closes the program (and the 24/7 service) — it is *not* a pause. |
| Kill switch without the dashboard | Create an empty file named `KILL` in the bot folder. The bot flattens and halts within a second. Delete the file before the next start. |
| Panic button when the bot isn't running | Menu **8 (flatten)** — cancels every order and closes every position on the account |
| Last resort | Close the position in TopstepX yourself |

When the bot shuts down it flattens any open position first (`execution.flatten_on_shutdown`).
If your PC crashes or loses power, the **protective stop stays at TopstepX**, so the position is
still protected — and when the bot starts again it re-adopts the position. Check TopstepX as soon
as you can anyway.

If the bot ever stops itself because of an internal error, it flattens first, sends a ⛔ alert,
writes the details to `logs/errors.log`, and (under the service) is restarted automatically.

---

## 14. The strategies

List them any time with menu **10**. Change the strategy with `strategy.name` in `config.yaml` (or
let training do it) and override any parameter under `strategy.params`, e.g.

```yaml
strategy:
  name: noise_breakout
  params: {band_mult: 1.25}
```

### What 10½ years of real data says

Every strategy was tested on **real 1-minute Nasdaq-100 futures data from January 2015 to July
2025** (NQ prices, traded as MNQ), with the bot's own trading code, $150 risk per trade, the default
risk limits, TopstepX fees and 1 tick of slippage per fill. Then two harder tests:

- **Walk-forward (out-of-sample):** for each year 2018–2025, settings were chosen using only the
  three years before it, then traded on that year "blind" — exactly what `train` does.
- **Robustness:** the same settings on a different market (S&P 500 futures, Dec 2022–Jul 2024), on
  the most recent 60 days (Aug–Oct 2026), and with 2–3 ticks of slippage instead of 1.

| Strategy | Walk-forward 2018–2025 | Positive years | S&P futures | 3 ticks slippage | Verdict |
|---|---|---|---|---|---|
| `noise_breakout` (checkpoint exits) | +$9.6k, PF 1.19, max DD $2.5k | 5 of 8 | **profitable in both periods** | still profitable | **Most robust — the default** |
| `orb_momentum` | **+$50.4k**, PF 1.30, max DD $7.6k | **8 of 8** | lost money in 2023–24 | barely breakeven over 10 years | Strongest on Nasdaq, fragile elsewhere |
| `orb` | +$10.9k, PF 1.11 | 6 of 8 | lost money | lost money | Weak |
| `ema_trend` | +$5.8k, PF 1.06 | 5 of 8 | – | – | Weak |
| `late_day_momentum` | −$3.6k, PF 0.88 | 3 of 8 | – | – | Did not hold up |
| `vwap_reversion` | −$8.4k, PF 0.61 | 0 of 8 | – | – | Lost every year — avoid |

Over the full 2015–2025 period with fixed settings, `noise_breakout` (checkpoint exits) made
+$18.6k with a $4.6k maximum drawdown and was profitable in 9 of 11 years; the older per-bar
trailing exit made +$10.4k with nearly twice the drawdown. On the most recent year of data
(Jul 2024–Jul 2025) it made +$3,300 over 100 trades with a $550 maximum drawdown.

What this means for you:

- **No strategy is a sure thing.** Every strategy had losing years with fixed settings, and the past
  two months were mixed for all of them (too few trades to mean much either way).
- **`orb_momentum` lives on wide stops.** Its edge is real on Nasdaq futures since 2018, when its
  stops (10% of the day's average range) are well over 100 ticks wide; where they are only ~20 ticks
  (S&P futures, Nasdaq before 2018) costs eat the edge. Trade it on MNQ/NQ only, and check it with
  `risk.slippage_ticks: 2`.
- **Choosing a strategy because it looked best recently doesn't work well.** Picking whichever
  strategy had the best past three years chose `noise_breakout` every year and made +$9.6k; sticking
  with `orb_momentum` made five times more. That is why `train` judges strategies on data they
  never saw, and why you should re-train every month or two rather than chase last week's winner.
- These results were produced with real exchange data but simulated fills. Expect live results to
  be somewhat worse.


### `noise_breakout` — Intraday Momentum (Noise Area)

Adapted from Zarattini, Aziz & Barbon (2024), *"Beat the Market: An Effective Intraday Momentum
Strategy for S&P500 ETF (SPY)"*. For each time of day it averages how far price had moved from the
open over the last `lookback_days` (14) sessions; that average forms a "noise band" around today's
open. At each half-hour checkpoint, a close above the band goes long and below goes short. No fixed
target — winners run until the session flatten time. Needs about 15 trading days of history.

This is the default strategy. Two exit styles (`exit_mode`):

- `checkpoint` (default) — as in the paper: the trailing exit (the band or VWAP, whichever is
  tighter) is only judged at the half-hour checkpoints, while a wider safety stop (`stop_atr` × ATR)
  stays at the broker the whole time.
- `trail` — the broker stop itself trails the band/VWAP on every bar. Tighter, but it gets shaken
  out of good trades much more often (see the table above).

### `orb_momentum` — Opening Range Momentum (5-minute ORB)

Adapted from Zarattini & Aziz (2023), *"Can Day Trading Really Be Profitable? Evidence of Sustainable
Long-term Profits from Opening Range Breakout (ORB) Day Trading Strategy vs. Benchmark in the US
Stock Market"*, and its 2024 follow-up. If the first 5-minute candle after the 8:30 CT open closes up,
buy right away (short if it closes down; skip a doji). By default the stop is `atr_stop_frac` (10%)
× the average daily range of the last 14 sessions and the trade is held until the session flatten
time (`target_r: 0`), as in the 2024 follow-up. The 2023 paper's version — stop at the other end of
the candle, target 10× the risk — is `stop_mode: range, target_r: 10`. One trade a day. Best on
Nasdaq futures; see the table above before using it elsewhere.

### `late_day_momentum` — Late-Day Momentum (first & last half hour)

Adapted from Gao, Han, Li & Zhou (2018), *"Market intraday momentum"*, Journal of Financial
Economics. The market's move from the previous session's close to 9:00 CT (the end of the first
half hour) tends to continue in the last half hour of the day. At `entry_time` (14:25 CT) the bot
enters in the direction of that morning move — optionally only if the move since 14:00 agrees
(`confirm_with_12th`) and the morning move was at least `min_move_pct` — with a safety stop
`stop_atr` × ATR away, and exits at the session flatten time. One trade a day, late in the session.
It did **not** hold up on Nasdaq futures in testing — the effect was documented on the SPY ETF, and
the short holding time Topstep's 15:10 deadline allows leaves little room after costs. Included so
you can test it on your own data; don't trade it unless `train` says it held up.

### `orb` — Opening Range Breakout

Marks the high and low of the first `range_minutes` after the 8:30 CT open. The first bar that
closes above the range high (plus `buffer_ticks`) goes long; below the low goes short. Stop at the
middle of the range (`stop_mode: middle`), the other side (`opposite`), or `atr_stop_mult` × ATR
(`atr`). Target at `target_r` × the risk. One trade a day by default (`max_trades_per_day`), never
twice in the same direction; no entries after `entry_cutoff`. Skips days whose range is narrower
than `min_range_ticks` or wider than `max_range_ticks`.

### `ema_trend` — EMA Trend Crossover

Long when the 9-EMA crosses above the 21-EMA while price is above the 50-EMA (short is the mirror
image). Stop at `atr_stop_mult` × ATR, target at `target_r` × risk, exit on the opposite crossover.

### `vwap_reversion` — VWAP Mean Reversion

Buys when price closes more than `band_k` standard deviations below the session VWAP, RSI is below
`rsi_low`, and the bar closes up (a reversal bar); targets a return to VWAP with a stop just beyond
the bar's low. Shorts are the mirror image. It lost money in every year of testing — not recommended.

Every strategy shares the same position sizing, risk limits and session rules — a strategy only
decides *when* to trade and *where* the stop and target go.

---

## 15. Risk settings explained

### Position size

The bot works out how many contracts to trade so that hitting the stop loses about
`risk_per_trade` dollars, including fees and one tick of slippage.

> **Example:** MNQ ($0.50 per tick), stop 39 ticks away, `risk_per_trade: 150`.
> Cost per contract if stopped = (39 + 1 slippage) × $0.50 + $1.22 fees = $21.22.
> 150 ÷ 21.22 = 7.07 → **7 contracts**.

The size is then reduced if needed so that a full stop-out could not:

- push today's loss past `personal_daily_loss_limit`,
- bring equity within `mll_buffer` of Topstep's Maximum Loss Limit,
- exceed Topstep's contract cap (5/10/15 minis or 50/100/150 micros for 50K/100K/150K), or `max_contracts`.

If even 1 contract would risk too much, the trade is skipped and the dashboard says why.

During the [ramp-up](#7-the-path-to-live-trading) the per-trade risk is multiplied by
`ramp_up_risk_fraction` (default 0.5).

### Daily limits

| Setting | Default | What happens |
|---|---|---|
| `personal_daily_loss_limit` | 500 | Includes open P&L. Hitting it flattens everything and stops trading until the next session. |
| `daily_profit_target` | Combine: 40% of the profit target ($1,200 on 50K) | No new trades after reaching it. This protects Topstep's consistency rule (best day must be under 50% of total profit). |
| `max_trades_per_day` | 4 | No new trades after this many. |
| `max_consecutive_losses` | 2 | Done for the day after this many losses in a row. |
| `cooldown_minutes_after_loss` | 10 | Pause after each loss. |
| `mll_buffer` | 200 | No new trades when equity is within this distance of the MLL floor; flatten at half of it. |

All of these survive a restart during the day.

### Stops

| Setting | Default | Meaning |
|---|---|---|
| `min_stop_ticks` | 8 | Stops closer than this are widened (prevents noise stop-outs and absurd position sizes). |
| `min_stop_atr` | off | E.g. `0.5`: also widen stops to at least 0.5 × ATR (14 bars). The size shrinks to keep the dollar risk the same. |
| `max_stop_ticks` | 400 | Signals needing a wider stop are skipped. |
| `breakeven_at_r` | off | E.g. `1.0`: once the trade is 1R in profit, move the stop to entry + `breakeven_offset_ticks`. |
| `trail_atr_multiple` | off | E.g. `2.0`: trail the stop 2 × ATR behind price. |

The stop only ever moves in your favor.

### Sessions (Central Time)

| Setting | Default | Meaning |
|---|---|---|
| `trade_start` | 08:30 | No entries before this. |
| `last_entry` | 14:30 | No entries after this. |
| `flatten_at` | 15:00 | Everything is closed at this time. Must be 15:08 or earlier (Topstep requires flat by 15:10 CT and starts force-closing at 15:08). |
| `blackout_windows` | none | Extra no-entry windows of your own, e.g. around an FOMC press conference. |
| `no_trade_dates` | CME holidays & early closes 2026–2027 | No entries on these dates. Edit the list as needed. |

---

## 16. News blackouts

The bot downloads this week's economic calendar (the free Forex Factory feed) and blocks new
entries from 5 minutes before to 10 minutes after every **high-impact USD** release — CPI, jobs
reports, FOMC and so on. The dashboard and Telegram show the reason (`news blackout: USD CPI m/m at
07:30 CT`), and at start-up the bot lists the blackouts in the next 24 hours.

- The calendar is refreshed every 6 hours and cached in `data/news_cache.json`, so a brief outage
  doesn't matter. If it can't be loaded at all, the bot keeps trading without news blackouts and the
  preflight warns you — add `session.blackout_windows` for big releases by hand in that case.
- An open trade keeps its stop and target through the release. To close it beforehand instead,
  set `news.flatten_before: true`.
- Change the window with `news.minutes_before` / `news.minutes_after`; include medium-impact events
  with `news.impacts: [High, Medium]`; turn it off with `news.enabled: false`.

Backtests and training don't apply news blackouts (there is no historical calendar), so live
results around news days can differ a little from them.

---

## 17. Topstep rules: what the bot enforces and what is still on you

| Topstep rule | How the bot handles it |
|---|---|
| **Maximum Loss Limit** — trails your highest end-of-day balance, locks at the starting balance, enforced in real time including open P&L | Tracked from end-of-day balances. Every trade is sized so a stop-out stays above the floor plus `mll_buffer`; open positions are flattened if equity gets within half the buffer. |
| **Daily Loss Limit** — none on new TopstepX accounts | Not applied unless you set `account.topstep_daily_loss_limit` (the bot sets it automatically for accounts whose name contains "DLL"). Your `personal_daily_loss_limit` always applies. |
| **Flat by 15:10 CT** | Flattens at `flatten_at` (15:00 default). |
| **Contract caps** — 5/10/15 minis (micros count 1/10) | Enforced. Express accounts use a conservative Scaling Plan table — check your real limit in TopstepX Risk Settings and set `risk.max_contracts` if lower. |
| **Combine consistency** — best day under 50% of total profit | Default daily profit cap at 40% of the target; the backtest's pass simulation checks this rule. |
| **No VPS/VPN, no HFT** | Bars of 1 minute or more and a few trades a day — nowhere near HFT. **Running it on your own PC is your responsibility.** |
| **No hedging** | One position at a time on one contract. Don't run opposite strategies on several accounts. |

**Keep the bot's MLL in sync.** The API doesn't report the MLL floor, so the bot calculates it. The
preflight asks for it when it can't work it out. If the bot was off for a while or you traded
manually, copy the current Maximum Loss Limit from your Topstep dashboard into `config.yaml`:

```yaml
account:
  mll_floor_override: 48500
```

For an Express Funded Account after your first payout, Topstep sets the MLL to $0 — set
`mll_floor_override: 0`.

Topstep changes its rules from time to time. Check help.topstep.com and your dashboard, and adjust
the config if anything differs.

---

## 18. Telegram control and alerts on your phone

With Telegram set up, the bot **messages you** (entries, exits, risk events, daily summary,
check-ins, crashes and restarts) and **takes commands from you**: check status, pause, resume,
flatten or stop — from anywhere.

### Set it up (about 2 minutes)

Run the setup wizard (menu **1**) and answer **yes** to *"Use Telegram for alerts AND to control
the bot from your phone?"*. It walks you through:

1. In Telegram, open **@BotFather**, send `/newbot`, pick a name and a username ending in `bot`.
2. Paste the **token** BotFather gives you into the wizard (it's hidden as you type).
3. Open your new bot in Telegram and press **Start** (or send it anything), then press Enter in the
   wizard. It finds your chat ID automatically.

Then choose menu **12 (telegram-test)** — you should receive a test message with the control
buttons. (If you prefer, put `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` in `.env` by hand.)

Telegram control starts automatically every time you run the bot (paper or live). The bot window
shows `Telegram control: on`, and your phone gets *"Topstep Bot is online"*.

### Commands

Type them, pick them from Telegram's **/** menu, or tap the buttons under the bot's messages.

| Command | What it does |
|---|---|
| `/status` | Mode, account, status, balance, today's P&L, open position with stop/target, MLL room, daily loss used, trades today, Combine progress |
| `/pause` | Stop opening new trades. An open trade keeps its stop and target. |
| `/resume` | Allow new trades again (all risk limits still apply). Not possible after a flatten — restart the bot. |
| `/flatten` | Close any position, cancel orders and **halt** trading until the bot is restarted. Asks for confirmation. |
| `/stop` | **Shuts the program down** (it flattens first; the 24/7 service exits too). Asks for confirmation. It can only be restarted from your PC. There is deliberately no quick button for it — type it. Use `/pause` to just stop new trades. |
| `/ideas` | Recommended trades with **Take** and **½ size** buttons ([section 19](#19-recommended-trades)) |
| `/settings` | Every setting you can change, with current values and limits |
| `/set <name> <value>` | Change a setting, e.g. `/set risk 150`, `/set dailyloss 400`, `/set strategy orb_momentum`, `/set news off` ([section 20](#20-changing-settings-from-the-dashboard-or-telegram)) |
| `/reset` | Undo every remote setting change (back to `config.yaml`) |
| `/trades` | The last few closed trades |
| `/log` | Recent bot activity |
| `/help` | The command list |

> `/stop` really does shut the program down, also under the 24/7 service — which is why it has no
> quick button and asks you to confirm. To stop new trades only, use `/pause`.

### Security

- The bot only obeys **your chat** (`TELEGRAM_CHAT_ID`). Messages from anyone else who finds your
  bot are ignored and logged. To restrict a group chat to specific people, list their numeric
  Telegram user IDs in `config.yaml` under `telegram.allowed_user_ids`.
- `/flatten` and `/stop` need you to tap **Yes** within 60 seconds. Set
  `telegram.confirm_dangerous: false` to skip that (not recommended).
- Commands sent while the bot was off are **discarded** at startup, so an old `/flatten` can never
  fire by surprise later.
- Remote commands can't change risk settings — the most a command can do is let the bot keep
  trading within the limits in `config.yaml`.
- The bot checks Telegram *from your PC* (no open ports, no webhook), and orders are still placed
  by your PC, as Topstep requires.
- Keep the bot token secret. If it leaks, send `/revoke` to @BotFather and run setup again. The
  bot never writes the token (or a Discord webhook) into its log files.
- Don't run two copies of the bot with the same Telegram token — Telegram only lets one program
  read a bot's messages at a time.

To keep alerts but turn off remote control, set `telegram.control_enabled: false`.

### Discord (alerts only)

In a Discord server you own, open *Server Settings → Integrations → Webhooks → New Webhook*, copy
the URL, and either enter it in the setup wizard or add it to `.env`:

```
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...
```

Telegram and Discord alerts are sent independently: if one fails, the other still arrives.

### Which alerts are sent

Choose in `config.yaml` (default: all):

```yaml
notifications:
  events: [start, stop, entry, exit, risk, error, daily_summary]
```

---

## 19. Recommended trades

While the bot runs (paper or live), **every strategy** watches the market on every bar:

- Your **auto-traded strategy** (`strategy.name`, marked ★) trades by itself. Each of its signals is
  listed as *taken* or *skipped*, with the reason (outside trading hours, daily limit reached, stop
  too wide, …).
- The **other strategies** run in "shadow" mode. Their signals appear as **ideas** — the bot never
  trades them unless you press **Take**.

Every recommendation shows the entry, stop, target, size (worked out with *your* risk settings),
dollars at risk and reward-to-risk. It is then followed bar by bar to a result — stop hit, target
hit, strategy exit or session end — so the dashboard's **scoreboard** shows how each strategy's
ideas are really doing today. Results for trades that weren't actually placed are marked
hypothetical (*): if a bar touches both the stop and the target, the stop is assumed to come first.

**Taking an idea** (dashboard **Take** button, or Telegram `/ideas` → **Take** / **½ size**):

1. You confirm the trade.
2. The bot re-prices it at the current market and re-checks every risk rule. A requested size can
   be smaller than the recommendation but **never larger** than your risk rules allow.
3. It's placed and managed exactly like a bot trade: protective stop, target, breakeven/trailing
   settings, the idea's own strategy exit, and the session flatten.

Ideas can be taken for 15 minutes. Taking one works even while automatic entries are paused, but
not when a loss limit, the session window, a news blackout or the post-loss cooldown blocks
trading — then the button shows why instead. One position at a time still applies.

Every recommendation and its result is saved in the journal (`recommendations` table) and in
`logs/events.jsonl`. Telegram alerts for new ideas are off by default; add `idea` to
`notifications.events` to get them. Turn the feature off with `recommendations.enabled: false`, or
limit it to some strategies with `recommendations.strategies: [orb_momentum, noise_breakout]`.

---

## 20. Changing settings from the dashboard or Telegram

These settings can be changed while the bot runs — on the dashboard's **Settings** panel or with
`/set` in Telegram:

| Setting (`/set` name) | Range |
|---|---|
| Risk per trade (`risk`) | $10 to 15% of your Max Loss Limit |
| Daily loss limit (`dailyloss`) | $50 to half your MLL (and below any Topstep DLL) |
| Max trades per day (`maxtrades`), max losses in a row (`losses`) | 1–20, 1–10 |
| Max contracts (`contracts`, blank = Topstep's cap) | 1 to Topstep's cap |
| Daily profit target (`target`, blank = default) | up to the plan's profit target |
| Breakeven at R (`breakeven`), ATR trailing stop (`trail`) | blank = off |
| First / last entry time (`start`, `lastentry`) | must stay before the flatten time |
| News pause (`news`), close before news (`newsflatten`) | on / off |
| Max entry slippage (`slippage`) | 0–40 ticks |
| Auto-traded strategy (`strategy`) | any strategy; switches as soon as you're flat |

How it works:

- Every change shows old → new and asks you to confirm; changes that **increase risk** are flagged.
- It takes effect immediately and is recorded in the activity log, the log files, and as a
  Telegram/Discord alert.
- It is saved in `data/remote_settings.json` and re-applied when the bot restarts. **`config.yaml`
  is never rewritten.** "Undo all changes" (dashboard) or `/reset` (Telegram) goes back to
  `config.yaml`.
- Switching the auto-traded strategy reuses the already-warmed-up shadow copy, so it trades right
  away. If a trade is open, the switch waits until it closes.

What can **never** be changed remotely: the mode (paper/live), account, symbol and API
credentials, and Topstep's own rules (Maximum Loss Limit, contract caps, flat by 15:10 CT).

---

## 21. Daily routine

**Running 24/7 (recommended):** nothing to do before the open. Glance at the 08:00 CT check-in on
your phone; if it doesn't arrive, look at the PC.

**Starting by hand:** start the bot (menu 2, 6 or 7) before 8:15 CT. Check the dashboard shows
"connected", the right account and contract, and "Trading normally".

**During the session:** glance at the dashboard (or send `/status` on Telegram) and TopstepX now and
then. Act on any alert.

**After 15:10 CT:** confirm the position is flat. Review the day with menu **9 (journal)**.

**Weekly:** compare the bot's MLL floor with your Topstep dashboard.

**Every month or two:** re-run **train** (menu 5) on fresh data.

**Contract roll:** the bot picks the active front-month contract each time it starts; the 24/7
service restarts it daily, so rolls are picked up automatically. If you pinned
`instrument.contract_id`, update it.

---

## 22. Full configuration reference

`config.yaml` only needs the settings you want to change; everything else uses these defaults.
Misspelled settings are rejected with a clear message, so typos can't silently do nothing.

```yaml
mode: paper                    # paper | live
log_level: INFO                # what the console shows; files always get everything
log_dir: logs                  # relative folders are next to config.yaml
log_retention_days: 30
data_dir: data

account:
  plan: "50K"                  # 50K | 100K | 150K
  stage: combine               # combine | express | practice
  account_id: null             # which TopstepX account (set by setup)
  account_name: null           # alternative to account_id
  starting_balance: null       # default: plan size (combine/practice) or 0 (express)
  mll_floor_override: null     # sync with Topstep's dashboard
  topstep_daily_loss_limit: null

instrument:
  symbol: MNQ
  contract_id: null            # pin an exact contract, e.g. CON.F.US.MNQ.Z26
  timeframe_minutes: 5         # 1-60

strategy:
  name: noise_breakout         # noise_breakout | orb_momentum | orb | ema_trend | late_day_momentum | vwap_reversion
  params: {}

risk:
  risk_per_trade: 150
  max_contracts: null
  personal_daily_loss_limit: 500
  daily_profit_target: null    # combine default: 40% of the profit target
  max_trades_per_day: 4
  max_consecutive_losses: 2
  cooldown_minutes_after_loss: 10
  mll_buffer: 200
  min_stop_ticks: 8
  min_stop_atr: null           # e.g. 0.5 = stops at least half an ATR away
  max_stop_ticks: 400
  breakeven_at_r: null
  breakeven_offset_ticks: 1
  trail_atr_multiple: null
  fees_per_contract_round_turn: null   # default: built-in TopstepX estimate per symbol
  slippage_ticks: 1.0                  # used by paper trading and backtests
  ramp_up_days: 3                      # first N live trading days on an account...
  ramp_up_risk_fraction: 0.5           # ...risk this fraction of risk_per_trade

session:
  timezone: America/Chicago
  trade_start: "08:30"
  last_entry: "14:30"
  flatten_at: "15:00"
  trade_weekdays: [0, 1, 2, 3, 4]      # Monday=0
  blackout_windows: []                 # e.g. [{start: "13:00", end: "13:45", label: "FOMC"}]
  no_trade_dates: [...]                # CME holidays/early closes 2026-2027

execution:
  use_native_brackets: false   # true = TopstepX server-side brackets (needs "Auto OCO Brackets" enabled)
  orphan_position_policy: flatten   # flatten | adopt | ignore
  max_entry_slippage_ticks: 8  # entries never fill worse than this; null = plain market orders
  max_risk_overrun: 1.5        # exit at once if a fill makes the trade 1.5x riskier than planned
  reconcile_interval_seconds: 15
  entry_fill_timeout_seconds: 20
  flatten_on_shutdown: true

data:
  warmup_days: 20
  live_market_data: false      # false = the sim data feed used by Combine/Express accounts

recommendations:              # trade ideas from every strategy on the dashboard / Telegram
  enabled: true
  strategies: []               # empty = ideas from every strategy

news:
  enabled: true
  impacts: [High]
  currencies: [USD]
  minutes_before: 5
  minutes_after: 10
  flatten_before: false        # true = also close open trades just before a release

service:                       # used by 'topstep-bot service'
  keep_awake: true
  daily_restart_time: "16:05"  # CT, during the CME halt; off = never
  check_in_time: "08:00"       # weekday 'still running' message; off = never
  heartbeat_timeout_seconds: 180
  max_restarts_per_hour: 6

notifications:
  enabled: true
  events: [start, stop, entry, exit, risk, error, daily_summary]

telegram:
  control_enabled: true        # obey commands from TELEGRAM_CHAT_ID (needs TELEGRAM_BOT_TOKEN)
  confirm_dangerous: true      # /flatten and /stop need a confirmation tap
  allowed_user_ids: []         # optionally only these Telegram user IDs

dashboard:
  enabled: true
  host: 127.0.0.1
  port: 8765
  open_browser: true

backtest:
  data_file: null
  report_dir: reports          # backtest and training reports

api:                           # only change if Topstep changes its endpoints
  base_url: https://api.topstepx.com
  user_hub_url: https://rtc.topstepx.com/hubs/user
  market_hub_url: https://rtc.topstepx.com/hubs/market
  timeout_seconds: 15
```

`orphan_position_policy` decides what happens to a position the bot didn't open (e.g. a manual
trade): `flatten` closes it, `adopt` manages it with a protective stop, `ignore` leaves it alone.
Positions the bot opened itself (recognised by their tagged stop) are always re-adopted after a
restart.

---

## 23. Command reference

Run any command with `--help` for its options. Add `-c other.yaml` before the command to use a
different config file.

| Command | Purpose |
|---|---|
| `topstep-bot` | Interactive menu |
| `topstep-bot setup` | Setup wizard |
| `topstep-bot check` | Test login, list accounts, show the contract |
| `topstep-bot preflight [--days N] [--skip-backtest]` | Every check the bot needs before trading live |
| `topstep-bot go-live [--days N] [--skip-backtest]` | Preflight, then start live trading (24/7 or this session) |
| `topstep-bot train [--days N] [--strategies A,B] [--folds K] [--save\|--no-save] [--data F]` | Walk-forward training (see [section 8](#8-training-let-the-bot-find-what-works)) |
| `topstep-bot strategies` | Describe strategies and parameters |
| `topstep-bot backtest [--data F] [--download] [--days N] [--strategy S] [--symbol X] [--timeframe M] [--tz TZ] [--no-open]` | Backtest and open a report |
| `topstep-bot demo` | Backtest on synthetic data |
| `topstep-bot download [--days N] [--tf M]` | Save history to `data/` |
| `topstep-bot run [--mode paper\|live] [--yes] [--no-dashboard] [--no-browser]` | Start the bot |
| `topstep-bot service [--mode paper\|live] [--yes]` | Run 24/7 with auto-restart |
| `topstep-bot autostart on\|off\|status` | Start the service when you sign in to Windows |
| `topstep-bot flatten [--yes]` | Emergency: cancel all orders, close all positions |
| `topstep-bot journal [--mode paper\|live] [--limit N]` | Recent trades and daily results |
| `topstep-bot telegram-test` | Check the Telegram token and chat ID with a test message |
| `topstep-bot logs [--all] [--open] [--bundle]` | Recent errors, open the log folder, or zip logs for support |

On Windows you can also pass commands through the launcher, e.g. `start.bat train --days 730`.

---

## 24. Files the bot creates

| Path | Contents |
|---|---|
| `config.yaml` | Your settings (`config.yaml.bak`: the version before training last saved settings) |
| `.env` | Your API credentials, Telegram token and webhook URLs — **private** |
| `data/journal_paper.db`, `data/journal_live.db` | Trade journal, daily results, MLL floor, paper balance |
| `data/<SYMBOL>_1m.csv` | Downloaded history |
| `data/news_cache.json` | The economic calendar |
| `data/heartbeat` | The running bot's heartbeat (used by the 24/7 service) |
| `reports/*.html` | Backtest and training reports (training also writes a `.json` with every detail) |
| `logs/` | Log files — see [section 25](#25-logs-finding-out-what-happened) |
| `data/remote_settings.json` | Settings changed from the dashboard/Telegram (delete it, or `/reset`, to undo) |

---

## 25. Logs: finding out what happened

Everything is recorded in the `logs` folder next to `config.yaml`, wherever you start the bot from:

| File | What's in it |
|---|---|
| `bot.log` | Everything, one file per day (kept 30 days, `log_retention_days`) |
| `errors.log` | Only warnings and errors — **look here first** |
| `events.jsonl` | Every trade, risk event, setting change and recommendation, one JSON object per line |
| `service.log` | The 24/7 service: starts, restarts and why |
| `commands.log` | Other commands (setup, backtest, train, ...) — kept apart so they never collide with a running bot |
| `crash_*.txt` | Full details of any crash that closes the program |
| `faults.log` | Low-level hang/crash dumps from Python |

- When the bot stops, the log records **why**: Stop from Telegram/dashboard, Ctrl+C, daily
  maintenance restart, or a crash with the full error.
- Background parts (Telegram, price stream, clock) that fail are logged instead of dying silently.
- API keys, tokens and webhook URLs are automatically masked as `***` in every log file.
- The dashboard header shows a **log badge** with the number of warnings and errors since start;
  click it to see the latest ones.

Commands:

```bash
start.bat logs
```

Shows recent warnings, errors and any crash report (`--all` adds general activity).

```bash
start.bat logs --open
```

Opens the log folder.

```bash
start.bat logs --bundle
```

Creates a zip of the logs and `config.yaml` (never your `.env`) to share when asking for help.

---

## 26. Troubleshooting

**The bot closed / stopped unexpectedly** — Run `start.bat logs`. The last lines say why it
stopped ("Shutting down. Reason: …"): a Stop from Telegram or the dashboard, Ctrl+C, a daily
maintenance restart, an internal error, or a crash (with a `crash_*.txt` file). `/stop` in
Telegram really shuts the program down; use `/pause` to only stop new trades. To keep the window
open after it exits, start the bot from a Command Prompt: open the bot folder, click the address
bar, type `cmd`, press Enter, then run `start.bat`.

**The bot froze until I pressed a key** — Windows' console QuickEdit mode. The bot turns it off
while it runs; if it still happens (e.g. an old version), right-click the window's title bar →
Properties → untick *QuickEdit Mode*.

**"Login failed"** — Use your TopstepX *username*, not your email. Copy the API key again in full.
Check your API subscription is active. Re-run setup or edit `.env`.

**"Several accounts can trade"** — Run setup and pick one, or set `account.account_id`
(`topstep-bot check` lists the IDs).

**"No contract found for symbol"** — Check the symbol spelling, or pin `instrument.contract_id`.

**"Configuration problem"** — The message names the exact setting and what's wrong with it (e.g.
`risk.risk_per_trad: unknown setting`). Fix it in `config.yaml` with Notepad; indentation (spaces)
matters in YAML.

**The bot never trades.** Look at the dashboard's Activity list — skipped signals are logged with a
reason (outside the entry window, news blackout, daily limit, stop too wide, 1 contract too risky,
...). Some strategies trade rarely (`orb` and `orb_momentum` at most once a day; `noise_breakout`
needs ~15 days of history). Check today isn't in `no_trade_dates`.

**"Skipped: 1 contract would risk more than the allowed budget"** — The stop is too far for your
`risk_per_trade`. Raise the risk, trade a micro, or use a tighter stop setting.

**Training says no strategy held up** — That is a real answer: on that data, nothing beat the
costs on days it hadn't seen. Try more history (`--days 730`) or another symbol, and don't go live
on the current settings.

**Dashboard doesn't open** — Browse to http://127.0.0.1:8765 yourself. If the port is taken, change
`dashboard.port`.

**"disconnected" on the dashboard** — The bot reconnects automatically; while disconnected it
polls prices instead. If it persists, check your internet connection.

**A position the bot didn't open was closed** — That is `orphan_position_policy: flatten`. Don't
trade manually on the bot's account, or change the policy.

**The bot's MLL floor differs from Topstep's** — Set `account.mll_floor_override` to Topstep's value.

**"Native brackets rejected"** in the log — You set `use_native_brackets: true` without enabling
"Auto OCO Brackets" in TopstepX. The bot falls back to placing its own stop; set the option back
to `false` or enable the TopstepX setting.

**Python not found when double-clicking start.bat** — Install Python 3.11+ and tick "Add python.exe
to PATH", then run `start.bat` again.

**Telegram: no reply to commands** — Check the bot window says `Telegram control: on`. Run
`topstep-bot telegram-test`. Make sure you're messaging from the chat that was set up (a different
chat is ignored — `logs/bot.log` shows "Ignored Telegram command from unauthorized chat ..." with
its ID). Only one program can read a bot's messages: close any other copy of the bot.

**Telegram: "the bot token was rejected"** — The token is wrong or was revoked. Run setup again.

**The service keeps restarting the bot** — `logs/service.log` says why (crash, no heartbeat). After
`max_restarts_per_hour` crashes it gives up and alerts you; the details of each crash are in
`logs/errors.log` and `crash_*.txt` (`start.bat logs` shows them).

---

## 27. Writing your own strategy

Create a file in `src/topstep_bot/strategies/`, subclass `Strategy`, and register it in
`strategies/__init__.py`:

```python
from topstep_bot.indicators import EMA
from topstep_bot.models import Bar, Signal
from topstep_bot.strategies.base import Strategy, StrategyContext


class MyStrategy(Strategy):
    name = "my_strategy"
    title = "My Strategy"
    description = "One sentence shown in the menu."
    defaults = {"length": 20, "stop_ticks": 40, "target_r": 2.0}

    def setup(self) -> None:
        self.ema = EMA(self.p["length"])

    def on_bar(self, bar: Bar, ctx: StrategyContext) -> Signal | None:
        ema = self.ema.update(bar.close)
        if ema is None or not self.is_rth_bar(ctx):
            return None
        if ctx.position == 0 and bar.close > ema:
            stop = bar.close - self.tick(self.p["stop_ticks"])
            target = bar.close + self.p["target_r"] * (bar.close - stop)
            return Signal("long", stop, target, "close above EMA")
        if ctx.position > 0 and bar.close < ema:
            return Signal("exit", reason="close below EMA")
        return None
```

Rules of thumb:

- Return `Signal("long" | "short", stop_price, target_price, reason)` to enter and
  `Signal("exit", reason=...)` to close. Every entry needs a stop; the bot sizes from it.
- Don't size positions, check the clock against session limits, or track P&L in a strategy — the
  bot does that for every strategy.
- Use the streaming indicators in `topstep_bot/indicators.py` so backtests and live trading behave
  identically.
- Optional hooks: `on_new_day(day)` to reset daily state, `trailing_stop(bar, ctx)` to move the
  stop, `state()` to show values on the dashboard, `warmup_days` if it needs more history.
- To let training tune it, add a list of candidate settings for it to `GRIDS` in
  `topstep_bot/training.py` — keep it small (a dozen or two combinations).

Backtest it with `topstep-bot backtest --strategy my_strategy`, and run the test suite with
`pytest` after any change to the bot itself.

---

*Trading futures involves substantial risk of loss and is not suitable for everyone. This software
is provided as-is, with no guarantee of profit or of compliance with Topstep's rules, which can
change. You are responsible for every order placed on your account.*
