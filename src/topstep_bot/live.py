"""Live and paper trading runner: one account, one or several markets at once.

The runner is the account's "desk". Each market it trades is a ``Lane``: its own TradingCore,
price stream, bars, knowledge base, news and market picture. All lanes share the account: one
broker connection, one journal, one Maximum Loss Limit tracker and one account book
(risk/book.py), so every Topstep limit counts all open positions together. Markets can be added,
dropped or switched while the bot runs (``add_market``, ``remove_market``, ``switch_market``): no
restart. A market is only dropped once it is flat; until then it takes no new trades.

Data flow, per lane:
  * Bars: the official bar history endpoint is polled right after each bar closes, so live
    bars are identical to the bars a backtest would use.
  * Prices: the realtime market hub streams quotes; they drive open-risk checks (and fills
    in paper mode). If the stream drops, prices fall back to REST polling.
  * Orders (live mode): the realtime user hub reports order/position/fill changes (shared by all
    lanes, see broker/shared.py), and a periodic REST reconcile repairs anything missed.

Stop the bot with Ctrl+C, the dashboard's or Telegram's Stop, or by creating a file named KILL
in the working directory (which also flattens and halts trading).

When started by the controller (topstep-bot start) the runner also writes a heartbeat file,
asks for a restart once a day during the CME maintenance halt, and announces restarts quietly.
See service.py for the contract between the two processes.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time as _time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx

from topstep_bot.api.realtime import MarketStream
from topstep_bot.api.rest import ProjectXClient, ProjectXError
from topstep_bot.bars import floor_time
from topstep_bot.broker.base import Broker
from topstep_bot.broker.paper import PaperBroker, PaperWallet
from topstep_bot.broker.shared import BrokerHub
from topstep_bot.config import MAX_MARKETS, BotConfig, Secrets
from topstep_bot.crossaccount import PositionBook
from topstep_bot.engine import TradingCore
from topstep_bot.factory import build_core, fees_for, starting_balance
from topstep_bot.journal import Journal
from topstep_bot.logging_setup import asyncio_exception_handler, spawn
from topstep_bot.models import Account, BarUnit, Contract, Quote
from topstep_bot.news import NewsCalendar
from topstep_bot.notify import Notifier
from topstep_bot.risk.book import AccountBook
from topstep_bot.risk.topstep import PLANS, LossLimitTracker
from topstep_bot.sessions import session_open_for

log = logging.getLogger(__name__)
UTC = timezone.utc
KILL_FILE = Path("KILL")
LEARN_CHECK_SECONDS = 300  # how often the learning loop checks whether the long-run memory is due


PREPARE_RETRY_FIRST = 15  # seconds before trying TopstepX again after it couldn't be reached at startup
PREPARE_RETRY_MAX = 300


def unreachable(exc: BaseException) -> bool:
    """A connection problem (network down, timeout, TopstepX answering 5xx or 429), not a refusal or a bug."""
    if isinstance(exc, httpx.TransportError | TimeoutError | ConnectionError):
        return True
    return isinstance(exc, ProjectXError) and exc.payload.get("success") is None and (exc.code or 0) in (
        429, *range(500, 600))


class SetupError(Exception):
    """A configuration/account problem the user needs to fix (shown without a traceback)."""


@dataclass
class Controls:
    """Requests coming from the controller (dashboard / Telegram), signal handlers or the bot itself."""

    stop: asyncio.Event = field(default_factory=asyncio.Event)
    flatten_requested: bool = False
    flatten_reason: str = "flatten requested"
    restart_requested: bool = False  # quiet daily maintenance restart (exit code 75)
    failure: str | None = None  # the bot stopped itself after an internal error (exit code 1)
    stop_reason: str = ""

    def request_stop(self, reason: str) -> None:
        if not self.stop_reason:
            self.stop_reason = reason
        self.stop.set()


async def select_account(client: ProjectXClient, cfg: BotConfig) -> Account:
    accounts = await client.search_accounts(only_active=True)
    if not accounts:
        raise SetupError("No active accounts found on this TopstepX login.")
    if cfg.account.account_id is not None:
        match = [a for a in accounts if a.id == cfg.account.account_id]
    elif cfg.account.account_name:
        match = [a for a in accounts if a.name.lower() == cfg.account.account_name.lower()]
    else:
        match = [a for a in accounts if a.can_trade]
        if len(match) > 1:
            names = ", ".join(f"{a.name} (id {a.id})" for a in match)
            raise SetupError(f"Several accounts can trade: {names}. Pick the account on the dashboard's Setup tab.")
    if not match:
        names = ", ".join(f"{a.name} (id {a.id})" for a in accounts)
        raise SetupError(f"Configured account not found. Available: {names}")
    return match[0]


async def resolve_contract(client: ProjectXClient, cfg: BotConfig) -> Contract:
    if cfg.instrument.contract_id:
        return await client.contract_by_id(cfg.instrument.contract_id)
    return await client.resolve_contract(cfg.instrument.symbol)


def lane_config(cfg: BotConfig, symbol: str) -> BotConfig:
    """The account's settings for one of its markets (paths such as the knowledge base follow the symbol)."""
    return cfg.for_market(symbol)


def _description(symbol: str) -> str:
    from topstep_bot.instruments import SPECS

    spec = SPECS.get(symbol.upper())
    return spec.description if spec else symbol


# ======================================================================== one market


class Lane:
    """One market the account trades: its trading core, its prices and bars, and what it learns from."""

    def __init__(self, desk: LiveRunner, cfg: BotConfig, *, primary: bool = False):
        self.desk = desk
        self.cfg = cfg
        self.primary = primary  # the first market: runs the first-trade planner
        self.core: TradingCore | None = None
        self.contract: Contract | None = None
        self.broker: Broker | None = None
        self.market: MarketStream | None = None
        self.positions: PositionBook | None = None  # hedge guard
        self.closing: str | None = None  # dropping this market once it is flat (no new trades meanwhile)
        self._last_bar_ts: datetime | None = None
        self._last_price_at: datetime | None = None
        self._tasks: list[asyncio.Task] = []
        self._training = False
        self._learn_after: datetime | None = None  # after a failed long-run learning run: wait before trying again
        self._train_after: datetime | None = None  # after a failed daily retrain: wait before trying again
        self._warmup_bars: tuple[list, tuple[datetime, datetime]] | None = None  # kept until the long-run memory opens
        self._catchup_bars: list = []  # today's bars so far: replayed once the knowledge base is ready

    @property
    def symbol(self) -> str:
        return self.cfg.instrument.symbol

    @property
    def client(self) -> ProjectXClient:
        return self.desk.client

    @property
    def journal(self) -> Journal:
        return self.desk.journal

    def now(self) -> datetime:
        return self.desk.now()

    # ------------------------------------------------------------------ setup

    async def open(self, balance: float, today: date) -> TradingCore:
        """Build this market's core, warm it up and load everything it learns from."""
        desk, cfg = self.desk, self.cfg
        self.contract = await resolve_contract(self.client, cfg)
        log.info("Market %s: contract %s (%s)", self.symbol, self.contract.name, self.contract.id)
        self.broker = desk.lane_broker(self.contract)
        core = build_core(
            cfg, self.contract, self.broker, clock=desk.now, account_label=desk.label, journal=self.journal,
            notifier=desk.notifier, book=desk.book, tracker=desk.tracker, guard_from=desk.guard_source(),
        )
        self.core = core
        core.day_roller = desk.roll_day
        core.keeps_records = not desk.lanes  # the first market writes the account's daily records
        core.balance = balance
        from topstep_bot.market_intel import MarketTracker

        core.intel = MarketTracker(self.contract.root or self.symbol, self.contract, core.schedule,
                                    cfg.account_dir / "market_journal.jsonl")
        core.intel.result_for_day = desk._day_result
        if self.primary:
            core.market_pick = desk.market_pick.to_dict() if desk.market_pick else None
            core.market_today = desk.market_today
        if cfg.recommendations.enabled:
            from topstep_bot.recommendations import RecommendationBook

            core.recommender = RecommendationBook(core, cfg.recommendations.strategies or None)
        from topstep_bot.reasoning import Reasoner

        core.attach_reasoner(Reasoner(core))
        await self._warmup()
        await core.begin_day(today, balance)  # the desk already started the account's day: counts carry on
        core.risk.live_guards = True
        try:
            core.apply_network()
        except Exception:  # noqa: BLE001 - the class defaults stay in place
            log.exception("Could not read the market network")
        from topstep_bot.remote import RemoteControl

        core.remote = RemoteControl(core, cfg.account_dir / "remote_settings.json")
        core.setups.enabled = True  # the dashboard's Setups panel: note setups forming, firing, cancelled
        for change in core.remote.load_saved():
            log.info("Re-applied remote setting (%s): %s", self.symbol, change)
        self._setup_memory()
        await self._setup_knowledge()
        self._catch_up()
        await self._setup_news()
        if self.primary:
            self._setup_first_trade()
        self._setup_hedge_guard()
        return core

    async def start(self) -> None:
        """Start this market's price stream and bar loop."""
        assert self.contract is not None
        self.market = MarketStream(self.desk.cfg.api.market_hub_url, self.client.get_token, self.contract.id)
        self.market.on_quote = self._on_quote
        self._tasks = [
            spawn(self.market.hub.run(), name=f"market-hub {self.symbol}"),
            spawn(self._bar_loop(), name=f"bars {self.symbol}"),
        ]
        for task in self._tasks:
            task.add_done_callback(self.desk._loop_ended)
        await self.reconcile()

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        self._tasks = []
        if self.market:
            with contextlib.suppress(Exception):
                await self.market.hub.stop()
        core = self.core
        if core and core.memory:
            core.memory.close()
        if self.positions and core and core.orders.is_flat:
            self.positions.release()

    def summary(self) -> dict:
        """One line per market for the dashboard's market chips."""
        core = self.core
        if core is None:
            return {"symbol": self.symbol, "ready": False}
        t = core.orders.trade
        today = [x for x in core.closed_trades if x.closed_at and core.schedule.trading_day(x.closed_at) == core.current_day]
        return {
            "symbol": self.symbol, "ready": True, "contract": core.contract.name,
            "description": _description(self.symbol),
            "position": core.orders.position, "open_pnl": round(core.orders.open_pnl(), 2),
            "last_price": core.last_price, "strategy": core.strategy.title,
            "trade": None if t is None else {"side": t.side.label, "size": t.filled_size or t.size,
                                             "entry": t.entry_price, "stop": t.stop_price, "target": t.target_price,
                                             "state": t.state.value},
            "trades_today": len(today), "realized_today": round(sum(x.net_pnl for x in today), 2),
            "closing": self.closing, "primary": self.primary, "halted": core.halted,
            "spark": [round(b.close, core.contract.price_decimals) for b in list(core.chart_bars)[-40:]],
        }

    def _setup_hedge_guard(self) -> None:
        """Coordinate with this PC's other account bots (and this account's other markets) so no two hold
        opposite positions in related markets."""
        cfg, core = self.cfg, self.core
        root = (self.contract.root if self.contract else "") or self.symbol  # MNQ, not the code MNQZ6
        self.positions = PositionBook(cfg.data_path / "positions", cfg.active_account or "main", cfg.mode, root,
                                      cfg.multi_account.hedge_guard, lane=root)
        self.sync_positions()
        if self.positions.enabled and core is not None:
            core.entry_guard = self.positions.claim

    def sync_positions(self) -> None:
        if self.positions is None or self.core is None:
            return
        orders = self.core.orders
        pos = orders.position
        side = (1 if pos > 0 else -1) if pos else (orders.trade.side.sign if orders.trade is not None else 0)
        self.positions.sync(side)

    async def _setup_knowledge(self) -> None:
        """Load the knowledge base the bot learns into and make sure its training covers every finished session.

        Training replays recent history through every strategy. It is refreshed at startup when a
        session has finished since the last training, when it is older than ``retrain_hours``, or
        when the strategy code changed (after an update). The history comes from the market library
        on disk first; only what it is missing is downloaded."""
        cfg, core = self.cfg, self.core
        if not cfg.knowledge.enabled or core is None:
            return
        from topstep_bot.knowledge import KnowledgeBase

        kb = KnowledgeBase.from_config(cfg, cfg.knowledge_path)
        core.attach_knowledge(kb)
        if kb.recovery:  # a damaged file was repaired: worth an alert
            core.event("warning", "Knowledge base repaired at startup: " + "; ".join(kb.recovery) + ".", "error")
        c = kb.counts()
        log.info("Knowledge base %s: %d observations (train %d, live ideas %d, real trades %d, manual %d)%s",
                 cfg.knowledge_path.name, len(kb.obs), c["train"], c["shadow"], c["real"], c["manual"],
                 f", trained {kb.trained['at'][:16]}" if kb.trained else ", untrained")
        now = self.now()
        why = kb.training_due_reason(now, cfg.knowledge.retrain_hours, session_end=core.schedule.last_session_end(now))
        if cfg.knowledge.auto_train and why:
            log.info("Retraining the knowledge base: %s", why)
            try:
                await self.retrain("startup")
            except Exception as exc:  # noqa: BLE001 - trading can go on with the knowledge we have
                log.warning("Training failed: %s", exc)
                core.event("warning", f"Could not retrain the knowledge base: {exc}")
        self.refresh_policy()
        core.event("info", self._knowledge_ready_text(kb))

    @staticmethod
    def _knowledge_ready_text(kb) -> str:
        c = kb.counts()
        parts = [f"Knowledge ready: {len(kb.obs):,} observations"]
        if kb.trained:
            t = kb.trained
            parts.append(f"training covers {t['days']} days to {t['to']}" + (f" ({t['origin']})" if t.get("origin") else ""))
        else:
            parts.append("not trained yet")
        parts.append(f"{c['shadow']} live ideas, {c['real']} real trades, {c['manual']} manual trades")
        return "; ".join(parts) + "."

    def _catch_up(self) -> None:
        """Replay today's bars from before this start, so today's ideas are learned from and followed on."""
        core, bars = self.core, self._catchup_bars
        self._catchup_bars = []
        if core is None or not bars:
            return
        book = core.recommender
        before = len(book.items) if book else 0
        learned = core.knowledge.updated if core.knowledge else 0
        for bar in bars:
            core.catch_up_bar(bar)
        if book is None:
            return
        ideas = list(book.items)[: len(book.items) - before]
        still_open = sum(1 for r in ideas if r.is_open)
        added = (core.knowledge.updated - learned) if core.knowledge else 0
        log.info("Caught up on %d bars from earlier today: %d idea(s), %d new observation(s), %d still followed live",
                 len(bars), len(ideas), added, still_open)
        if ideas:
            core.event("info", f"Caught up on today so far: {len(ideas)} idea(s) from before the start, "
                               f"{added} new for the knowledge base, {still_open} still being followed.")

    def _setup_first_trade(self) -> None:
        """Teach the bot: an educated trade soon after starting (first_trade.py), if turned on."""
        cfg, core = self.cfg, self.core
        if core is None or not cfg.first_trade.enabled:
            return
        from topstep_bot.first_trade import FirstTradePlanner

        planner = FirstTradePlanner(core, cfg.first_trade, journal=self.journal)
        core.first_trade = planner
        planner.request(self.desk.started_at, restart=self.desk.quiet_start)
        core.event("info", f"First trade after starting: {planner.message}")

    def _setup_memory(self) -> None:
        """Open the long-run memory (memory.py): the bars downloaded so far go straight into its library."""
        cfg, core = self.cfg, self.core
        if not (cfg.knowledge.enabled and cfg.knowledge.deep_learning) or core is None:
            return
        from topstep_bot.memory import LongRunMemory

        try:
            core.memory = LongRunMemory.open(cfg)
        except Exception as exc:  # noqa: BLE001 - the long-run memory is a bonus: trading goes on without it
            log.warning("Could not open the long-run memory: %s", exc)
            return
        if self._warmup_bars:
            bars, span = self._warmup_bars
            core.memory.remember(bars, self.contract.id, span)
        self._warmup_bars = None
        st = core.memory.status()
        log.info("Long-run memory: %d bars in the library, %d observations%s", st["library"]["bars"], st["observations"],
                 f", replayed {st['trained']['at'][:16]}" if st["trained"] else ", not replayed yet")

    def _remember(self, bars, span: tuple[datetime, datetime] | None = None) -> None:
        if self.core is not None and self.core.memory is not None and self.contract is not None:
            self.core.memory.remember(bars, self.contract.id, span)

    # ------------------------------------------------------------- learning

    async def learn(self, source: str) -> str:
        """Backfill the market library and replay all of it into the long-run knowledge (memory.py)."""
        core = self.core
        if core is None or core.memory is None:
            raise RuntimeError("The long-run memory is turned off (knowledge.deep_learning: false)")
        if not self.cfg.memory_owner:
            raise RuntimeError("Another account trading the same market keeps the long-run memory up to date - "
                               "ask that account's bot to learn")
        core.event("info", f"Learning from the long-run memory ({source}): downloading missing history, then replaying "
                           f"up to {self.cfg.knowledge.deep_history_days} days through every strategy...")
        result = await core.memory.learn(self.cfg, self.contract, self.client, self.now())
        message = result.text()
        core.event("info", message)
        lab = await self.run_lab()
        if lab is not None:
            message += "\n" + lab
        return message

    async def run_lab(self) -> str | None:
        """Replay both decision rules over the long-run memory (decision_lab.py) and trade with the one it supports."""
        from topstep_bot.decision_lab import result_text, run_lab

        core = self.core
        if core is None or core.memory is None:
            return None
        observations = list(core.memory.knowledge.obs)
        try:
            result = await asyncio.to_thread(run_lab, self.cfg, observations)
        except Exception as exc:  # noqa: BLE001 - the bot keeps the rule it has
            log.warning("The decision lab failed: %s", exc)
            return None
        text = result_text(result)
        log.info(text)
        self.refresh_policy(result)
        return text

    def refresh_policy(self, result: dict | None = None) -> None:
        """Trade with the decision rule config.yaml (or, on auto, the decision lab) asks for."""
        from topstep_bot.decision_lab import resolve_policy

        if self.core is not None:
            self.core.set_policy(*resolve_policy(self.cfg, result))

    async def learn_step(self, now: datetime) -> None:
        """Outside the entry window and while flat: retrain once each session has finished, and grow the long-run
        memory daily (the desk's learning loop calls this for every market)."""
        core = self.core
        if core is None or not core.orders.is_flat or core.schedule.entry_block_reason(now) is None:
            return  # inside the entry window or in a trade: learn later, so trading never waits on it
        await self._retrain_if_due(now)
        self.refresh_policy()  # another account may have rerun the decision lab; never switches mid-session
        memory = core.memory
        if memory is None or memory.running or (self._learn_after and now < self._learn_after):
            return
        if not self.cfg.memory_owner:
            return  # with several accounts, the first one trading this market does the replay for all
        if not memory.due(now, self.cfg.knowledge.retrain_hours):
            return
        try:
            await self.learn("daily")
        except Exception as exc:  # noqa: BLE001 - keep trading; try again later
            self._learn_after = now + timedelta(hours=2)
            log.warning("Long-run learning failed: %s", exc)
            core.event("warning", f"Could not update the long-run memory: {exc} (will try again later)")

    async def _retrain_if_due(self, now: datetime) -> None:
        core = self.core
        kb = core.knowledge if core else None
        if kb is None or not self.cfg.knowledge.auto_train or self._training or (self._train_after and now < self._train_after):
            return
        why = kb.training_due_reason(now, self.cfg.knowledge.retrain_hours, session_end=core.schedule.last_session_end(now))
        if why is None:
            return
        log.info("Retraining the knowledge base: %s", why)
        try:
            await self.retrain("daily")
        except Exception as exc:  # noqa: BLE001 - keep trading on the knowledge it has; try again later
            self._train_after = now + timedelta(hours=1)
            log.warning("Daily training failed: %s", exc)
            core.event("warning", f"Could not retrain the knowledge base: {exc} (will try again later)")

    async def retrain(self, source: str) -> str:
        """Download recent history and rebuild the knowledge base's training layer."""
        core = self.core
        if core is None or core.knowledge is None:
            raise RuntimeError("The knowledge base is turned off (knowledge.enabled: false)")
        if self._training:
            raise RuntimeError("Training is already running")
        from topstep_bot.knowledge import train_from_bars

        self._training = True
        try:
            days = self.cfg.knowledge.history_days
            end = self.now()
            core.event("info", f"Training the {self.symbol} knowledge base on the last {days} days ({source})...")
            bars, origin = await self._training_bars(days, end)
            result = await train_from_bars(self.cfg, self.contract, bars, core.knowledge, at=end, origin=origin,
                                           news_archive=self.news_archive())
            per = ", ".join(f"{k} {v}" for k, v in result["per_strategy"].items())
            message = (f"Knowledge base trained on {result['days']} days ({result['from']} to {result['to']}, {origin}): "
                       f"{result['observations']} observations ({per})")
            core.event("info", message)
            return message
        finally:
            self._training = False

    async def _training_bars(self, days: int, end: datetime) -> tuple[list, str]:
        """The last ``days`` of closed bars to train on, and where they came from.

        With the long-run memory on, the market library on disk is the source: only the ranges it
        doesn't have yet are downloaded, so a restart retrains in seconds, and a failed download
        still trains on the history already saved. Without it, the whole range is downloaded."""
        minutes = self.cfg.instrument.timeframe_minutes
        tf = timedelta(minutes=minutes)
        start = end - timedelta(days=days)
        memory = self.core.memory if self.core else None
        if memory is not None:
            problem = None
            try:
                new = await memory.backfill(self.client, self.contract, days, end, live=self.cfg.data.live_market_data)
            except Exception as exc:  # noqa: BLE001 - fall back to the history already saved
                problem, new = exc, 0
                log.warning("Could not download the missing history for training: %s", exc)
            bars = [b for b in memory.library.bars(memory.symbol, memory.tf, start, end) if b.ts + tf <= end]
            if bars:
                if problem is not None:
                    return bars, "saved history only: the download failed"
                return bars, (f"saved history + {new:,} new bars" if new else "saved history")
            if problem is not None:
                raise problem
        bars = await self.client.retrieve_bars_range(self.contract.id, start, end, BarUnit.MINUTE, minutes,
                                                     live=self.cfg.data.live_market_data)
        bars = [b for b in bars if b.ts + tf <= end]
        self._remember(bars, (start, end - tf))
        return bars, "downloaded"

    async def _warmup(self) -> None:
        assert self.core and self.contract
        tf = self.cfg.instrument.timeframe_minutes
        end = self.now()
        # Enough history for every strategy (and the shadows the bot learns from) to be signal-ready
        # on the first live bar, even if data.warmup_days was set lower than a strategy needs.
        strategies = [self.core.strategy, *(self.core.recommender.shadows if self.core.recommender else [])]
        days = max(self.cfg.data.warmup_days, *(s.warmup_days + 1 for s in strategies))
        start = end - timedelta(days=int(days * 1.6) + 3)
        bars = await self.client.retrieve_bars_range(
            self.contract.id, start, end, BarUnit.MINUTE, tf, live=self.cfg.data.live_market_data
        )
        closed = [b for b in bars if b.ts + timedelta(minutes=tf) <= end]
        today = self.core.schedule.trading_day(end)
        earlier = [b for b in closed if self.core.schedule.trading_day(b.ts) < today]
        for bar in earlier:
            self.core.warmup_bar(bar)
        # Today's bars so far are replayed once the knowledge base is ready (_catch_up), so the ideas
        # they gave are learned from rather than skipped.
        self._catchup_bars = closed[len(earlier):]
        if closed:
            self._last_bar_ts = closed[-1].ts
        self._warmup_bars = (closed, (start, end - timedelta(minutes=tf)))  # stored once the long-run memory is open
        log.info("Warmed up %s on %d %d-minute bars (%d from today replayed once the knowledge base is ready)",
                 self.symbol, len(closed), tf, len(self._catchup_bars))

    # --------------------------------------------------------------- news

    def _research(self):
        """The research library the controller fills (research.py), or None."""
        if not self.cfg.research.enabled:
            return None
        from topstep_bot.research import ResearchLibrary

        return ResearchLibrary(self.cfg.data_path / "research")

    def news_archive(self):
        """Past calendars from the research library, filtered like the live calendar (labels training's news days)."""
        lib = self._research()
        if lib is None:
            return None
        n = self.cfg.news
        symbol = (self.contract.root if self.contract else "") or self.symbol
        rules = NewsCalendar(n.url, self.cfg.data_path / "unused.json", n.impacts, n.currencies, n.minutes_before,
                             n.minutes_after, symbol=symbol, per_instrument=n.per_instrument)
        archive = lib.archive_calendar(rules.matters)
        return archive if archive.covered else None

    def _load_daily_history(self) -> None:
        lib = self._research()
        if lib is None or self.core is None or self.core.intel is None:
            return
        from topstep_bot.market_knowledge import product_for

        product = product_for(self.core.intel.symbol)
        if product:
            self.core.intel.daily = lib.load_daily(product.root)

    async def _setup_news(self) -> None:
        if self.core is None:
            return
        n, core = self.cfg.news, self.core
        symbol = self.contract.root or self.symbol
        if n.headlines and core.intel:
            from topstep_bot.market_knowledge import product_for
            from topstep_bot.news import HeadlineFeed

            product = product_for(symbol)
            if product:
                feed = HeadlineFeed([n.headline_url.format(symbol=y) for y in product.yahoo],
                                    self.cfg.data_path / f"headlines_{product.root}.json")
                feed.load_cache()
                core.intel.headlines = feed
        self._load_daily_history()
        if not n.enabled:
            return
        # One cache per instrument: which events matter depends on what is traded.
        calendar = NewsCalendar(n.url, self.cfg.data_path / f"news_cache_{symbol}.json", n.impacts, n.currencies,
                                n.minutes_before, n.minutes_after, symbol=symbol, per_instrument=n.per_instrument)
        if not calendar.load_cache() or calendar.fetched_at is None or (
            self.now() - calendar.fetched_at > timedelta(hours=6)
        ):
            await calendar.refresh()
        core.schedule.news = calendar
        if core.intel:
            core.intel.news = calendar
        upcoming = calendar.upcoming(self.now(), hours=24)
        if upcoming:
            tz = core.schedule.tz
            listing = ", ".join(f"{e.label} {e.time.astimezone(tz):%a %H:%M} CT" for e in upcoming[:6])
            core.event("info", f"News blackouts in the next 24h: {listing}")

    async def news_step(self, refresh_calendar: bool) -> None:
        """Refresh the headlines when due, and (every 6 hours) the calendar and daily history."""
        core = self.core
        if core is None:
            return
        feed = core.intel.headlines if core.intel else None
        if feed and (feed.fetched_at is None or self.now() - feed.fetched_at >= timedelta(
                minutes=self.cfg.news.headline_refresh_minutes)):
            await feed.refresh()
        if refresh_calendar:
            self._load_daily_history()  # the controller's research refreshes it daily
            if core.schedule.news:
                try:
                    await core.schedule.news.refresh()
                except Exception:  # noqa: BLE001 - keep the last calendar; trading goes on
                    log.exception("Economic calendar refresh failed")

    # --------------------------------------------------------------- prices and bars

    async def _on_quote(self, q: Quote) -> None:
        price = q.last if q.last is not None else q.mid
        if price is None or self.core is None:
            return
        self._last_price_at = self.now()
        if isinstance(self.broker, PaperBroker):
            await self.broker.on_price(q.ts, price, q.bid, q.ask)
        self.core.on_quote(q.bid, q.ask, self.now())
        await self.core.on_price(q.ts, price)

    async def _bar_loop(self) -> None:
        """Fetch each bar right after it closes."""
        assert self.core and self.contract
        tf = self.cfg.instrument.timeframe_minutes
        tf_s = tf * 60
        while True:
            now = self.now()
            next_close = floor_time(now, tf_s) + timedelta(seconds=tf_s)
            await asyncio.sleep((next_close - now).total_seconds() + 1.5)
            if not self.core.schedule.market_open(next_close - timedelta(seconds=tf_s)):
                continue  # weekend / daily halt: no bars to fetch
            for _ in range(6):
                try:
                    if await self._fetch_new_bars(tf):
                        break
                except ProjectXError as exc:
                    log.warning("Bar fetch failed (%s): %s", self.symbol, exc)
                except Exception:  # noqa: BLE001
                    log.exception("Bar fetch error (%s)", self.symbol)
                await asyncio.sleep(4)

    async def _fetch_new_bars(self, tf: int) -> bool:
        assert self.core and self.contract
        now = self.now()
        start = self._last_bar_ts or now - timedelta(minutes=tf * 5)
        bars = await self.client.retrieve_bars(
            self.contract.id, start, now, BarUnit.MINUTE, tf, limit=100, live=self.cfg.data.live_market_data
        )
        new = [b for b in bars if (self._last_bar_ts is None or b.ts > self._last_bar_ts) and b.ts + timedelta(minutes=tf) <= now]
        for bar in new:
            self._last_bar_ts = bar.ts
            await self.core.on_bar(bar)
        if new:
            self._remember(new, (start, new[-1].ts))
        return bool(new)

    def price_stale(self, now: datetime) -> bool:
        return self._last_price_at is None or now - self._last_price_at > timedelta(seconds=20)

    async def poll_price(self) -> None:
        """Fallback price source when the realtime stream is quiet or disconnected."""
        assert self.core and self.contract
        now = self.now()
        bars = await self.client.retrieve_bars(
            self.contract.id, now - timedelta(minutes=5), now, BarUnit.MINUTE, 1, limit=5,
            live=self.cfg.data.live_market_data, include_partial=True,
        )
        if bars:
            price = bars[-1].close
            if isinstance(self.broker, PaperBroker):
                await self.broker.on_price(now, price)
            await self.core.on_price(now, price)

    async def reconcile(self) -> None:
        if self.core is None:
            return
        try:
            await self.core.orders.reconcile()
        except Exception as exc:  # noqa: BLE001
            log.warning("Reconcile failed (%s): %s", self.symbol, exc)


# ======================================================================== the account


class LiveRunner:
    """The account's desk: one connection, one journal and one set of Topstep limits for every market it trades."""

    def __init__(self, cfg: BotConfig, secrets: Secrets, controls: Controls | None = None):
        if not secrets.has_credentials:
            raise SetupError("Missing TopstepX credentials. Add your TopstepX login on the dashboard's Setup tab.")
        self.cfg = cfg
        self.secrets = secrets
        self.controls = controls or Controls()
        self.client = ProjectXClient(secrets.username, secrets.api_key, cfg.api.base_url, cfg.api.timeout_seconds)
        self.journal, self.journal_damaged = Journal.open_or_recover(cfg.journal_path)
        who = f"{cfg.active_account}, {cfg.mode}" if cfg.active_account else cfg.mode
        self.notifier = Notifier(cfg.notifications, secrets, prefix=f"Topstep Bot ({who})")
        self.account: Account | None = None
        self.label = ""
        self.lanes: dict[str, Lane] = {}
        self.focus: str | None = None  # the market the dashboard and Telegram show (and act on) by default
        self.book: AccountBook | None = None
        self.tracker: LossLimitTracker | None = None
        self.hub: BrokerHub | None = None  # live: the one TopstepX connection, shared by every market
        self.wallet: PaperWallet | None = None  # paper: the one simulated balance, shared by every market
        self._shared_broker: Broker | None = None
        self._tasks: list[asyncio.Task] = []
        self._lane_lock = asyncio.Lock()
        self._rolling = False
        self._network_after: datetime | None = None  # the next time the knowledge network may grow on its own
        self._growing = False
        # Set by the 24/7 service (see service.py).
        self.supervised = os.environ.get("TOPSTEP_BOT_SUPERVISED") == "1"
        self.quiet_start = os.environ.get("TOPSTEP_BOT_QUIET_START") == "1"
        heartbeat = os.environ.get("TOPSTEP_BOT_HEARTBEAT")
        self.heartbeat_path = Path(heartbeat) if heartbeat else None
        self.started_at = datetime.now(UTC)
        self._checked_in: date | None = None
        self.market_pick = None  # market_pick.Pick when instrument.auto_pick chose the market
        self.market_today: dict | None = None  # the market(s) you switched the account to for today

    @staticmethod
    def now() -> datetime:
        return datetime.now(UTC)

    # ------------------------------------------------------------- the focused market

    @property
    def lane(self) -> Lane | None:
        if self.focus in self.lanes:
            return self.lanes[self.focus]
        return next(iter(self.lanes.values()), None)

    @property
    def core(self) -> TradingCore | None:
        lane = self.lane
        return lane.core if lane else None

    @property
    def contract(self) -> Contract | None:
        lane = self.lane
        return lane.contract if lane else None

    @property
    def broker(self) -> Broker | None:
        lane = self.lane
        return lane.broker if lane else self._shared_broker

    @property
    def market(self) -> MarketStream | None:
        lane = self.lane
        return lane.market if lane else None

    @property
    def keeper(self) -> Lane | None:
        """The market that writes the account's records (the day's result): the first one still traded."""
        return next(iter(self.lanes.values()), None)

    def lane_for(self, symbol: str | None) -> Lane:
        if not symbol:
            lane = self.lane
            if lane is None:
                raise RuntimeError("The bot isn't trading any market yet")
            return lane
        key = symbol.strip().upper()
        lane = self.lanes.get(key) or next((ln for ln in self.lanes.values() if ln.contract and ln.contract.name == key), None)
        if lane is None:
            raise ValueError(f"The bot isn't trading {key} right now (trading: {', '.join(self.lanes)})")
        return lane

    def attach(self, core: TradingCore, symbol: str | None = None) -> Lane:
        """Trade an already built core as one of this account's markets (tests and tools)."""
        lane = Lane(self, lane_config(self.cfg, symbol or core.contract.root or core.contract.name), primary=not self.lanes)
        lane.core, lane.contract, lane.broker = core, core.contract, core.broker
        self.lanes[lane.symbol] = lane
        return lane

    def cores(self) -> list[TradingCore]:
        return [ln.core for ln in self.lanes.values() if ln.core is not None]

    # ------------------------------------------------------------------ setup

    def lane_broker(self, contract: Contract) -> Broker:
        """The broker one market trades through: the shared TopstepX connection, or a paper broker on the shared wallet."""
        if self.hub is not None:
            return self.hub.lane()
        assert self.wallet is not None
        return PaperBroker(contract, self.wallet.balance, slippage_ticks=self.cfg.risk.slippage_ticks,
                           fees_round_turn=fees_for(self.cfg, contract), account_name=self.label, live=True,
                           wallet=self.wallet)

    def guard_source(self):
        """An existing market's order guard: every market shares its order-rate breaker and daily entry count."""
        core = next(iter(self.cores()), None)
        return core.orders.guard if core else None

    async def prepare(self) -> TradingCore:
        cfg = self.cfg
        self.account = await select_account(self.client, cfg)
        if cfg.mode == "live" and not self.account.can_trade:
            raise SetupError(f"Account {self.account.name} is not allowed to trade right now (canTrade = false).")
        if cfg.mode == "live":
            from topstep_bot.risk.guards import api_trading_block

            blocked = api_trading_block(self.account)
            if blocked:
                raise SetupError(blocked)
        symbols = await self._choose_markets()
        if "DLL" in self.account.name.upper() and cfg.account.topstep_daily_loss_limit is None:
            cfg.account.topstep_daily_loss_limit = PLANS[cfg.account.plan].daily_loss_limit
            log.info("Account name indicates a Topstep Daily Loss Limit; enforcing $%.0f", cfg.account.topstep_daily_loss_limit)
        label = self.label = f"{self.account.name}" if cfg.mode == "live" else f"PAPER-{self.account.name}"
        if cfg.mode == "live":
            from topstep_bot.broker.projectx import ProjectXBroker

            self._shared_broker = ProjectXBroker(self.client, self.account.id, cfg.api.user_hub_url)
            self.hub = BrokerHub(self._shared_broker)
            acct = await self._shared_broker.get_account()
        else:
            paper_balance = self.journal.get_state("paper_balance", starting_balance(cfg))
            self.wallet = PaperWallet(paper_balance)
            acct = Account(1, label, self.wallet.balance, can_trade=True, simulated=True)
        plan = PLANS[cfg.account.plan]
        floor = self.journal.get_state(f"mll_floor:{label}")
        if cfg.account.mll_floor_override is not None:
            floor = cfg.account.mll_floor_override
        self.tracker = LossLimitTracker(starting_balance(cfg), plan.max_loss_limit, floor=floor)
        self.book = AccountBook(shared=True)

        # Today's closed trades so far, so a mid-day restart keeps the day's P&L, trade count,
        # losing streak and cooldown. Live: from TopstepX (includes anything traded by hand).
        # Paper: the simulated broker starts empty each run, so use the journal.
        from topstep_bot.sessions import SessionSchedule, market_session

        schedule = SessionSchedule(market_session(cfg.session, symbols[0]))
        today = schedule.trading_day(self.now())
        if cfg.mode == "live":
            realized, closed_today = await self._shared_broker.realized_pnl_since(session_open_for(today, schedule.tz))
        else:
            closed_today = self.journal.closed_trades(label, today)
            realized = sum(pnl for _, pnl in closed_today)
        if closed_today:
            log.info("Restoring today's %d closed trade(s), net $%.2f", len(closed_today), realized)
        last_eod = self.journal.get_state(f"last_eod:{label}")
        if last_eod is None or last_eod < today.isoformat():
            self.tracker.end_of_day(acct.balance - realized)  # yesterday's close balance
        self.book.start_day(today, acct.balance, realized, closed_today)
        self.book.best_prior_day = self._best_prior_day(label, today, starting_balance(cfg))
        self._legacy_position_file()

        for i, symbol in enumerate(symbols):
            try:
                await self._open_lane(symbol, primary=i == 0, balance=acct.balance, today=today)
            except Exception as exc:
                if i == 0 or unreachable(exc):
                    raise
                log.warning("Could not add %s: %s", symbol, exc)
        self.focus = symbols[0]
        self._report_damaged_journal()
        self._apply_ramp_up()
        return self.core

    async def _open_lane(self, symbol: str, *, primary: bool, balance: float, today: date) -> Lane:
        lane = Lane(self, lane_config(self.cfg, symbol), primary=primary)
        try:
            await lane.open(balance, today)
        except Exception:
            self._discard(lane)
            raise
        self.lanes[lane.symbol] = lane
        self._scale_guards()
        return lane

    def _scale_guards(self) -> None:
        """The order guard's daily entry limit is a safety net per market: with several markets the account's
        shared count gets one allowance per market (still far below anything like high-frequency trading)."""
        from topstep_bot.risk.guards import DEFAULT_MAX_ENTRIES_PER_DAY

        for core in self.cores():
            core.orders.guard.max_entries_per_day = DEFAULT_MAX_ENTRIES_PER_DAY * max(1, len(self.lanes))

    def _discard(self, lane: Lane) -> None:
        """Forget a market's broker view and its place in the account book."""
        if self.hub is not None and lane.broker is not None:
            self.hub.drop(lane.broker)
        if self.wallet is not None and lane.broker in self.wallet.brokers:
            self.wallet.brokers.remove(lane.broker)
        if self.book is not None and lane.core is not None:
            self.book.unregister(lane.core.risk.lane)

    def _legacy_position_file(self) -> None:
        """Before markets had their own hedge-guard files, the account had one: remove it so it can't block anything."""
        with contextlib.suppress(OSError):
            (self.cfg.data_path / "positions" / f"{self.cfg.active_account or 'main'}.json").unlink()

    async def _choose_markets(self) -> list[str]:
        """The markets to trade: today's switch, else the pick (auto_pick), plus instrument.markets."""
        from topstep_bot import market_pick as mp

        cfg = self.cfg
        today = mp.load_today(cfg, self.now())
        if today:  # you switched this account's market(s) for the rest of the trading day
            cfg.instrument.symbol, cfg.instrument.contract_id = today["symbol"], None
            self.market_today = today
            log.info("Trading %s today: switched by %s at %s", ", ".join(today["markets"]), today.get("source", "you"),
                     today.get("at"))
            return list(dict.fromkeys([today["symbol"], *today["markets"]]))[:MAX_MARKETS]
        if cfg.instrument.auto_pick:
            await self._pick_market()
        return mp.lane_symbols(cfg, cfg.instrument.symbol, self.market_pick)

    def _report_damaged_journal(self) -> None:
        """The journal holds the trailing MLL floor: if it was damaged, say so loudly and (live) pause new trades."""
        if self.journal_damaged is None or self.core is None:
            return
        core = self.core
        text = (f"The trade journal was damaged and has been set aside as {self.journal_damaged.name}; a new one was "
                f"started. The bot's record of the trailing Maximum Loss Limit is gone (it now assumes "
                f"${core.tracker.floor:,.2f}): compare it with the MLL on TopstepX and, if it differs, set "
                "account.mll_floor_override in config.yaml.")
        if self.cfg.mode == "live":
            core.risk.paused = True
            text += " New trades are paused until you resume them."
        core.event("critical", text, "error")

    def _best_prior_day(self, label: str, today: date, start: float) -> float:
        """Best finished day since the account started (or was last reset), for the Consistency Target."""
        best = 0.0
        for row in self.journal.daily(label):
            if row["trading_day"] >= today.isoformat():
                continue
            if abs(row["start_balance"] - start) < 0.01:  # a fresh start or a reset: earlier days don't count
                best = 0.0
            best = max(best, row["net_pnl"] or 0.0)
        return best

    async def _pick_market(self) -> None:
        """instrument.auto_pick: choose the market before anything uses the symbol (market_pick.py).

        A pick from the last few hours is reused, so a restart during the day doesn't pick again."""
        from topstep_bot import market_pick as mp
        from topstep_bot.memory import LongRunMemory, MarketLibrary

        cfg, now = self.cfg, self.now()
        pick = mp.load_pick(cfg)
        if mp.fresh(pick, cfg, now):
            log.info("Market pick from %s reused: %s", pick.at, pick.symbol)
        else:
            held = await self._held_market(set(mp.candidates_for(cfg)) | {cfg.instrument.symbol})
            tf = cfg.instrument.timeframe_minutes
            library = MarketLibrary(cfg.library_path)

            async def load(symbol: str):
                contract = await self.client.resolve_contract(symbol)
                try:
                    await LongRunMemory(library, None, symbol, tf).backfill(
                        self.client, contract, cfg.knowledge.history_days, now, live=cfg.data.live_market_data)
                except Exception as exc:  # noqa: BLE001 - score on the history already saved
                    log.info("Market pick: could not download %s history (%s); using what is saved", symbol, exc)
                return contract, library.bars(symbol, tf, now - timedelta(days=mp.PICK_DAYS), now)

            def progress(symbol: str) -> None:
                self._beat()  # scoring takes a while: the controller must not think the bot hung
                log.info("Market pick: scoring %s", symbol)

            try:
                pick = await mp.pick_market(cfg, load, now=now, held=held, progress=progress)
            finally:
                library.close()
            log.info("%s", pick.text())
        cfg.instrument.symbol = pick.symbol
        self.market_pick = pick

    async def _held_market(self, markets: set[str]) -> str | None:
        """The market of an open position on the account (live only: paper positions don't survive a restart)."""
        if self.cfg.mode != "live" or self.account is None:
            return None
        from topstep_bot.instruments import matches_root

        try:
            positions = await self.client.search_open_positions(self.account.id)
        except Exception as exc:  # noqa: BLE001
            log.warning("Market pick: could not check open positions (%s)", exc)
            return None
        for p in positions:
            name = p.contract_id.split(".")[3] if p.contract_id.count(".") >= 4 else p.contract_id
            for m in sorted(markets):
                if p.size and (name == m or matches_root(name, m)):
                    return m
        return None

    # ------------------------------------------------------- markets while running

    def _check_new_market(self, symbol: str) -> str:
        from topstep_bot import market_pick as mp
        from topstep_bot.instruments import SPECS
        from topstep_bot.risk.topstep import product_limit

        symbol = (symbol or "").strip().upper()
        if symbol not in SPECS:
            raise ValueError(f"Unknown market '{symbol}'. Markets: {', '.join(SPECS)}")
        if product_limit(symbol, PLANS[self.cfg.account.plan]) == 0:
            raise ValueError(f"Topstep doesn't allow {symbol} on a {self.cfg.account.plan} account right now.")
        if symbol in self.lanes:
            raise ValueError(f"The bot already trades {symbol}.")
        twin = next((s for s in self.lanes if mp.same_product(symbol, s)), None)
        if twin:
            raise ValueError(f"{symbol} and {twin} are the same product: trading both would double one bet. Switch "
                             f"{twin} to {symbol} instead.")
        if len([ln for ln in self.lanes.values() if not ln.closing]) >= MAX_MARKETS:
            raise ValueError(f"The bot trades at most {MAX_MARKETS} markets at once. Drop one first.")
        return symbol

    async def add_market(self, symbol: str, source: str = "dashboard") -> str:
        """Start trading another market alongside the others, now (no restart)."""
        async with self._lane_lock:
            symbol = self._check_new_market(symbol)
            core = self.core
            balance = core.balance if core else (self.wallet.balance if self.wallet else 0.0)
            today = core.current_day if core and core.current_day else self.book.day
            lane = await self._open_lane(symbol, primary=not self.lanes, balance=balance, today=today)
            await lane.start()
            self._save_markets(source)
        others = [s for s in self.lanes if s != symbol]
        text = (f"Now trading {symbol} ({_description(symbol)})"
                + (f" alongside {', '.join(others)}. Every Topstep limit counts all of them together." if others else "."))
        lane.core.event("info", f"{text} Requested by {source}.", "start")
        return text

    async def remove_market(self, symbol: str, source: str = "dashboard", flatten: bool = False) -> str:
        """Stop trading a market. A flat market goes at once; one with an open trade takes no new trades and goes
        once it is flat (or at once after closing it, with ``flatten``)."""
        async with self._lane_lock:
            lane = self.lane_for(symbol)
            others = [ln for ln in self.lanes.values() if ln is not lane and not ln.closing]
            if not others:
                raise ValueError(f"{lane.symbol} is the only market left: add or switch to another one first.")
            lane.closing = f"dropped by {source}"
            if lane.core:
                lane.core.closing = f"{lane.symbol} is being dropped ({lane.closing})"
            if lane.core and not lane.core.orders.is_flat:
                if flatten:
                    await lane.core._flatten(f"market dropped by {source}", self.now())
                text = (f"{lane.symbol} takes no new trades and is dropped as soon as its open trade is closed"
                        + (" (closing it now)." if flatten else " (its stop and target stay in place)."))
            else:
                await self._drop(lane)
                text = f"Stopped trading {lane.symbol}. Still trading {', '.join(self.lanes)}."
            self._save_markets(source)
        if self.core:
            self.core.event("info", f"{text} Requested by {source}.")
        return text

    async def switch_market(self, old: str | None, new: str, source: str = "dashboard") -> str:
        """Trade ``new`` instead of ``old`` (the focused market by default), without a restart."""
        from topstep_bot import market_pick as mp

        lane = self.lane_for(old)
        new = (new or "").strip().upper()
        if new == lane.symbol:
            return f"Already trading {new}."
        if mp.same_product(new, lane.symbol):
            # MNQ <-> NQ: one product, so the new one can only open once the old one is gone
            if lane.core and not lane.core.orders.is_flat:
                raise ValueError(f"{lane.symbol} has an open trade: close it first, then switch to {new}.")
            async with self._lane_lock:
                await self._drop(lane)
            text = await self.add_market(new, source)
            self.focus = new
            return text.replace("Now trading", f"Switched from {lane.symbol} to", 1)
        added = await self.add_market(new, source)
        self.focus = new
        if len(self.lanes) > 1:
            removed = await self.remove_market(lane.symbol, source)
            return f"{added}\n{removed}"
        return added

    def set_focus(self, symbol: str) -> str:
        lane = self.lane_for(symbol)
        self.focus = lane.symbol
        return f"Showing {lane.symbol}."

    async def _drop(self, lane: Lane) -> None:
        await lane.stop()
        self._discard(lane)
        self.lanes.pop(lane.symbol, None)
        if self.focus == lane.symbol:
            self.focus = next(iter(self.lanes), None)
        if lane.primary and self.lanes:  # the next market takes over what only the first one runs
            next(iter(self.lanes.values())).primary = True
        keeper = self.keeper
        for ln in self.lanes.values():
            if ln.core:
                ln.core.keeps_records = ln is keeper
        self._scale_guards()

    def _save_markets(self, source: str) -> None:
        """Remember today's markets, so a restart today trades the same ones."""
        from topstep_bot import market_pick as mp

        live = [s for s, ln in self.lanes.items() if not ln.closing]
        if not live:
            return
        try:
            self.market_today = mp.set_today(self.cfg, live[0], source, self.now(), markets=live)
        except OSError as exc:
            log.warning("Could not save today's markets: %s", exc)

    def markets_view(self) -> dict:
        from topstep_bot import market_pick as mp
        from topstep_bot.instruments import CLASSES, SPECS
        from topstep_bot.risk.topstep import product_limit

        plan = PLANS[self.cfg.account.plan]
        total = self.book.total() if self.book else None
        available = [s for s in SPECS if product_limit(s, plan) != 0 and s not in self.lanes
                     and not any(mp.same_product(s, x) for x in self.lanes)]
        return {
            "focus": self.lane.symbol if self.lane else None,
            "max": MAX_MARKETS,
            "lanes": [ln.summary() for ln in self.lanes.values()],
            "available": available,
            "names": {s: [SPECS[s].description, CLASSES.get(SPECS[s].asset_class, SPECS[s].asset_class)] for s in available},
            "exposure": None if total is None else {
                "open_pnl": round(total.open_pnl, 2), "at_risk": round(total.at_risk, 2),
                "minis": round(total.minis, 2), "contracts": total.contracts,
            },
        }

    def snapshot(self) -> dict:
        """The focused market's full picture, plus every market the account trades."""
        core = self.core
        snap = core.snapshot() if core else {}
        snap["markets"] = self.markets_view()
        return snap

    # ---------------------------------------------------------- the account's day

    async def roll_day(self, ts: datetime) -> None:
        """A new trading day for every market at once: the day's record first (once), then a fresh day for all."""
        if self._rolling or not self.lanes:
            return
        keeper = self.keeper
        day = keeper.core.schedule.trading_day(ts)
        if all(ln.core.current_day == day for ln in self.lanes.values() if ln.core):
            return
        self._rolling = True
        try:
            for ln in self.lanes.values():
                if ln.core:
                    ln.core.keeps_records = ln is keeper
            if keeper.core.current_day != day:
                await keeper.core.roll_own_day(ts)  # records the day, then starts the account's new one
            for ln in list(self.lanes.values()):
                if ln.core and ln.core.current_day != day:
                    await ln.core.roll_own_day(ts)  # no record (the keeper wrote it); a fresh day and network
        finally:
            self._rolling = False

    # -------------------------------------------------------------------- run

    async def run(self, on_ready: Callable[[TradingCore], Awaitable[None]] | None = None) -> None:
        asyncio.get_running_loop().set_exception_handler(asyncio_exception_handler)
        log.info("Preparing: connecting to TopstepX and loading history")
        self.notifier.start()
        core = await self._prepare_when_reachable()
        if core is None:  # stopped while waiting for TopstepX
            await self.notifier.stop()
            await self.client.close()
            self.journal.close()
            return
        if self._shared_broker is not None:
            await self._shared_broker.start()
            if hasattr(self._shared_broker, "stream"):
                self._shared_broker.stream.hub.on_connected(self._safe_reconcile)
        for lane in list(self.lanes.values()):
            await lane.start()

        self._tasks = [
            spawn(self._clock_loop(), name="clock"),
            spawn(self._reconcile_loop(), name="reconcile"),
            spawn(self._news_loop(), name="news"),
            spawn(self._learn_loop(), name="learning"),
        ]
        for task in self._tasks:
            task.add_done_callback(self._loop_ended)
        markets = ", ".join(ln.contract.name for ln in self.lanes.values() if ln.contract)
        core.event(
            "info",
            f"Bot started in {self.cfg.mode.upper()} mode: {core.strategy.title} on {markets} "
            f"({self.cfg.instrument.timeframe_minutes}m), account {core.account_label}",
            None if self.quiet_start else "start",
        )
        try:
            if on_ready:
                await on_ready(core)
            await self.controls.stop.wait()
        except asyncio.CancelledError:
            self.controls.stop_reason = self.controls.stop_reason or "interrupted (Ctrl+C or window closed)"
            raise
        except Exception:
            self.controls.stop_reason = self.controls.stop_reason or "crashed - see the error above"
            log.critical("Bot crashed", exc_info=True)
            raise
        finally:
            log.warning("Shutting down. Reason: %s", self.controls.stop_reason or "unknown")
            await self.shutdown()

    async def _prepare_when_reachable(self) -> TradingCore | None:
        """prepare(), retried while TopstepX can't be reached (internet down, TopstepX maintenance), so a
        start during an outage waits for it instead of crashing until the controller gives up."""
        delay, told = PREPARE_RETRY_FIRST, False
        while True:
            self.started_at = self.now()
            try:
                return await self.prepare()
            except Exception as exc:  # noqa: BLE001 - only connection problems are retried
                if not unreachable(exc):
                    raise
                problem = exc
                for lane in list(self.lanes.values()):  # start clean on the next try
                    self._discard(lane)
                self.lanes.clear()
            text = f"TopstepX can't be reached ({problem}); the bot keeps trying and starts as soon as it answers."
            log.warning("%s Next try in %ds.", text, delay)
            if not told:
                told = True
                self.notifier.notify("error", text)
            waited = 0
            while waited < delay:  # wait in short steps: keep the heartbeat fresh and notice a stop at once
                self._beat()
                try:
                    await asyncio.wait_for(self.controls.stop.wait(), timeout=min(10, delay - waited))
                    return None
                except TimeoutError:
                    waited += 10
            delay = min(delay * 2, PREPARE_RETRY_MAX)

    async def shutdown(self) -> None:
        core = self.core
        if self.cfg.execution.flatten_on_shutdown:
            open_cores = [c for c in self.cores() if not c.orders.is_flat]
            for c in open_cores:
                c.event("warning", "Shutting down: flattening open position"
                        + (f" in {c.contract.name}" if len(self.lanes) > 1 else ""))
                try:
                    await c.orders.flatten_all("bot shutdown")
                except Exception as exc:  # noqa: BLE001
                    c.event("critical", f"Could not flatten on shutdown: {exc} - CHECK YOUR ACCOUNT")
            if open_cores:
                await asyncio.sleep(2)
        for task in self._tasks:
            task.cancel()
        for lane in list(self.lanes.values()):
            await lane.stop()
        if self._shared_broker:
            await self._shared_broker.stop()
        if core:
            self._save_paper_balance()
            if self.controls.restart_requested:
                core.event("info", "Restarting for daily maintenance")
            else:
                core.event("info", "Bot stopped", "stop")
        await self.notifier.stop()
        await self.client.close()
        self.journal.close()

    def _loop_ended(self, task: asyncio.Task) -> None:
        """A background loop must run until shutdown. If one dies, stop (and flatten) rather than keep
        running half-blind; the controller then restarts the bot."""
        if task.cancelled() or self.controls.stop.is_set():
            return
        exc = task.exception()
        reason = f"the {task.get_name()} loop " + (f"crashed ({exc!r})" if exc else "ended unexpectedly")
        # spawn() has already logged the traceback; record the decision and stop.
        if self.core:
            self.core.event("critical", f"Internal error: {reason}. Stopping the bot (it flattens first).", "error")
        else:
            log.critical("Internal error: %s. Stopping the bot.", reason)
        self.controls.failure = reason
        self.controls.request_stop(f"internal error: {reason}")

    # ------------------------------------------------------------------ loops

    async def _clock_loop(self) -> None:
        last_balance_check = self.now()
        while True:
            await asyncio.sleep(1)
            now = self.now()
            try:
                for lane in list(self.lanes.values()):
                    await self._lane_tick(lane, now)
                if self.controls.flatten_requested:
                    self.controls.flatten_requested = False
                    for c in self.cores():
                        await c.halt(self.controls.flatten_reason)
                if KILL_FILE.exists() and any(not c.halted for c in self.cores()):
                    log.warning("KILL file found at %s", KILL_FILE.resolve())
                    for c in self.cores():
                        await c.halt("KILL file found")
                if now - last_balance_check > timedelta(seconds=60):
                    last_balance_check = now
                    if self.cfg.mode == "live" and self._shared_broker is not None:
                        balance = (await self._shared_broker.get_account()).balance
                        for c in self.cores():
                            c.balance = balance
                    self._save_paper_balance()
                    self._apply_ramp_up()
                if now.second % 10 == 0:
                    self._beat()
                self._maybe_check_in(now)
                self._maybe_daily_restart(now)
                await self._drop_closed_markets()
            except Exception:  # noqa: BLE001 - keep the clock alive
                log.exception("Clock loop error")

    async def _lane_tick(self, lane: Lane, now: datetime) -> None:
        core = lane.core
        if core is None:
            return
        try:
            await core.on_clock(now)
            if lane.price_stale(now) and not core.orders.is_flat and now.second % 10 == 0:
                await lane.poll_price()
            lane.sync_positions()
        except Exception:  # noqa: BLE001 - one market's problem must not stop the others' clocks
            log.exception("Clock error (%s)", lane.symbol)

    async def _drop_closed_markets(self) -> None:
        for lane in [ln for ln in self.lanes.values() if ln.closing and ln.core and ln.core.orders.is_flat]:
            async with self._lane_lock:
                if lane.symbol in self.lanes and len(self.lanes) > 1:
                    await self._drop(lane)
                    self.core.event("info", f"Stopped trading {lane.symbol} ({lane.closing}); its trade is closed.")

    async def _reconcile_loop(self) -> None:
        while True:
            await asyncio.sleep(self.cfg.execution.reconcile_interval_seconds)
            await self._safe_reconcile()

    async def _safe_reconcile(self) -> None:
        for lane in list(self.lanes.values()):
            await lane.reconcile()

    async def _news_loop(self) -> None:
        """Refresh the calendars every 6 hours and the headlines every few minutes."""
        last_calendar = self.now()
        while True:
            refresh = self.now() - last_calendar > timedelta(hours=6)
            if refresh:
                last_calendar = self.now()
            for lane in list(self.lanes.values()):
                try:
                    await lane.news_step(refresh)
                except Exception:  # noqa: BLE001 - news is a bonus; trading goes on
                    log.exception("News refresh failed (%s)", lane.symbol)
            await asyncio.sleep(60)

    async def _learn_loop(self) -> None:
        """Outside the bot's entry window and while flat, each market retrains once each session has finished
        and grows its long-run memory daily."""
        while True:
            await asyncio.sleep(LEARN_CHECK_SECONDS)
            for lane in list(self.lanes.values()):
                await lane.learn_step(self.now())
            await self._grow_step(self.now())

    # ------------------------------------------------------------- learning

    async def retrain(self, source: str) -> str:
        """Rebuild the knowledge base of every market the account trades."""
        messages = [await lane.retrain(source) for lane in list(self.lanes.values())]
        if len(messages) == 1:
            return messages[0]
        return "\n".join(f"{s}: {m}" for s, m in zip(self.lanes, messages, strict=False))

    async def _grow_step(self, now: datetime) -> None:
        """Once a day, outside the entry window and while every market is flat: learn a few related markets from
        the price history saved on this PC, so the knowledge network keeps growing (knowledge_network.py)."""
        p = self.cfg.probability
        if not (p.enabled and p.network and p.grow_network and p.grow_per_day) or self._growing:
            return
        if self._network_after and now < self._network_after:
            return
        lanes = list(self.lanes.values())
        if not lanes or any(ln.core is None or not ln.core.orders.is_flat or ln._training
                            or ln.core.schedule.entry_block_reason(now) is None for ln in lanes):
            return  # trading never waits on it
        self._network_after = now + timedelta(hours=2)  # if it fails: try again later
        try:
            await self.grow_network("daily")
        except Exception as exc:  # noqa: BLE001 - keep trading; the network is a bonus
            log.warning("Could not grow the knowledge network: %s", exc)
            return
        from topstep_bot.knowledge_network import STALE

        self._network_after = now + STALE

    async def grow_network(self, source: str, limit: int | None = None) -> str:
        """Learn related markets from the saved price history now (nothing is traded)."""
        from topstep_bot import knowledge_network

        if self._growing:
            raise RuntimeError("The bot is already learning related markets")
        self._growing = True
        try:
            learned = await knowledge_network.grow(self.cfg, list(self.lanes), self.now(), limit=limit,
                                                   progress=lambda s: (self._beat(), log.info("Knowledge network: learning %s", s)))
        finally:
            self._growing = False
        for core in self.cores():
            core.probability.refresh_network(force=True)
        if not learned:
            text = ("No related market to learn right now: each one either has fresh knowledge already or too little "
                    f"saved history (at least {knowledge_network.MIN_DAYS} trading days). Download history on the "
                    "Knowledge tab (Simulations) to give it more.")
        else:
            text = ("Knowledge network: learned " + ", ".join(
                f"{x['symbol']} ({x['observations']:,} outcomes over {x['days']} days)" for x in learned)
                + f" from saved history ({source}). Nothing was traded; these now inform related markets' odds.")
        if self.core:
            self.core.event("info", text, "learn")
        return text

    async def learn(self, source: str) -> str:
        """Replay the long-run memory of the focused market (the one shown on the dashboard)."""
        lane = self.lane
        if lane is None:
            raise RuntimeError("The bot isn't trading any market yet")
        return await lane.learn(source)

    # ----------------------------------------------------------- 24/7 helpers

    def _day_result(self, day: date) -> tuple[float | None, int | None]:
        """The bot's own result on a day (for the market journal)."""
        row = next((r for r in self.journal.daily(self.label) if r["trading_day"] == day.isoformat()), None)
        return (row["net_pnl"], row["trades"]) if row else (None, None)

    def _apply_ramp_up(self) -> None:
        """The first live trading days on an account run at reduced risk (replaces a paper-trading period).

        Only days on which the bot actually closed a trade count, so days spent idle (holidays, news,
        no signal) don't use up the ramp-up.
        """
        core = self.core
        rcfg = self.cfg.risk
        if core is None or self.cfg.mode != "live" or rcfg.ramp_up_days <= 0:
            return
        today = core.schedule.trading_day(self.now()).isoformat()
        completed = len([d for d in self.journal.trading_days(core.account_label) if d < today])
        scale = rcfg.ramp_up_risk_fraction if completed < rcfg.ramp_up_days else 1.0
        if scale != core.risk.risk_scale:
            core.risk.risk_scale = scale  # kept in the account book: every market
            if scale < 1:
                core.event(
                    "info",
                    f"Ramp-up: live day {completed + 1} of {rcfg.ramp_up_days} on this account - risking "
                    f"${rcfg.risk_per_trade * scale:,.0f} per trade instead of ${rcfg.risk_per_trade:,.0f}",
                )
            else:
                core.event("info", "Ramp-up complete: trading at full configured risk")

    def _beat(self) -> None:
        """Tell the controller we're alive (it restarts the bot if this goes stale)."""
        if self.heartbeat_path:
            try:
                self.heartbeat_path.write_text(str(_time.time()), encoding="utf-8")
            except OSError as exc:
                log.debug("heartbeat write failed: %s", exc)

    def _save_paper_balance(self) -> None:
        if self.wallet is not None and all(b.position == 0 for b in self.wallet.brokers):
            self.journal.set_state("paper_balance", self.wallet.balance)

    def _maybe_check_in(self, now: datetime) -> None:
        """Once each weekday morning, send a short 'still running' message."""
        core = self.core
        check_in = self.cfg.service.check_in_time
        if core is None or check_in is None:
            return
        local = core.schedule.local(now)
        if local.weekday() >= 5 or local.time() < check_in or self._checked_in == local.date():
            return
        self._checked_in = local.date()
        if local.time() > (datetime.combine(local.date(), check_in) + timedelta(minutes=5)).time():
            return  # started later in the day: skip
        r = core.risk
        markets = ", ".join(ln.contract.name for ln in self.lanes.values() if ln.contract) or core.contract.name
        core.event(
            "info",
            f"Good morning: bot running in {self.cfg.mode.upper()} mode on {markets}. "
            f"Balance ${core.balance:,.2f}, MLL room ${core.tracker.room(core.balance):,.2f}"
            + (". Today is a no-trade day." if not core.schedule.is_trade_day(core.schedule.trading_day(now)) else "")
            + (" PAUSED." if r.paused else ""),
            "daily_summary",
        )

    def _maybe_daily_restart(self, now: datetime) -> None:
        """Under the 24/7 service, restart once a day in the CME halt (picks up contract rolls)."""
        core = self.core
        restart_at = self.cfg.service.daily_restart_time
        if not self.supervised or restart_at is None or core is None or self.controls.stop.is_set():
            return
        local = core.schedule.local(now)
        due = datetime.combine(local.date(), restart_at, tzinfo=core.schedule.tz)
        if self.started_at < due <= now and all(c.orders.is_flat for c in self.cores()):
            self.controls.restart_requested = True
            self.controls.request_stop("daily maintenance restart")
