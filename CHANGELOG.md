# Changelog

Every version of the bot, newest first. The release workflow publishes each version's section as its
GitHub release notes.

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
