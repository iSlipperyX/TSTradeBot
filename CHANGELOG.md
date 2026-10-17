# Changelog

Every version of the bot, newest first. The release workflow publishes each version's section as its
GitHub release notes.

## Unreleased — fixes from the first live week

Found by reading the first live week's journals, knowledge files and logs (two Combine accounts,
Oct 9–16). Nothing about how the bot picks or sizes trades changed.

- **Realtime connection stuck for hours.** With two accounts signed in with the same API key, a
  network blip left the account that signed in first unable to reconnect: TopstepX closed every new
  connection right after it opened, every 30 seconds, until a restart (seen all night on one account
  and for four hours on the other). The bot now signs in again for a fresh token after two quick
  closes in a row. The reconnect delay also starts again from one second after a connection that
  worked for a while, instead of staying at 30 seconds for the rest of the day.
- **Days not recorded when the bot was stopped before the end of day.** The day's result is saved,
  and the trailing MLL floor raised, at the bot's end of day. Stopped before that, the day had no
  record, so the Consistency Target's best day and the Combine progress missed its profit, and the
  floor never saw its close. On the next start the bot now records those days from TopstepX's fills
  (paper: the journal) and runs their closes through the floor in order.
- **An idea whose stop the price had already passed counted as a +1R win** (and went into the
  knowledge ledger). It is now marked "not scored" and nothing is learned from it.
- **The bot's own trade and the idea that followed the same signal shared one journal row**, so
  whichever finished last overwrote the other. They now have separate rows.
- **New: Your hand against the plan** (Knowledge → What works, and `/knowledge`): the bot's trades
  left alone, the ones you closed by hand against what their plans went on to do, and how your own
  trades end. A score only.
- A Telegram alert that fails with an empty error message now names the error.

One week of live trades is far too little to judge any strategy, and no bot can guarantee passing the
Combine or making a profit.
## 4.1.0 — the AI trading assistant

### An AI trading assistant, off until you switch it on

- A Claude model watches the market with the bot. It has two parts with their own switches, per
  account: **watch and manage open trades** (hold, tighten the stop, move it to breakeven, or close
  early; never a wider stop, more contracts or a moved target) and **place its own trades** (small
  and few: `assistant.max_contracts`, 1 by default, and `assistant.max_trades_per_day`, 2).
- Switch it on the Overview tab's new **AI assistant** panel, the Setup tab's **AI trading assistant**
  panel (with its limits), or Telegram (`/assistant`, `/assistant manage on|off`,
  `/assistant trade on|off`, `/assistant off`; turning a part on asks for a tap). The switches are saved
  in config.yaml and take effect at once while the bot runs.
- Every action goes through the same code as the bot's own trades: sizing, the daily loss limits,
  the room above the MLL, the consistency line, trade counts, news blackouts, the entry window,
  Topstep's position caps, the order guard and the hedge guard. It enters or closes early only when it
  is at least 65% sure (`assistant.min_confidence`).
- Every decision, including *wait* and *hold*, is saved in the journal with what it saw, what it
  chose, how sure it was, why, and what the bot did with it. The panel, `/assistant` and the Ask tab
  show them.
- Its own trades are tagged **AI** and filed in the knowledge base as `assistant`, never as a
  strategy's evidence. When it ends a strategy's trade early, the bot follows the strategy's own plan
  to its end, so the strategy is judged on its plan, and the difference is the assistant's
  **management score** (trades it helped and cost, in R).
- It uses the shared Claude API key and monthly budget, with a daily cap of its own
  (`assistant.daily_budget_usd`, $2). Without a key, with a budget used or with Claude unreachable, it
  does nothing and says why, and the bot trades exactly as before. A key saved or a budget raised on
  the dashboard reaches the running bot without a restart.

Nothing changes for anyone who leaves it off. No AI can promise a winning trade or a Combine pass.

## 4.0.1 — safer under a bad connection, with several markets and with several accounts

A fix release: nothing about how the bot picks or sizes trades changed.

### Orders under a bad connection

- A stop, target or close whose answer was lost no longer turns into a second stop or a forgotten
  exit: the bot checks TopstepX before trying again, and only ever keeps one stop per trade.
- A trade that closes in pieces is booked once, with the right exit price and size.
- A close that TopstepX refuses keeps both the stop and the target working until a close goes through,
  and says so as a critical alert.
- No new entry is sent while an earlier entry's outcome is still unknown.
- Each market now handles its own broker messages, so a market waiting on a slow answer no longer
  holds up another market's stop. The bot checks every market together, and a failed cancel, change or
  close is tried again one second later instead of waiting up to about 14 seconds.
- A network outage now writes one short line to the log instead of a long error report.

### Stopping, restarting and updating

- A Stop that could not close a position says the position is still open (dashboard, Telegram and the
  log), instead of looking like a normal stop.
- A market dropped while it still had a trade is remembered across a restart, keeps managing that trade,
  and goes once it is flat. A position the bot finds that no market owns is handled by
  `execution.orphan_position_policy`.
- After an update, only the accounts that were running come back, and the message names them.
- The emergency `topstep-bot flatten` command keeps going when one call fails, closes positions before
  cancelling orders, checks again, and ends with **Flat**, **STILL OPEN** or **NOT CHECKED** for each
  account. It does not halt a running bot: use **Flatten & halt** or `/flatten` for that.

### Settings and config files

- `account.mll_floor_override` is now a one-time sync: a new number is used once, then the bot follows
  its own trailing floor. A number left in config.yaml can no longer give the bot more room than Topstep
  allows on a later start. A number lower than the bot's own floor is used with a warning (after a
  Topstep reset that is right; otherwise check it).
- The coach's and the Simulation Lab's **Apply** change only the account being reviewed, never raise
  another account's risk, and keep the strategy's own settings when the strategy stays the same.
- config.yaml: a section written twice, or a time typed as a bare number (`8`, `830`), is now a clear
  message instead of being quietly ignored or read as a different time. Unquoted `14:30` still works.
- config.yaml and `.env` saved by PowerShell (UTF-16) or with a BOM are read; `export KEY=value` and a
  comment after a value work in `.env`. Saves are written so a crash or power cut never leaves half a
  file, and a read-only or locked file says **Not saved** in plain words.
- Phone dashboard: the market list, **Switch** and **Download every market** now work on the phone;
  anything that stays on the PC says "Only available on the PC dashboard" instead of "not found".

### Settings to look at

- **`account.mll_floor_override`**: if it is in your config.yaml, check it matches the Maximum Loss
  Limit on your Topstep dashboard, or remove it and let the bot follow its own floor.
- **`risk.slippage_ticks`**: 8 to 10 ticks is more realistic than the default for the index micros on
  a fast day, so paper results and sizing don't look better than live.
- Until you have watched this version for a few days: one account and one market, keep
  `execution.orphan_position_policy: flatten`, and after a Stop with a trade open, look at TopstepX.

### Honest notes

- Every fix was tested against a simulated TopstepX that loses messages and times out, not against the
  real one. In 4,000 simulated bad-connection days 5 still end with a stop left working for a short
  while after the position closed: that is the price of keeping the stop until TopstepX confirms the
  position is flat, which is a choice for you to make (it is the safer side).
- No bot can guarantee passing the Combine or making money.

## 4.0.0 — several markets, a probability calculator and a simpler dashboard

### Several markets at once

- The core was rebuilt around an account that trades several markets ("lanes") at once, up to six:
  one broker connection shared by every market, and one **account book** for the risk they share.
- Add, drop or switch markets while the bot runs, **without a restart**: the markets strip on the
  dashboard (click a chip to show that market, × to drop it, **Add a market**), the trade ticket's
  Market list, and Telegram `/markets`, `/add`, `/drop` and `/switch`. A new market loads its history,
  learns it and trades at once; a dropped or switched-out market with an open trade takes no new trades
  and goes once that trade is closed, its stop and target in place. Two sizes of one product (MNQ and
  NQ) are never traded together.
- Setup tab: **Markets at once** (one to six) with **Also trade**, or Auto with up to that many of the
  best-scoring markets (`instrument.markets`, `instrument.max_markets`).
- Every Topstep limit counts the whole account: the MLL and daily loss limits include the other
  markets' open trades and what each could still lose at a late stop, Topstep's position limit counts
  every open position (10 micros = 1 mini), the consistency line counts what the others could still
  make at their targets, and the hedge guard now also works between the markets of one account. One
  daily record, pause, losing-streak stop and daily restart for all of them.
- Changes made while running last until 15:10 CT and survive a restart that day (`market_today.json`).

### The probability calculator

- Every signal (the bot's own, every shadow strategy's, every idea and setup, and your trade ticket)
  gets P(win), average win and loss in R, expected value after fees and slippage, and an 80% range,
  pooled from related markets down to this market, strategy, side, time of day and market mood, without
  counting any outcome twice.
- `probability.veto`: a signal the calculator is at least 90% sure loses after costs is skipped, only
  with enough evidence.
- `probability.sizing`: in *Make money every day*, each trade is sized by a quarter of the Kelly
  fraction, from a quarter of the usual risk up to `aggressive.max_conviction` (1.5), inside every hard
  limit.
- Every prediction is saved with its outcome; Knowledge → Overview shows a reliability chart, the
  Brier score and its skill over always guessing the average.

### The knowledge network

- Related markets (same asset class) inform each other's odds at 30% of the weight, from every
  account's knowledge bases.
- Once a day, outside the entry window and while flat, the bot learns a few related markets it doesn't
  trade from the history saved on the PC (`probability.grow_network`, `probability.grow_per_day`), or at
  once with **Learn related markets now**. Nothing is traded.

### More aggressive *Make money every day*

- Each strategy may trade three times its usual daily count (was twice), each market brings its own
  `max_trades_per_day` (`aggressive.trades_per_market`), 35% of the day's realized profit presses the
  next trade (was 25%), up to 2.5 times the risk per trade (was 2), and sizing follows the calculator.
  Every Topstep guard, the consistency line, the jump allowance and the MLL cushion apply as before.
- A `config.yaml` written by 3.0's Setup tab kept 3.0's numbers: saving the Setup tab once with *Make money
  every day* moves them to 4.0's (numbers you changed yourself are kept). New configs list these settings
  as comments, so the version's defaults apply.

### The dashboard

- Seven tabs instead of eleven: **Overview**, **Trade** (Ticket, Ideas), **Ask** (Ask, Deep review),
  **Knowledge** (Overview, What works, Simulations, Market data), Settings, Setup and Logs.
- **Ask** explains anything in the bot: a new `explain` lookup searches the guide, the Topstep rules
  page and every setting (with this account's values), next to new `markets`, `probability` and
  `sources` lookups, for the built-in answers and the AI alike.
- **Knowledge → Overview**: every source of knowledge in one place (real trades, watched live, real
  market history, simulations, related markets, predictions scored), the odds of every strategy now,
  calibration, what the bot knows and the knowledge network.
- **Charts of the current trade**: live candles (the forming bar included) with entry, stop, target,
  the first stop, today's fills and the last price on the Overview, and the ticket's planned stop and
  target on the trade ticket. Hover for prices; works on the phone.

### Honest notes

- On simulated data (40 days × 12 seeds on MNQ, MES and MCL, one market each) the aggressive mode now
  averages about 2 trades a day per market against about 1.6 for *Pass the Combine*: the strategies only
  find so many signals, so the higher limits mostly matter on busy days and with several markets. No
  simulated day crossed the consistency line or the MLL. Simulated data proves the guards work; it says
  nothing about real profits.
- The calculator's odds are past frequencies of similar signals, and on most markets the evidence is
  still small; until a few hundred outcomes have been scored its calibration says little.
- Several markets were tested against a simulated TopstepX server (two markets on one connection,
  adding, dropping and switching while trading) and in unit tests of the shared limits, not over long
  live sessions yet. Start with two markets on Paper.
- No bot can guarantee passing the Combine or making a profit, and this one doesn't either.

## 3.0.0 — the first release

The first tagged release. Everything from 1.x and 2.x, plus:

### Make money every day (the aggressive mode)

- A third goal on the Setup tab, *Make money every day*, next to *Pass the Combine* and *Teach the
  bot*. It writes a visible `aggressive:` section to `config.yaml`.
- The adaptive strategy trades every strategy's signal except the ones the evidence shows are losing,
  and each strategy may trade twice its usual daily count (up to 12 trades a day).
- Signals the knowledge base hasn't proven trade at half the risk per trade.
- A quarter of the day's realized profit is added to the next trade's risk, never past twice the risk
  per trade; a stressed market halves it; one trade never risks more than a quarter of the room above
  the Maximum Loss Limit.
- A daily profit goal (default 45% of the profit target, always under the Combine's 50% line) ends the
  day; once the day has made half its goal it can't give back more than 40% of its best.
- An open trade is closed before a high-impact release.
- The Overview shows the goal, what the next trade may risk, what is locked in, and the line no trade
  may cross.

### Risk: two gaps closed, in every goal

- **Consistency line.** Every entry (automatic, manual, ideas, the first trade) is planned so that
  winning it can't lift a Combine day past 50% of the profit target: the target becomes a limit at the
  line or the size comes down; under 4 ticks of room, no trade. The same applies to an XFA
  consistency path's daily cap.
- **Sudden jumps.** The MLL, the personal daily loss limit, Topstep's DLL and the XFA winning-day
  protection are checked against the stop plus the market's jump allowance (measured from 5-minute
  history, with a floor per asset class), so a stop that fills late can't reach them. This makes some
  trades smaller than in 2.2, most of all in grains, livestock and energy.

### The market network

- Every market read together: trend and volatility state against its own decade, 60-day correlations
  and peers, divergences, a risk-appetite board, the next 24 hours' reports and headline flow, the
  Simulation Lab's best playbooks, and each market's jump allowance.
- Rebuilt every hour while the dashboard runs (`data/network.json`); shown on the Market tab, in
  Telegram `/network`, `topstep-bot network` (menu 25), the Ask tab and the AI coach's new
  `get_market_network` lookup.
- Protection only: it can make trades smaller, never bigger, and never picks a direction.

### Claude

- One module for everything Claude: the key, the models and prices (Opus 5.5 by default, Sonnet 5.5,
  Haiku 5.5, Fable 5.1), refusal fallbacks, prompt caching, the shared spending ledger and plain-words
  errors. The coach and the Ask answers use it the same way.
- The Setup tab picks the model from a list and shows the month's spending split between the coach
  and the Ask answers.

### Honest notes

- On simulated data (40 days × 12 seeds on MNQ, MES and MCL) the aggressive mode averaged about 2
  trades a day against about 1.7 for *Pass the Combine*: the strategies only find so many signals. No
  simulated day crossed the consistency line or the MLL. Simulated data proves the guards work; it says
  nothing about real profits.
- More trades and bigger size on good days mean bigger swings both ways. No bot can guarantee a profit
  or passing the Combine.
- The bot still uses the classic decision rule until the decision lab proves the evidence rule.
