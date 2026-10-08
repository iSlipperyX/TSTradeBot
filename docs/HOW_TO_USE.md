# Topstep Bot — Complete How-To-Use Guide

This guide takes you from zero to a running bot, explains every screen and setting, and covers
what to do when something goes wrong. If you only read one section, read
[The safe path to live trading](#7-the-safe-path-to-live-trading).

---

## Contents

1. [What the bot does (and doesn't do)](#1-what-the-bot-does-and-doesnt-do)
2. [What you need](#2-what-you-need)
3. [Install](#3-install)
4. [Get your TopstepX API key](#4-get-your-topstepx-api-key)
5. [Run the setup wizard](#5-run-the-setup-wizard)
6. [The main menu](#6-the-main-menu)
7. [The safe path to live trading](#7-the-safe-path-to-live-trading)
8. [Backtesting](#8-backtesting)
9. [Paper trading and the dashboard](#9-paper-trading-and-the-dashboard)
10. [Live trading](#10-live-trading)
11. [Stopping the bot and emergency controls](#11-stopping-the-bot-and-emergency-controls)
12. [The strategies](#12-the-strategies)
13. [Risk settings explained](#13-risk-settings-explained)
14. [Topstep rules: what the bot enforces and what is still on you](#14-topstep-rules-what-the-bot-enforces-and-what-is-still-on-you)
15. [Telegram control and alerts on your phone](#15-telegram-control-and-alerts-on-your-phone)
16. [Recommended trades](#16-recommended-trades)
17. [Training and the knowledge base: how the bot learns](#17-training-and-the-knowledge-base-how-the-bot-learns)
18. [Changing settings from the dashboard or Telegram](#18-changing-settings-from-the-dashboard-or-telegram)
19. [Running 24/7](#19-running-247)
20. [Starting today: preflight, ramp-up and news](#20-starting-today-preflight-ramp-up-and-news)
21. [Logs: finding out what happened](#21-logs-finding-out-what-happened)
22. [Daily routine](#22-daily-routine)
23. [Full configuration reference](#23-full-configuration-reference)
24. [Command reference](#24-command-reference)
25. [Files the bot creates](#25-files-the-bot-creates)
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
- Show everything on a dashboard in your browser, keep a trade journal, and optionally send alerts.

**It doesn't:**

- Guarantee profits. Backtests are hypothetical; markets change. Independent research has found
  that many popular intraday strategies (including the classic opening-range breakout) have little
  or no edge on index futures after costs. Treat the included strategies as starting points.
- Run on a VPS or behind a VPN. Topstep's terms require automated orders to come from **your own
  computer**. Run the bot on your PC.
- Trade Live Funded accounts. Topstep only allows API trading on Combine, Express Funded and
  Practice accounts.
- Hold positions overnight. It is a day-trading bot.

---

## 2. What you need

| Item | Notes |
|---|---|
| Windows 10/11 PC (macOS/Linux also work) | Must stay on and awake while the bot trades. Disable sleep in Windows power settings. |
| Python 3.11 or newer | [python.org/downloads](https://www.python.org/downloads/). During install, tick **"Add python.exe to PATH"**. |
| A Topstep account | Trading Combine, Express Funded Account, or Practice account on **TopstepX**. |
| TopstepX API access | A separate subscription (about $29/month; Topstep traders get 50% off with code `topstep`). Not needed for backtesting on demo data. |
| A stable internet connection | The bot reconnects automatically after drops, but frequent outages are risky. |

---

## 3. Install

### Easiest (Windows)

1. Open the `TOPSTEP TRADING BOT` folder.
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
2. **Which account** the bot should use (if you have several).
3. **Account size** — 50K, 100K or 150K. This sets the Maximum Loss Limit, profit target and
   contract cap the bot works with.
4. **Account type** — `combine` (evaluation), `express` (Express Funded Account) or `practice`.
5. **What to trade.** Start with a **micro** contract (MNQ, MES, M2K, MYM, MGC, MCL). Micros are
   1/10th the size of the minis, so mistakes cost 10× less.
6. **Strategy.** See [section 12](#12-the-strategies).
7. **Risk.** Dollars to risk per trade and your personal daily loss limit. The wizard suggests
   7.5% and 25% of your Maximum Loss Limit (for a 50K account: $150 per trade, $500 per day).
8. **Telegram and alerts** (optional) — control the bot from your phone and get alerts; see
   [section 15](#15-telegram-control-and-alerts-on-your-phone).

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
| 2 | go-live | **Start trading today:** runs every preflight check, then starts live ([section 20](#20-starting-today-preflight-ramp-up-and-news)) |
| 3 | check | Tests the connection, shows accounts and the contract |
| 4 | backtest | Tests the strategy on history and opens a report |
| 5 | train | **Teaches the bot** which strategy works at which time of day, from recent real data ([section 17](#17-training-and-the-knowledge-base-how-the-bot-learns)) |
| 6 | paper | **Starts the bot in PAPER mode** (simulated orders on real prices) with the dashboard and Telegram |
| 7 | live | **Starts the bot in LIVE mode** (real orders) with the dashboard and Telegram |
| 8 | flatten | **Emergency:** cancels all orders and closes all positions on the account |
| 9 | journal | Recent trades and daily results |
| 10 | strategies | Describes each strategy and its parameters |
| 11 | demo | Backtest on synthetic data, no account needed |
| 12 | telegram-test | Sends a test message with control buttons to your Telegram |
| 13 | autostart | Starts everything automatically when you sign in to Windows ([section 19](#19-running-247)) |
| 14 | logs | Shows recent errors and where the log files are ([section 21](#21-logs-finding-out-what-happened)) |

You can switch between paper and live later from the dashboard, without coming back to this menu.

---

## 7. The safe path to live trading

Follow these steps in order. Don't skip ahead.

1. **Train the bot** (menu 5). It replays the last 60 days of real data through every strategy and
   learns which ones work at which time of day ([section 17](#17-training-and-the-knowledge-base-how-the-bot-learns)).
   Read the table it prints: if everything is ✘, the market has not been kind to these strategies
   lately and the adaptive strategy will (rightly) sit out.
2. **Backtest on real data** (menu 4). Look at the report: is the result positive *after fees*,
   is the drawdown survivable, does the Combine pass rate look reasonable? Try a few settings, but
   be wary of tuning until the past looks perfect — that rarely carries into the future.
3. **Paper trade for at least 2–4 weeks** (menu 6, or the Paper switch on the dashboard). Paper mode uses real live prices but never
   sends an order. Leave it running during market hours with the dashboard open. Compare what it
   did with what the backtest says it should have done.
4. **Go live on a Combine or Practice account** — never on an account you can't afford to lose.
   Start with **1 micro contract**: set `max_contracts: 1` in `config.yaml`.
5. **Watch the first live days closely.** Keep TopstepX open alongside the dashboard and confirm
   every order the bot places shows up there with its stop.
6. Only then consider raising size, one step at a time.

---

## 8. Backtesting

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
report. The first 20 trading days of data (`data.warmup_days`) only warm up indicators and are
not traded, so download more days than you want to test.

### Download history separately

```bash
topstep-bot download --days 180
```

This saves `data/MNQ_1m.csv` (1-minute bars). Backtests resample it to whatever timeframe your
strategy uses. How far back TopstepX lets you go can vary; if you get fewer days than asked for,
that is the available history.

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
  room between them each day. Watch how close the blue line comes to the red dashed one.
- **Daily P&L** — one bar per day.
- **How trades ended** — stop loss, profit target, strategy exit, session flatten, etc.
- **Trade list** — every trade with entry, exit, P&L, R multiple and reason.

How fills are simulated (deliberately pessimistic): market orders fill at the next bar's open
plus 1 tick of slippage; stops fill at the stop plus slippage (or at the open if the market gaps
through); profit targets only fill if price trades *through* them; and if a bar touches both the
stop and the target, the stop is assumed to fill first. Fees default to TopstepX's round-turn
costs (about $1.22 for micros and $3.78 for minis); change them with
`risk.fees_per_contract_round_turn`.

---

## 9. Paper trading and the dashboard

Menu **5**, or:

```bash
topstep-bot start --mode paper
```

This starts two separate programs:

- the **controller** — the dashboard at **http://127.0.0.1:8765** and Telegram control;
- the **trading bot**, which the controller starts, watches and restarts.

Because they're separate, the dashboard and Telegram **stay online when the bot stops or crashes**:
you can see why it stopped (and its logs) and start or restart it from either one. Closing the
program window (or Ctrl+C in it) stops both.

What the bot does:

1. Logs in, finds your account and the current front-month contract.
2. Downloads recent history so the strategy's indicators are warmed up.
3. Connects to TopstepX's live price stream.
4. After each bar closes it fetches that bar, runs the strategy, and simulates any orders using
   live bid/ask prices. **No orders are sent to TopstepX in paper mode.**
5. Reports to the dashboard, which opens in your browser.

The paper account's balance carries over between runs (stored in `data/journal_paper.db`).
To start the paper account fresh, stop the bot and delete that file.

### The dashboard

**Top bar** (always visible):

- **Paper | Live** switch. Switching to Live asks you to type `LIVE`. It's refused while a trade is
  open, so flatten first. The bot restarts in the new mode, and the choice is remembered for next time.
- **Bot status:** Running, Starting…, Stopped, Crashed or Failed to start, plus uptime.
- **Start, Restart and Stop** for the bot process. Stop closes any position first; the dashboard stays up.
- **Log badge:** warnings and errors so far. Click it for the Logs tab.
- When the bot isn't running, a **banner** shows why (e.g. "stop requested from Telegram",
  "crashed: …", "could not start: …") with **Start bot** and **See logs** buttons.

**Overview tab**

- Balance, today's P&L, open P&L and position.
- **Guardrails:**
  - *Room above Max Loss Limit:* turns yellow below 50% and red below 25% of the MLL size.
  - *Daily loss limit used* and *Combine profit target* progress.
  - *Trades today*.
  - **Pause / Resume / Flatten & halt** buttons.
- Current trade and strategy levels, the time-of-day slot and volatility regime, and — with the
  adaptive strategy — which sub-strategy is managing the trade and the last signal it took or skipped.
- **Activity:** everything the bot did, newest first.

**Ideas tab:** recommended trades with **Take** buttons and today's results per strategy — see
[section 16](#16-recommended-trades). A badge on the tab shows how many ideas are live.

**Knowledge tab:** what the bot has learned — for every strategy, time of day and regime, the average
result per signal and whether the adaptive strategy trades it right now — with a **Retrain now** button.
See [section 17](#17-training-and-the-knowledge-base-how-the-bot-learns).

**Settings tab:** change risk, limits, times, the news pause and the auto-traded strategy — see
[section 18](#18-changing-settings-from-the-dashboard-or-telegram).

**Logs tab:** works even when the bot is down.

- Bot process events: starts, stops, crashes, restarts, mode switches.
- Recent warnings and errors.
- The latest crash report, if any.
- The end of the bot's log.

The dashboard only accepts connections from your own computer.

---

## 10. Live trading

Before going live, in TopstepX:

- Make sure the account is active and allowed to trade.
- Keep TopstepX open so you can see orders appear.
- You do **not** need to enable "Auto OCO Brackets"; the bot manages its own stop and target.

Then switch the dashboard's **Paper | Live** toggle to Live (type `LIVE` to confirm), or start with
menu **7** — which shows a red warning and asks you to type `LIVE` too.

Live mode works like paper mode, except orders really go to your account, and the bot also:

- listens to TopstepX's account stream for order, fill and position updates;
- double-checks every 15 seconds that its view matches the account (a missing stop is re-placed;
  a position it didn't open is handled per `execution.orphan_position_policy` — by default it is
  **closed**, so **don't trade manually on the account the bot is using**);
- refreshes the balance every minute.

If you restart the bot during the day, it reads today's results from TopstepX and continues where
it left off (trade count, daily P&L, loss limits).

---

## 11. Stopping the bot and emergency controls

| Want to... | Do this |
|---|---|
| Stop new trades but let the open trade finish | **Pause new trades** (Overview tab) or Telegram `/pause`; then **Resume**. You can still take ideas yourself while paused. |
| Close everything and stop trading | **Flatten & halt** or Telegram `/flatten`. Trading stays halted until the bot is restarted. |
| Stop the bot (dashboard and Telegram stay online) | **Stop** in the top bar or Telegram `/stop`. Start it again with **Start** or `/startbot`. |
| Restart the bot | **Restart** or Telegram `/restart` — it flattens first. |
| Close everything, including the dashboard | Close the Topstep Bot window, or press **Ctrl+C** in it. |
| Kill switch without the dashboard | Create an empty file named `KILL` in the bot folder. The bot flattens and halts within a second. Delete the file before the next start. |
| Panic button when nothing is running | Menu **7 (flatten)** — cancels every order and closes every position on the account. |
| Last resort | Close the position in TopstepX yourself. |

Whenever the bot stops, it flattens any open position first (`execution.flatten_on_shutdown`).
If your PC crashes or loses power, the **protective stop stays at TopstepX**, so the position is
still protected — but check TopstepX as soon as you can.

---

## 12. The strategies

List them any time with menu **10**. Change the strategy with `strategy.name` in `config.yaml` and
override any parameter under `strategy.params`, e.g.

```yaml
strategy:
  name: orb
  params: {range_minutes: 30, target_r: 1.5}
```

### `adaptive` — Adaptive All-Day (default)

Runs **all the other strategies at once, through the whole session**, and takes a signal only when
the bot's knowledge base shows that strategy has been working **at this time of day** (open 08:30–10:00,
midday 10:00–13:00, close 13:00–15:10 CT) **in the current volatility regime** (calm or volatile).
When several strategies signal on the same bar, the one with the best evidence wins; the strategy that
opened the trade manages it (its exits and trailing stop). Everything the sub-strategies signal —
traded or not — is followed to its outcome and fed back into the knowledge base, so the bot keeps
improving while it runs, and it retrains on recent history every day.
It trades **nothing** until it has evidence: run **train** (menu 5) first. See [section 17](#17-training-and-the-knowledge-base-how-the-bot-learns).
Parameters: `strategies` (list; empty = all), `trade_unproven` (`true` = also trade strategies the base
knows nothing about yet — not recommended).

### `orb` — Opening Range Breakout

Marks the high and low of the first `range_minutes` after the 8:30 CT open. The first bar that
closes above the range high (plus `buffer_ticks`) goes long; below the low goes short. Stop at the
middle of the range (`stop_mode: middle`), the other side (`opposite`), or `atr_stop_mult` × ATR
(`atr`). Target at `target_r` × the risk. At most one trade per direction per day; no entries after
`entry_cutoff`. Skips days whose range is narrower than `min_range_ticks` or wider than
`max_range_ticks`.

### `noise_breakout` — Intraday Momentum (Noise Area)

Adapted from Zarattini, Aziz & Barbon (2024), *"Beat the Market: An Effective Intraday Momentum
Strategy for S&P500 ETF (SPY)"*. For each time of day it averages how far price had moved from the
open over the last `lookback_days` (14) sessions; that average forms a "noise band" around today's
open. At each half-hour checkpoint, a close above the band goes long and below goes short. The stop
trails at the band or VWAP (whichever is tighter), so winners can run until the end of the session.
No fixed target. Needs about 15 trading days of history before it trades.

### `ema_trend` — EMA Trend Crossover

Long when the 9-EMA crosses above the 21-EMA while price is above the 50-EMA (short is the mirror
image). Stop at `atr_stop_mult` × ATR, target at `target_r` × risk, exit on the opposite crossover.

### `vwap_reversion` — VWAP Mean Reversion

Buys when price closes more than `band_k` standard deviations below the session VWAP, RSI is below
`rsi_low`, and the bar closes up (a reversal bar); targets a return to VWAP with a stop just beyond
the bar's low. Shorts are the mirror image. Works in choppy markets, loses in strong trends.

### `vwap_pullback` — VWAP Trend Pullback

Built for the middle and the end of the day. When price is trending (above VWAP with a rising
`trend_ema`, measured over `slope_bars`), it waits for a pullback into the VWAP band (`band_k`) and buys
the first bar that closes back above the previous bar's high. Stop `stop_atr_mult` × ATR under the
pullback low, target `target_r` × risk, exit on a close through VWAP. Shorts are the mirror image.
No entries before `min_minutes_after_open` or after `entry_cutoff`; at most `max_trades_per_day`.

Every strategy shares the same position sizing, risk limits and session rules — a strategy only
decides *when* to trade and *where* the stop and target go.

---

## 13. Risk settings explained

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

### Daily limits

| Setting | Default | What happens |
|---|---|---|
| `personal_daily_loss_limit` | 500 | Includes open P&L. Hitting it flattens everything and stops trading until the next session. |
| `daily_profit_target` | Combine: 40% of the profit target ($1,200 on 50K) | No new trades after reaching it. This protects Topstep's consistency rule (best day must be under 50% of total profit). |
| `max_trades_per_day` | 4 | No new trades after this many. |
| `max_consecutive_losses` | 2 | Done for the day after this many losses in a row. |
| `cooldown_minutes_after_loss` | 10 | Pause after each loss. |
| `mll_buffer` | 200 | No new trades when equity is within this distance of the MLL floor; flatten at half of it. |

### Stops

| Setting | Default | Meaning |
|---|---|---|
| `min_stop_ticks` | 8 | Stops closer than this are widened (prevents noise stop-outs and absurd position sizes). |
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
| `blackout_windows` | none | No entries in these windows, e.g. around CPI or FOMC releases. |
| `no_trade_dates` | CME holidays & early closes 2026–2027 | No entries on these dates. Edit the list as needed. |

Example blackout for a CPI morning (8:30 ET = 7:30 CT):

```yaml
session:
  blackout_windows:
    - {start: "07:25", end: "07:45", label: "CPI"}
```

---

## 14. Topstep rules: what the bot enforces and what is still on you

| Topstep rule | How the bot handles it |
|---|---|
| **Maximum Loss Limit** — trails your highest end-of-day balance, locks at the starting balance, enforced in real time including open P&L | Tracked from end-of-day balances. Every trade is sized so a stop-out stays above the floor plus `mll_buffer`; open positions are flattened if equity gets within half the buffer. |
| **Daily Loss Limit** — none on new TopstepX accounts | Not applied unless you set `account.topstep_daily_loss_limit`. Your `personal_daily_loss_limit` always applies. |
| **Flat by 15:10 CT** | Flattens at `flatten_at` (15:00 default). |
| **Contract caps** — 5/10/15 minis (micros count 1/10) | Enforced. Express accounts use a conservative Scaling Plan table — check your real limit in TopstepX Risk Settings and set `risk.max_contracts` if lower. |
| **Combine consistency** — best day under 50% of total profit | Default daily profit cap at 40% of the target; the backtest's pass simulation checks this rule. |
| **No VPS/VPN, no HFT** | Bars of 1 minute or more and a few trades a day — nowhere near HFT. **Running it on your own PC is your responsibility.** |
| **No hedging** | One position at a time on one contract. Don't run opposite strategies on several accounts. |

**Keep the bot's MLL in sync.** The API doesn't report the MLL floor, so the bot calculates it.
If the bot was off for a while or you traded manually, copy the current Maximum Loss Limit from
your Topstep dashboard into `config.yaml`:

```yaml
account:
  mll_floor_override: 48500
```

For an Express Funded Account after your first payout, Topstep sets the MLL to $0 — set
`mll_floor_override: 0`.

Topstep changes its rules from time to time. Check help.topstep.com and your dashboard, and adjust
the config if anything differs.

---

## 15. Telegram control and alerts on your phone

With Telegram set up, the bot **messages you** (entries, exits, risk events, daily summary) and
**takes commands from you**: check status, pause, resume, flatten or stop — from anywhere.

### Set it up (about 2 minutes)

Run the setup wizard (menu **1**) and answer **yes** to *"Use Telegram for alerts AND to control
the bot from your phone?"*. It walks you through:

1. In Telegram, open **@BotFather**, send `/newbot`, pick a name and a username ending in `bot`.
2. Paste the **token** BotFather gives you into the wizard (it's hidden as you type).
3. Open your new bot in Telegram and press **Start** (or send it anything), then press Enter in the
   wizard. It finds your chat ID automatically.

Then choose menu **12 (telegram-test)** — you should receive a test message with the control
buttons. (If you prefer, put `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` in `.env` by hand.)

Telegram control starts automatically with the controller, and **keeps working while the bot is
stopped or has crashed**: `/status` then tells you why it stopped, and `/startbot` or `/restart`
brings it back. The bot window
shows `Telegram control: on`, and your phone gets *"Topstep Bot is online"*.

### Commands

Type them, pick them from Telegram's **/** menu, or tap the buttons under the bot's messages.

| Command | What it does |
|---|---|
| `/status` | Mode, account, status, balance, today's P&L, open position with stop/target, MLL room, daily loss used, trades today, Combine progress |
| `/pause` | Stop opening new trades. An open trade keeps its stop and target. |
| `/resume` | Allow new trades again (all risk limits still apply). Not possible after a flatten — restart the bot. |
| `/flatten` | Close any position, cancel orders and **halt** trading until the bot is restarted. Asks for confirmation. |
| `/stop` | Stops the bot (it flattens first). Telegram and the dashboard stay online, so `/startbot` brings it back. Asks for confirmation. Use `/pause` to just stop new trades. |
| `/restart` | Restarts the bot (flattens first) — e.g. after an error. Asks for confirmation. |
| `/startbot` | Starts the bot if it's stopped or crashed. Asks for confirmation. |
| `/ideas` | Recommended trades with **Take** and **½ size** buttons ([section 16](#16-recommended-trades)) |
| `/knowledge` | What the bot has learned: per strategy and time of day, ✅ trades now / ❌ switched off / ❔ unproven ([section 17](#17-training-and-the-knowledge-base-how-the-bot-learns)) |
| `/train` | Retrain the knowledge base on recent history now (the bot keeps trading meanwhile) |
| `/settings` | Every setting you can change, with current values and limits |
| `/set <name> <value>` | Change a setting, e.g. `/set risk 150`, `/set dailyloss 400`, `/set strategy orb`, `/set news off` ([section 18](#18-changing-settings-from-the-dashboard-or-telegram)) |
| `/reset` | Undo every remote setting change (back to `config.yaml`) |
| `/trades` | The last few closed trades |
| `/log` | Recent bot activity |
| `/help` | The command list |

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
- Keep the bot token secret. If it leaks, send `/revoke` to @BotFather and run setup again.
- Don't run two copies of the bot with the same Telegram token — Telegram only lets one program
  read a bot's messages at a time.

To keep alerts but turn off remote control, set `telegram.control_enabled: false`.

### Discord (alerts only)

In a Discord server you own, open *Server Settings → Integrations → Webhooks → New Webhook*, copy
the URL, and either enter it in the setup wizard or add it to `.env`:

```
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...
```

### Which alerts are sent

Choose in `config.yaml` (default: all):

```yaml
notifications:
  events: [start, stop, entry, exit, risk, error, daily_summary]
```

---

## 16. Recommended trades

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
`logs/events.jsonl`, and every result is also handed to the **knowledge base** — this is how the bot
learns ([section 17](#17-training-and-the-knowledge-base-how-the-bot-learns)). Telegram alerts for new ideas are off by default; add `idea` to
`notifications.events` to get them. Turn the feature off with `recommendations.enabled: false`, or
limit it to some strategies with `recommendations.strategies: [orb, ema_trend]`.

---

## 17. Training and the knowledge base: how the bot learns

The bot keeps a **knowledge base**: for every strategy, what its signals have been worth, split by
**time of day** (open 08:30–10:00, midday 10:00–13:00, close 13:00–15:10 Chicago time) and by
**volatility regime** (*calm* or *volatile*: whether the 14-bar range during regular hours is at least
20% above its multi-day average). Each entry is one *observation*: a signal followed to its outcome,
measured in **R** (1R = the amount that trade risked).

Observations come from three places and are kept apart:

| Source | What it is | When |
|---|---|---|
| **training** | Every strategy replayed over the last `knowledge.history_days` (60) days of real data — exactly the way the running bot follows ideas, so it measures the same thing | Menu **5 (train)**, `topstep-bot train`, the dashboard's **Retrain now**, Telegram `/train`, and automatically at startup when the last training is older than `retrain_hours` (20) — so normally once a day after the 16:05 CT restart |
| **live ideas** | Hypothetical outcomes of signals the bot saw while running but did not trade (shadow strategies, skipped signals) | Continuously while the bot runs |
| **real trades** | The bot's own closed trades (count double) | Continuously while the bot runs |

Because every signal is followed whether it was traded or not, **the bot never has to try a bad idea
to learn it is bad.** Older observations fade out (half their weight after `half_life_days`, 20), so
the base follows the market as it changes. Retraining replaces the training layer and drops live ideas
the new training already covers, so nothing is counted twice; real trades are never dropped.

### How the adaptive strategy uses it

Before taking a signal from strategy *S* at slot *T* in regime *V*, the adaptive strategy looks for the
most specific evidence with enough samples (`min_samples`, 8 weighted observations): *S* at *T* in *V*;
otherwise *S* at *T* in any regime; otherwise *S* at any time. The strategy may trade if that evidence's
expectancy — shrunk toward zero while the sample is small — is at least `min_edge_r` (0.05R). A
strategy that has been losing at that time of day is **switched off there** even if it does well
elsewhere; an **unproven** one is not traded at all (unless you set `trade_unproven: true`).

Everything is visible:

- **Dashboard → Knowledge tab:** the full table (✔ trades now, ✘ switched off, ? unproven), counts
  by source, when it was last trained, the current slot and regime, and **Retrain now**.
- **Telegram:** `/knowledge` for the same table in text, `/train` to retrain.
- **`topstep-bot train`** prints the table after training; `bot.log` records every signal the adaptive
  strategy took or skipped and why.
- The Overview's Strategy panel shows the slot, the regime, the sub-strategy managing an open trade,
  and the last signal decision.

### Honest expectations

Training on the last two months of M2K showed most strategies **losing** in most slots, with a thin
positive edge only for the EMA trend strategy at midday. That is a feature, not a bug: the adaptive
strategy then trades little, and only where there is evidence. Evidence from 60 days is still
statistical noise to a large degree; the knowledge base reduces the damage from a strategy that has
stopped working, it does not guarantee profits. Backtests of the adaptive strategy are **walk-forward**:
it starts knowing nothing and learns as the data plays (no peeking), so the first weeks of a backtest
show few or no trades.

Settings (`knowledge:` in `config.yaml`): `enabled`, `auto_train`, `history_days`, `retrain_hours`,
`half_life_days`, `min_samples`, `min_edge_r`, `real_trade_weight`. The file is
`data/knowledge_<SYMBOL>_<TF>m.json`, shared by paper and live; delete it to start from scratch.

---

## 18. Changing settings from the dashboard or Telegram

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

## 19. Running 24/7

Topstep requires automated trading to run **on your own computer** — not a VPS. Starting the bot
(menu 6/7, or `start.bat start`) already runs it around the clock. The controller:

- restarts the bot if it crashes (waiting longer after repeated crashes, and giving up — with an
  alert — after `service.max_restarts_per_hour`) or if it stops responding;
- restarts it every day at 16:05 CT, during the CME maintenance break, which picks up contract rolls
  and fresh connections;
- keeps Windows from sleeping while it runs;
- leaves an open trade protected through a crash: the protective stop stays at TopstepX, and the
  restarted bot re-adopts the position;
- sends a "good morning" check-in on weekdays at 08:00 CT;
- leaves a bot you stopped on purpose stopped until you start it again — from the dashboard or
  Telegram, even remotely.

To start everything automatically whenever you sign in to Windows:

```bash
start.bat autostart on
```

It starts in the mode you used last (Paper or Live). Turn it off with `start.bat autostart off`.
Also set your PC to **never sleep** while plugged in, and set Windows Update *active hours* to cover
the trading day.

---

## 20. Starting today: preflight, ramp-up and news

Menu **2 (go-live)** is the same-day start. It runs the **preflight check**, which you can also run
alone with `start.bat preflight`. The check verifies:

- your login, the selected account and that it is allowed to trade;
- that the account is flat;
- the Maximum Loss Limit — it asks you for the value on your Topstep dashboard if it can't work it out;
- any Topstep Daily Loss Limit on the account;
- the contract, live market data and your PC clock;
- today's trading calendar and upcoming news;
- your risk settings and Telegram;
- a backtest of every strategy on the most recent real data.

If nothing fails, you type `LIVE`, then choose 24/7 or a single session.

Built-in protection for a new account:

- **Ramp-up:** the first 3 live trading days on an account risk 50% of your normal amount
  (`risk.ramp_up_days`, `risk.ramp_up_risk_fraction`).
- **News pause:** no new trades from 5 minutes before to 10 minutes after high-impact US economic
  releases, using this week's economic calendar (`news.*`). `news.flatten_before: true` also closes
  open trades before a release.
- **Price-capped entries:** entries can't fill more than `execution.max_entry_slippage_ticks`
  (default 8) worse than the signal price, and trades are sized for that worst case. If a fill still
  leaves a trade too risky, it is closed immediately.

---

## 21. Logs: finding out what happened

Everything is recorded in the `logs` folder next to `config.yaml`, wherever you start the bot from:

| File | What's in it |
|---|---|
| `bot.log` | Everything, one file per day (kept 30 days, `log_retention_days`) |
| `errors.log` | Only warnings and errors — **look here first** |
| `events.jsonl` | Every trade, risk event, setting change and recommendation, one JSON object per line |
| `controller.log` | The controller: bot starts, stops, crashes, restarts, mode switches, Telegram |
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

## 22. Daily routine

**Before the open (by ~8:15 CT):**

1. Check the economic calendar; add `blackout_windows` for big releases if you want.
2. Confirm your PC won't sleep or install updates during the session.
3. Start the bot (menu 6 or 7, or let autostart do it). Check the dashboard shows "connected", the right account and
   contract, and "Trading normally".

**During the session:** glance at the dashboard (or send `/status` on Telegram) and TopstepX now and
then. Act on any alert.

**After 15:10 CT:** confirm the position is flat. You can leave the bot running overnight (it
won't trade outside the session) or stop it. Review the day with menu **9 (journal)** and the Knowledge tab.

**Weekly:** compare the bot's MLL floor with your Topstep dashboard; look at the Knowledge tab to see
which strategies have stopped (or started) working, and re-run a backtest on recent data.

**Contract roll:** the bot picks the active front-month contract each time it starts, so restart
it after the quarterly roll. If you pinned `instrument.contract_id`, update it.

---

## 23. Full configuration reference

`config.yaml` only needs the settings you want to change; everything else uses these defaults.
Misspelled settings are rejected with an error, so typos can't silently do nothing.

```yaml
mode: paper                    # paper | live
log_level: INFO                # what the console shows; files always get everything
log_dir: logs                  # relative paths are next to config.yaml
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
  name: adaptive               # adaptive | orb | noise_breakout | ema_trend | vwap_reversion | vwap_pullback
  params: {}

knowledge:                     # what the bot learns while it runs (drives the adaptive strategy)
  enabled: true
  auto_train: true             # retrain from history at startup when the last training is older than retrain_hours
  history_days: 60             # 10-120
  retrain_hours: 20
  half_life_days: 20           # observations lose half their weight after this many days
  min_samples: 8               # weighted observations needed before a strategy may trade in a slot
  min_edge_r: 0.05             # minimum (shrunk) expectancy in R to keep trading a strategy
  real_trade_weight: 2.0       # a real trade counts this many times an idea

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
  max_stop_ticks: 400
  breakeven_at_r: null
  breakeven_offset_ticks: 1
  trail_atr_multiple: null
  fees_per_contract_round_turn: null   # default: built-in TopstepX estimate per symbol
  slippage_ticks: 1.0                  # used by paper trading and backtests
  ramp_up_days: 3                      # first N live days on an account...
  ramp_up_risk_fraction: 0.5           # ...risk this fraction of risk_per_trade

session:
  timezone: America/Chicago
  trade_start: "08:30"
  last_entry: "14:30"
  flatten_at: "15:00"
  trade_weekdays: [0, 1, 2, 3, 4]      # Monday=0
  blackout_windows: []
  no_trade_dates: [...]                # CME holidays/early closes 2026-2027

execution:
  max_entry_slippage_ticks: 8  # entries can't fill worse than this (None = plain market orders)
  max_risk_overrun: 1.5        # close at once if a fill leaves the trade riskier than this x planned
  use_native_brackets: false   # true = TopstepX server-side brackets (needs "Auto OCO Brackets" enabled)
  orphan_position_policy: flatten   # flatten | adopt | ignore
  reconcile_interval_seconds: 15
  entry_fill_timeout_seconds: 20
  flatten_on_shutdown: true

data:
  warmup_days: 20
  live_market_data: false      # false = the sim data feed used by Combine/Express accounts

notifications:
  enabled: true
  events: [start, stop, entry, exit, risk, error, daily_summary]

telegram:
  control_enabled: true        # obey commands from TELEGRAM_CHAT_ID (needs TELEGRAM_BOT_TOKEN)
  confirm_dangerous: true      # /flatten and /stop need a confirmation tap
  allowed_user_ids: []         # optionally only these Telegram user IDs

recommendations:
  enabled: true
  strategies: []               # empty = ideas from every strategy

news:
  enabled: true
  impacts: [High]
  currencies: [USD]
  minutes_before: 5
  minutes_after: 10
  flatten_before: false

service:                       # used by 'topstep-bot service'
  keep_awake: true
  daily_restart_time: "16:05"  # CT, during the CME daily halt
  check_in_time: "08:00"       # weekday "bot is alive" message
  heartbeat_timeout_seconds: 180
  max_restarts_per_hour: 6

dashboard:
  enabled: true
  host: 127.0.0.1
  port: 8765
  open_browser: true

backtest:
  data_file: null
  report_dir: reports

api:                           # only change if Topstep changes its endpoints
  base_url: https://api.topstepx.com
  user_hub_url: https://rtc.topstepx.com/hubs/user
  market_hub_url: https://rtc.topstepx.com/hubs/market
  timeout_seconds: 15
```

`orphan_position_policy` decides what happens to a position the bot didn't open (e.g. a manual
trade): `flatten` closes it, `adopt` manages it with a protective stop, `ignore` leaves it alone.

---

## 24. Command reference

Run any command with `--help` for its options. Add `-c other.yaml` before the command to use a
different config file.

| Command | Purpose |
|---|---|
| `topstep-bot` | Interactive menu |
| `topstep-bot setup` | Setup wizard |
| `topstep-bot check` | Test login, list accounts, show the contract |
| `topstep-bot strategies` | Describe strategies and parameters |
| `topstep-bot backtest [--data F] [--download] [--days N] [--strategy S] [--symbol X] [--timeframe M] [--tz TZ] [--no-open]` | Backtest and open a report |
| `topstep-bot demo` | Backtest on synthetic data |
| `topstep-bot train [--data F] [--days N] [--tz TZ]` | Teach the bot which strategy works at which time of day from recent real data |
| `topstep-bot download [--days N] [--tf M]` | Save history to `data/` |
| `topstep-bot start [--mode paper\|live] [--yes] [--no-bot] [--no-browser]` | Start the dashboard + Telegram, which run the bot 24/7 (`service` does the same) |
| `topstep-bot run [--mode paper\|live] [--yes]` | Run only the trading bot, without dashboard (normally started for you by `start`) |
| `topstep-bot flatten [--yes]` | Emergency: cancel all orders, close all positions |
| `topstep-bot journal [--mode paper\|live] [--limit N]` | Recent trades and daily results |
| `topstep-bot telegram-test` | Check the Telegram token and chat ID with a test message |
| `topstep-bot preflight [--days N] [--skip-backtest]` | Check everything before trading live |
| `topstep-bot go-live` | Preflight, then start live trading |
| `topstep-bot autostart on\|off\|status` | Start everything when you sign in to Windows |
| `topstep-bot logs [--all] [--open] [--bundle]` | Recent errors, open the log folder, or zip logs for support |

On Windows you can also pass commands through the launcher, e.g. `start.bat backtest --days 180`.

---

## 25. Files the bot creates

| Path | Contents |
|---|---|
| `config.yaml` | Your settings |
| `.env` | Your API credentials and webhook URLs — **private** |
| `data/journal_paper.db`, `data/journal_live.db` | Trade journal, daily results, MLL floor, paper balance |
| `data/<SYMBOL>_1m.csv` | Downloaded history |
| `reports/*.html` | Backtest reports |
| `logs/` | Log files — see [section 21](#21-logs-finding-out-what-happened) |
| `data/remote_settings.json` | Settings changed from the dashboard/Telegram (delete it, or `/reset`, to undo) |
| `data/knowledge_<SYMBOL>_<TF>m.json` | The knowledge base: what works when (delete it to start learning from scratch) |
| `data/news_cache.json` | This week's economic calendar |
| `data/controller.json` | The mode you chose last (Paper/Live) |
| `data/bot_exit.json` | Why the bot last exited (shown on the dashboard) |
| `data/heartbeat` | "Still alive" signal the controller watches |

---

## 26. Troubleshooting

**"Login failed"** — Use your TopstepX *username*, not your email. Copy the API key again in full.
Check your API subscription is active. Re-run setup or edit `.env`.

**"Several accounts can trade"** — Run setup and pick one, or set `account.account_id`
(`topstep-bot check` lists the IDs).

**"No contract found for symbol"** — Check the symbol spelling, or pin `instrument.contract_id`.

**The bot never trades.** With the `adaptive` strategy, first open the Knowledge tab: if it is not
trained yet, press **Retrain now** (or run menu 5); if every cell is ✘ or ?, no strategy has proven
itself lately and the bot is right to wait. Otherwise look at the dashboard's Activity list and
`bot.log` — skipped signals are logged with a reason (outside the entry window, daily limit, stop too
wide, 1 contract too risky, "skipped: unproven", ...). Some strategies trade rarely (ORB at most twice
a day; noise_breakout needs ~15 days of history). Check today isn't in `no_trade_dates`.

**"Skipped: 1 contract would risk more than the allowed budget"** — The stop is too far for your
`risk_per_trade`. Raise the risk, trade a micro, or use a tighter stop setting.

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

**The bot closed / stopped unexpectedly** — The dashboard's banner and Logs tab (or Telegram
`/status`) show why, and **Start** / `/startbot` brings it back. On the PC, run `start.bat logs`. The last lines say why it
stopped ("Shutting down. Reason: …"): a Stop from Telegram or the dashboard, Ctrl+C, a daily
maintenance restart, or a crash (with a `crash_*.txt` file).

**The dashboard doesn't load** — The controller window was closed (or the PC restarted). Start it
again from the menu, or turn on `autostart`. If it says the port is busy, it's already running:
open http://127.0.0.1:8765.

**Telegram: no reply to commands** — Check the bot window says `Telegram control: on`. Run
`topstep-bot telegram-test`. Make sure you're messaging from the chat that was set up (a different
chat is ignored — `logs/bot.log` shows "Ignored Telegram command from unauthorized chat ..." with
its ID). Only one program can read a bot's messages: close any other copy of the bot.

**Telegram: "the bot token was rejected"** — The token is wrong or was revoked. Run setup again.

For anything else, `logs/bot.log` has the details.

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
  stop, `state()` to show values on the dashboard.

Backtest it with `topstep-bot backtest --strategy my_strategy`, and run the test suite with
`pytest` after any change to the bot itself.

---

*Trading futures involves substantial risk of loss and is not suitable for everyone. This software
is provided as-is, with no guarantee of profit or of compliance with Topstep's rules, which can
change. You are responsible for every order placed on your account.*
