# Changelog

Every version of the bot, newest first. The release workflow publishes each version's section as its
GitHub release notes.

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
