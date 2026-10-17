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
2. Get a TopstepX API key ([section 4](#4-get-your-topstepx-api-key)).
3. Double-click **`start.bat`**. The first run installs everything (about a minute). Then the
   dashboard opens in your browser by itself. Leave the black window open: it *is* the bot.
4. The dashboard opens on the **Setup** tab. Fill it in from top to bottom and press **Save**
   ([section 5](#5-set-up-on-the-dashboard)). Every field already has a sensible suggestion. If the
   account is just for teaching the bot, choose **"Teach the bot"**
   ([section 17](#using-a-combine-to-teach-the-bot)).
5. Press **Start** with the top switch on **Paper**: watch it trade with simulated orders for a few
   days.
6. When you're ready for real orders, stop the bot, flip the switch to **Live** (type `LIVE`) and
   press **Start**. For the full check first, use **go-live** in the text menu
   ([section 7](#7-the-path-to-live-trading)).
7. Keep the PC on. Watch it on the dashboard (http://127.0.0.1:8765) or Telegram (`/status`).
   `/flatten` closes everything, a confirmed `/stop` stops trading, `/startbot` starts it again.

Optional extras like training, backtests and tuning are in the text menu: `start.bat menu`
([section 6](#6-the-text-menu-optional)).

**New in 2.0:** a redesigned dashboard, the **Simulations** tab, which replays the bot through history
as hundreds of fresh Combines ([section 34](#34-simulation-lab-hundreds-of-combines-from-history)), and the
**AI Coach**, where Claude reviews the evidence, tests ideas and proposes changes you approve
([section 35](#35-the-ai-coach-claude-teaches-the-bot)).

**New in 2.1:** every market Topstep permits (51: indexes, crypto, currencies, energy, metals, interest
rates, grains and livestock). The bot downloads all of their history with no login needed, the
Simulation Lab runs on all of them and lists **Opportunities** by time of day
([section 36](#36-every-market-topstep-permits)), the bot can pick its market each morning from all of
them, and you can switch an account to any market for the rest of the day from the trade ticket
([section 37](#37-trading-another-market-today)).

**New in 2.2:** ask the bot why. The dashboard's **Ask** tab (or just typing to the bot in Telegram)
explains why it took or skipped every trade, what each trade taught it, and which decision rule it
trusts, from its own records ([section 38](#38-the-central-intelligence-ask-the-bot-why)). The AI
coach and the Ask tab's optional AI answers now share one Claude API key and one monthly budget.

**New in 3.0, the first release:**

- **Make money every day.** A third goal on the Setup tab, next to *Pass the Combine* and *Teach the
  bot*: the bot trades every signal its strategies find, up to 12 a day, presses a little harder on a
  good day and stops at a daily profit goal ([section 39](#39-make-money-every-day-the-aggressive-mode)).
- **Two risk gaps closed, in every goal.** No single trade can carry a Combine day past the
  consistency line any more, and every loss limit now allows for a sudden jump past the stop
  ([section 39](#two-protections-that-work-in-every-goal)).
- **The market network.** Every market Topstep permits, read together: which move together, which
  are stressed, what is scheduled, how far each can jump. On Knowledge → Market data, in Telegram (`/network`)
  and to the AI ([section 40](#40-the-market-network)).
- **One Claude integration.** The AI coach and the Ask answers share one key, one model list with
  prices, one spending log and the same plain-words errors.

**New in 4.0:**

- **Several markets at once, switched without a restart.** One account can trade up to six markets at
  the same time. Add, drop or switch them from the markets strip at the top of the dashboard or in
  Telegram (`/markets`, `/add`, `/drop`, `/switch`); an open trade keeps its stop and target, and every
  Topstep limit counts all the markets together ([section 41](#41-several-markets-at-once)).
- **A probability calculator.** Every signal gets its chance of winning, its average win and loss and
  its expected value after costs, with an 80% range. The bot skips a signal it is at least 90% sure
  loses, and *Make money every day* sizes each trade by the strength of its edge
  ([section 42](#42-the-probability-calculator)).
- **A knowledge network that grows by itself.** Related markets inform each other's odds, and each
  night the bot learns a few more related markets from the history saved on your PC
  ([section 43](#43-the-knowledge-network)).
- **A more aggressive *Make money every day*.** Each strategy may trade three times its usual count,
  each market brings its own 12 trades a day, and a large, well-measured edge may risk up to 1.5 times
  the usual amount ([section 39](#39-make-money-every-day-the-aggressive-mode)).
- **Charts of the current trade** on the Overview and the trade ticket ([section 44](#44-the-trade-chart)).
- **A simpler dashboard.** Seven tabs instead of eleven: **Ask** explains anything in the bot, and
  **Knowledge** puts real trades, simulations and market data in one place
  ([section 45](#45-the-simpler-dashboard)).

**New in 4.1:** an **AI trading assistant**. A Claude model watches the market with the bot and, if you
switch it on, manages open trades (tighter stop, breakeven, early close) and places a few small trades
of its own. Each part has its own switch per account, both start off, every action goes through the
same risk rules and Topstep rules as the bot's own trades, and every decision is recorded with why
([section 46](#46-the-ai-trading-assistant)).

**New in 4.2:** the bot **reads the chart for trends** (up, down or sideways, how clear and how strong,
drawn on the trade chart) and **plans each day with you**. Each trading morning it asks for every
account's profit goal on Telegram and on the Overview tab, then lays out a route to it: which part of the
day usually brings its trades, how often its own past days reached a goal like that, and when the day
stops. The goal never makes a trade bigger; it only tells the bot when the day is done. A filter that
skips signals against a strong trend turns on by itself only once a test on the bot's own history shows
it helps ([section 47](#47-trends-and-the-daily-plan)).

---

## Contents

1. [What the bot does (and doesn't do)](#1-what-the-bot-does-and-doesnt-do)
2. [What you need](#2-what-you-need)
3. [Install](#3-install)
4. [Get your TopstepX API key](#4-get-your-topstepx-api-key)
5. [Set up on the dashboard](#5-set-up-on-the-dashboard)
6. [The text menu (optional)](#6-the-text-menu-optional)
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
29. [Trading several accounts](#29-trading-several-accounts)
30. [Several API keys](#30-several-api-keys)
31. [What the bot knows about the market and the news](#31-what-the-bot-knows-about-the-market-and-the-news)
32. [Funded-account goals and Topstep's rules](#32-funded-account-goals-and-topsteps-rules)
33. [Research: knowledge that grows from the internet](#33-research-knowledge-that-grows-from-the-internet)
34. [Simulation Lab: hundreds of Combines from history](#34-simulation-lab-hundreds-of-combines-from-history)
35. [The AI coach: Claude teaches the bot](#35-the-ai-coach-claude-teaches-the-bot)
36. [Every market Topstep permits](#36-every-market-topstep-permits)
37. [Trading another market today](#37-trading-another-market-today)
38. [The central intelligence: ask the bot why](#38-the-central-intelligence-ask-the-bot-why)
39. [Make money every day: the aggressive mode](#39-make-money-every-day-the-aggressive-mode)
40. [The market network](#40-the-market-network)
41. [Several markets at once](#41-several-markets-at-once)
42. [The probability calculator](#42-the-probability-calculator)
43. [The knowledge network](#43-the-knowledge-network)
44. [The trade chart](#44-the-trade-chart)
45. [The simpler dashboard](#45-the-simpler-dashboard)
46. [The AI trading assistant](#46-the-ai-trading-assistant)
47. [Trends and the daily plan](#47-trends-and-the-daily-plan)

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
   After that it starts in a few seconds.
4. The **dashboard** opens in your browser at http://127.0.0.1:8765. That's where you set
   everything up and start and stop the bot. The black window is the bot's engine: keep it open
   (you can minimize it). Closing it stops everything.

Double-clicking `start.bat` again while it's already running just opens the dashboard again.
Nothing is started twice.

### Manual (any OS)

```bash
python -m venv .venv
```

Then activate it (`.venv\Scripts\activate` on Windows, `source .venv/bin/activate` on macOS/Linux) and install:

```bash
pip install -e .
```

Now `topstep-bot` is available as a command. Running it with no arguments starts the dashboard
(`topstep-bot menu` opens the text menu).

### Updating

The bot checks GitHub for a newer version by itself and tells you about it. **Nothing is installed
until you say so**, and never while a trade or order is open.

#### One-time setup

**Nothing to set up.** The bot's GitHub repository is public, so the bot can look for updates
whether you cloned it with git or downloaded it as a ZIP. It only reads from GitHub: nobody can
send your bot an update except through the repository's `main` branch, which only its owner can change.

<details>
<summary>Only if the repository is ever made private again: give the bot a read-only GitHub token</summary>

A ZIP copy of a private repository needs a GitHub token that can only **read** it. When the bot
can't see the repository, the dashboard's **Settings** tab (Updates) shows a box to paste one into
(or run `start.bat update --token`).

1. Open <https://github.com/settings/personal-access-tokens/new> (signed in to GitHub).
2. Token name: `Topstep Bot updates`. Expiration: 1 year.
3. Repository access: **Only select repositories** → `TSTradeBot`.
4. Permissions → Repository permissions → **Contents: Read-only**. Leave everything else alone.
5. **Generate token**, copy it (it starts with `github_pat_`) and paste it into the bot. It is
   tested right away and saved in `.env` as `GITHUB_TOKEN`.

When the token expires, the Updates panel says "GitHub rejected the token": make a new one the same
way. A **git** clone of a private repository uses your git sign-in instead (run `git pull` once in
the folder if git has never signed in).

</details>

#### How you hear about a new version

- **Dashboard:** a green **Update available** banner with the newest change, and the **Settings** tab
  → **Updates** panel listing every change (one line per change, newest first). **Check now** asks
  GitHub straight away.
- **Telegram:** one message per new version, with an **Update options** button. `/update` checks any time.
- **Menu:** a line above the options says an update is waiting; **18 (update)** shows the changes.

The bot checks every 6 hours (`updates.check_every_hours`). Your own trades, settings and data are
not sent anywhere: the bot only asks GitHub what is new.

#### Installing an update

Pick one:

- **Dashboard:** Settings tab → Updates → **Install now** or **Install after the close**.
- **Telegram:** `/update` → **Install now** or **After the close** (during trading hours).
- **Menu 18** on the PC, while the bot is not running (if it is running, the menu sends you to the
  dashboard or Telegram, which restart it for you).

What happens when you install:

1. The bot pauses new trades and checks that **no trade or working order is open** — in live mode
   it also asks TopstepX itself. If one is, nothing changes and it tells you why. Close the trade
   (or let it finish) and try again, or choose **after the close**.
2. It stops the trading bot, downloads only the files that changed (or does a `git` fast-forward)
   and installs any new Python packages the new version needs.
3. It test-starts the new version with your `config.yaml`.
4. The dashboard, Telegram and the bot restart with the new version, in the same mode (Paper or
   Live); the bot only if it was running. This takes about 30 seconds. The dashboard reloads by
   itself, and Telegram says `✅ Topstep Bot updated to …` with what changed.

**After the close** waits for the first moment outside the trading hours (from 15 minutes before
`session.trade_start` until 10 minutes after `session.flatten_at` on trading days) when the bot is
flat, then installs the same way. Cancel it on the Settings tab before then if you change your mind.

An update never touches your `config.yaml`, `.env`, `data/` (journal, knowledge base, remote
settings), `logs/` or `reports/`.

#### If something goes wrong

- If a new package can't be installed, or the new version doesn't start or rejects your
  `config.yaml`, the **previous version is put back automatically**, the bot is started again and
  you get an alert. Nothing else changes.
- If the new version crashes within 2 minutes of restarting, the previous version is restored and
  started instead, again with an alert.
- To go back by hand: close the bot window, then run `start.bat update --undo`.
- In a git folder, an update refuses to install if you edited the bot's own files (it names them),
  if you are on another branch, or if you have commits that are not on GitHub. Undo or commit those
  first. Your `config.yaml` is not one of the bot's files, so editing it is always fine.
- The files an update replaced are kept in `data/updates/backup_*` (the last 3 updates).

#### Settings

```yaml
updates:
  enabled: true          # check automatically (false: only when you press Check now or send /update)
  check_every_hours: 6
  notify: true           # tell you once on Telegram/Discord when a new version appears
  branch: main           # the branch the bot follows
```

To update by hand instead, download the new version into the same folder (keep your `config.yaml`,
`.env` and `data/`), open a Command Prompt there and run `.venv\Scripts\python -m pip install -e .`
once, so any new dependencies are installed.

### Try it before setting anything up

From the text menu (`start.bat menu`) choose **11 (demo)**, or run:

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

You can store more than one key — for example one per account, or the key of a second TopstepX
login. See [section 30](#30-several-api-keys).

---

## 5. Set up on the dashboard

Double-click **`start.bat`**. The dashboard opens in your browser, and the first time it goes
straight to the **Setup** tab. Nothing is asked in the black window. Fill in the steps from top to
bottom; every field already has a suggestion, and a short note under each one explains it.

1. **TopstepX login.** Enter your username and API key, then press **Connect**. The bot logs in
   to check them and lists your accounts. The key is saved in a file called `.env` in the bot
   folder, which only lives on your computer. The dashboard never shows it again: it only says
   "saved", and you can paste a new one any time. Under **More API keys** you can store another
   key (tested before it is saved) for a second TopstepX login, or one per account
   ([section 30](#30-several-api-keys)).
2. **Account.** At the top, a card shows **what TopstepX reports for your account**, read with
   your API key: its name and id, balance, whether it can trade right now, its size and type,
   progress toward the Combine target, open positions and working orders, and today's trades and
   net P&L. Anything that needs attention is in red: the key can't log in or can't see the
   account, TopstepX says it can't trade, or its name points to a different size or type than your
   settings (a wrong size means wrong loss limits). It's checked each time you open the tab;
   **Check accounts** checks again. **+ Add account** adds another account
   ([section 29](#29-trading-several-accounts)). Below the card, pick the account the bot should trade. The bot reads the account name (e.g.
   `50KTC-...`, `XFA-150K-...`) and fills in the matching **size** (50K, 100K or 150K) and
   **type** (`combine`, `express` or `practice`). Size sets the Maximum Loss Limit, profit target
   and contract cap the bot works with. Live Funded accounts are not offered: Topstep doesn't
   allow API trading on them.
   - **What is this account for?** *Pass the Combine* protects the account and stops early on big
     days. *Teach the bot* trades more often and tries unproven strategies so it learns faster;
     use it only on an account you're willing to lose ([section 17](#using-a-combine-to-teach-the-bot)).
     *Make money every day* is the aggressive mode: as many trades as the rules allow, pressing harder on
     good days, stopping at a **daily profit goal** you can set ([section 39](#39-make-money-every-day-the-aggressive-mode)).
     Every Topstep rule applies whichever you choose. With *Teach the bot*, a **First trade after starting**
     box appears, ticked: the bot then makes one educated trade within 15 minutes of starting
     ([The first trade after starting](#the-first-trade-after-starting)).
   - Tick **Daily Loss Limit** if you added Topstep's optional one at checkout (it's under Risk
     Settings in TopstepX; the bot then stops before it). For an Express Funded Account, pick the
     **payout path** you chose.
3. **What to trade.** Start with a **micro** contract (MNQ, MES, M2K, MYM, MGC, MCL). Micros are
   1/10th the size of the minis, so mistakes cost 10× less. Or choose **Auto: the bot picks each
   day** and let it choose among the micros
   ([Letting the bot pick the market](#letting-the-bot-pick-the-market)). For the **strategy**, `adaptive`
   (recommended) runs every strategy all day and trades only what the bot has learned is working;
   or pick one strategy. See [section 12](#12-the-strategies).
4. **Risk.** Dollars to risk per trade, your personal daily loss limit and the most trades per day.
   The suggestion is 7.5% and 25% of your Maximum Loss Limit (for a 50K account: $150 per trade,
   $500 per day), and **Use suggested** puts it back. If a value breaks a Topstep limit, the
   dashboard says which one and doesn't save it.

Press **Save**. The settings go into **`config.yaml`** in the bot folder; anything else in that file,
including your own notes and hand edits, is kept. The previous version is kept as
`config.yaml.bak`. Then press **Start the bot** (or **Start** at the top). It starts in **Paper**
mode: real prices, simulated orders.

You can come back to the Setup tab any time and change anything. If the bot is running when you
save, the top of the page offers **Restart bot** so the new settings take effect.

Further down the Setup tab, both optional:

- **Alerts and remote control**: connect Telegram (step-by-step instructions are right there;
  see [section 15](#15-telegram-control-and-alerts-on-your-phone)) or a Discord channel.
- **Start with Windows**: starts everything about 30 seconds after you sign in, for running 24/7
  ([section 20](#20-running-247)).

**If `config.yaml` has a mistake** (for example after a hand edit), the dashboard still opens and the
Setup tab says what's wrong and where. Fix it in Notepad, or tick *Start over with a fresh
config.yaml* and save. The bot won't start until the file is fixed.

**Prefer typing?** The old question-and-answer setup still works: `start.bat setup` (or menu
**1**). It writes the same `config.yaml`.

To check the connection in detail, choose **3 (check)** in the text menu. It logs in, lists your
accounts (the selected one is marked ◀), and shows the exact contract the bot will trade, its tick
value and estimated fees.

---

## 6. The text menu (optional)

Everyday use needs only the dashboard. The text menu has the extras: training, backtests, tuning,
the preflight, the journal and so on. To open it, open a Command Prompt in the bot folder and run
**`start.bat menu`** (or `topstep-bot menu`). When this guide says "menu 5", it means option 5 here.

| # | Option | What it does |
|---|---|---|
| 1 | setup | The question-and-answer setup (the dashboard's Setup tab does the same) |
| 2 | go-live | **Start trading today:** runs every preflight check, then starts live ([section 21](#21-starting-today-preflight-ramp-up-and-news)) |
| 3 | check | Tests the connection, shows accounts and the contract |
| 4 | backtest | Tests the strategy on history and opens a report |
| 5 | train | **Teaches the bot** which strategy works at which time of day, from recent real data ([section 17](#17-training-and-the-knowledge-base-how-the-bot-learns)) |
| 6 | paper | **Starts the bot in PAPER mode** (simulated orders on real prices) with the dashboard and Telegram |
| 7 | live | **Starts the bot in LIVE mode** (real orders) with the dashboard and Telegram |
| 8 | flatten | **Emergency:** closes every position, then cancels every order, on each account; checks again and lists anything still open. It does not stop a running bot (use Flatten & halt for that). |
| 9 | journal | Recent trades and daily results |
| 10 | strategies | Describes each strategy and its parameters |
| 11 | demo | Backtest on synthetic data, no account needed |
| 12 | telegram-test | Sends a test message with control buttons to your Telegram |
| 13 | autostart | Starts everything automatically when you sign in to Windows ([section 20](#20-running-247)) |
| 14 | logs | Shows recent errors and where the log files are ([section 22](#22-logs-finding-out-what-happened)) |
| 15 | tune | **Tuning:** tests every strategy and its settings on real data they never saw and can save what held up ([section 18](#18-tuning-test-strategy-settings-on-unseen-data)) |
| 16 | rules | Shows Topstep's rules for **your** account (limits in dollars and contracts) and what the bot does about each one |
| 17 | insights | **What the bot has learned:** each strategy's results after costs, real fills against simulated ones, and the market conditions it did best and worst in ([section 17](#what-the-bot-learned-results-after-costs-real-fills-and-conditions)) |
| 18 | update | **Updates:** checks GitHub for a newer version, lists what changed and installs it when you say so ([Updating](#updating)) |
| 19 | learn | **Feeds the bot more history:** downloads up to a year it doesn't have yet and replays all of it through every strategy ([long-run memory](#the-long-run-memory-a-much-bigger-knowledge-base)) |
| 20 | first-trade-test | **Tests the first trade after starting:** replays history with it off, with the bot's educated pick, and with a coin flip, and shows what each did ([The first trade after starting](#the-first-trade-after-starting)) |
| 21 | accounts | Your accounts: what each one trades, and which share the same calls ([section 29](#29-trading-several-accounts)) |
| 22 | keys | Manage your TopstepX API keys ([section 30](#30-several-api-keys)) |
| 23 | markets | **Every market Topstep permits:** downloads all their history (no login needed) and lists what the bot has ([section 36](#36-every-market-topstep-permits)) |
| 24 | simulate | **Simulation Lab** on every market and strategy ([section 34](#34-simulation-lab-hundreds-of-combines-from-history)) |

Above the list, the menu shows a one-line summary of your setup (account, symbol, strategy, mode).
If `config.yaml` has a mistake, that line says what and where. When a newer version of the bot is
waiting, a second line says so.

Starting the bot from the menu (6, 7 or 2) also opens the dashboard. You can switch between paper
and live there later, without coming back to this menu.

---

## 7. The path to live trading

You can start the same day — the bot protects a new account with reduced risk while it proves
itself — but the more evidence you have first, the better. In order:

1. **Set up** on the dashboard's Setup tab ([section 5](#5-set-up-on-the-dashboard)) and, if you
   like, **check** the connection (menu 3).
2. **Train the bot** (menu 5). It replays the last 60 days of real data through every strategy and
   learns which ones work at which time of day ([section 17](#17-training-and-the-knowledge-base-how-the-bot-learns)). Read the table it prints: if everything
   is ✘, the market has not been kind to these strategies lately and the adaptive strategy will
   (rightly) sit out.
3. **Backtest on real data** (menu 4). Is the result positive *after fees*, is the drawdown
   survivable, does the Combine pass rate look reasonable? To compare single strategies and their
   settings over a longer history without fooling yourself, use **tune** (menu 15, [section 18](#18-tuning-test-strategy-settings-on-unseen-data)).
4. **Paper trade** (the dashboard's **Start** button with the switch on Paper, or menu 6): real live prices, simulated orders, no
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

Double-click `start.bat`, then press **Start** on the dashboard with the top switch on **Paper**.
(Menu **6** and `topstep-bot start --mode paper` do the same in one step.)

Two separate programs are involved:

- the **controller**, the dashboard at **http://127.0.0.1:8765** and Telegram control, which
  `start.bat` starts;
- the **trading bot**, which the controller starts when you press **Start**, then watches and
  restarts.

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

The dashboard is laid out like a trading desk: a **navigation rail** on the left (Overview, Trade,
Ask, Knowledge, then Settings, Setup and Logs), a **status bar** along the top, the **markets strip**
under it while the bot runs, and the page you picked underneath. Tabs with more than one page (Trade,
Ask and Knowledge) show their pages as buttons at the top. On a phone or a narrow window the rail
becomes a row of tabs across the top. Counts on the rail show live ideas, a running simulation and a
deep review with proposals waiting for you. At the bottom of the rail: the **Logs** badge, the phone-link
badge and the **Auto / Light / Dark** theme switch (Auto follows your computer).

**Status bar** (always visible):

- **Paper | Live** switch. Switching to Live asks you to type `LIVE`. It's refused while a trade is
  open, so flatten first. The bot restarts in the new mode, and the choice is remembered for next time.
- **Bot status:** Running, Starting…, Stopped, Crashed or Failed to start, plus uptime.
- **Start, Restart and Stop** for the bot process. Stop closes any position first; the dashboard stays up.
- **Logs badge** (rail): warnings and errors so far. Click it for the Logs tab.
- **Phone link on** (rail, only while phone access is on): the dashboard can be opened in Telegram on
  your phone. Click it for its settings ([how it works](#open-the-dashboard-on-your-phone-inside-telegram)).
- **Theme switch** (rail): Auto, Light or Dark. The dashboard remembers it, and which tab you were on.
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
- **Markets strip** (while the bot runs): one chip per market the account trades, with its price
  line, position and open P&L. Click a chip to show that market on every page, **×** to stop trading
  it, and **Add a market** to trade another one alongside, all without a restart. The account's total
  open P&L and size are on the right ([section 41](#41-several-markets-at-once)).
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
- **Current trade:** a live candle chart of the market with the trade's entry, stop and target
  ([section 44](#44-the-trade-chart)); then side and size, who opened it (*bot*, *idea* you took, or
  *you*), entry, stop, target, dollars at risk, live P&L in dollars and R, why it was opened and its odds
  when it was opened ([section 42](#42-the-probability-calculator)). Two buttons:
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

**Account goal panel** (Overview): progress to your Combine pass or your next Express Funded payout —
see [section 32](#32-funded-account-goals-and-topsteps-rules).

**Trade tab**, two pages:

- **Ticket:** open your own trade with suggestions from the bot, the odds of each strategy on your side
  and a chart of your stop and target — see
  [Trading manually from the dashboard](#trading-manually-from-the-dashboard) below.
- **Ideas:** recommended trades with **Take** buttons, their odds and today's results per strategy —
  see [section 16](#16-recommended-trades). A badge on the tab shows how many ideas are live.

**Ask tab**, two pages:

- **Ask:** ask anything about the bot in your own words — why it took or skipped a trade, the odds now,
  which markets it trades, what a rule or setting means — plus every decision it made and the decision
  lab. See [section 38](#38-the-central-intelligence-ask-the-bot-why) and
  [section 45](#45-the-simpler-dashboard).
- **Deep review:** the AI coach. Claude reviews the bot's evidence, runs its own simulations, records
  lessons and proposes changes you approve. Your Claude API key, credits and spending limits are here
  too. See [section 35](#35-the-ai-coach-claude-teaches-the-bot).

**Knowledge tab**, four pages:

- **Overview:** every source of the bot's knowledge in one place (real trades, signals watched live, real
  market history, simulations, related markets), the probability calculator's odds now, how well its
  predictions held up, what the bot knows in plain words and the knowledge network. See
  [section 45](#45-the-simpler-dashboard).
- **What works:** for every strategy, time of day and regime, the average result per signal and whether
  the adaptive strategy trades it right now, with a **Retrain now** button, plus **Your manual trades**:
  your record by time of day and regime and your latest trades. See
  [section 17](#17-training-and-the-knowledge-base-how-the-bot-learns).
- **Simulations:** the Simulation Lab — the bot replayed through history as fresh Combines, every
  playbook (market + strategy) scored, and a **Use** button to make one the bot's. See
  [section 34](#34-simulation-lab-hundreds-of-combines-from-history). The Overview shows a summary.
- **Market data:** what the bot knows about the market shown — key levels, a normal day's range in
  points and dollars, today's market-moving reports, headlines, the market network and a profile of the
  contract. See [section 31](#31-what-the-bot-knows-about-the-market-and-the-news).

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
  slot and regime, your record counts toward the reading);
- the **probability calculator's** odds for the strategies on your side right now: the chance of a win,
  its 80% range and the expected result after costs ([section 42](#42-the-probability-calculator)).

The ticket's **Chart** shows the last few hours with your planned entry, stop and target as dashed
lines, the dollars at risk and the R:R, and it moves as you change them
([section 44](#44-the-trade-chart)).

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
the Knowledge tab's **What works** page show your record next to the strategies', so over a Combine you can see where your
own trades work and where they don't. Like the bot's real trades, they count double and are never
dropped by retraining. Trades closed by a shutdown, halt or daily restart aren't counted (the
ending says nothing about the trade).

### Watching the bot get ready to trade

The **Getting ready to trade** section of the Overview tab shows every trade a strategy is building
toward, before it happens. Each card is one setup: a strategy, a side and your contract.

- **bot trades it** (blue border) means the auto-traded strategy places this trade when it fires.
  With the adaptive strategy that's any sub-strategy the knowledge base allows right now; the others
  show **idea only**, and when they fire they appear as ideas on the Trade tab's Ideas page instead.
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

The recommended way in is **menu 2 (Start trading today)** in the text menu, which runs the
preflight first ([section 21](#21-starting-today-preflight-ramp-up-and-news)). You can also stop
the bot on the dashboard, switch the **Paper | Live** toggle to Live (type `LIVE` to confirm) and
press **Start**, or start with menu **7**, which shows a red warning and asks you to type `LIVE`
too. Once you've chosen Live, `start.bat` remembers it, but it never starts trading by itself:
you always press **Start**.

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
| Panic button when nothing is running | Menu **8 (flatten)** — closes every position and cancels every order on each account at TopstepX, keeps going if one call fails, then checks again and lists anything **STILL OPEN** or **NOT CHECKED**. It does not halt a running bot: use **Flatten & halt** or `/flatten` for that. |
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

This is a one-time sync. The bot records the number the first time it sees it and from then on follows
its own trailing floor, so leaving the line in the file never pulls the floor back down on a later
start. To sync again, type the new number. A number that was already in the file before version 4.0.1
is ignored with a warning when the bot has learned a floor of its own (it may be months old). A new
number lower than the bot's floor is applied once, with a warning naming both.

Topstep changes its rules from time to time. Check help.topstep.com and your TopstepX Risk Settings,
and adjust the config if anything differs: a lower position limit goes in `risk.max_contracts`, a
different Daily Loss Limit in `account.topstep_daily_loss_limit` (a dollar amount).

---

## 15. Telegram control and alerts on your phone

With Telegram set up, the bot **messages you** (entries, exits, risk events, daily summary,
check-ins, crashes and restarts) and
**takes commands from you**: check status, pause, resume, flatten or stop — from anywhere.

### Set it up (about 2 minutes)

On the dashboard's **Setup** tab, scroll to **Alerts and remote control**:

1. In Telegram, open **@BotFather**, send `/newbot`, pick a name and a username ending in `bot`.
2. Paste the **token** BotFather gives you into **Bot token**.
3. Open your new bot in Telegram and press **Start** (or send it anything), then press **Find my
   chat** on the dashboard. It finds your chat for you.
4. Leave **Also control the bot from Telegram** ticked (untick it for alerts only) and press
   **Save Telegram**. You get a test message with the control buttons straight away.

Once it's connected, the same place has **Send a test message**, a switch for remote control,
**Use another bot** and **Remove**. The token is saved in `.env` and never shown again. (The text
setup, `start.bat setup`, can do this too, or put `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` in
`.env` by hand.)

Telegram control starts automatically with the controller, and **keeps working while the bot is
stopped or has crashed**: `/status` then tells you why it stopped, and `/startbot` or `/restart`
brings it back. The bot window
shows `Telegram control: on`, and your phone gets *"Topstep Bot is online"*.

### Commands

Type them, pick them from Telegram's **/** menu, or tap the buttons under the bot's messages.

| Command | What it does |
|---|---|
| `/status` | Mode, account, status, balance, today's P&L, open position with stop/target, MLL room, daily loss used, trades today, Combine progress |
| `/dashboard` | Opens the full dashboard inside Telegram on your phone. Offers to turn phone access on if it's off; `/dashboard off` turns it off ([below](#open-the-dashboard-on-your-phone-inside-telegram)) |
| `/pause` | Stop opening new trades. An open trade keeps its stop and target. |
| `/resume` | Allow new trades again (all risk limits still apply). Not possible after a flatten — restart the bot. |
| `/flatten` | Close any position, cancel orders and **halt** trading until the bot is restarted. Asks for confirmation. |
| `/stop` | Stops the bot (it flattens first). Telegram and the dashboard stay online, so `/startbot` brings it back. Asks for confirmation. Use `/pause` to just stop new trades. |
| `/restart` | Restarts the bot (flattens first) — e.g. after an error. Asks for confirmation. |
| `/startbot` | Starts the bot if it's stopped or crashed. Asks for confirmation. |
| `/ask <question>` | Ask the bot anything about its trades and knowledge, e.g. `/ask why did you take the last trade?`. You can also just type the question without `/ask` ([section 38](#38-the-central-intelligence-ask-the-bot-why)) |
| `/ideas` | Recommended trades with **Take** and **½ size** buttons ([section 16](#16-recommended-trades)) |
| `/brief` | **What the bot knows**, in plain sentences: how much it has seen, what it would and wouldn't trade right now and why, what has worked after costs, lessons worth testing, and when it expects its next trade ([section 17](#what-the-bot-knows-and-when-it-trades-next)) |
| `/market` | **Market brief** for the market this account trades: key levels, normal range, today's reports, headlines ([section 31](#31-what-the-bot-knows-about-the-market-and-the-news)) |
| `/funded` | Progress toward passing the Combine or the next payout ([section 32](#32-funded-account-goals-and-topsteps-rules)) |
| `/rules` | Every Topstep rule the bot knows and what it does about each |
| `/research` | What the bot has gathered from the internet ([section 33](#33-research-knowledge-that-grows-from-the-internet)) |
| `/network` | The market network: the board's mood, the biggest moves against normal, busy asset classes, your market's peers, reports in the next 24 hours and jump allowance ([section 40](#40-the-market-network)) |
| `/lab` | The Simulation Lab's latest results: Combines passed/failed, the best playbooks, the opportunities, the held-out check and its suggestion ([section 34](#34-simulation-lab-hundreds-of-combines-from-history)) |
| `/switch MES` | **Trade another market instead, for the rest of today**, without a restart (asks to confirm; an open trade on the old market keeps its stop and target and finishes first). `/switch usual` goes back ([section 37](#37-trading-another-market-today)) |
| `/markets` | The markets the bot trades now, each with its position and trades today, and the account's total at risk ([section 41](#41-several-markets-at-once)) |
| `/add MES` | **Trade another market alongside the others**, at once (asks to confirm). Every Topstep limit counts all of them together |
| `/drop MES` | **Stop trading a market** (asks to confirm). An open trade there keeps its stop and target and finishes first |
| `/coach` | The AI coach: latest lessons, proposals waiting for you, and this month's spending ([section 35](#35-the-ai-coach-claude-teaches-the-bot)) |
| `/assistant` | **The AI trading assistant**: its switches, today's decisions with why, and its spending. `/assistant manage on`, `/assistant trade on` (each asks to confirm), `/assistant off` ([section 46](#46-the-ai-trading-assistant)) |
| `/accounts` | All your accounts at a glance ([section 29](#29-trading-several-accounts)) |
| `/next` | **When the next trade is likely**, and what that estimate is based on (also a button under every message) |
| `/goal` | **Today's profit goal**: `/goal 400`, `/goal c1 400 c2 1200` (several accounts), `/goal all 300`, `/goal suggested` or `/goal none`. `/goal` alone shows each account's goal with buttons. No confirmation needed: a goal can only end a day sooner ([section 47](#47-trends-and-the-daily-plan)) |
| `/plan` | **Today's plan**: the goal, the route through the day, the odds from the bot's past days, progress and when the day stops |
| `/trend` | **What the chart is doing**: trending up, down or sideways, how clear and how strong, how often its calls held, and the trend filter |
| `/learn` | Grow the long-run memory now: download the history it's missing and replay all of it ([section 17](#the-long-run-memory-a-much-bigger-knowledge-base)) |
| `/knowledge` | What the bot has learned: per strategy and time of day, ✅ trades now / ❌ switched off / ❔ unproven, then results after costs, real fill slippage and conditions worth testing ([section 17](#17-training-and-the-knowledge-base-how-the-bot-learns)) |
| `/train` | Retrain the knowledge base on recent history now (the bot keeps trading meanwhile) |
| `/settings` | Every setting you can change, with current values and limits |
| `/set <name> <value>` | Change a setting, e.g. `/set risk 150`, `/set dailyloss 400`, `/set strategy noise_breakout`, `/set news off` ([section 19](#19-changing-settings-from-the-dashboard-or-telegram)) |
| `/reset` | Undo every remote setting change (back to `config.yaml`) |
| `/trades` | The last few closed trades |
| `/log` | Recent bot activity |
| `/update` | Checks GitHub for a newer version, lists what changed and offers **Install now** / **After the close** ([Updating](#updating)) |
| `/help` | The command list |

### Open the dashboard on your phone (inside Telegram)

When you're away from home, the whole dashboard (Overview, Trade, Ideas, Knowledge, Settings, Logs)
can open **inside Telegram** on your phone, in Telegram's built-in browser. Nothing to install on
the phone and nothing to change on your router.

**Turn it on** (it is off until you do):

- on the PC: dashboard → **Settings** tab → **Phone access (Telegram)** → **Turn on**, or
- from your phone: send `/dashboard` (or tap **📱 Dashboard**) and tap **Yes, turn it on**.

The first time, the bot downloads Cloudflare's small tunnel program, `cloudflared` (about 60 MB,
from Cloudflare's official GitHub releases, checked against its published checksum) into
`data/tools/`. A few seconds later Telegram gets an **📊 Open dashboard** button, and a
**Dashboard** button appears beside the message box. Tap either one. The choice is remembered: if
the PC or the bot restarts, phone access comes back on by itself and sends a fresh button.

**Turn it off** with **Turn off** on the Settings tab (on the PC or the phone) or `/dashboard off`.
The link stops at once.

What you can do from the phone: run, pause, flatten and switch markets, read the logs, ask
questions, run simulations and the coach, and switch back to Paper. Keys, accounts and Telegram
(the Setup tab), switching to **LIVE**, turning phone access on, the coach's key, settings and
Apply, and saving a playbook stay on the PC. The phone says "Only available on the PC dashboard"
when you try one of those.
Confirmations appear as a box at the bottom of the screen; every action is listed in Activity as
*phone (Telegram)*.

How it stays yours:

- **Only your Telegram account can open it.** When you tap the button, Telegram hands the page a
  sign-in signed with your bot's token. The PC checks that signature, that it is recent, and that
  it belongs to you (the account of your private chat with the bot, or the IDs in
  `telegram.allowed_user_ids`). Anyone else, including someone who finds the address, sees
  *"Only the owner's Telegram account can open this dashboard"*, and the refusal is logged on the
  Logs tab. Opening the address in an ordinary browser doesn't work either.
- The sign-in lasts at most 12 hours and ends when phone access is turned off or restarts; the
  page then signs you in again by itself through Telegram.
- The link only reaches the dashboard. Your API key, `.env` and files on the PC are never served.
- The address (`https://<random words>.trycloudflare.com`) changes every time the link starts.
  The newest button always works; old ones stop working.

**The bot still runs on your PC.** Topstep requires the bot to run on your own computer and
forbids VPS, VPN and remote servers. Phone access doesn't change that: the bot keeps trading from
your PC and your home internet connection, exactly as before. The Cloudflare tunnel is an
outgoing connection from the PC that only carries the **dashboard** to your phone. It is not a
VPN, and none of the bot's traffic to TopstepX goes through it. (Like any website behind
Cloudflare, Cloudflare carries the dashboard's traffic on its way to your phone.) The PC has to
stay on, online and with the Topstep Bot window open.

Good to know:

- It needs Telegram set up (above) and a **private chat** with your bot. In a group chat, list your
  own Telegram user ID in `telegram.allowed_user_ids` first.
- Cloudflare's free quick tunnels have no uptime guarantee. If the link drops, the bot reconnects
  by itself (10 s, 30 s, then up to 5 minutes apart) and sends a new button; the Settings tab shows
  what it is doing.
- If you already have `cloudflared` installed (for example `winget install Cloudflare.cloudflared`),
  the bot uses that copy. To point it at a specific file, set `dashboard.cloudflared_path` in
  `config.yaml`.

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
- Phone access is off by default. While it is on, only your own Telegram account can open the
  dashboard, and LIVE mode can't be switched on from the phone ([above](#open-the-dashboard-on-your-phone-inside-telegram)).
- Keep the bot token secret. If it leaks, send `/revoke` to @BotFather and connect the new token on
  the dashboard's Setup tab (**Use another bot**). The
  bot never writes the token (or a Discord webhook) into its log files.
- Don't run two copies of the bot with the same Telegram token — Telegram only lets one program
  read a bot's messages at a time.

To keep alerts but turn off remote control, untick remote control on the Setup tab (it sets
`telegram.control_enabled: false`).

### Discord (alerts only)

In a Discord server you own, open *Server Settings → Integrations → Webhooks → New Webhook*, copy
the URL, and paste it into **Discord** on the dashboard's Setup tab (it sends a test message), or
add it to `.env`:

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
| **training** | Every strategy replayed over the last `knowledge.history_days` (60) days of real data — exactly the way the running bot follows ideas, so it measures the same thing | Menu **5 (train)**, `topstep-bot train`, the dashboard's **Retrain now**, Telegram `/train`, and automatically after every finished session (see [Starting up](#starting-up-a-clean-warm-start)) |
| **live ideas** | Hypothetical outcomes of signals the bot saw while running but did not trade (shadow strategies, skipped signals), around the clock: the overnight session too | Continuously while the bot runs |
| **real trades** | The bot's own closed trades (count double) | Continuously while the bot runs |
| **manual trades** | Trades you opened from the dashboard's Trade tab (count double, shown as *Your manual trades*; they never switch a strategy on or off) | When each one closes |

Because every signal is followed whether it was traded or not, **the bot never has to try a bad idea
to learn it is bad.** Older observations fade out (half their weight after `half_life_days`, 20), so
the base follows the market as it changes. Retraining replaces the training layer and drops live ideas
the new training already covers, so nothing is counted twice; real and manual trades are never dropped.

### Starting up: a clean warm-start

Every time the bot starts (including the daily 16:05 CT restart) it gets its knowledge ready before it
trades:

1. **Loads the knowledge base** and checks it (see [Keeping it safe](#keeping-what-it-learned-safe)).
2. **Retrains when the training is behind.** That is: a trading session has finished since the last
   training, the training is older than `retrain_hours` (20), or the strategy code changed since (an
   update). The history comes from the market library on your PC; only what it doesn't have yet is
   downloaded, so this takes seconds, and if TopstepX doesn't answer the bot still trains on the
   history it already saved.
3. **Catches up on today.** If it starts in the middle of a session (after a crash, an update or a
   restart you asked for), it replays today's bars so far through every strategy. The ideas they gave
   before the start are followed to their outcome and learned from, and ideas still running are
   followed on live. Nothing is traded in the replay, no alerts are sent for these past ideas, and an
   idea the bot already learned from before the restart is not counted again.
4. **Says what it knows** in the Activity log, for example: *Knowledge ready: 1,240 observations;
   training covers 42 days to 2026-10-09 (saved history + 288 new bars); 37 live ideas, 6 real trades,
   2 manual trades.*

If the bot runs for days without restarting (`service.daily_restart_time: off`), it retrains the same
way after each session closes, outside your entry window and only while flat, so trading never waits
on it. `/knowledge` also shows what it learned today.

### Keeping what it learned safe

Training can always be rebuilt from price history, but what the bot sees live (its real trades, your
manual trades, the ideas it followed) can never be recorded again. So it is saved three ways, all in
`data/`:

- **The knowledge file** (`knowledge_<SYMBOL>_<TF>m.json`), saved after every observation. It is
  written to a temporary file first and swapped in, so a crash or power cut never leaves half a file.
- **The ledger** (`knowledge_<SYMBOL>_<TF>m.ledger.jsonl`): every live observation is also added here
  as one line and never removed, even when the knowledge base trims its oldest ideas.
- **Daily backups** in `data/knowledge_backups/`: a copy of the knowledge file once a day; the last
  `knowledge.backups_kept` (14) are kept.

If the knowledge file is ever damaged, the bot sets it aside as `….json.damaged-<time>` (so you can
look at it), loads the newest good backup, adds back everything from the ledger the backup is missing,
and tells you in the Activity log. If you run `topstep-bot train` while the bot is running, the bot keeps
the new training instead of saving over it. Your PC is still the only copy: copy the `data` folder
somewhere else now and then if you want to be safe from a disk failure.

### How the adaptive strategy uses it

Before taking a signal from strategy *S* at slot *T* in regime *V*, the adaptive strategy looks for the
most specific evidence with enough samples (`min_samples`, 8 weighted observations): *S* at *T* in *V*;
otherwise *S* at *T* in any regime; otherwise *S* at any time. The strategy may trade if that evidence's
expectancy — shrunk toward zero while the sample is small — is at least `min_edge_r` (0.05R). A
strategy that has been losing at that time of day is **switched off there** even if it does well
elsewhere; an **unproven** one is not traded at all (unless you set `trade_unproven: true`).

Everything is visible:

- **Dashboard → Knowledge tab → What works:** the full table (✔ trades now, ✘ switched off, ? unproven), counts
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

- **Dashboard → Knowledge tab → What works → What the bot has learned:** each strategy's average result before
  and after costs, its best and worst points, the share of losers that were 1R in profit first (a
  sign a breakeven stop or target might help), and real trades against simulated ones. Below that,
  real fill slippage against what backtests assume, *conditions worth testing*, and a **Compare by**
  table that splits every strategy's results by one measurement (low / middle / high, or by weekday).
  **Download CSV** saves every observation for Excel.
- **Telegram:** `/knowledge` ends with a short version.
- **Menu 17** or **`topstep-bot insights`** prints it; `topstep-bot insights --csv knowledge.csv` exports.
- **Your hand against the plan** (same panel, and the end of `/knowledge`): from this account's journal,
  what the bot's trades made when left alone, what closing them by hand made or cost against what
  their own plan (stop and target) went on to do, and how your own trades end: average win and loss,
  fees, and where you closed winners compared with the best point they had reached. It is a score,
  not a rule: nothing in it changes how the bot trades.

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
  Knowledge tab's Overview, `/learn`, menu **19** or `topstep-bot learn`.
- Have older data? `topstep-bot learn --import mydata.csv` adds a CSV of bars (1-minute or your
  bot's timeframe) to the library, then replays. `--offline` replays without downloading; `--days 730`
  reaches further back for that run.

**What it changes:** what the bot can tell you. The **What the bot has learned** panel (Knowledge → What works) has a
**Recent / Long run** switch, the briefing compares each strategy's recent results with the long run
(a strategy that is hot lately but weak over a year deserves suspicion), and conditions worth testing
get far more data. `topstep-bot insights --longrun` prints the long-run report.

**What it changes in trading:** nothing by itself. The long-run memory only enters trading decisions
through the **evidence rule**, and the bot only switches to that rule when the **decision lab** has
shown it doing better on enough replayed history ([section 38](#the-decision-rule-and-the-decision-lab)).
Every risk rule, limit and position size stays exactly as configured either way.

**Reaching further back.** TopstepX only serves recent history for each futures contract (in October
2026: about a month for the current contract and a few weeks more for the one before; older contracts
returned nothing). So the backfill also asks for the **two previous contracts** once each and keeps
whatever they return, and the library keeps growing every day the bot runs. For years of history, import
a CSV with `topstep-bot learn --import`.

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

If an account is for teaching the bot rather than passing, choose **"Teach the bot"** on the dashboard's
Setup tab (step 2, *What is this account for?*) and press Save. That writes a learning configuration:

| Setting | Learning | Passing (default) | Why |
|---|---|---|---|
| `strategy` | `adaptive` with `trade_unproven: true` | your choice | Also trades strategies the bot has no evidence on yet, so it gets real fills for them. |
| `risk_per_trade` (50K) | $100 (5% of the MLL) | $150 | Smaller trades, so more of them fit before a daily or account limit. |
| `max_trades_per_day` | 8 | 4 | More real outcomes per day. |
| `max_consecutive_losses` | 4 | 2 | Doesn't stop after two losses. |
| `cooldown_minutes_after_loss` | 5 | 10 | |
| `daily_profit_target` | the full profit target | 40% of it | No early stop on a big day. |
| `consistency_guard` | off | on | A big day only raises the target; that doesn't matter on a learning account. |
| `first_trade.enabled` | on | off | One educated trade within 15 minutes of starting, so every start teaches it something ([below](#the-first-trade-after-starting)). |

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
- When you move to an account you want to pass, choose **"Pass the Combine"** on the Setup tab and
  save. Your other settings stay as they are.

### The first trade after starting

With *Teach the bot*, the bot makes its own educated trade within 15 minutes of starting, so each
start gives it a real fill to learn from instead of waiting for a strategy signal that may take hours.
Untick **First trade after starting** on the Setup tab and press Save to turn it off.

How it works:

- **When.** The 15 minutes count from the start, or from 08:30 CT when the bot starts before the
  session, on a weekend or on a holiday. Outside trading hours it waits, and the Overview tab says
  so: *"Outside trading hours: waiting for the session. Entries open Mon 08:30 CT; the first trade
  follows by Mon 08:45 CT."*
  It is due by 14:29 CT at the latest (the last entry is 14:30). It happens at most once per trading
  day: a restart the same day doesn't add another one.
- **A strategy can go first.** The bot keeps watching for a normal signal. If a strategy trades
  before the deadline, that trade counts and nothing more happens.
- **What it picks.** On the last bar before the deadline it ranks everything it could take, using
  the knowledge base for this time of day and market mood:
  1. a strategy signal that fired just now (it keeps that signal's stop and target),
  2. a setup that is close to firing,
  3. simply long or short, whichever side its record here favours.

  A strategy the knowledge base has switched off for this time is never followed. Setups and plain
  long/short trades get a stop of 1 ATR (within the usual minimum and maximum stop) and a target of 1.5 times the risk. The reason it chose is
  shown on the Overview tab and in the trade's log line.
- **Size and rules.** One contract (`first_trade.contracts`), and never more than `risk_per_trade`
  allows. Every Topstep rule and risk limit applies: the MLL, any DLL, contract caps, news blackouts,
  trades per day, the losing-streak pause. If a rule blocks it, it tries again on every bar; if the
  rules block it for the rest of the session, it moves to the next session.
- **What it learns.** Its result is filed in the knowledge base as a *first trade*, apart from the
  strategies, with the reason it was chosen, so it never inflates a strategy's record. The Knowledge
  tab and `insights` show first trades on their own line.
- It is off on an Express Funded account and with the *Pass the Combine* goal.

On the dashboard: the Overview tab has a **First trade after starting** panel with a countdown, what
it would take right now and why, and the trade once it's placed. The **Next trade** countdown at the
top shows the first trade while it's due.

**Test it before you rely on it.** In the text menu choose **20 (first-trade-test)**, or run
`topstep-bot first-trade-test --days 365`. It downloads your TopstepX history and replays it three
times through the real trading code: the strategy alone, with the bot's educated first trade, and
with a coin-flip first trade (random side, an ATR stop and a 1.5R target). Each day the bot is "started" at a
different time, so every part of the session is tested. The result is saved to
`reports/first_trade_test.json`. Read it honestly: if the educated pick doesn't beat the coin flip,
the first trade has no edge, and its value is only what the bot learns from it. **No setting makes
the bot profitable or guarantees passing the Combine**, and an extra trade a day is an extra chance
to lose. Keep it to an account you're willing to lose.

Settings (`first_trade:` in `config.yaml`): `enabled`, `within_minutes` (5-120), `contracts` (1-5).

Settings (`knowledge:` in `config.yaml`): `enabled`, `auto_train`, `history_days`, `retrain_hours`,
`half_life_days`, `min_samples`, `min_edge_r`, `real_trade_weight`, `backups_kept`, `deep_learning`, `deep_history_days`.
The file is `data/knowledge_<SYMBOL>_<TF>m.json`, shared by paper and live. To start from scratch, stop the bot and
delete it together with its `.ledger.jsonl` (otherwise the ledger puts your real and manual trades back).

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
(the dashboard's **Start** button, menu 6/7, or menu 2 after the preflight) already runs it around the clock: the
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

To start everything automatically whenever you sign in to Windows, tick **Start with Windows** on
the dashboard's Setup tab (or choose **autostart**, menu 13, or run `topstep-bot autostart on`). It adds a small script to your personal Startup folder (no
administrator rights needed) that starts the bot about 30 seconds after you sign in, minimized, in
the mode you used last (Paper or Live). Check with `topstep-bot autostart status`; remove with
`topstep-bot autostart off` (or untick the box).

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

**Starting by hand:** double-click `start.bat` and press **Start** on the dashboard (or use menu 2,
6 or 7) before 8:15 CT. Check the dashboard shows
"connected", the right account and contract, and "Trading normally".

**During the session:** glance at the dashboard (or send `/status` on Telegram) and TopstepX now and
then. Act on any alert.

**After 15:10 CT:** confirm the position is flat. Review the day with menu **9 (journal)** and the
Knowledge tab's **What works** page (or menu **17**, insights).

**When an update is waiting:** read what changed (dashboard Settings tab, or `/update`) and install
it **after the close**, so the bot never restarts mid-session.

**Weekly:** compare the bot's MLL floor and position limit with your TopstepX dashboard and Risk
Settings (Topstep changes product limits with market conditions); look at Knowledge → What works to see
which strategies have stopped (or started) working, and at Knowledge → Overview → *How well it predicts*
to see whether the probability calculator's odds are holding up.

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
  mll_floor_override: null     # one-time sync with Topstep's dashboard (applied once per new number)
  topstep_daily_loss_limit: null  # true = your plan's optional DLL ($1,000/$2,000/$3,000), or a dollar amount
  payout_path: standard        # Express Funded payout path: standard | consistency
  login: null                  # which stored API key (section 30); default: the main key

instrument:
  symbol: MNQ                  # with auto_pick: the market used when none shows an edge
  contract_id: null            # pin an exact contract, e.g. CON.F.US.MNQ.Z26 (not with auto_pick)
  timeframe_minutes: 5         # 1-60
  auto_pick: false             # true = the bot picks the market each day (section 31)
  candidates: []               # markets auto_pick chooses from (default: the micros your plan allows);
                               # also ALL, MICROS or an asset class: EQUITY, CRYPTO, FX, ENERGY, METALS, RATES, AGS
  markets: []                  # more markets to trade at the same time as symbol, e.g. [MGC, MCL] (up to 5; section 41)
  max_markets: 1               # with auto_pick: trade up to this many of the best-scoring markets at once (1-6)

strategy:
  name: adaptive               # adaptive | noise_breakout | orb_momentum | orb | ema_trend
                               # | late_day_momentum | vwap_reversion | vwap_pullback
  params: {}

knowledge:                     # what the bot learns while it runs (drives the adaptive strategy)
  enabled: true
  auto_train: true             # retrain after every finished session, after an update, or when older than retrain_hours
  history_days: 60             # 10-120
  retrain_hours: 20
  half_life_days: 20           # observations lose half their weight after this many days
  min_samples: 8               # weighted observations needed before a strategy may trade in a slot
  min_edge_r: 0.05             # minimum (shrunk) expectancy in R to keep trading a strategy
  real_trade_weight: 2.0       # a real trade counts this many times an idea
  backups_kept: 14             # daily copies of the knowledge base in data/knowledge_backups (0 = none)
  deep_learning: true          # long-run memory: keep every bar, backfill and replay it all daily (reports only)
  deep_history_days: 365       # 30-3650: how far back the long-run memory reaches
  policy: auto                 # auto | classic | evidence: how the adaptive strategy decides (section 38)

brain:                         # the central intelligence (section 38)
  ai: false                    # true = a Claude model writes the answers (needs the Claude API key, shared with the coach)
  model: claude-opus-5-5
  effort: medium               # low | medium | high
  max_tool_calls: 8            # 1-20 lookups per answer
  explain_trades: true         # add why a trade was taken (and what it taught) to its alerts

first_trade:                   # one educated trade soon after starting ("Teach the bot" turns it on)
  enabled: false
  within_minutes: 15           # 5-120, from the start, or from 08:30 CT if it starts outside hours
  contracts: 1                 # 1-5, and never more than risk_per_trade allows

day_plan:                      # the daily plan (section 47); per account in an accounts entry
  ask: true                    # ask for each account's profit goal every trading morning (Telegram and the Overview tab)
  ask_at: "08:00"              # Chicago time, when the Telegram question is sent
  stop_at_goal: true           # no new trades once the day's goal is reached (it never makes a trade bigger)
  unanswered: last             # no answer: last = your last goal (else the suggestion) | suggested = the bot's suggestion
  days_to_target: 10           # 3-60: the suggested goal is what is left to the target divided by this

trend:                         # the chart's trend reading and the trend filter (section 47); per account too
  filter: auto                 # auto = on only once the trend test proves it helps | on | off
  min_score: 0.6               # 0.35-0.95: how clear a trend must be before the filter skips signals against it
  min_adx: 25                  # 0-60: and how strong (ADX)

assistant:                     # the AI trading assistant (section 46); per account in an accounts entry
  manage: false                # watch and manage open trades: tighten the stop, breakeven, close early
  trade: false                 # place its own trades
  include_manual: false        # also manage the trades you open yourself
  model: claude-opus-5-5
  effort: medium               # low | medium | high
  max_contracts: 1             # 1-10: largest size of one of its own trades (the risk rules may allow fewer)
  max_trades_per_day: 2        # 1-10 of its own trades a day
  min_confidence: 0.65         # 0.5-0.95: how sure it must be to enter or to close a trade early
  think_every_minutes: 15      # 5-120: how often it looks for a trade while flat (sooner on a fresh signal)
  manage_every_minutes: 10     # 5-60: how often it reviews an open trade (sooner at +1R, near the stop, before news)
  daily_budget_usd: 2.0        # Claude spending per day at most; it also counts toward coach.monthly_budget_usd

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
  consistency_guard: true              # combine: close out at 50% of the profit target in a day (Topstep's line is 55%)
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
  max_entry_slippage_ticks: 2  # entries can't fill worse than this, nor through the bid/offer (None = market orders)
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
  per_instrument: true         # also pause around medium-impact reports that move your market
  headlines: true              # recent headlines about your market (information only)

funded:                        # Express Funded payout goals (section 32)
  last_payout_date: null       # progress restarts after your last payout request
  protect_winning_day: true    # standard path: keep a $150+ day a winning day
  cycle_profit_goal: null      # consistency path: each day capped at 40% of this (default: the profit target)

aggressive:                    # "Make money every day" (section 39); the Setup tab writes this
  enabled: false
  daily_goal: null             # $ per day; default 45% of the profit target (must stay under 50% on a Combine)
  trades_multiple: 3           # adaptive: each strategy may trade this many times its own daily count (1-4)
  unproven_risk: 0.5           # share of the risk per trade for signals the knowledge base hasn't proven
                               # (used until the probability calculator has enough outcomes for a signal)
  house_money: 0.35            # share of today's realized profit added to the next trade's risk
  max_risk_multiple: 2.5       # ...never past this multiple of risk.risk_per_trade
  max_conviction: 1.5          # a large, well-measured edge may risk up to this multiple (probability.sizing)
  trades_per_market: true      # several markets: each brings its own risk.max_trades_per_day
  cushion_share: 0.25          # one trade never risks more than this share of the room above the MLL
  lock_in_at: 0.5              # once the day has made this share of its goal...
  give_back: 0.4               # ...it can't give back more than this share of its best (and stops if it does)
  stressed_risk: 0.5           # risk per trade while the market network marks the market as stressed
  flatten_before_news: true    # close an open trade just before a high-impact release

probability:                   # the probability calculator and the knowledge network (sections 42 and 43)
  enabled: true                # work out P(win) and the expected value of every signal
  veto: true                   # skip a signal it is at least 90% sure loses after costs (with enough evidence)
  sizing: true                 # Make money every day: size by a quarter of the Kelly fraction
  network: true                # related markets of the same asset class inform the odds (at 30% weight)
  grow_network: true           # outside trading hours, while flat, learn related markets from saved history
  grow_per_day: 3              # related markets learned per day at most (0 = none)

research:                      # what the bot gathers from the internet (section 33)
  enabled: true
  calendar: true
  headlines: true
  headline_minutes: 30
  daily_history: true
  rules_watch: true
  rules_watch_hours: 12
  sources: []                  # [{name: x, url: https://..., kind: rss|page}]

service:                       # used by the controller ('start')
  keep_awake: true
  daily_restart_time: "16:05"  # CT, during the CME daily halt; off = never
  check_in_time: "08:00"       # weekday "bot is alive" message; off = never
  heartbeat_timeout_seconds: 180
  max_restarts_per_hour: 6

updates:                       # new versions from GitHub (see "Updating" in section 3)
  enabled: true                # check automatically every check_every_hours
  check_every_hours: 6
  notify: true                 # tell you once (Telegram/Discord) when a new version appears
  repo: iSlipperyX/TSTradeBot
  branch: main                 # the branch the bot follows

dashboard:
  enabled: true
  host: 127.0.0.1
  port: 8765
  open_browser: true
  phone_access: false          # the dashboard inside Telegram on your phone; the Settings tab switch wins over this
  cloudflared_path: null       # optional: where cloudflared is; empty = find it or download it once

backtest:
  data_file: null
  report_dir: reports          # backtest and tuning reports

coach:                         # the AI coach (section 35); the API key goes in .env, never here
  enabled: true
  model: claude-opus-5-5       # claude-opus-5-5 | claude-sonnet-5-5 | claude-haiku-5-5 | claude-fable-5-1
  effort: high                 # low | medium | high | xhigh | max - higher is more thorough and costs more
  max_session_usd: 1.5         # a session stops by itself at this cost
  monthly_budget_usd: 20       # no new session once this month's spending reaches this
  max_turns: 24                # most back-and-forth steps in one session
  allow_simulations: true      # let the coach run its own Simulation Lab experiments

lab:                           # the Simulation Lab and every market's history (sections 34 and 36)
  markets: all                 # all = every market with history; tradable = only this account's auto-pick markets
  refresh_data: true           # keep every market's history up to date in the background (Yahoo, about twice a day)
  nightly: true                # run the lab on every market each weekday after the close
  nightly_time: "15:30"        # when (CT)

multi_account:                 # only matters with an accounts list (section 29)
  hedge_guard: correlated      # correlated | same_product | off - no opposite positions across accounts
  stagger_seconds: 3           # pause between starting each account's bot

accounts: []                   # one entry per account; each overrides only what differs (section 29)

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
| `topstep-bot` | Start the dashboard (same as `server`); set up and start the bot there |
| `topstep-bot server [--no-browser]` | Start the dashboard and Telegram without starting the bot or asking anything |
| `topstep-bot menu` | The text menu ([section 6](#6-the-text-menu-optional)) |
| `topstep-bot setup` | The question-and-answer setup in the console (the dashboard's Setup tab does the same) |
| `topstep-bot check` | Test login, list accounts, show the contract |
| `topstep-bot strategies` | Describe strategies and parameters |
| `topstep-bot rules` | Topstep's rules for your account and how the bot enforces each one |
| `topstep-bot backtest [--data F] [--download] [--days N] [--strategy S] [--symbol X] [--timeframe M] [--tz TZ] [--no-open]` | Backtest and open a report |
| `topstep-bot demo` | Backtest on synthetic data |
| `topstep-bot train [--data F] [--days N] [--tz TZ]` | Teach the bot which strategy works at which time of day from recent real data ([section 17](#17-training-and-the-knowledge-base-how-the-bot-learns)) |
| `topstep-bot tune [--days N] [--strategies A,B] [--folds K] [--save\|--no-save] [--data F]` | Walk-forward test of single strategies and their settings ([section 18](#18-tuning-test-strategy-settings-on-unseen-data)) |
| `topstep-bot download [--days N] [--tf M]` | Save history to `data/` |
| `topstep-bot start [--mode paper\|live] [--yes] [--no-bot] [--no-browser]` | Start the dashboard + Telegram **and** the bot, which runs 24/7 (`service` does the same) |
| `topstep-bot run [--mode paper\|live] [--yes]` | Run only the trading bot, without dashboard (normally started for you by `start`) |
| `topstep-bot flatten [--yes]` | Emergency: close all positions, then cancel all orders, on every account; reports anything still open. Does not halt a running bot. |
| `topstep-bot journal [--mode paper\|live] [--limit N]` | Recent trades and daily results |
| `topstep-bot telegram-test` | Check the Telegram token and chat ID with a test message |
| `topstep-bot preflight [--days N] [--skip-backtest]` | Check everything before trading live |
| `topstep-bot go-live [--days N] [--skip-backtest]` | Preflight, then start live trading (24/7 or this session) |
| `topstep-bot autostart on\|off\|status` | Start everything when you sign in to Windows |
| `topstep-bot logs [--all] [--open] [--bundle]` | Recent errors, open the log folder, or zip logs for support |
| `topstep-bot update [--check] [--yes] [--token] [--undo]` | Check GitHub for a newer version and install it; `--token` sets up a GitHub token (only for a private repository), `--undo` goes back to the version before the last update ([Updating](#updating)) |
| `topstep-bot learn [--days N] [--import CSV] [--offline]` | Grow the long-run memory: download the history it's missing, add a CSV, then replay all of it through every strategy ([section 17](#the-long-run-memory-a-much-bigger-knowledge-base)) |
| `topstep-bot decision-lab` | Decision lab: replay the classic and the evidence rule on the long-run memory without peeking and pick one ([section 38](#the-decision-rule-and-the-decision-lab)); `learn` runs it at the end too |
| `topstep-bot first-trade-test [--days N] [--data F] [--download]` | Backtest the first trade after starting against a coin flip ([section 17](#the-first-trade-after-starting)) |
| `topstep-bot accounts [--check]` | Every account: what it trades, its mode and key, and which accounts share the same calls; `--check` also asks TopstepX about each one (balance, can trade, open positions and orders, today's trades) ([section 29](#29-trading-several-accounts)) |
| `topstep-bot keys [add\|remove\|test] [NAME]` | List, add (tests the login first), remove or test stored API keys ([section 30](#30-several-api-keys)) |
| `... --account NAME` | With several accounts: `preflight`, `go-live`, `flatten`, `journal`, `logs`, `rules` and `start --mode` act on one account (default: all); `backtest`, `train`, `download`, `tune`, `learn`, `insights` use that account's strategy and market |
| `topstep-bot simulate [--starts N] [--symbols A,B] [--scope all\|tradable] [--classes fx,metals] [--strategies A,B] [--no-teach] [--show]` | Simulation Lab: replay the bot through history as fresh Combines started on N days per market and strategy, on every market with history (or the ones you name), score every playbook and every time-of-day opportunity, then teach the long-run memory; `--show` prints the last report ([section 34](#34-simulation-lab-hundreds-of-combines-from-history)) |
| `topstep-bot markets [--download] [--yahoo] [--symbols A,B]` | Every market Topstep permits, with the history the bot has for each; `--download` brings all of them up to date (TopstepX when your login works, Yahoo otherwise or with `--yahoo`) ([section 36](#36-every-market-topstep-permits)) |
| `topstep-bot network [--symbols A,B] [--show]` | Build the market network from the history on this PC and print it; `--show` prints the last one without rebuilding ([section 40](#40-the-market-network)) |
| `topstep-bot coach ["question"] [--status]` | Run an AI coaching session (uses your Claude API credits), or `--status` for lessons, proposals and spending ([section 35](#35-the-ai-coach-claude-teaches-the-bot)) |
| `topstep-bot insights [--csv FILE] [--longrun]` | What the bot has learned: results after costs, real fills, market conditions; or export every observation to CSV ([section 17](#what-the-bot-learned-results-after-costs-real-fills-and-conditions)) |

On Windows you can also pass commands through the launcher, e.g. `start.bat menu` or
`start.bat tune --days 730`. Double-clicking `start.bat` with no command starts the dashboard.

---

## 26. Files the bot creates

| Path | Contents |
|---|---|
| `config.yaml` | Your settings (`config.yaml.bak`: the version before `tune` last saved settings) |
| `.env` | Your API credentials, Telegram token, webhook URLs and the Claude API key (shared by the AI coach and the Ask tab) — **private** |
| `data/journal_paper.db`, `data/journal_live.db` | Trade journal, daily results, MLL floor, paper balance, and every decision record (why each signal was taken, passed on or skipped) |
| `data/<SYMBOL>_1m.csv` | Downloaded history |
| `reports/*.html` | Backtest and tuning reports (tuning also writes a `.json` with every detail) |
| `logs/` | Log files — see [section 22](#22-logs-finding-out-what-happened) |
| `data/remote_settings.json` | Settings changed from the dashboard/Telegram (delete it, or `/reset`, to undo) |
| `data/knowledge_<SYMBOL>_<TF>m.json` | The knowledge base: what works when, with each observation's market snapshot, price path and costs |
| `data/knowledge_<SYMBOL>_<TF>m.ledger.jsonl` | Every live observation (real, manual, ideas), one per line, never trimmed: used to repair the knowledge base ([Keeping it safe](#keeping-what-it-learned-safe)). To start learning from scratch, stop the bot and delete this and the file above |
| `data/knowledge_backups/` | A daily copy of the knowledge base (the last `knowledge.backups_kept`) |
| `data/market_library.sqlite` | The long-run memory's market library: every price bar the bot has downloaded or imported |
| `data/knowledge_<SYMBOL>_<TF>m_longrun.json` | What every strategy did on the whole library (reports only; rebuilt daily, safe to delete) |
| `day_goals.json` (in the account's folder) | Each trading day's profit goal, who set it and when (the last 60 days). Delete it to forget them |
| `data/trend_<SYMBOL>_<TF>m.json` | The trend test's last result: the bot's history replayed with and without the trend filter, and whether `trend.filter: auto` uses it (safe to delete) |
| `data/lab_<SYMBOL>_<TF>m.json` | The decision lab's last result: both rules' replayed results and which one `policy: auto` uses (safe to delete) |
| `data/news_cache.json` | This week's economic calendar |
| `data/research/` | The research library ([section 33](#33-research-knowledge-that-grows-from-the-internet)): `calendar.json` (every calendar seen), `headlines_<ROOT>.jsonl`, `daily_<ROOT>.json` (daily history), `watched_pages.json` (rule pages). Safe to delete |
| `data/market_journal.jsonl` | One line per finished session: range, gap, overnight levels, the day's reports and the bot's result |
| `data/accounts/<name>/`, `logs/accounts/<name>/` | With several accounts: each account's journal, knowledge base, remote settings, heartbeat and logs ([section 29](#29-trading-several-accounts)) |
| `data/positions/` | What each account holds, for the hedge guard (rewritten while the bots run) |
| `market_pick.json` (in the account's folder) | With the market on Auto: today's pick, the reason, and every candidate's score |
| `market_today.json` (in the account's folder) | A market you switched the account to for the day, and when the switch ends (section 37). Delete it to go back early |
| `data/research/daily_<MARKET>.json` | Ten years of daily prices per market: the long-term context in the Markets list (how busy a market is now compared with its own past) |
| `data/simlab/` | The Simulation Lab: `report.json` (the latest), `runs.csv` (every simulated Combine, opens in Excel), `history/` (the last 20 reports). Safe to delete |
| `data/coach/` | The AI coach: `lessons.json`, `proposals.json`, `sessions/` (each session's report, tool calls and cost), `latest_report.md`, `usage.jsonl` (every API call's tokens and cost) |
| `data/network.json` | The market network (section 40): rebuilt every hour while the dashboard runs. Safe to delete |
| `data/controller.json` | The mode you chose last (Paper/Live) and whether phone access is on |
| `data/tools/cloudflared.exe` | Cloudflare's tunnel program for phone access (downloaded the first time you turn it on) |
| `data/bot_exit.json` | Why the bot last exited (shown on the dashboard) |
| `data/heartbeat` | "Still alive" signal the controller watches |
| `data/updates.json` | Update checks: the last result, which version is installed, a scheduled install |
| `data/updates/backup_*` | Files the last 3 updates replaced (used to undo an update) |

---

## 27. Troubleshooting

**The log keeps saying "hub connection lost: server closed connection" every few seconds.** TopstepX
is closing new realtime connections made with an older session token. Seen when two accounts (two
bots) sign in with the same API key: after a network blip, the bot that signed in first could not
reconnect until it was restarted. The bot now signs in again by itself after two quick closes in a
row ("signing in again for a fresh session token" in the log). If the lines keep coming after that,
restart that account.

**Lots of "getaddrinfo failed" lines.** The PC could not look up TopstepX's address: the internet or
its DNS dropped. Orders already at TopstepX (your stop and target) keep working; the bot carries on
when the connection is back. If it happens often, a wired connection or another DNS server (your
router's settings, or 1.1.1.1) usually helps. A debugging proxy such as Fiddler left running routes
all of the PC's traffic through itself: close it while the bot trades.

**Phone access: the Open dashboard button doesn't appear.** Look at **Settings → Phone access** on
the PC dashboard: it says what the link is doing. "Cloudflare did not answer in time" or
"cloudflared stopped" usually means the PC is offline or a firewall or antivirus blocks
`cloudflared.exe`: allow it and the bot retries by itself. If the download fails, install it with
`winget install Cloudflare.cloudflared` in a Command Prompt and turn phone access off and on.

**Phone access: "Only the owner's Telegram account can open this dashboard".** You opened it from a
different Telegram account than the one in your chat with the bot, or your chat is a group: put
your own Telegram user ID in `telegram.allowed_user_ids`. **"This page only opens from your Topstep
bot in Telegram"** means the address was opened in a normal browser: use the button in Telegram.

**Phone access: "Lost contact with your PC".** The PC is off or offline, the Topstep Bot window was
closed, or the link restarted with a new address: send `/dashboard` for a fresh button.

**Phone access: the dashboard is empty, or says "Part of the dashboard could not be shown".** The
bot keeps trading either way: only the page is affected. Install the latest update (Settings →
Updates); older versions could open an empty dashboard on some phones. The notice names the part
that failed, and the PC's **Logs** tab has the same line (*"The dashboard on your phone hit a
problem showing the page: …"*). Send that line, or the zip from `start.bat logs --bundle`, with your
report.

**"Login failed"** — Use your TopstepX *username*, not your email. Copy the API key again in full.
Check your API subscription is active. Paste the key again on the dashboard's Setup tab and press
**Connect**.

**"Several accounts can trade"** — Pick one on the dashboard's Setup tab, or set `account.account_id`
(`topstep-bot check` lists the IDs).

**"No contract found for symbol"** — Check the symbol spelling, or pin `instrument.contract_id`.

**"Configuration problem"** — The message names the exact setting and what's wrong with it (e.g.
`risk.risk_per_trad: unknown setting`). Fix it in `config.yaml` with Notepad; indentation (spaces)
matters in YAML.

**The bot never trades.** With the `adaptive` strategy, first open Knowledge → What works: if it is not
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

**Dashboard doesn't open** — Browse to http://127.0.0.1:8765 yourself. If another program uses that
port, change `dashboard.port` in `config.yaml`.

**"disconnected" on the dashboard** — The bot reconnects automatically; while disconnected it
polls prices instead. If it persists, check your internet connection.

**"TopstepX can't be reached ... the bot keeps trying"** — the bot was started (or restarted) while
your internet or TopstepX was down. It waits and tries again (15 seconds, then up to every 5
minutes) and starts by itself as soon as TopstepX answers; nothing needs doing. You get one alert,
not one per try.

**"The trade journal was damaged and has been set aside"** — the journal (`journal_<mode>.db`) could
not be read, for example after a power cut or a sync tool (OneDrive, Dropbox) overwrote it. The bot
kept it as `….damaged-<time>` and started a new one, so it still runs. The journal is where the bot
remembers your trailing Maximum Loss Limit: compare the MLL the dashboard shows with the one on
TopstepX and, if they differ, type TopstepX's value as `account.mll_floor_override` (applied once). In live mode new
trades stay paused until you press **Resume**. Keep the data folder out of synced folders.

**"Could not cancel order ... will check again" / "Entry skipped: earlier order(s) ... may still be
working"** — a request to TopstepX timed out, so the bot can't be sure an old stop or target is gone.
It doesn't open a new trade until the next check confirms it (within the reconcile interval,
15 seconds by default) and cancels it if it is still there. **"Entry order may not have gone
through ... checking with the broker"** is the same for an entry: the bot waits for TopstepX to show
whether it filled, protects it with a stop if it did, and drops it if it didn't.

**"Position ... is on the wrong side for the ... trade - closing it"** — the account ended up long
when the bot's trade was short (or the reverse), e.g. an old order filled after a connection
problem. The bot closes it rather than protect it with orders meant for the other side.

**A position the bot didn't open was closed** — That is `orphan_position_policy: flatten`. Don't
trade manually on the bot's account, or change the policy.

**The bot's MLL floor differs from Topstep's** — Type Topstep's value as `account.mll_floor_override`.
The bot applies a new number once and then follows its own trailing floor; to sync again, change the
number.

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

**The dashboard doesn't load** — The black bot window was closed (or the PC restarted).
Double-click `start.bat` again, or tick **Start with Windows** on the Setup tab. If it's already
running, double-clicking `start.bat` just opens the dashboard; you can also open
http://127.0.0.1:8765 yourself.

**Start is greyed out** — Setup isn't finished, or `config.yaml` has a mistake. Hover over the
button to see why, and open the **Setup** tab: it says what's missing.

**Telegram: no reply to commands** — Check the bot window says `Telegram control: on`. Run
`topstep-bot telegram-test`. Make sure you're messaging from the chat that was set up (a different
chat is ignored — `logs/bot.log` shows "Ignored Telegram command from unauthorized chat ..." with
its ID). Only one program can read a bot's messages: close any other copy of the bot.

**Telegram: "the bot token was rejected"** — The token is wrong or was revoked. On the Setup tab,
press **Use another bot** and paste a new token from @BotFather.

**Updates: "GitHub did not show the repository"** — The repository is private (or `updates.repo` in
`config.yaml` is misspelled). For a private repository, paste a token on the dashboard (Settings tab, Updates), or run
`start.bat update --token`,
and follow the steps in [Updating](#updating). **"GitHub rejected the token"** means it expired or
was deleted: make a new one, or delete the `GITHUB_TOKEN` line in `.env` if the repository is public.

**Updates: "GitHub's hourly limit for update checks was reached"** — Without a token GitHub allows
60 requests an hour from your internet connection, and a check uses a handful. It clears by itself
within the hour; the next automatic check tries again.

**Updates: "A trade is open"** — Updates only install while the bot is flat. Let the trade finish,
or choose **Install after the close**.

**Updates: "bot files were edited on this PC"** (git folders) — You (or an editor) changed one of the
bot's own files. Undo the change (`git checkout -- <file>`) or commit it, then update again.

**After an update the bot behaves differently** — Every change is listed on the Settings tab before
you install. To go back, close the bot window and run `start.bat update --undo`.

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

## 29. Trading several accounts

If you have more than one Combine (or Express/Practice account) under the same TopstepX login, the
bot can trade them together. Each account gets **its own bot process**, so one account crashing,
being stopped or hitting its loss limit never affects the others. One dashboard and one Telegram
chat control all of them.

### Same calls or different calls?

| You want | How | What happens |
|---|---|---|
| **Same calls** on every account | List the accounts without overriding `strategy`/`instrument` | Every account runs the same strategy on the same bars, so they get the same signal at the same moment. Each one sizes the trade from **its own** risk settings and checks **its own** limits — an account that is done for the day simply skips it. |
| **Different calls** | Give an account its own `strategy:` (and/or `instrument:`) | That account trades its own signals, e.g. Adaptive on one Combine and ORB on another, or MNQ on one and MES on another. |
| **Copy one account's calls** | `follow: c1` | The account trades whatever c1's strategy and market are (even if you change them later), with its own risk. |
| **Same calls, different size** | Same strategy, different `risk:` | e.g. $150 per trade on a 50K, $300 on a 100K. |

`topstep-bot accounts` (menu **21**) shows every account, what it trades, and which accounts share
the same calls; `topstep-bot accounts --check` also asks TopstepX about each one, like the Setup tab. The dashboard shows the same grouping under the account cards.

### Set it up

**On the dashboard** (easiest): Setup tab → **2. Accounts** → **+ Add account**. The form lists
every account your API keys can see that isn't set up yet, with its balance and whether it can
trade; pick one and the bot fills in its size and type from the name and balance (check them).
Give it a short name, then choose:

- **API key**: the key that can see the account is picked for you.
- **Trade calls**: *Same calls as the shared settings*, *Copy another account's calls* or *Its own
  strategy*.
- **Market**: the shared market, a market of its own, or **Auto** (the bot picks).
- **Risk**: per trade, daily limit and trades per day. Values equal to the shared settings aren't
  copied, so a later change to the shared settings reaches this account too.

**Save account** writes it into `config.yaml` (the previous file is kept as `config.yaml.bak`) and
its card appears at the top of the dashboard at once, stopped and in **Paper** mode. Each account's
card on the Setup tab has **Edit** and **Remove**; the bot must be stopped to remove an account or
rename it.

The **first** extra account turns your single account into an `accounts:` list. Your current account
gets a short name (`c1` unless you choose another), and its trade journal, knowledge base and remote
settings are **copied** into `data/accounts/c1/`, so nothing it has learned is lost (the originals
stay where they were). Stop the bot before saving this first one. With more than one account, the
fields under the cards edit the **shared settings** every account uses unless it sets its own.

The setup wizard (menu 1) can do the same in the console. Or edit `config.yaml` by hand — everything
above the `accounts:` list is shared, and each account lists only what is different:

```yaml
mode: paper
strategy: {name: adaptive}
risk:
  risk_per_trade: 150
  personal_daily_loss_limit: 500

multi_account:
  hedge_guard: correlated     # correlated (default) | same_product | off
  stagger_seconds: 3          # start the bots a few seconds apart

accounts:
  - name: c1                  # short name: letters, digits, - and _ (used on the dashboard and in Telegram)
    account_id: 111111        # from 'topstep-bot check'
  - name: c2
    account_id: 222222
    plan: "100K"              # plan/stage/account shortcuts, or a full account: {...} section
    risk: {risk_per_trade: 300}
  - name: c3
    account_id: 333333
    strategy: {name: orb}     # different calls
    instrument: {symbol: MES}
  - name: c4
    account_id: 444444
    follow: c3                # c3's calls, this account's risk
    mode: live                # this account starts in live mode; the others use the shared mode
  - name: old
    account_id: 555555
    enabled: false            # kept in the file, not traded
```

Sections an account can override: `account` (or the shortcuts `account_id`, `account_name`, `plan`,
`stage`, `starting_balance`, `mll_floor_override`, `topstep_daily_loss_limit`), `instrument`,
`strategy`, `risk`, `session`, `execution`, `data`, `notifications`, `news`, `recommendations` and
`knowledge`, plus `mode` and `enabled`. Everything else (Telegram, dashboard, service...) is shared.
An account that changes strategy doesn't inherit the shared strategy's `params`. An account that
`follow`s another can't also set `strategy`, `instrument`, `session`, `knowledge`, `news` or
`recommendations` (those decide the calls), and you can't follow an account that itself follows.
Names like `all`, `main`, `risk` or `news` are reserved because Telegram would confuse them.

Without an `accounts:` list nothing changes: the bot trades the single account in `account:` exactly
as before, and its files stay where they were.

### The hedge guard

Topstep prohibits **hedging across accounts** — holding opposite positions in the same or a
correlated market on two of your accounts (for example long MNQ on one Combine and short NQ on
another). Accounts with different calls will sometimes disagree, so before any entry the bot checks
what your other accounts hold. If the trade would be opposite to another account's position, it is
skipped (you'll see *"Skipped SHORT: account c1 is long MNQ - opposite positions across accounts
count as hedging, which Topstep prohibits"* in Activity, and the idea is marked skipped). The first
account to enter wins; the others can trade again once it's flat.

| `hedge_guard` | Blocks |
|---|---|
| `correlated` (default, safest) | Opposite positions in the same product **or** a related one: all US equity index futures (ES/MES, NQ/MNQ, YM/MYM, RTY/M2K) count as one group; gold and crude oil are their own groups |
| `same_product` | Only the same product (NQ with MNQ, ES with MES...) |
| `off` | Nothing — only if you are sure your accounts can never disagree |

The guard covers trades placed by the bot, including ideas you take from the dashboard or Telegram.
Paper and live accounts are checked separately (paper positions aren't real). It **can't see trades
you place by hand** in TopstepX on accounts the bot doesn't run — keep those in mind yourself. The
rule's exact wording can change; check help.topstep.com.

### The dashboard with several accounts

A card per account sits under the top bar: its state, Paper/LIVE, market and strategy, today's P&L
and position. **Click a card** to select that account — every tab (Overview, Ideas, Knowledge,
Settings, Logs) and every button (Start/Stop/Restart, the Paper | Live switch, Pause, Flatten,
setting changes, Take idea) then applies to that account only. The **All accounts** row below the
cards pauses, resumes, starts, stops or flattens every account at once. The dashboard remembers
which account you had selected. The **Setup** tab edits the shared settings at the top of
`config.yaml`; per-account differences live in the `accounts:` list (edit the file, or use the
console setup, menu 1).

### Telegram with several accounts

Name the account after the command, or `all`:

```
/status            one line per account
/status c2         full status of c2
/pause all         pause every account          /pause c1   just c1
/flatten all       close everything everywhere (asks to confirm)
/set c1 risk 150   change a setting on c1       /set all news off   on every account
/ideas c3          c3's ideas, with Take buttons
```

Leave the name out and the bot replies with a button per account (and **All accounts** for
pause/resume/flatten/stop/restart/startbot/reset). `/ideas`, `/trades`, `/settings`, `/knowledge`,
`/train`, `/log`, `/brief`, `/next`, `/learn`, `/market` and `/funded` work on one account at a time. Alerts say which account they're from, e.g.
*[Topstep Bot (c2, live)] ENTRY ...*.

### Modes, files and logs

- **Mode per account.** Each account can be paper or live independently: use the dashboard switch
  with the account selected, `mode:` on the account in `config.yaml`, or `start --mode live --account c2`.
  `start --mode live` (and menu 7) switches every account. The dashboard remembers each account's mode.
- **Separate files.** Each account keeps its own journal, remote setting changes, knowledge base,
  heartbeat and logs under `data/accounts/<name>/` and `logs/accounts/<name>/`. Accounts that trade
  the same calls each learn from the same signals, plus their own real trades.
- **Separate limits.** Daily loss limit, trade count, losing streak, MLL tracking and the ramp-up are
  all per account. Risk is not pooled: three accounts with $500 daily limits can lose up to $1,500
  between them on a bad day.
- **Kill switch.** A file named `KILL` in the bot's folder halts **every** account.
- **Commands.** `preflight`, `go-live`, `flatten`, `journal` and `logs` cover every account unless
  you add `--account`; `backtest`, `train` and `download` need `--account` to know which account's
  strategy and market to use.
- **API usage.** Each bot logs in and downloads its own history. TopstepX allows 50 history requests
  per 30 seconds; the staggered start keeps a handful of accounts well inside that. Running more than
  about five at once isn't recommended.

### Good practice

- Start with every account in **paper** mode for a while, as with one account.
- Accounts on the **same calls** multiply both the wins and the losses — it's the same bet several
  times. Size each account so that a bad day on all of them at once is acceptable.
- **Different calls** on the same market will often be skipped by the hedge guard when they disagree.
  Different markets in different groups (e.g. equity index on one account, gold on another) avoid
  that.
- Topstep limits how many Express Funded Accounts you may hold, and its multi-account rules change
  from time to time — check the current rules on help.topstep.com before scaling up.

---

## 30. Several API keys

One API key works for every account on your TopstepX login, and that is all you need. You can store
more, for two reasons:

- **One key per account** keeps the accounts' connections apart, so a problem with one key (revoked,
  rate-limited, a login hiccup) only affects that account.
- **Another TopstepX login** (a different username) — its accounts can then be traded too.

Keys live only in `.env`, never in `config.yaml`:

```
TOPSTEPX_USERNAME=myname            # the main login
TOPSTEPX_API_KEY=...                # the main key
TOPSTEPX_API_KEY_C2=...             # a key named "c2" (same username as the main login)
TOPSTEPX_USERNAME_SECOND=partner    # a key named "second" for another login
TOPSTEPX_API_KEY_SECOND=...
```

**Which key an account uses:** its `login:` setting if it has one, otherwise a key with the same
name as the account (so a key named `c2` is used by account `c2` automatically), otherwise the main
key. `topstep-bot accounts` shows the key each account uses.

```yaml
accounts:
  - name: c1                  # main key
  - name: c2                  # TOPSTEPX_API_KEY_C2, automatically
  - name: p1
    account_id: 999999
    login: second             # the "second" login's key
```

Manage them with `topstep-bot keys` (menu **22**):

| Command | What it does |
|---|---|
| `topstep-bot keys` | Lists the stored keys (only the last 4 characters shown) and which accounts use each; flags accounts whose key is missing |
| `topstep-bot keys add c2` | Asks for the username (default: the main one) and the key, **tests the login**, then saves it. Nothing is saved if the login fails |
| `topstep-bot keys test` | Logs in with every key and shows how many accounts each sees |
| `topstep-bot keys remove c2` | Deletes the key from `.env` (and warns if an account still uses it) |

The dashboard does the same: Setup tab → **More API keys** → **+ Add another API key** (tested
before it is saved; each key shows whether it works and how many accounts it sees), and **Remove**
(refused while an account still uses the key). The setup wizard can also store keys for another
login and give each account its own key. Every
stored key is masked in log files. If a key is missing when a bot starts, that account stops with
*"No API key named ..."* on the dashboard; the others carry on.

---

## 31. What the bot knows about the market and the news

The bot now knows the market it trades, not just its own strategies. All of this is on the dashboard's
**Knowledge** tab → **Market data** page (for the market shown on the markets strip) and in Telegram
**/market** (`/brief` is still "what the bot knows" about its own strategies, [section 17](#what-the-bot-knows-and-when-it-trades-next)).

**The contract.** For each market (S&P, Nasdaq, Dow, Russell, gold, crude — minis and micros): exchange,
contract months, expiry, how it rolls, regular hours, tick and point value, what moves it (e.g.
mega-cap tech earnings for NQ, the weekly EIA report for crude) and practical notes (NQ moves 1.3-1.6x
as much as ES; the Russell gaps and fakes out more often).

**Today's numbers**, from the bars the bot already receives — no extra data subscription:

| | |
|---|---|
| Key levels | Yesterday's regular-session high / low / close, today's overnight high / low, open, high and low, and how far price is from each |
| Normal day's range | The average daily range of the last 14 sessions, in points and in dollars per contract (useful for sizing stops) |
| Today so far | Today's range and what share of a normal day it has used; the opening gap |
| Long-term context | Where the recent normal range sits among ten years of daily ranges, e.g. "wilder than 85% of days" (needs the research library, [section 33](#33-research-knowledge-that-grows-from-the-internet)) |
| Notes | Plain-language observations: a big gap, an already-extended day, a quiet day, above/below/inside yesterday's range |

**The news that matters for it.** Before, the bot paused around high-impact US releases. Now, with
`news.per_instrument: true` (default), it also pauses around the **medium-impact reports that move your
market** — e.g. the Wednesday EIA crude inventories for CL/MCL, ISM and jobless claims for the index
futures. Knowledge → Market data lists today's reports with forecast and previous values, plus the next three
days.

**Headlines.** The latest headlines about your market (from Yahoo Finance's free feeds for the future
and the ETFs that track it: SPY, QQQ, DIA, IWM, GLD, USO), refreshed every 15 minutes, older than three
days dropped. **They are for your information — the bot does not trade on headlines.** Turn them off
with `news.headlines: false`.

**It keeps a record.** Each finished session is added to `market_journal.jsonl` — range, gap, overnight
levels, the reports that day and the bot's own result — so you build up a history of market
conditions next to your trades.

**It learns from the news.** Every live observation in the knowledge base now notes whether the day had
a market-moving report. The **news / quiet days** column on Knowledge → What works (and `/knowledge`) shows each
strategy's average result on each kind of day. Replayed history is labelled too, from the research
library's calendar archive ([section 33](#33-research-knowledge-that-grows-from-the-internet)) — but
only for days the archive covers; older days count as "unknown", never as "quiet". The bot doesn't act
on this yet: it needs weeks of observations before the difference means anything.

### Letting the bot pick the market

Set the market to **Auto** (Setup tab, or an account's **Market** in the Add/Edit form), or in
`config.yaml`. There are two Auto choices: **Auto** picks from the micro index, gold and crude
contracts; **Auto: every Topstep market** picks from all 51 markets Topstep permits (the ones your plan
can't trade right now are left out).

```yaml
instrument:
  symbol: MNQ          # used on days no market shows an edge
  auto_pick: true
  candidates: []       # default: the micros your plan allows (MNQ, MES, M2K, MYM, MGC, MCL)
  # candidates: [ALL]          every market Topstep permits
  # candidates: [MICROS, FX]   every micro, plus every currency future
  # candidates: [MNQ, 6E, GC]  just these
```

Each time the bot starts (so every trading day, after the daily restart) it replays the recent
history of every candidate through **this account's** strategy, the same way training does. The
replayed days are split: what it learns on the earlier two thirds decides which signals it would
have taken on the last third, and **only that held-back part is scored**: R per day after costs,
from trades sized with this account's risk (at most your trades-per-day). That keeps the choice
honest: a market is picked for what worked on days the bot hadn't learned from, not for what it was
fitted to.

- A market needs at least 6 such trades and a result above zero. If none qualifies, the bot trades
  `instrument.symbol` and says why.
- Signals whose stop is too wide for even one contract at your risk per trade don't count; a market
  where most are is skipped ("too big for your risk").
- The current market is kept unless another beats it clearly (0.05R per day), so the bot doesn't
  hop between markets on noise.
- **With more than 8 candidates** the pick must also have made money on the earlier two thirds, not
  only on the held-back days. Comparing 48 markets on a few weeks always produces a lucky winner; this
  rule throws most of those out ("made money only on the held-back days ... likely luck").
- Scoring every market takes a few seconds per market, so a start with **every market** takes a
  minute or two longer. The bot keeps the dashboard informed meanwhile.
- An open position on the account keeps its market. A pick is reused for 20 hours, so a restart
  during the day doesn't pick again; changing the strategy, plan, risk or candidates picks again.
- The **Why this market** panel (Knowledge → Market data) shows every candidate's result and the
  reason; the pick is saved in `market_pick.json` in the account's folder.
- **Several at once:** with `max_markets` above 1 (Setup tab → **Markets at once**), the bot also trades
  the next best markets that qualified, up to that many, never two sizes of one product
  ([section 41](#41-several-markets-at-once)).

**Honest limits.** TopstepX keeps about 60 days of intraday history, so the held-back part is two to
four weeks until the long-run memory has collected more (it uses up to 120 days when it has them).
That is a short sample: the minimum and the margin make luck less likely to decide, not impossible.
On the history tested while building this, no market showed an edge, and the bot correctly stayed
on the set market rather than chasing the least-bad loser. Auto never makes the bot trade bigger
or more often; every risk setting and Topstep rule applies as before.

---

## 32. Funded-account goals and Topstep's rules

The bot knows the rules of each account stage and tracks your progress toward the goal that matters
there. Overview's **Account goal** panel and Telegram **/funded** show it; **/rules** lists every rule
with what the bot does about it.

| Stage | Goal | What the bot tracks | What the bot does |
|---|---|---|---|
| Combine | Hit the profit target ($3K / $6K / $9K) without touching the MLL | Profit vs target; your best day against the Consistency Target (55% of the profit target) and the raised target if it's over | Stops for the day at 40% of the target and closes an open trade at 50%, so one big day can't raise the target ([section 14](#14-topstep-rules-what-the-bot-enforces-and-what-is-still-on-you)) |
| Express — **standard** path | 5 winning days of $150+ net per payout | Winning days since your last payout, today's progress to $150, an estimate of what you could request (50% of balance, capped), your Scaling Plan limit | Once today is a winning day, sizes new trades so a stop-out leaves the day at $150+ (`protect_winning_day`) |
| Express — **consistency** path | Best day ≤ 40% of profit over at least 3 trading days | Trading days, the ratio, the daily cap | Caps each day at 40% of `cycle_profit_goal` (default: the plan's profit target) |

Set your path and last payout so the counts are right:

```yaml
account:
  stage: express
  payout_path: standard        # or consistency - whatever you chose when the XFA was activated
funded:
  last_payout_date: "2026-10-02"   # progress restarts after your last payout request
  protect_winning_day: true        # standard path: keep a winning day a winning day
  cycle_profit_goal: null          # consistency path: profit to build per payout (default: the profit target)
```

**Rules the bot knows** (checked against Topstep's help center on 2026-10-11): Maximum Loss Limit,
optional Daily Loss Limit, flat by 15:10 CT, profit targets and consistency, position limits and the
XFA Scaling Plan, both payout paths and payout amounts, no hedging across accounts, **no trading your
full maximum position into scheduled major news**, **no simulator exploitation** (high-frequency
scalping, tight brackets or auto-breakeven designed to capture unrealistic fills, orders outside the
best bid/offer), and running from your own computer. Preflight now **warns** if the news filter is off
or the breakeven stop is tighter than 1R.

None of this makes the bot trade bigger or more often to reach a goal faster — every limit in your risk
settings still applies, and the goal-based rules only ever make it more careful. Rules change: the
numbers in your TopstepX dashboard always win, and if they differ, tell the bot (`funded:` and `risk:`
settings).

---

## 33. Research: knowledge that grows from the internet

While the dashboard is open, the bot keeps gathering information from free public sources into a
**research library** (`data/research/`). The library only grows: the longer the bot runs, the more
it knows. The **Research library** panel (Knowledge → Market data) and Telegram
**/research** show what it holds and when each source was last collected.

| What | From | How often | What it's used for |
|---|---|---|---|
| **Economic calendar archive** | The same free calendar the news filter uses | Every 6 hours | Every week's calendar is kept, so when the bot retrains it knows which past days had a report that moves your market (the knowledge base's news/quiet days) |
| **Headline archive** | Yahoo Finance feeds for each market your accounts trade, and the ETF that tracks it | Every 30 minutes | A searchable record of what was said about your market, kept next to its sessions |
| **Daily price history** | Yahoo Finance's daily chart for the continuous future | Daily | Up to ten years of daily ranges, so Knowledge → Market data can say whether today and recent days are unusually quiet or wild |
| **Topstep's rule pages** | Eight help.topstep.com articles: Combine and Express Funded parameters, Maximum and Daily Loss Limits, the Consistency Target, the Scaling Plan, API access and prohibited strategies | Every 12 hours | If Topstep changes a page, you get an alert (Telegram/Discord and Activity) with a link. The bot never changes its own rules from a web page: you read the change and update your settings if needed |
| **Your own sources** | RSS feeds or pages you list in `research.sources` | With the headlines | Feeds are archived like headlines; pages are watched for changes like the rule pages |

```yaml
research:
  enabled: true
  calendar: true
  headlines: true
  headline_minutes: 30          # 5-1440
  daily_history: true
  rules_watch: true
  rules_watch_hours: 12         # 1-168
  sources:
    - {name: myfeed, url: "https://example.com/rss", kind: rss}     # archived
    - {name: cme-margins, url: "https://example.com/page", kind: page}  # alert when it changes
```

Good to know:

- **It never trades on any of it.** Nothing in the library places, sizes, blocks or closes a trade.
  It feeds the reports, Knowledge → Market data, the knowledge base's news labels and the rule-change alerts.
- **The controller collects, the bots read.** With several accounts the internet is asked once, for
  every market the accounts trade; each account's bot reads the shared library.
- **A failing source doesn't stop the others.** The panel shows which one failed and why; it tries
  again next time.
- **Polite and small.** A few requests per hour in total, a clear user agent, and the library stays
  small (text and numbers only). Delete `data/research/` at any time to start it again.
- **No internet, no problem.** Trading doesn't depend on it; turn it off with `research.enabled: false`.

---

## 34. Simulation Lab: hundreds of Combines from history

The Simulation Lab answers the question that matters most: **if the bot started a Combine with this
market and strategy, how would it have gone?** It replays the bot through the price history on your
PC, as if it were trading live, many times over.

**What one simulation is.** A fresh Combine account (your plan: target, trailing Maximum Loss Limit,
consistency rule) started on one particular day and played bar by bar through the bot's *own*
trading code: the same risk manager, Topstep guards, sizing, stops and 15:10 CT flatten that trade
live, with simulated fills that include your assumed slippage and fees. It ends when the Combine is
passed, failed, or the history runs out ("still open"). Each playbook is started on several different
days, so one lucky or unlucky start can't decide the result.

**Playbooks.** A playbook is a market plus a strategy, e.g. *MNQ · orb_momentum*. The lab tries
**every market with history on this PC** with every strategy: with all 51 of Topstep's markets that is
408 playbooks × 4 start days = **about 1,600 live-like Combines**, plus one full-history run per
playbook, in about three minutes on a typical PC. To look at fewer, choose **Only this account's picks**
or one asset class next to the Run button (or `lab.markets: tradable`). Markets your plan can't trade
right now (Topstep caps them at zero) are still simulated, marked **not on plan**, and never suggested.
Each market uses its own contract size, tick value and Topstep fees, but your account's settings:
a full-size contract whose stops are too wide for your risk per trade simply takes no trade. The adaptive strategy starts each simulation knowing only what it would have known
that morning: it is trained on the days before the start, never on the days it is about to trade.

**Run it.** Knowledge tab → Simulations → choose the start days per playbook (4 is about a minute; 12 gives 384
Combines) → **Run simulations**. Or `topstep-bot simulate --starts 6`. The bot keeps trading meanwhile.

**Reading the results.**

| What | Means |
|---|---|
| Live-like Combines: passed / failed / still open | The fresh-account runs. With two months of history most end "still open": the target is $3,000 on a 50K, and few playbooks get there that fast |
| Verdict | **Edge**: made money in *both* halves of the history after costs, with neither half close to flat. **Mixed**: made money overall, but most of it in one half (likely luck). **Losing**: lost money. **Too few**: under 10 trades, nothing to judge |
| 1st half / 2nd half | The history split in two. An edge that appears in only one half is the most common way a backtest fools you |
| Resampled | Monte Carlo: 2,000 Combines built by drawing the playbook's own simulated days at random, up to 60 days each. A rough pass / fail probability for a Combine longer than your history. Green = passed, red = failed |
| Held-out check | The playbook that looks best on the *first* half alone, scored on the second half it never saw. Comparing 32 playbooks always produces a winner by luck; this shows how much of the winner's result survives |
| Equity | Each playbook's cumulative profit over the history |
| Opportunities | Every market × strategy × **time of day** (open 08:30-10:00, midday 10:00-13:00, close 13:00-15:10 CT) that made money in *both* halves of the history, strongest first. A strategy that loses over the whole day can still win at the open; this is where you see it. **Strength** is how far the pocket stands out from chance: strong, promising or early. The note under the table says how many pockets were tested and how many would look this good by luck alone, so you know whether the list is evidence or noise |
| By asset class | How many playbooks per class, how many have an edge, and the best one |

**Use a playbook.** The **Use** button on a row (or the button under a confirmed edge) writes that
market and strategy into `config.yaml` (the old file is kept as `config.yaml.bak`). A running bot keeps
its settings until you restart it. Nothing is changed without that click. **Trade today** on an
opportunity switches the account to that market for the rest of the day only
([section 37](#37-trading-another-market-today)).

**Every night.** Each weekday at 15:30 CT (after the 15:10 flatten) the bot refreshes every market's
history and runs the lab on all of them, so the Opportunities list is fresh every morning. It never
changes a setting or places a trade. Turn it off with `lab.nightly: false`.

**Teaching.** With **Teach long-run memory** ticked, every market's history is also added to the
long-run memory and replayed through every strategy, so the *Long run* view (Knowledge → What works) and the
AI coach cover every market the bot could trade. The knowledge base the bot actually trades with is
still retrained by the bot itself from fresh history each day.

**Honest expectations.** On all 51 markets in October 2026 (about 60 trading days each), 7 playbooks
were marked *edge*, but the held-out check did **not** confirm the ranking, and only 17 of 276
time-of-day pockets made money in both halves, none of them standing out from luck. Earlier, on the
four index micros alone, no playbook showed a dependable edge: the strongest, MNQ with the adaptive strategy, made nearly all of its money
in the second half, and the held-out check did not confirm the ranking. That is the lab doing its
job. More history makes it far more useful: the bot saves everything it downloads, so run the lab
again every week or two. Results are evidence, not a promise.

## 35. The AI coach: Claude teaches the bot

The AI coach is Claude, Anthropic's AI model, working as a trading research analyst for your bot. In
a coaching session it reads everything the bot knows, runs its own experiments in the Simulation Lab,
writes down what it learned, and proposes changes. You decide what to apply.

**What it can do in a session:**

- **Read:** your account and Topstep's rules, the bot's settings, the Simulation Lab report, the
  knowledge base (what each strategy's signals have been worth by time of day and volatility), the
  long-run memory, your trade journal and every strategy's settings.
- **Test:** run Simulation Lab experiments of its own: chosen markets and strategies, different
  strategy settings, or a smaller risk per trade. Experiments never replace the lab's main report.
- **Teach:** record **lessons** with their evidence and its confidence. They appear on the AI Coach
  tab and every later session starts from them, so what it learns builds up over time. You can
  remove any lesson with **Forget**.
- **Propose:** a different playbook (market + strategy), or a *lower* risk setting (risk per trade,
  daily loss limit, trades per day). Proposals wait on the Ask tab's **Deep review** page with **Apply** and
  **Dismiss**. Applying writes `config.yaml` (with a backup); the bot uses it after a restart.

**What it can't do:** place, close or size a trade; change a setting by itself; raise your risk; or
touch your TopstepX account. It is instructed to be honest about small samples and to say "no edge
found" rather than tune results until they look good.

### Set it up (5 minutes)

1. **Create a Claude API account and key.** Open
   [platform.claude.com/settings/keys](https://platform.claude.com/settings/keys) (the **Create an API
   key** button on Ask → Deep review), sign in or sign up, and create a key. It starts with `sk-ant-`.
2. **Buy credits.** The Claude API is prepaid. On Ask → Deep review press **Buy credits** (it opens
   [platform.claude.com/settings/billing](https://platform.claude.com/settings/billing)) and add
   credits there, with your own card on Anthropic's site. The bot never sees or handles payment
   details. $5–$10 is plenty to start.
3. **Paste the key** into **Claude API → API key** and press **Test and save**. The bot checks it with
   Claude (a free call), then saves it in `.env` on this PC as `COACH_ANTHROPIC_API_KEY` (the Ask tab's AI answers use the same key). It is never
   shown again, never sent to the phone, and masked in every log.
4. **Set your limits** under **Spending**: the model, how hard it thinks (effort), the most one session
   may cost, and the most per month. Press **Save limits**.

### Run a session

Type what you want looked into, or leave it empty for a full review, and press **Start session**.
Examples: *"Is MNQ a better market than M2K for my 50K Combine?"*, *"Should I lower my risk per
trade?"*, *"Why has orb been losing lately?"*. The panel shows what the coach is doing (reading the
journal, simulating orb on MNQ, …), the time so far and the cost so far. A full review usually takes
a few minutes. When it finishes you get a written report, plus any lessons and proposals.

From the command line: `topstep-bot coach "Should I trade MNQ instead?"`. In Telegram, `/coach`
shows the latest lessons, proposals and spending.

### Cost

You pay Anthropic per token (roughly per word read and written). Every call is priced and logged in
`data/coach/usage.jsonl`, and Ask → Deep review shows this month's total.

| Model | Price per million tokens (input / output) | Use it for |
|---|---|---|
| Claude Opus 5.5 (default) | $4 / $20 | The recommended choice: thorough and careful |
| Claude Sonnet 5.5 | $2 / $10 | About half the price, still strong |
| Claude Haiku 5.5 | $0.10 / $0.50 | Quick, cheap check-ins |
| Claude Fable 5.1 | $10 / $50 | Anthropic's most capable model, for the hardest questions |

A full review on Opus at High effort typically costs well under a dollar. The two limits make
surprises impossible: a session stops itself at `coach.max_session_usd` (it is asked to write its
report as the limit approaches) and no session starts once `coach.monthly_budget_usd` is reached.

**Good to know:**

- **Your data.** A session sends Anthropic the evidence it reads: settings, simulation results,
  knowledge summaries and your trade journal (no API keys, passwords or account credentials).
- **If a request is declined** for safety reasons, Claude's API automatically retries it on another
  Claude model, and the cost of each attempt is counted.
- **Errors in plain words:** a refused key, too little credit ("Buy credits at …"), a rate limit or no
  internet are shown on the tab and in the Activity feed. Nothing about trading depends on the coach.
- **Turn it off** with `coach.enabled: false`, or simply never add a key.

## 36. Every market Topstep permits

Topstep permits 51 futures, and the bot knows every one: contract size, tick value, Topstep's fees,
busiest hours, what moves it and which reports matter for it.

| Class | Markets |
|---|---|
| Equity index | ES, MES, NQ, MNQ, RTY, M2K, YM, MYM, NKD |
| Crypto | MBT (micro bitcoin), MET (micro ether) |
| Currencies | 6A, M6A, 6B, M6B, 6C, 6E, E7, M6E, 6J, 6S, 6M, 6N |
| Energy | CL, QM, MCL, NG, QG, MNG, RB, HO |
| Metals | GC, MGC, SI, SIL, HG, MHG, PL |
| Interest rates | ZT, ZF, ZN, TN, ZB, UB |
| Agriculture | ZC, ZW, ZS, ZM, ZL (grains), HE, LE (livestock) |

**Their history.** The **Markets and price history** panel (Knowledge → Simulations) lists every market with
the history on this PC, where it came from, how busy the market is now compared with its own last ten
years, its 20-day trend and whether your plan may trade it. **Download every market** brings them all up
to date (also `topstep-bot markets --download`, or menu 23):

- **TopstepX** first when your login works: the actual contract the bot trades, about 60 days of
  5-minute bars.
- **Yahoo Finance** for everything else, no login needed: the continuous front-month future, 60 trading
  days. Yahoo only fills gaps; it never overwrites a bar TopstepX delivered.
- Ten years of **daily** prices per market go to `data/research` for the long-term context.

Everything lands in the bot's market library, which only grows: refresh it regularly and the history
gets longer every week. The controller does this by itself about twice a day (`lab.refresh_data`).

**Early closes.** Grains (13:20 CT) and livestock (13:05 CT) stop trading before Topstep's 15:10 CT
deadline. On those markets the bot opens nothing in the last half hour and closes out 5 minutes before
the market's own close, in live trading, backtests and the lab alike.

**Caps your plan has.** Topstep currently caps silver, copper and platinum (SI, HG, PL) at zero, and
the bot caps micro silver and micro copper (SIL, MHG) at 2 / 4 / 6 contracts on 50K / 100K / 150K, from
a February 2026 Topstep notice that couldn't be confirmed since. Topstep changes these limits with
market conditions: check the product limits in TopstepX, and tell the bot (`risk/topstep.py`,
`PRODUCT_LIMITS`) if they differ.

**Good to know.** Micros and their full-size contract share prices, so MNQ's history is NQ's. A
full-size contract (ES, CL, 6E, ZB…) moves 10 times or more per tick than its micro; at a typical risk
per trade its stops are often too wide for even one contract, and the bot then simply doesn't trade
it. Yahoo's continuous future can differ from TopstepX's contract for a day or two around a roll.

## 37. Trading another market today

You can move an account to **any** market Topstep permits for the rest of the trading day, then it goes
back by itself. Since 4.0 a running bot switches **at once, without a restart**, and it can also trade
several markets side by side ([section 41](#41-several-markets-at-once)):

- **Trade tab:** the **Market** list at the top of the ticket → pick a market → **Switch to …** (or
  **Add … alongside** to keep the current one too).
- **Markets strip:** **Add a market** next to the chips, and **×** on a chip to drop one.
- **Knowledge → Simulations:** **Trade today** next to an opportunity or a market in the Markets list.
- **Telegram:** `/switch MES` (with several accounts: `/switch c1 MES`), then confirm.

What happens:

1. The new market starts straight away: the bot loads its recent history, learns it (with the adaptive
   strategy) and trades it with its own strategy, and your trade ticket places trades there, with every
   risk setting, Topstep guard and news pause exactly as before.
2. The old market takes no new trades. If it has an **open trade**, that trade keeps its stop and target
   and the old market goes once it is closed; otherwise it goes at once. Two sizes of one product
   (MNQ and NQ, say) can only be switched while flat, because both at once would double one bet.
3. At **15:10 CT** the switch ends; after the daily restart the account is back on its usual markets (or
   its Auto pick). A switch made after 15:10 CT is for the next trading day. **Back to usual market**
   (or `/switch usual`) ends it earlier. When the bot isn't running, the switch is saved and used when
   it starts.

Accounts that copy another account's calls follow it: switch the leader, not the follower. The hedge
guard still keeps accounts (and the markets of one account) from holding opposite positions in the same
or related markets. The day's markets are saved in `market_today.json` in the account's folder, so a
restart during the day keeps them.

---
## 38. The central intelligence: ask the bot why

Version 2.2 gives the bot one place that knows everything it recorded, and you can talk to it. Ask it
why it took a trade, why it skipped one, what a trade taught it, how a strategy is doing or when it
expects to trade next. It answers from its own records only, so every number it gives you is one the
bot actually wrote down.

### Where to ask

- **Dashboard → Ask tab.** Type a question or tap one of the suggestions. Next to it, **Decisions**
  lists every signal the bot weighed (filter: Taken, Skipped by a rule, Passed on); click one for the
  full explanation. Below is the **Decision lab** panel.
- **Telegram.** Just type the question to the bot, like a chat (`/ask why the 10:35 trade?` works too).
  Commands still start with `/`. With several accounts, start with the account's name:
  `combine2 why didn't you trade today?`.
- It works while the bot is stopped as well: it then reads the files on your PC.

Things to try: *why did you take the last trade?*, *why the 10:35 trade?*, *why didn't you trade today?*,
*what did you learn?*, *how is ORB doing?*, *what do you know?*, *when is the next trade?*,
*which decision rule are you using?*, *what are my rules?*

It can explain and report, but it **can't place, change or close trades or change settings**. That stays
on the dashboard and the Telegram commands, behind the same confirmations and risk limits as before.

### Every decision is recorded

Whenever a strategy signals, the bot writes a **decision record** before it does anything else:

- **What it saw:** the signal, the price, the time of day and the market (trend or range, how big the
  move is next to normal, gaps, overnight levels, today's reports).
- **Every strategy's verdict:** which strategies signalled on that bar, whether each was allowed to trade
  and why (in plain words, with the numbers behind it).
- **The evidence:** the recent record at three levels (this time of day and market, this time of day,
  the strategy overall), the after-cost estimate, the long-run memory's record, and the most similar
  past situations.
- **Your limits at that moment:** today's P&L, trades taken and allowed, losses in a row, room above the
  Maximum Loss Limit, and whether trading was paused or locked.
- **The result:** what the trade made, or, for a signal it passed on or that a risk rule blocked, what
  it would have made. Then the **lesson**: how its verdict on that strategy, time of day and market moved
  once the result was learned.

Records are kept in the trade journal (`data/journal_<mode>.db`), survive restarts, and stay on your PC (with AI answers
on, the parts the AI looks up for a question are sent to Anthropic to write the answer). With `brain.explain_trades: true` (the default), the Telegram entry alert gets a short
**💡 Why** line, and the exit alert a **📘 lesson**.

### AI answers (optional)

The built-in answers cover the common questions and need nothing extra. For free-form questions
("compare this week with last week", "what would make you trade more in the afternoon?"), you can let a
Claude model write the answers:

1. Get an API key at [platform.claude.com](https://platform.claude.com/settings/keys) (this is separate from a
   Claude subscription and is billed per use; a question usually costs from a few cents to a few tens of cents, more with
   `brain.effort: high`). If you already saved one for the AI coach, skip this: it is the same key.
2. Dashboard → **Setup** tab → **AI answers** → paste the key → **Check and save** (saving it on the
   **Deep review** page on the Ask tab does the same). The key is checked first and stored in `.env` on your PC as
   `COACH_ANTHROPIC_API_KEY`, like your other keys.
3. Tick **Let the AI answer questions**.

Every AI answer is priced and logged with the coach's usage (`data/coach/usage.jsonl`) and counts
toward the same monthly limit (`coach.monthly_budget_usd`, Ask → Deep review). Once the month's limit is
reached, the Ask tab goes back to its built-in answers until you raise it or the month ends.

The AI gets **read-only lookups** into the same records and nothing else: it can't trade or change
settings, and it is told to answer only from what the lookups return. If it can't be reached (no
internet, a bad key), the bot answers by itself and says so. Remove the key any time with **Remove**
(this also stops the AI coach until a key is saved again).

The lookups include the Simulation Lab's latest report and the AI coach's lessons, so you can also
ask "what did the simulations find?" or "what has the coach learned?".

### The decision rule and the decision lab

The adaptive strategy needs a rule for which signals to trade. Version 2.2 has two:

- **Classic** (the 1.2 rule): the most specific recent evidence decides. A strategy trades in a slot
  once it has enough recent results there and they are positive after shrinking toward zero.
- **Evidence** (new in 2.2): one estimate per signal that pools every level at once (this time of day
  and market, this time of day, the strategy overall) and the long-run memory, after costs. A handful of
  lucky trades in one corner can't switch a strategy on: the estimate leans on the wider levels until
  the corner has earned its own weight.

Which one is "better" is a question for data, not opinion. So the **decision lab** replays both rules
day by day over the long-run memory, using only what was known before each day and with your trade
limits applied, and compares them with a bootstrap test. With `knowledge.policy: auto` (the default) the
bot keeps the classic rule **until the evidence rule wins clearly**: at least 60 test days, 30 trades
each, and 95% of resampled histories agree. The lab runs after each daily `learn`, and the bot only
changes rule outside the entry window, never in the middle of a trade. A switch shows in the
dashboard's Activity. Run it yourself with `topstep-bot decision-lab`; the result shows on the Ask tab and in `/ask which decision
rule are you using?`. Set `policy: classic` or `policy: evidence` to choose the rule yourself.

The decision rule only chooses **which** signals to trade. Position size, stops, the daily loss limit,
the trade limit, the MLL buffer, news blackouts and every Topstep rule are untouched by it.

### Honest expectations

The intelligence makes the bot **explainable**; it doesn't make it profitable by itself. In October 2026
the long-run memory held about 19 test days of MNQ from TopstepX, far too few for the lab to prefer
either rule, so it keeps the classic rule. That will change only as the memory grows (it does every
day the bot runs, faster with an imported CSV). No bot, rule or AI can guarantee profits or passing
the Combine.

## 39. Make money every day: the aggressive mode

Choose **Make money every day** on the Setup tab (step 2, *What is this account for?*) and press
**Save**. It works on a Combine, an Express Funded Account or a practice account.

**What changes:**

- **Strategy:** `adaptive` with every strategy, including the ones the knowledge base hasn't judged
  yet. A strategy the evidence shows is *losing* at this time of day still isn't traded, and neither is
  a signal the probability calculator is at least 90% sure loses. Each strategy may trade **three times
  its usual daily count** (ORB's one breakout a day becomes three), so the bot takes every chance its
  strategies find, up to **12 trades a day per market**: trading several markets, each brings its own
  12 (`aggressive.trades_per_market`), while every loss limit still counts the whole account.
- **Size by edge:** the probability calculator sizes each trade by a quarter of the Kelly fraction
  ([section 42](#42-the-probability-calculator)): a large, well-measured edge risks up to **1.5 times**
  your risk per trade (`aggressive.max_conviction`), a thin one as little as a quarter of it. Until the
  calculator has enough outcomes for a signal, one the knowledge base hasn't proven trades at **half**
  the risk and a proven one at full risk.
- **Press on good days:** 35% of the day's *realized* profit is added to the next trade's risk, never
  past **2.5 times** your risk per trade. A losing day never increases the size.
- **Coming from 3.0?** 3.0's Setup tab wrote its own numbers into `config.yaml` (a quarter of the day's
  profit, at most twice the risk). Press **Save** on the Setup tab once with *Make money every day*
  chosen and they move to 4.0's; numbers you changed yourself are kept. A new `config.yaml` lists these
  settings as comments, so later versions can improve them.
- **Daily profit goal:** trading stops for the day once it is reached. The default is 45% of the
  profit target ($1,350 on a 50K, $2,700 on a 100K, $4,050 on a 150K); type your own on the Setup tab.
  On a Combine it must stay under 50% of the target, because of the consistency rule.
- **Lock in the day:** once the day has made half its goal, new trades are sized so a stop-out can't
  give back more than 40% of the day's best, and the bot stops for the day if it does.
- **Protect the account:** one trade never risks more than a quarter of the room left above the
  Maximum Loss Limit, so a losing streak shrinks the size instead of ending the account. Losing days
  stop at your daily loss limit, after 3 losses in a row, as always.
- **Stressed markets:** when the market network marks the market as stressed (its volatility in the
  top 10% of its last decade), the risk per trade is halved for the day.
- **News:** an open trade is closed just before a high-impact release instead of riding it.

The **Overview** shows a *Make money every day* panel: today's progress toward the goal, what the next
trade may risk and why, how much is locked in, and the consistency line no trade can cross.

**Honest expectations.** Aggressive means more trades and bigger size on good days and strong edges, so
the swings are bigger in both directions. More trades are not more profit: each extra trade only helps if the
strategy behind it has an edge, and the bot's evidence for most strategies is still small. On
simulated data (40 days, 12 seeds per market, MNQ, MES and MCL) the aggressive mode averaged about 2
trades a day against about 1.7 for *Pass the Combine*; the strategies find only so many signals.
Simulated data shows the guards work (no simulated day crossed the consistency line or the MLL); it
says nothing about whether real trading will make money. No bot can guarantee a profit or a pass.

### Two protections that work in every goal

**The consistency line.** On a Combine, Topstep raises your profit target if one day makes more than
55% of it. The bot already closed an open trade once the day reached 50%, but a single fast winner
(a gap through the target) could still jump past the line before the close arrived. Now every entry,
automatic, manual, an idea you take or the first trade, is planned so that **winning it can't lift the
day past 50% of the target**: the profit target becomes a limit order at the broker, and when the
strategy's target would go past the line it moves to the line (if that is still at least as far as
the stop), otherwise the size comes down until it fits. With less than 4 ticks of room left, no new
trade is taken. On an Express Funded Account on the consistency path the same applies to the path's
daily cap.

**Sudden jumps past the stop.** A stop is a promise to sell at the next price, not at the stop price:
in a fast market or at a report the fill can be many ticks worse, and grains, livestock and energy jump
the most. Every hard limit (the Maximum Loss Limit, your daily loss limit and Topstep's optional Daily
Loss Limit) is now checked against the stop **plus the market's jump allowance**, so a stop that fills
late still can't reach them. The allowance comes from the market network (the largest 5-minute gap in
one bar out of 200 over the last 120 days, with a floor per asset class: 3 ticks for rates, 4 for
currencies, 8 for indexes and metals, 10 for energy and crypto, 12 for grains and livestock, and a cap
of four times that floor). It only ever makes trades smaller.

## 40. The market network

Whether the bot trades one market or several, markets move together: the index futures follow each
other, bonds and gold rise when stocks are feared, the dollar moves every currency future, crude leads
the energy complex. The market network reads every market Topstep permits together, from what the bot
already collects:

- **Each market's state:** trend over 5 and 20 days, and how volatile it is now against its own last
  ten years (calm, normal, busy or stressed).
- **Links:** which markets moved together over the last 60 days, and the ones that moved against their
  usual partners today.
- **The board:** a risk-appetite reading from stocks, bonds, the yen and gold (risk-on, risk-off or
  mixed), how stressed the stock market is, the biggest moves against normal, and which asset classes
  are busy.
- **The day ahead:** the reports in the next 24 hours that matter for each market, and how many
  headlines it has had.
- **The bot's own evidence:** the Simulation Lab's best playbooks.
- **Jump allowance:** how far each market can jump against a stop (above).

Where to see it: **Knowledge** tab → **Market data** → **Market network** (your market highlighted), `/network` in Telegram,
`topstep-bot network`, and the Ask tab ("is gold stressed?", "how are the markets moving together?").
The AI coach reads it with its own lookup.

It builds from the daily history the research library downloads (section 33) and the 5-minute history
in the market library (Knowledge → Simulations → *Download every market*), and is rebuilt every hour while the
dashboard runs. A picture older than 36 hours isn't used: the bot falls back to the class defaults.

**What it may change:** only protection. The jump allowance makes trades smaller, and in the aggressive
mode a stressed market halves the risk per trade. It never makes the bot trade bigger or more often,
and never picks a direction: correlations and moods change, and treating them as signals would be
fitting the past.

---

## 41. Several markets at once

One account can trade up to **six markets at the same time**, and you can add, drop or switch them
while the bot runs, without a restart. An open trade is never cut short by a change: it keeps its stop
and target.

**Choose them on the Setup tab.** Under *What to trade*, **Markets at once** sets how many:

- **One** (the default): one market, as before. You can still add more while the bot runs.
- **2 to 6, with a market picked:** choose the others under **Also trade**. Pick markets that don't move
  together if you can (an index, gold and crude, say): the bot never holds opposite positions in related
  markets, so two index futures often get in each other's way. Two sizes of one product (MNQ and NQ)
  can't be traded together, because that would double one bet.
- **2 to 6, with Auto:** each day the bot trades up to that many of the best-scoring markets. Each one
  must show an edge on days held back from its training, the same test as the single pick
  ([section 36](#36-every-market-topstep-permits)); on a day when fewer show one, it trades fewer.

Press **Save**. These are the markets the bot starts with each day (`instrument.markets` and
`instrument.max_markets` in `config.yaml`).

**While it runs: the markets strip.** Under the status bar, each market has a chip with its symbol and
name, a small price line, its position and its open P&L or today's result.

- **Click a chip** to show that market: the Overview, the trade ticket and the Knowledge tab follow it.
- **×** drops that market. A market with an open trade takes no new trades and goes as soon as that
  trade is closed (its stop and target stay in place); to close it now, show it and press **Close trade**.
- **Add a market** starts another one at once: the bot loads its recent history, learns it and trades
  it with its own strategy.
- On the right: the whole account's open P&L, what it has at risk at the stops and its size in minis
  (10 micros count as 1 mini).

The trade ticket's **Market** list can also switch the shown market or add one alongside
([section 37](#37-trading-another-market-today)). Changes made while the bot runs last for the rest of
the trading day: after the daily restart the account is back on the markets from the Setup tab. A
restart during the day keeps them.

**In Telegram:** `/markets` lists what the bot trades with each market's position and trades today,
`/add MES` adds a market, `/drop MES` drops one and `/switch MES` (or `/switch usual`) replaces the
market shown. Each asks you to confirm. With several accounts, name the account first: `/add c1 MES`.

**What the markets share.** Every Topstep rule counts the whole account, not each market:

- **One Maximum Loss Limit and one daily loss limit.** Each market's room counts the other markets'
  open trades, including what each could still lose if its stop filled late (the jump allowance).
- **Topstep's position limit** counts every open position in the account (10 micros = 1 mini), so a
  market's size comes down while the others hold contracts.
- **The consistency line.** The room left for today's profit counts what the other markets' open trades
  could still make at their targets, so all of them winning together still can't cross the line.
- **The day:** one daily result, one pause, one losing-streak stop and one daily restart for all of them.
- **The hedge guard:** no opposite positions in the same or related markets, inside this account or
  across accounts.

Each market has its own strategy, knowledge base (`knowledge_<SYMBOL>_5m.json`), chart, ideas and
setups. In *Make money every day* each market brings its own `risk.max_trades_per_day`
(`aggressive.trades_per_market`); in the other goals the day's trades are shared between the markets.

**Honest note.** More markets means more signals, not more edge. Each market's strategies need evidence
of their own, markets that move together tend to lose together, and because the limits are shared, a
bad trade on one market leaves less room for the others.

---

## 42. The probability calculator

Before every trade, the bot works out four things about the signal from everything it has seen, after
fees and slippage:

- **P(win):** the chance the trade ends in profit.
- **Average win and average loss**, in R (1R is what the trade risks at its stop).
- **Expected value:** P(win) × average win − (1 − P(win)) × average loss, in R per trade. Above 0, signals
  like this one have paid on average; below 0, they have cost money.
- **How sure it is:** an 80% range for P(win) and for the expected value. Few outcomes give a wide range.

**Where the numbers come from.** The knowledge base records every signal of every strategy and how it
ended: the bot's real trades (counted double), your manual trades, the signals it watched live and the
training replays of recent history. The calculator starts wide and narrows step by step:

1. related markets of the same asset class (the knowledge network, [section 43](#43-the-knowledge-network)),
   at 30% of the weight;
2. this market, this strategy and this side (long or short);
3. the same at this time of day (open, midday or close);
4. the same in this kind of market (calm or volatile).

Each step moves away from the one before only as far as its own number of outcomes justifies, so eight
lucky trades at one time of day can't outvote two hundred elsewhere, and a strategy new to this market
starts from what it did on related ones instead of from nothing. Recent outcomes count more, the
long-run memory joins at a small weight, and no outcome is counted twice.

**What it changes.**

- **It skips sure losers** (`probability.veto`, on): a signal the calculator is at least 90% sure loses
  money after costs is skipped, and the Activity list says so with the numbers. It needs enough evidence
  first (`knowledge.min_samples` outcomes): a signal is never skipped just for being unknown.
- **It sizes by edge in *Make money every day*** (`probability.sizing`, on): the size follows a quarter
  of the Kelly fraction, from a quarter of your usual risk for a thin edge up to
  `aggressive.max_conviction` (1.5 times) for a large, well-measured one. The Kelly fraction is the
  share of an account that would grow it fastest if the odds were exactly right; odds measured on a few
  weeks rarely are, so the bot uses a quarter of it. Every hard limit still applies on top: the MLL, the
  daily loss limits, Topstep's size limit and the consistency line. In the other goals the size doesn't
  change.
- Nothing else. The strategies still decide when to trade, and the risk rules decide whether and how big.

**Where you see it.**

- **Overview → Current trade:** the odds the trade had when it was opened.
- **Overview → Getting ready to trade:** each setup's odds.
- **Trade → Ticket:** the odds of the strategies on your side right now.
- **Trade → Ideas:** each idea's odds.
- **Knowledge → Overview → Probability calculator:** every strategy and side right now, with P(win) and
  its range, average win and loss, expected value, how many outcomes it rests on (its own, related
  markets, the long-run memory) and a verdict: **edge** (even the low end of the range is above 0),
  **losing** (even the high end is below 0), **unclear** or **no data**.
- **Ask** or Telegram: "what are the odds on ORB?", "how likely is a short to win now?".

**How well it predicts.** Every prediction is saved with its outcome. **Knowledge → Overview → How well
it predicts** plots what it said (across) against how often those signals actually won (up): points
near the diagonal mean its numbers can be trusted. The **Brier score** (lower is better) is compared
with always guessing the average win rate, and **skill** above 0 means it beats that guess. Training
replays are scored day by day using only the days before, like live trading. Until a few hundred
outcomes have been scored, treat its numbers as rough.

**Honest limits.** A P(win) of 55% is how often signals like this one won in the past, not a promise
about the next trade. Markets change, and on most markets the strategies' evidence is still small.
Nothing here can guarantee a profit or a passed Combine.

Settings (`probability:` in `config.yaml`, all on by default): `enabled`, `veto`, `sizing`, `network`,
`grow_network` and `grow_per_day` (3).

---

## 43. The knowledge network

Each market keeps its own knowledge base. The knowledge network lets related markets learn from each
other: when the calculator works out the odds on MNQ, the knowledge bases of the other equity index
markets (MES, ES, NQ, M2K and so on) count too, at 30% of the weight of MNQ's own outcomes. Related means
the same asset class: equity indexes, currencies, interest rates, energy, metals, agriculture or crypto.

It grows in three ways:

- **Trading.** Every market the bot trades, one or several, records every signal it sees.
- **Learning related markets by itself.** Once a day, outside the entry window and while every market is
  flat, the bot replays the price history saved on this PC for a few related markets it doesn't trade
  (`probability.grow_per_day`, 3 a day), through every strategy, exactly like its own training. A market
  needs at least 25 trading days of saved history, and one learned in the last 20 hours waits its turn.
  Nothing is traded. **Learn related markets now** (Knowledge → Overview) does it at once.
- **Other accounts.** The knowledge bases of every account on this PC count too.

More saved history means more to learn from: **Download every market** (Knowledge → Simulations) saves
the recent 5-minute history of every market Topstep permits.

**Knowledge → Overview → Knowledge network** lists the related markets: outcomes, days, win rate,
average R, how each was learned (traded, watched or learned from saved history) and when. Ask "where
does your knowledge come from?" on the Ask tab or in Telegram for the same in words.

**What it may change:** only the odds, and mostly for a market with little evidence of its own. One
outcome on the market itself counts as much as about three on related markets, so as a market's own record
grows, the network matters less.

---

## 44. The trade chart

**Overview → Current trade** opens with a live candle chart of the market shown, in Chicago time:

- the last ten hours or so of bars (up to 120), with the bar forming now drawn lighter and moving with
  every price, and a dashed line where the market was closed in between;
- the open trade's **entry**, **stop** (red) and **target** (green) as lines with their prices on the
  right edge, shaded red between entry and stop and green between entry and target, and the **first
  stop** dashed once the stop has moved (breakeven or trailing);
- today's fills: ▲ and ▼ where trades went in, ● where they came out;
- the last price, dotted.

Point at (or touch) a bar for its time, open, high, low and close. The legend underneath lists the
prices. With several markets, click a chip on the markets strip to see another market's chart.

**Trade → Ticket** has a smaller chart: pick Buy or Sell and your stop and target appear on it as dashed
lines, with the dollars at risk and the R:R in the legend, and they move as you change them. You see
where they sit against the last few hours before you place the trade.

The chart is drawn from the same bars the strategies use. It also works on the phone dashboard
([section 15](#open-the-dashboard-on-your-phone-inside-telegram)).

---

## 45. The simpler dashboard

The rail has seven tabs instead of eleven. Nothing was taken away: each page moved under the tab it
belongs to, and a tab with several pages shows them as buttons at the top.

| Tab | Pages | Before 4.0 |
|---|---|---|
| **Overview** | – | Overview |
| **Trade** | Ticket, Ideas | the Trade and Ideas tabs |
| **Ask** | Ask, Deep review | the Ask and AI Coach tabs |
| **Knowledge** | Overview, What works, Simulations, Market data | the Knowledge, Simulations and Market tabs (Overview is new) |
| **Settings**, **Setup**, **Logs** | – | the same |

**Ask** explains anything in the bot, in your own words: why it took or skipped a trade, the odds of each
strategy now, which markets it trades, what it learned, and what any rule or setting means (ask about it
by name, in your own words). It answers from its own records and from this guide, the Topstep rules page and every setting's
description, with the values your account uses. With a Claude API key and AI answers on
([section 38](#38-the-central-intelligence-ask-the-bot-why)), Claude writes the answer from the same
lookups and can go deeper; without one, the built-in answers still work. **Deep review** is the AI coach
([section 35](#35-the-ai-coach-claude-teaches-the-bot)).

**Knowledge → Overview** brings everything the bot knows together:

- six cards: **real trades** (the bot's and yours), signals **watched live**, **real market history**
  (training replays and the long-run memory), **simulations** (the Simulation Lab's Combines),
  **related markets** (the knowledge network) and **predictions scored**. Each opens the page with the
  detail;
- the **probability calculator**: every strategy and side right now ([section 42](#42-the-probability-calculator));
- **What the bot knows**, in plain sentences, with the long-run memory and **Learn from history now**;
- **How well it predicts**: how the calculator's past predictions held up;
- the **knowledge network** ([section 43](#43-the-knowledge-network)).

The other Knowledge pages are the ones you know: **What works**
([section 17](#17-training-and-the-knowledge-base-how-the-bot-learns)), **Simulations**
([section 34](#34-simulation-lab-hundreds-of-combines-from-history)) and **Market data**
([section 31](#31-what-the-bot-knows-about-the-market-and-the-news)).

## 46. The AI trading assistant

A Claude model that watches the market with the bot. It sees what the bot sees and, if you let it,
acts. It has two parts, each with its own switch, **per account**, and both start **off**:

| Switch | What it may do | What it can never do |
|---|---|---|
| **Watch and manage open trades** (`assistant.manage`) | Hold, **tighten the stop**, move the stop **to breakeven**, or **close the trade early** | Widen a stop, add contracts, remove the stop or move the target |
| **Place its own trades** (`assistant.trade`) | Buy or sell with its own stop and target, at most `max_contracts` and `max_trades_per_day` | Skip any risk rule or Topstep rule |

### Switch it on

1. Save a Claude API key on the **Setup** tab under **AI answers** (the same key the Ask answers and
   the AI coach use; get one at platform.claude.com > API keys).
2. Switch it on in any of three places:
   - the **Overview** tab's **AI assistant** panel (while the bot runs);
   - the **Setup** tab's **AI trading assistant** panel, which also has its limits;
   - Telegram: `/assistant manage on`, `/assistant trade on`, `/assistant off` (turning a part on asks
     for a confirmation tap). `/assistant` alone shows what it is doing.

The switches are saved in `config.yaml` (in the account's own entry when you trade several accounts)
and take effect at once while the bot runs. The limits on the Setup tab apply the next time the bot starts.

### What it sees

Each decision gets one picture of the situation: the price and the last 30 bars, the ATR, the time of
day and the market's mood (calm or volatile), the market picture, the strategies' records at this time
of day, the **probability calculator** for both sides ([section 42](#42-the-probability-calculator)),
live ideas and setups that are nearly ready, the bot's own ranking of what it would trade, today's
reports and how far away they are, and the account's limits (daily loss room, room above the Maximum
Loss Limit, the room left under the consistency line, trades today). For an open trade it also sees
the entry, stop, target, open profit in R, the best and worst it has been, and why it was opened.

### When it thinks

It never slows the bot down: it thinks in the background after a bar closes and only acts if the
answer is still valid when it arrives (an answer that takes more than two minutes is not acted on).

- **An open trade** is reviewed when it opens, every `manage_every_minutes` (10), and sooner when it
  reaches +1R (+2R, ...), when it comes close to its stop, or when a scheduled report is less than 15
  minutes away. Trades you opened yourself are left alone unless **Also manage the trades I open
  myself** is ticked.
- **While flat**, inside the entry window and only when the rules allow a new trade, it looks for a
  trade when a strategy signals, when a setup is at least 75% formed, and otherwise every
  `think_every_minutes` (15). It stops looking once it made `max_trades_per_day` trades.

### Guardrails

- **Its own trades** are sized and checked by exactly the code the bot's own trades go through: risk
  per trade, the daily loss limits, the room above the MLL, the consistency line, trade counts, news
  blackouts, the entry window, Topstep's position caps and the hedge guard. Then they are capped at
  `max_contracts`. The stop must be on the protective side of the price (the bot may widen one closer
  than its minimum stop, and refuses one wider than its maximum); the target must be at least 1R away
  (without one it uses 1.5R).
- **A tightened stop** must improve on the current one and stay at least 2 ticks (and a fifth of an ATR)
  from the price. **Breakeven** needs the price past the entry by that much.
- **Entering or closing early** needs it to be at least `min_confidence` (65%) sure. Below that it
  says what it would have done, and does nothing.
- It never trades at or after the last entry time, and every trade is flat by the session end like any other.

### Every decision is recorded

Every decision, including *wait* and *hold*, is saved in the journal with the situation it saw, what it
chose, how sure it was, why, the facts that decided it, and what the bot did with it (done, refused by
the rules and why, not sure enough, skipped because the trade had already closed). The Overview panel
lists today's decisions; `/assistant` and the **Ask** tab ("what is the AI assistant doing?") report them too.
Its actions appear in the activity feed and as Telegram alerts (its trades as entries, its stop moves
and closes as risk alerts).

### What the bot learns from it

- **Its own trades** are tagged **AI** in the journal and are filed in the knowledge base under the
  name `assistant`. Like your manual trades, they never count as a strategy's evidence, so they can't
  switch a strategy on or off. Knowledge → What works shows their record on a row of their own.
- **A strategy's trade it ended early** (it closed it, or a stop it tightened was hit) does not spoil
  that strategy's record. The bot keeps following the strategy's original stop and target to their own
  end, and the strategy is judged on that, as a watched signal. The difference between what the trade
  really made and what the plan would have made is the assistant's **management score**: the panel and
  `/assistant` show how many trades it helped and how many it cost, in R. If that number stays negative,
  switch managing off.

### When the AI isn't there

Without a key, with today's cap (`daily_budget_usd`) or the monthly Claude budget used, or with Claude
unreachable, it does nothing and says why on the panel; the bot trades exactly as it would without it.
After a connection problem it tries again five minutes later; saving a key or raising the budget makes
it try again at once.

### Cost

Each decision is one request to Claude and costs a few cents with Claude Opus 5.5, the default (more on a
busy market with a large knowledge base, less with a cheaper model chosen in `assistant.model`). A day
with both parts on can make 20 to 40 decisions. Two limits stop it: `assistant.daily_budget_usd` ($2 a
day by default) and the monthly Claude budget it shares with the AI coach and the Ask answers
(`coach.monthly_budget_usd`, $20 by default, changed on Ask > Deep review). If you use it every day,
raise the monthly budget or it runs out partway through the month.

### Honest expectations

The assistant is a careful second opinion, not an edge by itself. It can't see anything the bot can't,
its confidence is its own estimate rather than a measured probability, and closing winners early is a
common way to turn a good strategy into a losing one. Start with **Watch and manage** on Paper, read
its decisions and its management score for a few weeks, and only then let it place its own trades.
No bot or AI can guarantee a winning trade, passing the Combine or a profit.

---

## 47. Trends and the daily plan

### The trend on the chart

The bot reads every chart it trades for a trend, bar by bar, from four things a trader looks at:

| Part | Weight | What it looks at |
|---|---|---|
| Slope | 35% | The straight line through the last 24 bars (two hours of 5-minute bars): how far it moved, in ATRs, and how cleanly the bars follow it |
| Averages | 25% | Price against its 20- and 50-bar averages, and which average is on top |
| Swings | 25% | Higher highs and higher lows (an uptrend), lower highs and lower lows (a downtrend) |
| Today | 15% | Price against today's VWAP |

Together they make a **trend score** from -1 (a clean downtrend) to +1 (a clean uptrend). Above +0.35 it
calls the chart **up**, below -0.35 **down**, and in between **sideways** (once it has called a direction it
keeps it until the score falls back under 0.20, so it doesn't flip on every bar). **ADX** says how strong the
move is: a trend with a score of 0.6 or more and an ADX of 25 or more is a **strong** trend. It also says
when price has run far from its 20-bar average (**stretched**) and whether a trend is starting, steady or
fading.

Where you see it:

- **Overview → Trend**: the direction, a gauge from Down to Up, the reasons in plain words, the score, the
  ADX, **how often its calls held** (for every up or down call it checks an hour later whether price kept
  going that way; a coin flip would be about 50%), and how the bot's own signals did **with and against**
  the trend, after costs.
- **The trade chart** draws the trend line it fitted, a band around it, and marks the swing highs (H) and
  lows (L).
- Telegram `/trend`, the **Ask** tab ("what's the trend?") and the AI trading assistant, which is told the
  trend and how signals have done with and against it.

Every signal the bot sees is saved with the trend reading at that moment, so the knowledge base learns how
each strategy does with the trend and against it. A trend describes the last hours; it does not promise
where price goes next.

### The trend filter, and the test that decides it

The **trend filter** skips signals that go against a strong trend (a buy while the score is -0.6 or lower,
or a sell at +0.6 or higher, with ADX 25 or more). Because that changes which trades are taken, it follows
the same evidence rule as the decision lab: with `trend.filter: auto` (the default) it stays **off** until a
test shows it helps.

The **trend test** runs after the long-run memory learns (daily, and on **Learn from history now** or
`/learn`). It replays the bot's history twice, once as the bot trades now and once with the filter, and
compares them day by day. The filter turns on only if:

- at least 60 test days carry the trend reading,
- it would have skipped at least 15 trades, and
- the filter made more, with 95% confidence (from resampling the days).

Otherwise it stays off and the Trend panel says why. The result is kept in `data/trend_<SYMBOL>_<TF>m.json`.
`trend.filter: on` uses the filter now, without evidence (the Setup tab asks you to confirm); `off` never
uses it. Either way the trend is still read, shown and learned from. Manual trades are never filtered, and
the AI trading assistant's own trades are refused while the filter is on and a strong trend is against them.

### Today's profit goal

Each trading morning the bot asks for every account's **profit goal for the day**:

- **On Telegram** at `day_plan.ask_at` (08:00 Chicago time by default), with buttons for a steadier goal,
  the suggested goal and a stretch goal, plus **No goal**, and with several accounts **Use the
  suggestions**. You can also just type it: `400`, or `c1 400 c2 1200` with several accounts, or `none`.
  Asked only on trading days, once a day, and only for accounts that don't have a goal yet.
- **On the Overview tab**, in the **Today's plan** box: type an amount and press **Set goal**, tap one of the
  suggestions, or press **No goal today**. This works with the bot stopped too.
- **Any time** with `/goal 400`, `/goal c2 1200`, `/goal all 300`, `/goal suggested` or `/goal none`.

Changing the goal takes effect within seconds, with no restart. After 15:10 CT a goal is for the next
trading day.

**No answer?** The bot keeps your last goal from the past 30 days (`day_plan.unanswered: last`, the default), or
uses its suggestion if you haven't given one (`suggested` always uses the suggestion).

**The suggestion** comes from where the account stands:

- **Combine**: what is left to the profit target, spread over `day_plan.days_to_target` trading days (10 by
  default). With $1,500 left and 10 days, it suggests $150.
- **Express Funded, standard payout path**: a winning day ($150 or more) with a cushion for fees.
- **Consistency payout path**: the payout goal spread over the days, under the 40% daily cap.

**What a goal can and can't do.** The goal never makes a trade bigger or riskier: every trade is still sized
by your risk settings, and every loss limit and Topstep rule still applies. A goal above what one day may
make (the consistency line, a payout path's daily cap, or your daily profit target) is **capped there**, and
the plan says so. With `day_plan.stop_at_goal: true` (the default), once the day's P&L reaches the goal the
bot opens no new trades for the rest of the day, so the profit is kept; an open trade keeps its stop and
target. Raising the goal later that day lifts that stop. If you use **Teach the bot**, a goal ends some
learning days early: untick **Stop opening trades once the day's goal is reached** on the Setup tab if you
want it to keep trading for the lessons.

### The route

The **Today's plan** box (and `/plan`) turns the goal into a plan built from the bot's own record: it replays
every past day in its knowledge base (the signals it would take, with today's rules: your risk per trade,
trade limit, losing-streak stop, cooldown, daily loss limit, news blackouts and the profit ceiling, and the
trend filter when it is on) and reports:

- **The goal in trades**: $400 at $150 a trade is 2.7R, about 2 winning trades more than the losses at
  the bot's average win.
- **The route through the day**: the open (08:30-10:00 CT), midday and the close, with how many trades
  each part usually brings, their average result after costs and the strategies behind them, and the trend
  on the chart for the part of the day you are in.
- **The odds**: on how many past days the bot's signals reached a goal like this, how many ended green and
  how many in a loss, the typical day, a goal reached on about half the days, and, once trading has
  started, the chance **from where the day stands now**.
- **When the day stops**: the goal, the losing-streak and daily loss stops, the trade limit and the most
  one day may make.
- **Progress**: made so far, left to go, trades taken.

The plan needs at least 10 past days in the knowledge base to put odds on it; before that it shows the goal,
the stops and the progress. Ask "how is today's plan going?" on the Ask tab for the same plan in words.

### Settings

On the **Setup** tab under **Daily goal and trend** (per account): the morning question and its time, what
happens with no answer, stopping at the goal, days to the target and the trend filter. In `config.yaml` they
are the `day_plan:` and `trend:` sections ([section 24](#24-full-configuration-reference)).

### Honest expectations

A plan is not a promise. The odds are how the bot's own past days went under today's rules, not a forecast:
markets change, and a goal reached on 40% of past days is missed on most days. A goal is most useful as a
reason to stop on a good day. No bot can guarantee a profit, a winning day or a passed Combine.

---

*Trading futures involves substantial risk of loss and is not suitable for everyone. This software
is provided as-is, with no guarantee of profit or of compliance with Topstep's rules, which can
change. You are responsible for every order placed on your account.*
