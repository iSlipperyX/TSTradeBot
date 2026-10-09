# Topstep Bot — Complete How-To-Use Guide

This guide takes you from zero to a running bot, explains every screen and setting, and covers
what to do when something goes wrong. If you only read one section, read
[The path to live trading](#7-the-path-to-live-trading). For every Topstep rule and the guard that
enforces it, see [TOPSTEP_RULES.md](TOPSTEP_RULES.md).

> **Honest expectations.** No bot can guarantee passing the Combine or making money, and this one
> doesn't either. What it does guarantee is that it won't break a Topstep rule by itself: every
> rule below is enforced in code and covered by tests. Backtest results are evidence, not a promise.
> Paper trade first, then run it on a Combine, and only then on an account you care about.

## Quick start (the short version)

1. Install [Python 3.11+](https://www.python.org/downloads/) and tick **"Add python.exe to PATH"**.
2. Double-click **`start.bat`**. The first run installs everything.
3. Get a TopstepX API key ([section 4](#4-get-your-topstepx-api-key)).
4. Menu **1 (setup)**: answer the questions. Press Enter to accept a suggestion. If the account is
   just for teaching the bot, choose **"Teach the bot"** ([section 17](#using-a-combine-to-teach-the-bot)).
5. Menu **16 (rules)**: check the limits the bot will enforce for your account.
6. Menu **5 (train)**, then menu **4 (backtest)** and read the report.
7. Menu **6 (paper)**: watch it trade with simulated orders for a few days.
8. Menu **2 (go-live)**: it checks everything, then you type `LIVE`.
9. Keep the PC on. Watch it on the dashboard (http://127.0.0.1:8765) or Telegram (`/status`).
   `/flatten` closes everything, a confirmed `/stop` stops trading, `/startbot` starts it again.

---

## Contents

1. [What the bot does (and doesn't do)](#1-what-the-bot-does-and-doesnt-do)
2. [What you need](#2-what-you-need)
3. [Install](#3-install)
4. [Get your TopstepX API key](#4-get-your-topstepx-api-key)
5. [Run the setup wizard](#5-run-the-setup-wizard)
6. [The main menu](#6-the-main-menu)
7. [The path to live trading](#7-the-path-to-live-trading)
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
18. [Tuning: test strategy settings on unseen data](#18-tuning-test-strategy-settings-on-unseen-data)
19. [Changing settings from the dashboard or Telegram](#19-changing-settings-from-the-dashboard-or-telegram)
20. [Running 24/7](#20-running-247)
21. [Starting today: preflight, ramp-up and news](#21-starting-today-preflight-ramp-up-and-news)
22. [Logs: finding out what happened](#22-logs-finding-out-what-happened)
23. [Daily routine](#23-daily-routine)
24. [Full configuration reference](#24-full-configuration-reference)
25. [Command reference](#25-command-reference)
26. [Files the bot creates](#26-files-the-bot-creates)
27. [Troubleshooting](#27-troubleshooting)
28. [Writing your own strategy](#28-writing-your-own-strategy)

---

## 1. What the bot does (and doesn't do)

**It does:**

- Watch one futures contract (for example MNQ, the Micro Nasdaq) on 1–60 minute bars.
- Ask a strategy, once per closed bar, whether to buy, sell or exit.
- Size every trade from the dollars you are willing to lose on it.
- Place the entry, then immediately a **protective stop** and (optionally) a **profit target**.
  When one of those fills, it cancels the other.
- Enforce Topstep's rules *and* stricter personal limits: the Maximum Loss Limit, the optional
  Daily Loss Limit, the Combine Consistency Target, contract caps (including the XFA Scaling Plan
  and product caps), never holding the maximum size into news, and being flat before 15:10 CT
  ([section 14](#14-topstep-rules-what-the-bot-enforces-and-what-is-still-on-you)).
- Stop trading once the Combine profit target is reached, so a late loss can't undo the pass.
- Pause automatically around high-impact economic news.
- Run unattended 24/7 on your PC, restart itself after a crash, and recover an open position
  after a restart.
- **Learn** which strategy works at which time of day: every strategy's signals, traded or not,
  are followed to their outcome, and the default `adaptive` strategy only trades what has been
  working ([section 17](#17-training-and-the-knowledge-base-how-the-bot-learns)).
- **Tune**: test every strategy and setting on real history the way it would have traded,
  judged only on data the choice never saw, and tell you what held up ([section 18](#18-tuning-test-strategy-settings-on-unseen-data)).
- Show everything on a dashboard in your browser and on Telegram, keep a trade journal, and send
  alerts.

**It doesn't:**

- Guarantee profits. Backtests are hypothetical; markets change. On 10½ years of real Nasdaq
  futures data, two of the bundled strategies lost money and the classic opening-range breakout
  roughly broke even (see [section 12](#12-the-strategies)). Treat every result as evidence, not
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
| Windows 10/11 PC (macOS/Linux also work) | Must stay on while the bot trades. The bot keeps Windows awake itself (see [section 20](#20-running-247)), but a closed laptop lid or a Windows Update restart still stops it. |
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
   (e.g. `50KTC-...`, `XFA-150K-...`) and pre-selects the matching size and type below. Live
   Funded accounts are not offered: Topstep doesn't allow API trading on them.
3. **Account size** — 50K, 100K or 150K. This sets the Maximum Loss Limit, profit target and
   contract cap the bot works with.
4. **Account type** — `combine` (evaluation), `express` (Express Funded Account) or `practice`.
   Then two yes/no questions: whether you added Topstep's optional **Daily Loss Limit** at checkout
   (it's under Risk Settings in TopstepX; the bot then stops before it), and for an Express Funded
   Account, which **payout path** you chose.
5. **What to trade.** Start with a **micro** contract (MNQ, MES, M2K, MYM, MGC, MCL). Micros are
   1/10th the size of the minis, so mistakes cost 10× less.
6. **Strategy.** `adaptive` (recommended) runs every strategy all day and trades only what the bot
   has learned is working; or pick one strategy. See [section 12](#12-the-strategies).
7. **Risk.** Dollars to risk per trade and your personal daily loss limit. The wizard suggests
   7.5% and 25% of your Maximum Loss Limit (for a 50K account: $150 per trade, $500 per day). Your
   daily limit must be below Topstep's limits; the wizard won't accept one that isn't.
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
| 2 | go-live | **Start trading today:** runs every preflight check, then starts live ([section 21](#21-starting-today-preflight-ramp-up-and-news)) |
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
| 13 | autostart | Starts everything automatically when you sign in to Windows ([section 20](#20-running-247)) |
| 14 | logs | Shows recent errors and where the log files are ([section 22](#22-logs-finding-out-what-happened)) |
| 15 | tune | **Tuning:** tests every strategy and its settings on real data they never saw and can save what held up ([section 18](#18-tuning-test-strategy-settings-on-unseen-data)) |
| 16 | rules | Shows Topstep's rules for **your** account (limits in dollars and contracts) and what the bot does about each one |
| 17 | insights | **What the bot has learned:** each strategy's results after costs, real fills against simulated ones, and the market conditions it did best and worst in ([section 17](#what-the-bot-learned-results-after-costs-real-fills-and-conditions)) |
| 18 | learn | **Feeds the bot more history:** downloads up to a year it doesn't have yet and replays all of it through every strategy ([long-run memory](#the-long-run-memory-a-much-bigger-knowledge-base)) |

Above the list, the menu shows a one-line summary of your setup (account, symbol, strategy, mode).
If `config.yaml` has a mistake, that line says what and where.

You can switch between paper and live later from the dashboard, without coming back to this menu.

---

## 7. The path to live trading

You can start the same day — the bot protects a new account with reduced risk while it proves
itself — but the more evidence you have first, the better. In order:

1. **Set up** (menu 1) and **check** the connection (menu 3).
2. **Train the bot** (menu 5). It replays the last 60 days of real data through every strategy and
   learns which ones work at which time of day ([section 17](#17-training-and-the-knowledge-base-how-the-bot-learns)). Read the table it prints: if everything
   is ✘, the market has not been kind to these strategies lately and the adaptive strategy will
   (rightly) sit out.
3. **Backtest on real data** (menu 4). Is the result positive *after fees*, is the drawdown
   survivable, does the Combine pass rate look reasonable? To compare single strategies and their
   settings over a longer history without fooling yourself, use **tune** (menu 15, [section 18](#18-tuning-test-strategy-settings-on-unseen-data)).
4. **Paper trade** (menu 6, or the dashboard's Paper switch): real live prices, simulated orders, no
   risk. Optional, but two to four weeks of it tells you more than any backtest — compare what it did
   with what the backtest says it should have done.
5. **Start trading today** (menu 2). The preflight checks the login, the account (and whether it
   is allowed to trade), its current Maximum Loss Limit (it asks you for it if it can't work it out),
   that the account is flat, the contract, live data, your PC clock, today's calendar and news, your
   alerts and risk settings — and backtests every strategy on the last 90 days. Fix anything marked
   ✘, then type `LIVE`. Use a Combine or Practice account — never one you can't afford to lose — and
   consider `max_contracts: 1` in `config.yaml` for the first week.
6. **The first live days run at reduced risk** (ramp-up): by default the first 3 trading days on a
   new account risk 50% of your normal amount per trade. Only days on which the bot actually traded
   count.
7. **Watch the first trades closely.** Keep TopstepX open alongside the dashboard (or Telegram) and
   confirm every order shows up there with its stop.
8. Only then consider raising size, one step at a time.

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
report. The first few trading days only warm up indicators and are not traded (2 days for most
strategies, 15 for `noise_breakout` and the ATR version of `orb_momentum`, the longest of them
for `adaptive`).

A backtest of the `adaptive` strategy is **walk-forward**: it starts knowing nothing and learns
from every strategy's outcomes as the data plays, exactly as it does live — so expect few or no
trades in its first weeks.

### Download history separately

```bash
topstep-bot download --days 365
```

This saves `data/MNQ_1m.csv` (1-minute bars). Backtests, training and tuning resample it to whatever
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

## 9. Paper trading and the dashboard

Menu **6**, or:

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

The paper account's balance carries over between runs (stored in `data/journal_paper.db`), and so
do the day's trades after a restart. To start the paper account fresh, stop the bot and delete
that file.

### The dashboard

**Top bar** (always visible):

- **Paper | Live** switch. Switching to Live asks you to type `LIVE`. It's refused while a trade is
  open, so flatten first. The bot restarts in the new mode, and the choice is remembered for next time.
- **Bot status:** Running, Starting…, Stopped, Crashed or Failed to start, plus uptime.
- **Start, Restart and Stop** for the bot process. Stop closes any position first; the dashboard stays up.
- **Log badge:** warnings and errors so far. Click it for the Logs tab.
- **Theme button** (◐): follow your computer's light/dark setting, or force light or dark. The
  dashboard remembers it, and it remembers which tab you were on.
- **Market clock** (just under the top bar, works even while the bot is stopped): whether the market
  is open, the time in Chicago, and live countdowns to the next:
  - **Market opens / closes:** the CME Globex session, 17:00–16:00 CT, closed Friday 16:00 to Sunday 17:00.
  - **Regular hours open / close:** your contract's regular trading hours, the busiest part of the
    day. For index futures (ES, NQ, MES, MNQ, …) that's 08:30–15:00 CT, the US stock market's hours;
    crude oil (CL, MCL) is 08:00–13:30 CT and gold (GC, MGC) 07:20–12:30 CT.
  - **Bot starts trading / Last new entry:** your `session.trade_start` and `last_entry`.
  - **Bot closes all trades:** your `session.flatten_at`.
  - **Topstep flat-by:** 15:10 CT, Topstep's deadline (it starts closing positions at 15:08).

  Countdowns turn amber in the last 15 minutes before a close. They use the same rules the bot trades
  by, so weekends and the days in `no_trade_dates` (holidays) and outside `trade_weekdays` are skipped.
  On such a day a note says so. CME's own holiday hours can differ, so check its calendar for
  early closes.
- When the bot isn't running, a **banner** shows why (e.g. "stop requested from Telegram",
  "crashed: …", "could not start: …") with **Start bot** and **See logs** buttons.

**Overview tab**

- Balance (and profit since the start), today's P&L (and trades won), open P&L (also in R) and
  position (with today's contract limit and the last price).
- **Guardrails:**
  - *Room above Max Loss Limit:* turns yellow below 50% and red below 25% of the MLL size.
  - *Daily loss limit used*, and *Topstep daily loss limit used* if your account has one.
  - *Combine profit target* progress, with the Consistency Target status underneath.
  - *Trades today*.
  - **Pause / Resume / Flatten & halt** buttons.
- **Current trade:** side and size, who opened it (*bot*, *idea* you took, or *you*), entry, stop,
  target, dollars at risk, live P&L in dollars and R, and why it was opened. Two buttons:
  - **Close trade** closes it at the market and **the bot keeps running** (Flatten & halt, by
    contrast, stops all trading until a restart).
  - **Stop to breakeven** moves the stop to the entry price. It's available once price is at least
    2 ticks beyond the entry, and the stop only ever moves in your favour.
  - **New manual trade** opens the Trade tab.
- **Getting ready to trade:** the trades each strategy is building toward right now, which of their
  conditions are met, the planned entry, stop, target and size, and whether a rule would block them.
  Below it, a history of setups forming, firing and being cancelled. See
  [Watching the bot get ready to trade](#watching-the-bot-get-ready-to-trade) below.
- Strategy levels, the time-of-day slot and volatility regime, and — with the adaptive strategy —
  which sub-strategy is managing the trade and the last signal it took or skipped.
- **Today's trades:** every closed trade today, who opened it, entry, exit, P&L, R and how it ended.
- **Activity:** everything the bot did, newest first.

**Trade tab:** open your own trade with suggestions from the bot — see
[Trading manually from the dashboard](#trading-manually-from-the-dashboard) below.

**Ideas tab:** recommended trades with **Take** buttons and today's results per strategy — see
[section 16](#16-recommended-trades). A badge on the tab shows how many ideas are live.

**Knowledge tab:** what the bot has learned — for every strategy, time of day and regime, the average
result per signal and whether the adaptive strategy trades it right now — with a **Retrain now** button,
plus a **Your manual trades** panel: your record by time of day and regime and your latest trades.
See [section 17](#17-training-and-the-knowledge-base-how-the-bot-learns).

**Settings tab:** change risk, limits, times, the news pause and the auto-traded strategy — see
[section 19](#19-changing-settings-from-the-dashboard-or-telegram).

**Logs tab:** works even when the bot is down.

- Bot process events: starts, stops, crashes, restarts, mode switches.
- Recent warnings and errors.
- The latest crash report, if any.
- The end of the bot's log.

The dashboard only accepts connections from your own computer.

### Trading manually from the dashboard

The **Trade** tab is a trade ticket. It lets you place your own trade, and it shows you everything
the bot knows before you press the button. Manual trades go through **exactly the same risk rules
and Topstep guards as the bot's own trades**, and the bot learns from their results.

**1. Pick Buy or Sell.** The ticket fills itself in straight away:

- **Stop** — suggested from the best live idea on that side (the one from the strategy with the
  best record at this time of day), or else 1 × ATR(14) from the price, kept between `min_stop_ticks`
  and `max_stop_ticks`. Type a price to use your own. Every manual trade has a stop; it decides the size.
- **Target** — the idea's target, or 1.5R by default. The **1R / 1.5R / 2R / 3R** buttons set it
  from your stop, and **None** trades without a target (it then ends at the stop, when you close it,
  by the breakeven/trailing settings, or at 15:10 CT).
- **Size** — the most your risk rules allow with this stop (shown as *max*). **½** and **1** pick
  smaller sizes. You can type any size; anything above the maximum is cut back, never placed.
- **Note** (optional) — why you're taking it. It's saved with the trade in the journal and activity log.

**2. Read the summary.** The exact order: side, size, market price and the worst fill allowed
(`execution.max_entry_slippage_ticks`), stop and dollars at risk, target, reward and R:R. If the
bot adjusted something (stop widened to the minimum distance, size cut back), it says so.

**3. Check the rules.** The *Rule checks* panel lists ✔ / ⚠ / ⛔ for: the trading window, one
position at a time, your daily loss room, the room above the Maximum Loss Limit (after
`mll_buffer`), any Topstep daily loss limit, the Combine consistency guard, today's position limit
(halved near scheduled news), losing streak, news coming up, and the order guard. Anything that
blocks the trade turns the button off and says why. Manual trades may go past
`max_trades_per_day` and work while automatic entries are paused — **nothing else is relaxed**.

**4. See what the bot knows.** The *What the bot knows* panel gives a plain reading —
**Supported**, **Some support**, **Mixed evidence**, **No clear evidence** or **Evidence against** —
and the reasons behind it:

- which strategies have worked at this time of day in this regime (✔ / ✘ / ?, average R, signals);
- how **longs and shorts** have done here across all strategies;
- **live ideas** from the last 15 minutes that agree or disagree with your side (**Use its levels**
  copies an idea's stop and target into the ticket);
- **your own manual trades**, overall and at this time of day and regime (after 5 trades in the same
  slot and regime, your record counts toward the reading).

This is evidence from past signals and trades, not a forecast. A "Supported" trade can still lose.

**5. Place it.** The button shows the order (e.g. *Place SELL 1 MNQ · paper*, or *· LIVE* in live
mode). You confirm once more, then it's sent as a market order capped at the worst fill allowed,
protected by its stop at once. After that it's managed like any bot trade: the stop, target,
breakeven/trailing settings, risk flattening (loss limits, MLL, Combine guards), closing before
news if `news.flatten_before` is on, and the 15:10 CT flatten. The auto-traded strategy never
exits your trade. Manage it from the Overview's **Close trade** and **Stop to breakeven**.

**How the bot learns from your trades.** When a manual trade closes, its result (in R), time of day,
regime and how it ended are added to the knowledge base, tagged **manual**. Manual trades are kept
apart from the strategies' evidence: they never switch a strategy on or off, but the trade ticket and
the Knowledge tab show your record next to the strategies', so over a Combine you can see where your
own trades work and where they don't. Like the bot's real trades, they count double and are never
dropped by retraining. Trades closed by a shutdown, halt or daily restart aren't counted (the
ending says nothing about the trade).

### Watching the bot get ready to trade

The **Getting ready to trade** section of the Overview tab shows every trade a strategy is building
toward, before it happens. Each card is one setup: a strategy, a side and your contract.

- **bot trades it** (blue border) means the auto-traded strategy places this trade when it fires.
  With the adaptive strategy that's any sub-strategy the knowledge base allows right now; the others
  show **idea only**, and when they fire they appear as ideas on the Ideas tab instead.
- **The checklist** is the strategy's own entry rules in plain words. ✔ is met, ○ is still waiting.
  They're checked against the live price as if the current bar closed there, so a card can tick and
  untick as the price moves. Strategies only act on closed bars, so nothing happens until the bar
  actually closes. The bar shows how many conditions are met; a card whose conditions are all met
  turns amber, meaning it fires if the bar closes here.
- **Entry** is the trigger price when the setup waits for a level ("a bar closes above the range high
  (21,050.25)"), or *~price at the close* when it waits for a time or an event.
- **Stop, Target and Size** are what the bot would use if it fired now, sized by your risk rules exactly
  like a real trade. *Set when it fires* means the stop depends on the bar that triggers it.
- **Blocked now** and the amber note above the cards mean the bot wouldn't enter even if the setup
  fired: a trade is already open, it's outside your trading window, a loss limit is close, the bot is
  halted and so on. **The bot would skip it** means the setup itself can't be traded as it stands,
  for example because 1 contract would risk more than your risk per trade.
- **Open in trade ticket** fills the Trade tab with the setup's side, stop and target so you can take
  it yourself. Careful: a manual trade enters at the market price now, not at the setup's trigger, and
  the ticket re-checks every rule before you place it.
- **Also watching** lists setups that haven't met a single condition yet.

**Setup history** follows each setup through the day, one line per closed bar where something changed:

- **Forming:** half or more of its conditions are met, with the next one it's waiting for.
- **Fired:** the strategy signalled. It says whether the bot placed the trade, skipped it (and why),
  or posted it as an idea.
- **Cancelled:** it fell apart before firing (a condition stopped being true), its time window
  closed, or the strategy took the other side instead.

Nothing in this section places or blocks a trade; it shows what the strategies and your rules are
already doing. A setup that's forming is not a prediction that it will fire, or that it would win.

---

## 10. Live trading

Before going live, in TopstepX:

- Make sure the account is active and allowed to trade.
- Keep TopstepX open so you can see orders appear.
- You do **not** need to enable "Auto OCO Brackets"; the bot manages its own stop and target.

The recommended way in is **menu 2 (Start trading today)**, which runs the preflight first
([section 21](#21-starting-today-preflight-ramp-up-and-news)). You can also switch the dashboard's **Paper | Live** toggle to Live (type `LIVE` to
confirm), or start with menu **7** — which shows a red warning and asks you to type `LIVE` too.

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

## 11. Stopping the bot and emergency controls

| Want to... | Do this |
|---|---|
| Stop new trades but let the open trade finish | **Pause new trades** (Overview tab) or Telegram `/pause`; then **Resume**. You can still take ideas and trade manually while paused. |
| Close the open trade but keep the bot running | **Close trade** under Current trade (Overview tab). |
| Close everything and stop trading | **Flatten & halt** or Telegram `/flatten`. Trading stays halted until the bot is restarted. |
| Stop the bot (dashboard and Telegram stay online) | **Stop** in the top bar or Telegram `/stop`. Start it again with **Start** or `/startbot`. |
| Restart the bot | **Restart** or Telegram `/restart` — it flattens first. |
| Close everything, including the dashboard | Close the Topstep Bot window, or press **Ctrl+C** in it. |
| Kill switch without the dashboard | Create an empty file named `KILL` in the bot folder. The bot flattens and halts within a second. Delete the file before the next start. |
| Panic button when nothing is running | Menu **8 (flatten)** — cancels every order and closes every position on the account. |
| Last resort | Close the position in TopstepX yourself. |

Whenever the bot stops, it flattens any open position first (`execution.flatten_on_shutdown`).
If your PC crashes or loses power, the **protective stop stays at TopstepX**, so the position is
still protected — and when the bot starts again it re-adopts the position. Check TopstepX as soon
as you can anyway.

If the bot ever stops itself because of an internal error, it flattens first, sends a ⛔ alert,
writes the details to `logs/errors.log`, and the controller restarts it automatically.

---

## 12. The strategies

List them any time with menu **10**. Change the strategy with `strategy.name` in `config.yaml` (or
let `tune` save one) and override any parameter under `strategy.params`, e.g.

```yaml
strategy:
  name: noise_breakout
  params: {band_mult: 1.25}
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

### What 10½ years of real data says

Each strategy on its own was tested on **real 1-minute Nasdaq-100 futures data from January 2015 to July
2025** (NQ prices, traded as MNQ), with the bot's own trading code, $150 risk per trade, the default
risk limits, TopstepX fees and 1 tick of slippage per fill. Then two harder tests:

- **Walk-forward (out-of-sample):** for each year 2018–2025, settings were chosen using only the
  three years before it, then traded on that year "blind" — exactly what `tune` does.
- **Robustness:** the same settings on a different market (S&P 500 futures, Dec 2022–Jul 2024), on
  the most recent 60 days (Aug–Oct 2026), and with 2–3 ticks of slippage instead of 1.

| Strategy | Walk-forward 2018–2025 | Positive years | S&P futures | 3 ticks slippage | Verdict |
|---|---|---|---|---|---|
| `noise_breakout` (checkpoint exits) | +$9.6k, PF 1.19, max DD $2.5k | 5 of 8 | **profitable in both periods** | still profitable | **Most robust** |
| `orb_momentum` | **+$50.4k**, PF 1.30, max DD $7.6k | **8 of 8** | lost money in 2023–24 | barely breakeven over 10 years | Strongest on Nasdaq, fragile elsewhere |
| `orb` | +$10.9k, PF 1.11 | 6 of 8 | lost money | lost money | Weak |
| `ema_trend` | +$5.8k, PF 1.06 | 5 of 8 | – | – | Weak |
| `late_day_momentum` | −$3.6k, PF 0.88 | 3 of 8 | – | – | Did not hold up |
| `vwap_reversion` | −$8.4k, PF 0.61 | 0 of 8 | – | – | Lost every year — avoid |

Over the full 2015–2025 period with fixed settings, `noise_breakout` (checkpoint exits) made
+$18.6k with a $4.6k maximum drawdown and was profitable in 9 of 11 years; the older per-bar
trailing exit made +$10.4k with nearly twice the drawdown. On the most recent year of data
(Jul 2024–Jul 2025) it made +$3,300 over 100 trades with a $550 maximum drawdown.

`adaptive` and `vwap_pullback` are newer than this study and were not part of it. The adaptive
strategy runs the others with these default settings (the ones the study arrived at).

What this means for you:

- **No strategy is a sure thing.** Every strategy had losing years with fixed settings, and the past
  two months were mixed for all of them (too few trades to mean much either way).
- **`orb_momentum` lives on wide stops.** Its edge is real on Nasdaq futures since 2018, when its
  stops (10% of the day's average range) are well over 100 ticks wide; where they are only ~20 ticks
  (S&P futures, Nasdaq before 2018) costs eat the edge. Trade it on MNQ/NQ only, and check it with
  `risk.slippage_ticks: 2`.
- **Choosing a strategy because it looked best recently doesn't work well.** Picking whichever
  strategy had the best past three years chose `noise_breakout` every year and made +$9.6k; sticking
  with `orb_momentum` made five times more. That is why `tune` judges strategies on data they
  never saw, and why you should re-tune every month or two rather than chase last week's winner.
- **The same caution applies to `adaptive`,** which chooses by each strategy's last 60 days at that
  time of day. It hasn't been run over this 10-year history yet; if you want the choice with the
  most evidence behind it today, set `strategy.name: noise_breakout`.
- These results were produced with real exchange data but simulated fills. Expect live results to
  be somewhat worse.

### `noise_breakout` — Intraday Momentum (Noise Area)

Adapted from Zarattini, Aziz & Barbon (2024), *"Beat the Market: An Effective Intraday Momentum
Strategy for S&P500 ETF (SPY)"*. For each time of day it averages how far price had moved from the
open over the last `lookback_days` (14) sessions; that average forms a "noise band" around today's
open. At each half-hour checkpoint, a close above the band goes long and below goes short. No fixed
target — winners run until the session flatten time. Needs about 15 trading days of history.

Two exit styles (`exit_mode`):

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
you can test it on your own data; don't trade it unless `tune` says it held up.

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

### `vwap_pullback` — VWAP Trend Pullback

Built for the middle and the end of the day. When price is trending (above VWAP with a rising
`trend_ema`, measured over `slope_bars`), it waits for a pullback into the VWAP band (`band_k`) and buys
the first bar that closes back above the previous bar's high. Stop `stop_atr_mult` × ATR under the
pullback low, target `target_r` × risk, exit on a close through VWAP. Shorts are the mirror image.
No entries before `min_minutes_after_open` or after `entry_cutoff`; at most `max_trades_per_day`.
Newer than the 10-year study above, so it is untested on that data.

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
- exceed Topstep's position limit for today (5/10/15 minis or 50/100/150 micros for 50K/100K/150K,
  the XFA Scaling Plan, or a product limit such as 3/6/9 for Gold and Crude), or `max_contracts`.

Within 30 minutes of a scheduled high-impact release (and whenever the economic calendar can't be
loaded) the limit is halved, because Topstep prohibits taking the maximum position size into news.
Right before an entry order is sent, an independent check refuses it if it would still take the
position past Topstep's limit.

If even 1 contract would risk too much, the trade is skipped and the dashboard says why.

During the [ramp-up](#21-starting-today-preflight-ramp-up-and-news) the per-trade risk is multiplied by
`ramp_up_risk_fraction` (default 0.5).

### Daily limits

| Setting | Default | What happens |
|---|---|---|
| `personal_daily_loss_limit` | 500 | Includes open P&L. Hitting it flattens everything and stops trading until the next session. |
| `daily_profit_target` | Combine: 40% of the profit target ($1,200 on 50K) | No new trades after reaching it. This protects Topstep's Consistency Target (best day at most 55% of the profit target, or the target goes up). |
| `consistency_guard` | on | Combine: if an open trade carries the day to 50% of the profit target ($1,500 on 50K), it is closed and the day is done, before the 55% line. |
| `stop_at_profit_target` | on | Combine: once the profit target is reached, the bot closes out and stops trading so the pass can't be given back. |
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

## 14. Topstep rules: what the bot enforces and what is still on you

Run **`topstep-bot rules`** (menu 16) to see these limits in dollars and contracts for your own
account. [TOPSTEP_RULES.md](TOPSTEP_RULES.md) explains each rule in detail, with Topstep's sources.
The rules were checked against Topstep's Help Center in October 2026.

| Topstep rule | How the bot handles it |
|---|---|
| **Maximum Loss Limit** — $2,000 / $3,000 / $4,500, trails your highest end-of-day balance, locks at the starting balance ($0 in an XFA), enforced in real time including open P&L | Tracked from end-of-day balances. Every trade is sized so a stop-out stays above the floor plus `mll_buffer`; open positions are flattened if equity gets within half the buffer. |
| **Daily Loss Limit** — optional, chosen at checkout: $1,000 / $2,000 / $3,000 | Set `account.topstep_daily_loss_limit: true` if you have it (setup asks). The bot stops new trades at 90% of it and closes trades at 95%. Your `personal_daily_loss_limit` always applies and must be lower. |
| **Consistency Target (Combine)** — best day at most 55% of the profit target ($1,650 / $3,300 / $4,950), or the target rises to best day ÷ 0.55 | No new trades after 40% of the target in a day; an open trade is closed at 50%. The dashboard's target meter shows any increase. |
| **Profit target (Combine)** — $3,000 / $6,000 / $9,000 | Once reached, the bot closes out and stops trading. |
| **Position limits** — 5/10/15 minis (10 micros = 1 mini), XFA Scaling Plan, product caps (Gold/Crude 3/6/9, micros 30/60/90; Silver, Copper, Platinum not tradable) | Enforced when sizing and again right before every entry order. A config for a product Topstep doesn't allow is refused. |
| **Flat by 15:10 CT** | Flattens at `flatten_at` (15:00 default); the config refuses anything later than 15:08. |
| **News** — no maximum-size position into a scheduled major release | Entries pause around high-impact releases, sizes are halved in the 30 minutes before one, and a full-size position is closed before it. |
| **Your own computer only** — no VPS, VPN or remote server | The preflight warns if the PC looks like a cloud server, virtual machine or Remote Desktop session. Running it on your own PC is your responsibility. |
| **No high-frequency trading** | A few trades a day on 1-minute bars or slower. An order-rate breaker stops new entries (and halts the bot) after 30 order actions in a minute or 20 entries in a day. |
| **No API trading on Live Funded Accounts** | The bot refuses to trade an account the API reports as real-money. |
| **No hedging** | One position at a time on one contract. Don't run opposite strategies on several accounts. |
| **Automation is your responsibility** — Topstep makes no exceptions for bot malfunctions | Paper trade first, watch the first live days closely, and keep Telegram set up so you can `/flatten` from anywhere. |

**Keep the bot's MLL in sync.** The API doesn't report the MLL floor, so the bot calculates it. The
preflight asks for it when it can't work it out. If the bot was off for a while or you traded
manually, copy the current Maximum Loss Limit from your Topstep dashboard into `config.yaml`:

```yaml
account:
  mll_floor_override: 48500
```

For an Express Funded Account after your first payout, Topstep sets the MLL to $0 — set
`mll_floor_override: 0`.

Topstep changes its rules from time to time. Check help.topstep.com and your TopstepX Risk Settings,
and adjust the config if anything differs: a lower position limit goes in `risk.max_contracts`, a
different Daily Loss Limit in `account.topstep_daily_loss_limit` (a dollar amount).

---

## 15. Telegram control and alerts on your phone

With Telegram set up, the bot **messages you** (entries, exits, risk events, daily summary,
check-ins, crashes and restarts) and
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
| `/brief` | **What the bot knows**, in plain sentences: how much it has seen, what it would and wouldn't trade right now and why, what has worked after costs, lessons worth testing, and when it expects its next trade ([section 17](#what-the-bot-knows-and-when-it-trades-next)) |
| `/next` | **When the next trade is likely**, and what that estimate is based on (also a button under every message) |
| `/learn` | Grow the long-run memory now: download the history it's missing and replay all of it ([section 17](#the-long-run-memory-a-much-bigger-knowledge-base)) |
| `/knowledge` | What the bot has learned: per strategy and time of day, ✅ trades now / ❌ switched off / ❔ unproven, then results after costs, real fill slippage and conditions worth testing ([section 17](#17-training-and-the-knowledge-base-how-the-bot-learns)) |
| `/train` | Retrain the knowledge base on recent history now (the bot keeps trading meanwhile) |
| `/settings` | Every setting you can change, with current values and limits |
| `/set <name> <value>` | Change a setting, e.g. `/set risk 150`, `/set dailyloss 400`, `/set strategy noise_breakout`, `/set news off` ([section 19](#19-changing-settings-from-the-dashboard-or-telegram)) |
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
limit it to some strategies with `recommendations.strategies: [noise_breakout, orb_momentum]`.

---

## 17. Training and the knowledge base: how the bot learns

Not to be confused with **tune** ([section 18](#18-tuning-test-strategy-settings-on-unseen-data)), which tests single strategies' settings on a long
history; training is what the adaptive strategy runs on.

The bot keeps a **knowledge base**: for every strategy, what its signals have been worth, split by
**time of day** (open 08:30–10:00, midday 10:00–13:00, close 13:00–15:10 Chicago time) and by
**volatility regime** (*calm* or *volatile*: whether the 14-bar range during regular hours is at least
20% above its multi-day average). Each entry is one *observation*: a signal followed to its outcome,
measured in **R** (1R = the amount that trade risked).

Observations come from four places and are kept apart:

| Source | What it is | When |
|---|---|---|
| **training** | Every strategy replayed over the last `knowledge.history_days` (60) days of real data — exactly the way the running bot follows ideas, so it measures the same thing | Menu **5 (train)**, `topstep-bot train`, the dashboard's **Retrain now**, Telegram `/train`, and automatically at startup when the last training is older than `retrain_hours` (20) — so normally once a day after the 16:05 CT restart |
| **live ideas** | Hypothetical outcomes of signals the bot saw while running but did not trade (shadow strategies, skipped signals) | Continuously while the bot runs |
| **real trades** | The bot's own closed trades (count double) | Continuously while the bot runs |
| **manual trades** | Trades you opened from the dashboard's Trade tab (count double, shown as *Your manual trades*; they never switch a strategy on or off) | When each one closes |

Because every signal is followed whether it was traded or not, **the bot never has to try a bad idea
to learn it is bad.** Older observations fade out (half their weight after `half_life_days`, 20), so
the base follows the market as it changes. Retraining replaces the training layer and drops live ideas
the new training already covers, so nothing is counted twice; real and manual trades are never dropped.

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

### What the bot learned: results after costs, real fills and conditions

Besides its result, every observation keeps what the bot may want to know later. None of this
changes a trading decision yet; it is recorded now because a real fill can never be recorded again.

| Recorded | What it is |
|---|---|
| **Market snapshot** | At the moment of the signal: volatility, minutes since the open, the opening gap, the move since the open, how much of a normal day's range is used, the place in today's range, the distance from VWAP, the trend, volume and the day of the week. Distances are in *average day ranges* (the last 10 days' regular-hours high-low), so they compare across quiet and busy days. |
| **Price path** | How far it went in its favour (best point, *MFE*) and against it (worst point, *MAE*) before it ended, in R, and how many bars it lasted |
| **Costs** | Fees in R, plus for ideas that weren't traded the slippage your backtests assume (`risk.slippage_ticks` per fill). Real fills already include their slippage. |
| **Fill quality** | For real and manual trades: how many ticks the entry and the exit slipped against the price the bot expected (positive = a worse price) |

The report built from it:

- **Dashboard → Knowledge tab → What the bot has learned:** each strategy's average result before
  and after costs, its best and worst points, the share of losers that were 1R in profit first (a
  sign a breakeven stop or target might help), and real trades against simulated ones. Below that,
  real fill slippage against what backtests assume, *conditions worth testing*, and a **Compare by**
  table that splits every strategy's results by one measurement (low / middle / high, or by weekday).
  **Download CSV** saves every observation for Excel.
- **Telegram:** `/knowledge` ends with a short version.
- **Menu 17** or **`topstep-bot insights`** prints it; `topstep-bot insights --csv knowledge.csv` exports.

Read the conditions as **hints, not rules.** The bot compares 7 strategies on 10 measurements, so
some differences are luck. A hint is only listed when both sides have at least 15 observations and
the result flips from losing to winning. A condition earns a place in the bot's decisions only after
it holds up on data it never saw, which is the planned next step.
Directional measurements are turned around for shorts, so "high" always means "further in the
trade's direction". If real fills slip more than `risk.slippage_ticks`, raise it so backtests
stay honest.

Knowledge files and journals from older versions keep working; their older observations simply
have no snapshot, path or costs (and count before costs).

### What the bot knows, and when it trades next

Ask the bot directly:

- **Dashboard → Knowledge tab → What the bot knows** (or `/brief` in Telegram) answers in plain
  sentences: how many signal outcomes it knows and where they came from, how big its long-run memory
  is, what it would trade *right now* and what it is staying out of (and why), which strategies have
  worked after fees and slippage (recent next to the long run), the lessons worth testing, and its
  next-trade estimate.
- **Next trade** — a countdown tile in the market clock at the top of the dashboard, a **Next trade**
  panel on the Overview tab, `/next` in Telegram, and a line at the end of `/status`.

How the next-trade estimate works, so you know how far to trust it:

1. **The rules first.** It walks the same rules the bot trades by (entry window, trading days and
   holidays, blackout windows and news, the daily trade count, losing streak and cooldown, profit and
   loss locks) to the first moment an entry is allowed. If the bot is paused, halted or done until
   you act, it says so instead of guessing.
2. **Then history.** On each of the last 90 days in the knowledge base, it finds the first signal
   the bot would have taken *with what it knows today* (for the adaptive strategy: only strategies the
   knowledge base allows at that time of day and regime). Starting from the first allowed moment, those
   days give the **most likely time** (half of past days had their signal sooner), the **usual range**
   (the middle half of days) and the **chance of a trade today**. Days with nothing left today carry
   the wait over to the next trading days.
3. **Then the setups forming now.** It names the auto-traded setup closest to firing, what it still
   waits for and when it can fire (a checkpoint or decision time, or the next bar close).

It needs at least 10 trading days of history. It is an estimate, not a promise: a signal can still be
skipped by a risk check, markets change, and some days have no trade at all.

### The long-run memory: a much bigger knowledge base

The knowledge base the bot trades with looks at the last two months on purpose: markets change, and
old evidence fades out. Next to it, the bot keeps a far bigger **long-run memory**, and it is hungry:

- **Every price bar it ever sees is kept** in `data/market_library.sqlite` on your PC: warm-up bars,
  training downloads, every live bar and everything it backfills. Nothing is thrown away.
- **Every day it backfills** what it is still missing, up to `knowledge.deep_history_days` back
  (365 by default, up to 3650; the "Teach the bot" setup uses 730). It only asks TopstepX for ranges it
  has never downloaded, so after the first time it is a small top-up.
- **Every day it replays all of it** through every strategy, the same way training replays the last
  60 days, into `data/knowledge_<SYMBOL>_<TF>m_longrun.json`: every signal every strategy would have
  given on every day in the library, with its outcome, costs, price path and market snapshot. A year
  of 5-minute bars takes about a minute.
- It runs once a day **outside the bot's entry window and only while flat**, so trading never waits on
  it (normally right after the 16:05 CT restart). Run it any time with **Learn from history now** on the
  Knowledge tab, `/learn`, menu **18** or `topstep-bot learn`.
- Have older data? `topstep-bot learn --import mydata.csv` adds a CSV of bars (1-minute or your
  bot's timeframe) to the library, then replays. `--offline` replays without downloading; `--days 730`
  reaches further back for that run.

**What it changes:** what the bot can tell you. The Knowledge tab's **What the bot has learned** has a
**Recent / Long run** switch, the briefing compares each strategy's recent results with the long run
(a strategy that is hot lately but weak over a year deserves suspicion), and conditions worth testing
get far more data. `topstep-bot insights --longrun` prints the long-run report.

**What it doesn't change:** how the bot trades. The adaptive strategy still decides from the recent
knowledge base, and every risk rule, limit and position size stays exactly as configured. Using the
long run in decisions is a later, separately tested step (the roadmap's "decide on evidence" phase).

How far back TopstepX serves history depends on the contract; the library simply keeps whatever it
gets and grows every day the bot runs. Prices jump when the front-month contract rolls; the strategies
reset every day, so that only touches a measurement or two on the first day after a roll.

### Honest expectations

Training on the last two months of M2K showed most strategies **losing** in most slots, with a thin
positive edge only for the EMA trend strategy at midday. That is a feature, not a bug: the adaptive
strategy then trades little, and only where there is evidence. Evidence from 60 days is still
statistical noise to a large degree; the knowledge base reduces the damage from a strategy that has
stopped working, it does not guarantee profits. Backtests of the adaptive strategy are **walk-forward**:
it starts knowing nothing and learns as the data plays (no peeking), so the first weeks of a backtest
show few or no trades.

### Using a Combine to teach the bot

If an account is for teaching the bot rather than passing, choose **"Teach the bot"** when setup asks
what the account is for. That writes a learning configuration:

| Setting | Learning | Passing (default) | Why |
|---|---|---|---|
| `strategy` | `adaptive` with `trade_unproven: true` | your choice | Also trades strategies the bot has no evidence on yet, so it gets real fills for them. |
| `risk_per_trade` (50K) | $100 (5% of the MLL) | $150 | Smaller trades, so more of them fit before a daily or account limit. |
| `max_trades_per_day` | 8 | 4 | More real outcomes per day. |
| `max_consecutive_losses` | 4 | 2 | Doesn't stop after two losses. |
| `cooldown_minutes_after_loss` | 5 | 10 | |
| `daily_profit_target` | the full profit target | 40% of it | No early stop on a big day. |
| `consistency_guard` | off | on | A big day only raises the target; that doesn't matter on a learning account. |

What stays the same: **every Topstep rule** (MLL, any DLL, position limits, news, flat by 15:10) and
your personal daily loss limit. Breaking a Topstep rule or touching the MLL ends the Combine, and
that ends the learning until you reset it, so the bot still protects the account from that.

Good to know:

- The bot learns from **every** strategy's signals, traded or not. Each idea is followed to its
  outcome and added to the knowledge base, so it learns from the whole market every day, not only
  from the trades it takes. Real trades count twice as much (`knowledge.real_trade_weight`), because
  they include real fills and slippage.
- The knowledge base (`data/knowledge_<SYMBOL>_<TF>m.json`) is shared by paper and live and by
  every account on the same symbol and timeframe. What it learns on the learning Combine carries
  over when you later point the bot at an account you want to pass.
- Watch what it's learning on the dashboard's **Knowledge** tab or with `/knowledge` on Telegram.
- You can teach it with your own trades too: trades you place from the dashboard's **Trade** tab are
  recorded as *manual* observations, so the ticket can show you where your own trades work
  ([Trading manually from the dashboard](#trading-manually-from-the-dashboard)).
- When you move to an account you want to pass, run setup again and choose **"Pass the Combine"**.

Settings (`knowledge:` in `config.yaml`): `enabled`, `auto_train`, `history_days`, `retrain_hours`,
`half_life_days`, `min_samples`, `min_edge_r`, `real_trade_weight`, `deep_learning`, `deep_history_days`. The file is
`data/knowledge_<SYMBOL>_<TF>m.json`, shared by paper and live; delete it to start from scratch.

---

## 18. Tuning: test strategy settings on unseen data

Tuning finds which single strategy, with which settings, has held up — for when you'd rather run
one strategy than let `adaptive` choose. It is deliberately strict, because tuning settings until a
backtest looks perfect is the classic way to build a strategy that loses money live: with enough
combinations, something always looks great on the past by luck.

### How it works

1. Every candidate — a strategy plus one combination of its settings (about 140 in total) — is
   backtested over the whole history using the bot's real trading code, your risk settings and
   realistic fills and fees.
2. The history is split into rolling **windows**. In each one, the bot *chooses* the best settings
   for each strategy using only the **training** part, then *scores* them on the **test** part that
   comes right after — data the choice never saw. Then it rolls forward and repeats.
3. Only those out-of-sample (test) results count. A strategy is recommended only if it made money
   on them with a profit factor of at least 1.05 and enough trades to judge. The settings it
   recommends are the ones chosen on the most recent window.

One honest caveat: picking the best of several strategies by their out-of-sample results flatters
the winner a little, so expect live results to be somewhat below the report's numbers even when
nothing else changes.

### Run it

Menu **15**, or:

```bash
topstep-bot tune
```

| Option | Meaning |
|---|---|
| `--days 730` | Download more history (default 365). More history gives more reliable results. |
| `--strategies orb_momentum,noise_breakout` | Test only these strategies. |
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

Your current strategy is always included unchanged, so you can see how it compares — with
`adaptive` that means a walk-forward run of it alongside the single strategies (it isn't tuned
itself; it uses the others' default settings). `vwap_pullback` is tested with its defaults only.

When you save, only the `strategy:` block of `config.yaml` changes (a tuning note is added
above it) — which switches the bot from `adaptive` to that one strategy; everything else,
including your comments, stays as it was, and the previous file is kept as `config.yaml.bak`.
Re-tune every month or two, and after big changes in the market.

> Tuning on random demo data (`--synthetic`) only shows how the process works; its result is
> never saved.

---

## 19. Changing settings from the dashboard or Telegram

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

## 20. Running 24/7

Topstep requires automated trading to run **on your own computer** — not a VPS. Starting the bot
(menu 6/7, `start.bat start`, or menu 2 after the preflight) already runs it around the clock: the
**controller** (the dashboard and Telegram) runs the trading bot as a separate program and

- **restarts it after a crash**, waiting a little longer after each one (10 s, 30 s, 1 min, ...),
  and gives up — with an alert — after `service.max_restarts_per_hour` crashes in an hour;
- **restarts it if it hangs**: the bot writes a heartbeat, and if it goes quiet for
  `service.heartbeat_timeout_seconds` (default 3 minutes) the controller restarts it. Protective stops
  stay at TopstepX meanwhile, and the restarted bot re-adopts the position;
- **restarts it every day at 16:05 CT**, during the CME maintenance halt, so contract rolls, login
  tokens and connections are always fresh (only when flat; quietly — no "bot started" alert);
- **leaves a bot you stopped on purpose stopped** until you start it again — from the dashboard or
  Telegram, even remotely;
- sends Telegram/Discord alerts about crashes and restarts.

While it runs it also:

- **keeps the PC awake** (`service.keep_awake`) — the same request a video player makes; it ends
  when the program exits. A closed laptop lid, the power button or a Windows Update restart still
  stop the PC: set Windows Update's *active hours* to cover the trading day;
- **turns off the console's QuickEdit mode**. With QuickEdit on (the Windows default), one click
  inside the bot's window freezes the whole program until you press a key;
- sends a **good-morning check-in** at 08:00 CT on weekdays (`service.check_in_time`) with the
  balance and MLL room, so silence tells you something is wrong.

To start everything automatically whenever you sign in to Windows, choose **autostart** (menu 13),
or run `topstep-bot autostart on`. It adds a small script to your personal Startup folder (no
administrator rights needed) that starts the bot about 30 seconds after you sign in, minimized, in
the mode you used last (Paper or Live). Check with `topstep-bot autostart status`; remove with
`topstep-bot autostart off`.

---

## 21. Starting today: preflight, ramp-up and news

Menu **2 (go-live)** is the same-day start. It runs the **preflight check**, which you can also run
alone with `start.bat preflight`. The check verifies:

- your login, the selected account and that it is allowed to trade;
- that the account is flat;
- the Maximum Loss Limit — it asks you for the value on your Topstep dashboard if it can't work it out;
- any Topstep Daily Loss Limit on the account, the Consistency Target and today's position limit;
- that the account is not a Live Funded account (Topstep doesn't allow API trading on those) and
  that the PC doesn't look like a VPS, virtual machine or Remote Desktop session;
- the contract, live market data and your PC clock;
- today's trading calendar and upcoming news;
- your risk settings and Telegram;
- a backtest of every strategy on the most recent real data.

If nothing fails, you type `LIVE`, then choose 24/7 or a single session.

Built-in protection for a new account:

- **Ramp-up:** the first 3 live trading days on an account risk 50% of your normal amount
  (`risk.ramp_up_days`, `risk.ramp_up_risk_fraction`). Only days on which the bot actually traded
  count.
- **News pause:** no new trades from 5 minutes before to 10 minutes after high-impact US economic
  releases, using this week's economic calendar (`news.*`) — see below.
- **Price-capped entries:** entries can't fill more than `execution.max_entry_slippage_ticks`
  (default 8) worse than the signal price, and trades are sized for that worst case. If a fill still
  leaves a trade too risky, it is closed immediately.

### News blackouts

The bot downloads this week's economic calendar (the free Forex Factory feed) and blocks new
entries from 5 minutes before to 10 minutes after every **high-impact USD** release — CPI, jobs
reports, FOMC and so on. The dashboard and Telegram show the reason (`news blackout: USD CPI m/m at
07:30 CT`), and at start-up the bot lists the blackouts in the next 24 hours.

- The calendar is refreshed every 6 hours and cached in `data/news_cache.json`, so a brief outage
  doesn't matter. If it can't be loaded at all, the bot keeps trading at no more than half of
  Topstep's maximum position size and the preflight warns you — add `session.blackout_windows` for
  big releases by hand in that case.
- Topstep prohibits taking your maximum position size into a scheduled major release. In the 30
  minutes before one, new trades use at most half of Topstep's limit, and a position at the full
  limit is closed just before the release.
- A smaller open trade keeps its stop and target through the release. To close every trade
  beforehand instead, set `news.flatten_before: true`.
- Change the window with `news.minutes_before` / `news.minutes_after`; include medium-impact events
  with `news.impacts: [High, Medium]`; turn it off with `news.enabled: false`.

Backtests, training and tuning don't apply news blackouts (there is no historical calendar), so live
results around news days can differ a little from them.

---

## 22. Logs: finding out what happened

Everything is recorded in the `logs` folder next to `config.yaml`, wherever you start the bot from:

| File | What's in it |
|---|---|
| `bot.log` | Everything, one file per day (kept 30 days, `log_retention_days`) |
| `errors.log` | Only warnings and errors — **look here first** |
| `events.jsonl` | Every trade, risk event, setting change and recommendation, one JSON object per line |
| `controller.log` | The controller: bot starts, stops, crashes, restarts, mode switches, Telegram |
| `commands.log` | Other commands (setup, backtest, train, tune, ...) — kept apart so they never collide with a running bot |
| `crash_*.txt` | Full details of any crash that closes the program |
| `faults.log` | Low-level hang/crash dumps from Python |

- When the bot stops, the log records **why**: Stop from Telegram/dashboard, Ctrl+C, daily
  maintenance restart, an internal error, or a crash with the full error.
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

## 23. Daily routine

**Running 24/7 (recommended):** nothing to do before the open. Glance at the 08:00 CT check-in on
your phone; if it doesn't arrive, look at the PC.

**Starting by hand:** start the bot (menu 2, 6 or 7) before 8:15 CT. Check the dashboard shows
"connected", the right account and contract, and "Trading normally".

**During the session:** glance at the dashboard (or send `/status` on Telegram) and TopstepX now and
then. Act on any alert.

**After 15:10 CT:** confirm the position is flat. Review the day with menu **9 (journal)** and the
Knowledge tab (or menu **17**, insights).

**Weekly:** compare the bot's MLL floor and position limit with your TopstepX dashboard and Risk
Settings (Topstep changes product limits with market conditions); look at the Knowledge tab to see
which strategies have stopped (or started) working.

**Every month or two:** if you trade a single strategy, re-run **tune** (menu 15) on fresh data.

**Contract roll:** the bot picks the active front-month contract each time it starts; the daily
maintenance restart picks up rolls automatically. If you pinned `instrument.contract_id`, update it.

---

## 24. Full configuration reference

`config.yaml` only needs the settings you want to change; everything else uses these defaults.
Misspelled settings are rejected with a clear message, so typos can't silently do nothing.

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
  topstep_daily_loss_limit: null  # true = your plan's optional DLL ($1,000/$2,000/$3,000), or a dollar amount
  payout_path: standard        # Express Funded payout path: standard | consistency

instrument:
  symbol: MNQ
  contract_id: null            # pin an exact contract, e.g. CON.F.US.MNQ.Z26
  timeframe_minutes: 5         # 1-60

strategy:
  name: adaptive               # adaptive | noise_breakout | orb_momentum | orb | ema_trend
                               # | late_day_momentum | vwap_reversion | vwap_pullback
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
  deep_learning: true          # long-run memory: keep every bar, backfill and replay it all daily (reports only)
  deep_history_days: 365       # 30-3650: how far back the long-run memory reaches

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
  consistency_guard: true              # combine: close out at 50% of the profit target in a day
  stop_at_profit_target: true          # combine: stop trading once the profit target is reached

session:
  timezone: America/Chicago
  trade_start: "08:30"
  last_entry: "14:30"
  flatten_at: "15:00"
  trade_weekdays: [0, 1, 2, 3, 4]      # Monday=0
  blackout_windows: []                 # e.g. [{start: "13:00", end: "13:45", label: "FOMC"}]
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
  flatten_before: false        # true = also close open trades just before a release

service:                       # used by the controller ('start')
  keep_awake: true
  daily_restart_time: "16:05"  # CT, during the CME daily halt; off = never
  check_in_time: "08:00"       # weekday "bot is alive" message; off = never
  heartbeat_timeout_seconds: 180
  max_restarts_per_hour: 6

dashboard:
  enabled: true
  host: 127.0.0.1
  port: 8765
  open_browser: true

backtest:
  data_file: null
  report_dir: reports          # backtest and tuning reports

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

## 25. Command reference

Run any command with `--help` for its options. Add `-c other.yaml` before the command to use a
different config file.

| Command | Purpose |
|---|---|
| `topstep-bot` | Interactive menu |
| `topstep-bot setup` | Setup wizard |
| `topstep-bot check` | Test login, list accounts, show the contract |
| `topstep-bot strategies` | Describe strategies and parameters |
| `topstep-bot rules` | Topstep's rules for your account and how the bot enforces each one |
| `topstep-bot backtest [--data F] [--download] [--days N] [--strategy S] [--symbol X] [--timeframe M] [--tz TZ] [--no-open]` | Backtest and open a report |
| `topstep-bot demo` | Backtest on synthetic data |
| `topstep-bot train [--data F] [--days N] [--tz TZ]` | Teach the bot which strategy works at which time of day from recent real data ([section 17](#17-training-and-the-knowledge-base-how-the-bot-learns)) |
| `topstep-bot tune [--days N] [--strategies A,B] [--folds K] [--save\|--no-save] [--data F]` | Walk-forward test of single strategies and their settings ([section 18](#18-tuning-test-strategy-settings-on-unseen-data)) |
| `topstep-bot download [--days N] [--tf M]` | Save history to `data/` |
| `topstep-bot start [--mode paper\|live] [--yes] [--no-bot] [--no-browser]` | Start the dashboard + Telegram, which run the bot 24/7 (`service` does the same) |
| `topstep-bot run [--mode paper\|live] [--yes]` | Run only the trading bot, without dashboard (normally started for you by `start`) |
| `topstep-bot flatten [--yes]` | Emergency: cancel all orders, close all positions |
| `topstep-bot journal [--mode paper\|live] [--limit N]` | Recent trades and daily results |
| `topstep-bot telegram-test` | Check the Telegram token and chat ID with a test message |
| `topstep-bot preflight [--days N] [--skip-backtest]` | Check everything before trading live |
| `topstep-bot go-live [--days N] [--skip-backtest]` | Preflight, then start live trading (24/7 or this session) |
| `topstep-bot autostart on\|off\|status` | Start everything when you sign in to Windows |
| `topstep-bot logs [--all] [--open] [--bundle]` | Recent errors, open the log folder, or zip logs for support |
| `topstep-bot learn [--days N] [--import CSV] [--offline]` | Grow the long-run memory: download the history it's missing, add a CSV, then replay all of it through every strategy ([section 17](#the-long-run-memory-a-much-bigger-knowledge-base)) |
| `topstep-bot insights [--csv FILE] [--longrun]` | What the bot has learned: results after costs, real fills, market conditions; or export every observation to CSV ([section 17](#what-the-bot-learned-results-after-costs-real-fills-and-conditions)) |

On Windows you can also pass commands through the launcher, e.g. `start.bat tune --days 730`.

---

## 26. Files the bot creates

| Path | Contents |
|---|---|
| `config.yaml` | Your settings (`config.yaml.bak`: the version before `tune` last saved settings) |
| `.env` | Your API credentials, Telegram token and webhook URLs — **private** |
| `data/journal_paper.db`, `data/journal_live.db` | Trade journal, daily results, MLL floor, paper balance |
| `data/<SYMBOL>_1m.csv` | Downloaded history |
| `reports/*.html` | Backtest and tuning reports (tuning also writes a `.json` with every detail) |
| `logs/` | Log files — see [section 22](#22-logs-finding-out-what-happened) |
| `data/remote_settings.json` | Settings changed from the dashboard/Telegram (delete it, or `/reset`, to undo) |
| `data/knowledge_<SYMBOL>_<TF>m.json` | The knowledge base: what works when, with each observation's market snapshot, price path and costs (delete it to start learning from scratch) |
| `data/market_library.sqlite` | The long-run memory's market library: every price bar the bot has downloaded or imported |
| `data/knowledge_<SYMBOL>_<TF>m_longrun.json` | What every strategy did on the whole library (reports only; rebuilt daily, safe to delete) |
| `data/news_cache.json` | This week's economic calendar |
| `data/controller.json` | The mode you chose last (Paper/Live) |
| `data/bot_exit.json` | Why the bot last exited (shown on the dashboard) |
| `data/heartbeat` | "Still alive" signal the controller watches |

---

## 27. Troubleshooting

**"Login failed"** — Use your TopstepX *username*, not your email. Copy the API key again in full.
Check your API subscription is active. Re-run setup or edit `.env`.

**"Several accounts can trade"** — Run setup and pick one, or set `account.account_id`
(`topstep-bot check` lists the IDs).

**"No contract found for symbol"** — Check the symbol spelling, or pin `instrument.contract_id`.

**"Configuration problem"** — The message names the exact setting and what's wrong with it (e.g.
`risk.risk_per_trad: unknown setting`). Fix it in `config.yaml` with Notepad; indentation (spaces)
matters in YAML.

**The bot never trades.** With the `adaptive` strategy, first open the Knowledge tab: if it is not
trained yet, press **Retrain now** (or run menu 5); if every cell is ✘ or ?, no strategy has proven
itself lately and the bot is right to wait. Otherwise look at the dashboard's Activity list and
`bot.log` — skipped signals are logged with a reason (outside the entry window, news blackout, daily
limit, stop too wide, 1 contract too risky, "skipped: unproven", ...). Some strategies trade rarely
(`orb` and `orb_momentum` at most once a day; `noise_breakout` needs ~15 days of history). Check
today isn't in `no_trade_dates`.

**"... must be below your Topstep Daily Loss Limit / the Maximum Loss Limit"** (when loading the
config) — your `risk.personal_daily_loss_limit` has to be lower than Topstep's limits, so the bot's
own limit always fires first. Lower it in `config.yaml`.

**"Topstep does not allow Live Funded Accounts to trade through the TopstepX API"** — the selected
account is a real-money Live account. That's Topstep's rule; pick a Combine, Express Funded or
Practice account in setup.

**"Combine profit target reached - trading stopped"** — congratulations. Topstep can take up to
30 minutes to update the account. The bot won't trade it again; point it at your next account.

**"near the Consistency Target - done for the day"** — the day is up 50% of the profit target; one
more good trade would raise the Combine target. Trading resumes the next session.

**"ORDER GUARD: ... order actions in one minute"** — something sent orders far faster than any
strategy should. The bot closed out and halted. Check `bot.log` for what happened, then restart it.

**Preflight warns "this computer looks like a virtual machine or cloud server"** — Topstep only
allows trading from your own personal computer. If this is your own PC (some laptops report a
virtualised firmware), you can ignore it.

**"Skipped: 1 contract would risk more than the allowed budget"** — The stop is too far for your
`risk_per_trade`. Raise the risk, trade a micro, or use a tighter stop setting.

**Tuning says no strategy held up** — That is a real answer: on that data, nothing beat the
costs on days it hadn't seen. Try more history (`--days 730`) or another symbol, and don't go live
on single-strategy settings from it.

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
maintenance restart, an internal error, or a crash (with a `crash_*.txt` file). If the controller
keeps restarting it, `logs/controller.log` says why (crash, no heartbeat); after
`service.max_restarts_per_hour` crashes it gives up and alerts you.

**The bot froze until I pressed a key** — Windows' console QuickEdit mode. The bot turns it off
while it runs; if it still happens (e.g. an old version), right-click the window's title bar →
Properties → untick *QuickEdit Mode*.

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

## 28. Writing your own strategy

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
  stop, `state()` to show values on the dashboard, `setups(price, now)` to list the entries it's
  building toward for the dashboard's Getting ready to trade section (see `orb.py` for an example),
  `warmup_days` if it needs more history.
- Once registered, the `adaptive` strategy runs it too and the knowledge base starts tracking it.
- To let `tune` tune it, add a list of candidate settings for it to `GRIDS` in
  `topstep_bot/training.py` — keep it small (a dozen or two combinations).

Backtest it with `topstep-bot backtest --strategy my_strategy`, and run the test suite with
`pytest` after any change to the bot itself.

---

*Trading futures involves substantial risk of loss and is not suitable for everyone. This software
is provided as-is, with no guarantee of profit or of compliance with Topstep's rules, which can
change. You are responsible for every order placed on your account.*
