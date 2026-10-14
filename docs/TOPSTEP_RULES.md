# Topstep rules and how the bot enforces them

This page lists every Topstep rule that matters to an automated strategy, what the numbers are,
and exactly what the bot does about each one. The rules were checked against the Topstep Help
Center in **October 2026**. Topstep changes its rules from time to time, so confirm the current
values in your TopstepX dashboard (Risk Settings) before trading, and tell the bot if anything
differs (see "If Topstep changes a rule" at the end).

> **No bot can guarantee that you pass the Combine or make money.** The guards below make sure
> the bot never breaks a Topstep rule by itself. Whether it makes money depends on the market and
> the strategy, and backtest results are evidence, not a promise. Run it in paper mode and on a
> Combine before you rely on it.

To see these numbers for your own account (plan, account type, Daily Loss Limit, symbol), run
**`topstep-bot rules`** or pick **"Show Topstep's rules"** in the menu.

## The rules at a glance

| Rule | 50K | 100K | 150K |
|---|---|---|---|
| Profit target (Combine) | $3,000 | $6,000 | $9,000 |
| Maximum Loss Limit (trailing, end of day) | $2,000 | $3,000 | $4,500 |
| Daily Loss Limit (optional, chosen at checkout) | $1,000 | $2,000 | $3,000 |
| Best day allowed by the Consistency Target (55% of target) | $1,650 | $3,300 | $4,950 |
| Bot's default daily profit cap (40% of target) | $1,200 | $2,400 | $3,600 |
| Maximum position, minis (10 micros = 1 mini) | 5 | 10 | 15 |
| Gold / Crude Oil (GC, CL) | 3 | 6 | 9 |
| Micro Gold / Micro Crude (MGC, MCL) | 30 | 60 | 90 |
| Silver, Copper, Platinum | not tradable | not tradable | not tradable |

Express Funded Account (XFA) Scaling Plan, maximum position in minis by account balance:

| XFA balance | 50K | 100K | 150K |
|---|---|---|---|
| below $1,500 | 2 | 3 | 3 |
| $1,500 and up | 3 | 4 | 4 |
| $2,000 and up | 5 | 5 | 5 |
| $3,000 and up | 5 | 10 | 10 |
| $4,500 and up | 5 | 10 | 15 |

## Rule by rule

### Maximum Loss Limit (MLL)

**Topstep:** the MLL trails your highest end-of-day balance, never moves down, and locks at the
starting balance (Combine) or at $0 (XFA). It is checked in real time including open P&L, and
touching it ends the account. In an XFA the MLL moves to $0 after the first payout.

**The bot:**
- Tracks the floor the same way (end-of-day, locking) and saves it, so restarts don't lose it.
- Sizes every trade so that a stop-out (including slippage, fees **and the market's jump
  allowance**, the sudden gap a stop can fill at in a fast market) still leaves `mll_buffer` above the
  floor. The allowance is measured per market by the market network (3 to 48 ticks; grains,
  livestock and energy the most) and only ever makes trades smaller.
- Stops new trades when equity is within `mll_buffer` of the floor, and closes an open trade at
  once when it gets within half of it.
- If the bot can't work out the floor (for example an account that was traded before), the
  preflight check asks you for the value shown in your dashboard, or you can type it as
  `account.mll_floor_override`. That is a one-time sync: the bot applies a new number once, then
  follows its own trailing floor.

### Daily Loss Limit (DLL)

**Topstep:** optional in the Combine and XFA. You choose it at checkout and it can't be changed
later. When your day's P&L reaches it, Topstep flattens the account and blocks trading until
5 PM CT. It is not a rule violation.

**The bot:**
- Tell it whether your account has one: `account.topstep_daily_loss_limit: true` (uses your plan's
  amount) or leave it empty. The dashboard's Setup tab asks you.
- Stops new trades at 90% of the DLL and closes an open trade at 95%, so Topstep's limit is never
  the one that fires.
- Sizes every trade so a stop-out that fills a jump late still stays inside 90% of the DLL and
  inside your personal limit.
- Always has its own, lower limit too (`risk.personal_daily_loss_limit`, $500 by default). The bot
  refuses to start if your personal limit is not below Topstep's DLL and MLL.

### Consistency Target (Combine)

**Topstep:** your best single day must stay at or below 55% of the profit target. If a day goes
over, the profit target rises to `best day / 0.55` (a $2,200 day on a 50K raises the target to
$4,000).

**The bot:**
- Stops opening trades once the day is up 40% of the profit target (`risk.daily_profit_target`).
- If an open trade keeps running, it is closed when the day reaches 50% of the target, before the
  55% line (`risk.consistency_guard`, on by default).
- Plans every entry so that **winning it can't carry the day past that 50% line**: the trade's
  profit target (a limit order at the broker) is moved to the line or the size is reduced, and with
  less than 4 ticks of room no trade is taken. This covers the case the close-out can't: one fast
  winner that gaps through the line between two price updates.
- In the *Make money every day* goal the day stops at its own goal instead (45% of the target by
  default, always under 50%).
- Shows the current target (including any increase) on the dashboard's profit target meter.

### Profit target (Combine)

**Topstep:** reach the profit target (and keep it) while respecting the Consistency Target. There
is no minimum number of days, but the consistency rule means it takes at least two.

**The bot:** once the target is reached, it closes any open trade and stops trading
(`risk.stop_at_profit_target`, on by default), so a late loss can't undo the pass while Topstep
processes it.

### Position size

**Topstep:** 5 / 10 / 15 minis, with micros counted at 10:1, and the Scaling Plan in an XFA (the
limit only changes between sessions). Some metals and energy products have their own lower caps:
the bot uses GC 3/6/9, MGC 30/60/90, CL 3/6/9, MCL 30/60/90, SI, HG and PL 0 (not tradable now), and
SIL and MHG 2/4/6 (from a February 2026 notice; confirm in TopstepX). These change with market
conditions.

**The bot:**
- Sizes each trade from its stop and your `risk_per_trade`, then caps it at Topstep's limit for
  today (plan, Scaling Plan and product cap) and your own `risk.max_contracts`.
- Checks again right before every entry order goes out: an entry that would take the position
  past the limit is refused, whatever asked for it.
- Refuses to load a config for a product Topstep doesn't currently allow.

### Trading hours

**Topstep:** the trading day runs 5:00 PM to 3:10 PM CT. Every position must be closed by 3:10 PM
CT; nothing may be held overnight or over the weekend.

**The bot:** only opens trades between `session.trade_start` and `session.last_entry` and closes
everything at `session.flatten_at` (15:00 CT by default). The config refuses a flatten time later
than 15:08. CME holidays and early-close days are in `session.no_trade_dates`.

Markets that close before 15:10 CT: the CBOT grains (corn, wheat, soybeans, meal, oil) stop at
13:20 CT and live cattle and lean hogs at 13:05 CT, and Topstep requires you to be out before the
product's own close. On those markets the bot opens nothing in the last 30 minutes and closes
everything 5 minutes before that close (13:15 / 13:00 CT), in live trading, backtests and the
Simulation Lab.

### News

**Topstep:** there is no news blackout, but taking your **maximum position size** into a scheduled
major economic release is a prohibited strategy. Topstep makes no adjustments for slippage around
news.

**The bot:**
- Pauses new entries from 5 minutes before to 10 minutes after each high-impact US release (from
  the economic calendar; `news` settings).
- In the 30 minutes before a release, or whenever the calendar can't be loaded, new trades use
  at most half of Topstep's maximum position size.
- If a position at the maximum size is open just before a release, it is closed.
- `news.flatten_before: true` closes every open trade before releases, not just full-size ones.

### Automation, VPS and Live accounts

**Topstep:** bots are allowed in the Combine and XFA through the TopstepX (ProjectX) API, but
- all trading must come from **your own computer**. VPS, VPN and remote servers are prohibited and
  can get you removed from the program;
- high-frequency trading is prohibited;
- **Live Funded Accounts may not trade through the API**;
- you are responsible for everything the bot does: there are no exceptions for bot malfunctions.

**The bot:**
- Runs on your own Windows computer (`start.bat`), and the preflight check warns if the computer
  looks like a cloud server, a virtual machine or a Remote Desktop session.
- Refuses to trade an account the API reports as a real-money (Live) account.
- Has an order-rate circuit breaker: more than 30 order actions in a minute, or more than 20 entries
  in a day, stops new entries (closing positions is never blocked). A normal day is a handful of
  orders.
- Telegram and the dashboard only send commands to the bot on your computer; the orders still
  come from your computer.

### Express Funded Account payouts

**Topstep:** the Standard path needs 5 winning days of $150 or more. The Consistency path needs
3 trading days with the best day at or below 40% of net profit. Payouts are 90/10.

**The bot:** trades the same way on either path; set `account.payout_path` to the one you chose
so `topstep-bot rules` describes it correctly. Your TopstepX dashboard tracks payout progress.

## If Topstep changes a rule

All the numbers live in one file, `src/topstep_bot/risk/topstep.py`, with the source of each
one. If your TopstepX Risk Settings show different values:

- **Different position limit** (for example a temporary product cap): set `risk.max_contracts` to
  the lower number in `config.yaml`. The bot never goes above it.
- **Different Daily Loss Limit**: set `account.topstep_daily_loss_limit` to the dollar amount.
- **Different MLL floor**: type the value in your dashboard as `account.mll_floor_override`. The bot
  applies a new number once and then trails from it; a number that was already in the file before it
  remembered overrides is ignored with a warning. To sync again, change the number.
- Anything else: update `risk/topstep.py` and run the tests (`pytest`); every rule has a test in
  `tests/test_topstep_compliance.py`.

## Sources

- [Trading Combine Parameters](https://help.topstep.com/en/articles/8284197-trading-combine-parameters)
- [Maximum Loss Limit](https://help.topstep.com/en/articles/8284204-what-is-the-maximum-loss-limit)
- [Consistency Target](https://help.topstep.com/en/articles/8284208-what-is-the-consistency-target)
- [Daily Loss Limit in the Combine and XFA](https://help.topstep.com/en/articles/10490293-daily-loss-limit-in-the-trading-combine-and-express-funded-account)
- [Scaling Plan](https://help.topstep.com/en/articles/8284223-what-is-the-scaling-plan)
- [Express Funded Account Parameters](https://help.topstep.com/en/articles/8284215-express-funded-account-parameters)
- [TopstepX API Access](https://help.topstep.com/en/articles/11187768-topstepx-api-access)
- [ProjectX API rate limits](https://gateway.docs.projectx.com/docs/getting-started/rate-limits/)
