"""Trade lifecycle management: entry -> protective stop + target (OCO) -> exit.

Safety rules enforced here:
  * Every filled entry immediately gets a protective stop. If the stop cannot be placed,
    the position is flattened.
  * When the stop or target fills, the other is cancelled (one-cancels-other).
  * The broker's real state is periodically reconciled against ours: missing stops are
    re-placed, unexpected positions are handled per ``orphan_position_policy``.
  * One trade at a time per instrument (no pyramiding, no hedging).
"""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import TYPE_CHECKING

from topstep_bot.broker.base import Broker
from topstep_bot.models import Contract, Fill, Order, OrderSide, OrderStatus, OrderType, Position

if TYPE_CHECKING:
    from topstep_bot.risk.guards import OrderGuard

log = logging.getLogger(__name__)

MAX_STOP_FAILURES = 2
EXIT_RETRY_AFTER = timedelta(seconds=10)
# A close asked for by a position report or by reconcile is not sent again within this time of the last
# one: the report often arrives milliseconds after a close went out, and a second close while the first
# is still unfilled could reverse the position.
CLOSE_DEBOUNCE = timedelta(seconds=1)
_OWN_TAG = re.compile(r"^(tsb[0-9a-f]{10})-(S\d+|T)$")
_ANY_OWN_TAG = re.compile(r"^(tsb[0-9a-f]{10})-")


def may_have_reached_broker(exc: BaseException) -> bool:
    """Whether a failed order request may still have been carried out: a network error, a timeout or a
    server error. A refusal (the broker answered no, or the order itself was invalid) means it was not."""
    if isinstance(exc, ValueError):
        return False
    payload = getattr(exc, "payload", None)
    if isinstance(payload, dict):  # ProjectXError: TopstepX answered
        return payload.get("success") is None and (getattr(exc, "code", None) or 0) >= 500
    return True


class TradeState(str, Enum):
    PENDING = "pending"
    OPEN = "open"
    EXITING = "exiting"
    CLOSED = "closed"


@dataclass
class ManagedTrade:
    tag: str
    side: OrderSide
    size: int
    stop_price: float
    target_price: float | None
    reason: str
    created_at: datetime
    strategy: str = ""
    state: TradeState = TradeState.PENDING
    entry_order_id: int | None = None
    entry_price: float | None = None
    filled_size: int = 0
    opened_at: datetime | None = None
    initial_stop: float = 0.0
    stop_order_id: int | None = None
    target_order_id: int | None = None
    stop_seq: int = 0
    stop_failures: int = 0
    exit_requested: bool = False
    planned_risk: float | None = None  # dollars at risk when the trade was sized
    exit_sent_at: datetime | None = None
    # What closed the position, per order id: the fills reported for it (each execution counted once) and
    # the filled quantity its order update reported. The same close can be reported both ways, so per
    # order the larger of the two counts, never their sum.
    exit_fills: dict[int, list[tuple[float, int]]] = field(default_factory=dict)
    exit_fill_ids: set[int] = field(default_factory=set)
    exit_reports: dict[int, tuple[float, int]] = field(default_factory=dict)
    # Closes sent because a stop or target filled (one per order is enough), and when the last close went out.
    close_sent_for: set[int] = field(default_factory=set)
    last_close_at: datetime | None = None
    exit_price: float | None = None
    exit_reason: str = ""
    closed_at: datetime | None = None
    gross_pnl: float = 0.0
    fees: float = 0.0
    breakeven_done: bool = False
    # What the bot learns from (knowledge base, journal): the price it expected each fill at, the best
    # and worst price reached while open, and the market snapshot when the trade was opened.
    ref_price: float | None = None  # price when the entry was sent
    exit_ref: float | None = None  # stop / target price for those exits, last price for market exits
    best_price: float | None = None
    worst_price: float | None = None
    context: dict = field(default_factory=dict)
    odds: dict | None = None  # the probability calculator's estimate when it was entered (dashboard only)

    def __post_init__(self) -> None:
        if not self.initial_stop:
            self.initial_stop = self.stop_price

    @property
    def net_pnl(self) -> float:
        return self.gross_pnl - self.fees

    @property
    def signed_size(self) -> int:
        return self.filled_size * self.side.sign

    def _exit_parts(self) -> list[tuple[float, int]]:
        parts = []
        for oid in set(self.exit_fills) | set(self.exit_reports):
            fills = self.exit_fills.get(oid, [])
            report = self.exit_reports.get(oid)
            if report is not None and report[1] > sum(q for _, q in fills):
                parts.append(report)
            else:
                parts.extend(fills)
        return parts

    @property
    def exited_size(self) -> int:
        """Contracts reported closed by our stop, our target or a closing order."""
        return sum(q for _, q in self._exit_parts())

    def note_exit(self, order_id: int, price: float, qty: int, fill_id: int | None = None) -> bool:
        """Record a closing execution (``fill_id`` set) or an order's reported fill. False if already counted."""
        if fill_id is not None:
            if fill_id in self.exit_fill_ids:
                return False  # the same execution reported twice (e.g. resent after a reconnect)
            self.exit_fill_ids.add(fill_id)
            self.exit_fills.setdefault(order_id, []).append((price, qty))
            return True
        old = self.exit_reports.get(order_id)
        if old is not None and old[1] >= qty:
            return False
        self.exit_reports[order_id] = (price, qty)
        return True

    def average_exit(self) -> float | None:
        parts = [(p, q) for p, q in self._exit_parts() if q > 0]
        qty = sum(q for _, q in parts)
        return sum(p * q for p, q in parts) / qty if qty else None

    @property
    def risk_points(self) -> float:
        return abs((self.entry_price or 0.0) - self.initial_stop)

    def r_multiple(self) -> float | None:
        if not self.entry_price or self.exit_price is None or self.risk_points == 0:
            return None
        return (self.exit_price - self.entry_price) * self.side.sign / self.risk_points

    def note_prices(self, high: float, low: float) -> None:
        """Track the best and worst price reached while the position is open."""
        if self.entry_price is None or self.state not in (TradeState.OPEN, TradeState.EXITING):
            return
        best, worst = (high, low) if self.side == OrderSide.BUY else (low, high)
        better = max if self.side == OrderSide.BUY else min
        worse = min if self.side == OrderSide.BUY else max
        self.best_price = best if self.best_price is None else better(self.best_price, best)
        self.worst_price = worst if self.worst_price is None else worse(self.worst_price, worst)

    def excursion_r(self) -> tuple[float | None, float | None]:
        """(MFE, MAE) in R: how far the trade went for (>= 0) and against (<= 0) it while open."""
        if not self.entry_price or self.risk_points == 0:
            return None, None
        prices = [p for p in (self.best_price, self.worst_price, self.exit_price) if p is not None]
        if not prices:
            return None, None
        moves = [(p - self.entry_price) * self.side.sign / self.risk_points for p in prices]
        return round(max(0.0, *moves), 2), round(min(0.0, *moves), 2)

    def slippage_ticks(self, tick_size: float) -> tuple[float | None, float | None]:
        """(entry, exit) slippage in ticks against the expected price; positive = a worse fill."""
        def ticks(expected: float | None, actual: float | None, sign: int) -> float | None:
            if expected is None or actual is None or tick_size <= 0:
                return None
            return round((actual - expected) * sign / tick_size, 2)

        return (ticks(self.ref_price, self.entry_price, self.side.sign),
                ticks(self.exit_ref, self.exit_price, -self.side.sign))

    def to_dict(self) -> dict:
        return {
            "tag": self.tag,
            "side": self.side.label,
            "size": self.filled_size or self.size,
            "state": self.state.value,
            "entry_price": self.entry_price,
            "stop_price": self.stop_price,
            "target_price": self.target_price,
            "exit_price": self.exit_price,
            "reason": self.reason,
            "strategy": self.strategy,
            "planned_risk": None if self.planned_risk is None else round(self.planned_risk, 2),
            "exit_reason": self.exit_reason,
            "opened_at": self.opened_at.isoformat() if self.opened_at else None,
            "closed_at": self.closed_at.isoformat() if self.closed_at else None,
            "net_pnl": round(self.net_pnl, 2),
            "r": round(self.r_multiple(), 2) if self.r_multiple() is not None else None,
        }


class OrderManager:
    def __init__(
        self,
        broker: Broker,
        contract: Contract,
        *,
        fees_round_turn: float,
        clock: Callable[[], datetime],
        use_native_brackets: bool = False,
        orphan_policy: str = "flatten",
        entry_timeout: float = 20.0,
        strategy_name: str = "",
        max_risk_overrun: float = 1.5,
    ):
        self.broker = broker
        self.contract = contract
        self.fees_round_turn = fees_round_turn
        self.clock = clock
        self.use_native_brackets = use_native_brackets
        self.orphan_policy = orphan_policy
        self.entry_timeout = timedelta(seconds=entry_timeout)
        self.strategy_name = strategy_name
        self.max_risk_overrun = max_risk_overrun
        self.trade: ManagedTrade | None = None
        self.last_trade: ManagedTrade | None = None
        self.position = 0
        self.position_avg = 0.0
        self.last_price: float | None = None
        self.on_trade_closed: Callable[[ManagedTrade], Awaitable[None]] | None = None
        self.on_event: Callable[[str, str], None] | None = None
        # Last-line Topstep guard (position cap, order-rate breaker); set by the factory.
        self.guard: OrderGuard | None = None
        self._lock = asyncio.Lock()
        # Orders a cancel request may not have reached (network error): until reconcile sees they are no
        # longer working, no new entry is sent, so an old stop or target can't fill into the next trade.
        self._leftovers: set[int] = set()
        # Tags of stops/targets whose placement may have reached TopstepX although no answer came back: the
        # order may be working without the bot knowing its id. No new entry until reconcile has listed the
        # orders (and adopted or cancelled any such order), or it could outlive its trade and fill later.
        self._unconfirmed_tags: set[str] = set()
        # Bumped on every change to our trade/orders/position. reconcile() fetches the broker's view
        # without holding the lock, and discards that view if anything changed while it was fetching
        # (otherwise a fill arriving mid-fetch looks like "position closed" or "stop missing").
        self._version = 0
        broker.on_order = self.handle_order
        broker.on_position = self.handle_position
        broker.on_fill = self.handle_fill

    # ---------------------------------------------------------------- helpers

    def _event(self, level: str, message: str) -> None:
        getattr(log, "critical" if level == "critical" else level, log.info)(message)
        if self.on_event:
            try:
                self.on_event(level, message)
            except Exception:  # noqa: BLE001 - reporting (journal, alerts) must never stop a stop or a flatten
                log.exception("Could not record order event")

    def _count_action(self) -> None:
        """Every order action goes through the rate breaker (it never blocks exits, only reports)."""
        if self.guard is not None:
            tripped = self.guard.record_action(self.clock())
            if tripped:
                self._event("critical", f"ORDER GUARD: {tripped}")

    @property
    def is_flat(self) -> bool:
        return self.trade is None and self.position == 0

    def open_pnl(self, price: float | None = None) -> float:
        price = price if price is not None else self.last_price
        if price is None or self.position == 0:
            return 0.0
        avg = self.position_avg
        if self.trade and self.trade.entry_price:
            avg = self.trade.entry_price
        return self.contract.pnl(avg, price, self.position)

    def _role(self, order: Order, t: ManagedTrade) -> str | None:
        tag = order.custom_tag or ""
        if order.id == t.entry_order_id or tag == f"{t.tag}-E":
            return "entry"
        if order.id == t.stop_order_id or tag.startswith(f"{t.tag}-S"):
            return "stop"
        if order.id == t.target_order_id or tag == f"{t.tag}-T":
            return "target"
        return None

    async def _safe_cancel(self, order_id: int | None) -> None:
        if order_id is None:
            return
        try:
            self._count_action()
            await self.broker.cancel_order(order_id)
        except Exception as exc:  # noqa: BLE001 - already filled/cancelled is fine
            if may_have_reached_broker(exc):
                self._leftovers.add(order_id)  # it may still be working: reconcile checks and cancels again
                self._event("warning", f"Could not cancel order {order_id} ({exc}); will check again")
            else:
                log.debug("cancel %s ignored: %s", order_id, exc)
        else:
            self._leftovers.discard(order_id)

    # ------------------------------------------------------------------ entry

    async def enter(
        self,
        side: OrderSide,
        size: int,
        stop_price: float,
        target_price: float | None,
        reason: str,
        ref_price: float,
        limit_price: float | None = None,
        planned_risk: float | None = None,
        strategy: str | None = None,
    ) -> ManagedTrade | None:
        """Open a trade. With ``limit_price`` the entry can't fill worse than that price."""
        async with self._lock:
            self._version += 1
            if self.trade is not None or self.position != 0:
                log.info("Entry ignored: already in a trade")
                return None
            if self._leftovers:
                self._event("warning", f"Entry skipped: earlier order(s) {sorted(self._leftovers)} may still be working "
                                       "(a cancel didn't get through); waiting until they are confirmed cancelled")
                return None
            if self._unconfirmed_tags:
                self._event("warning", "Entry skipped: an earlier stop or target may have reached TopstepX without an "
                                       "answer; waiting until the next check of the open orders")
                return None
            if self.guard is not None:
                refused = self.guard.check_entry(size, self.position, self.clock())
                if refused:
                    self._event("error", f"Entry refused: {refused}")
                    return None
                self.guard.record_entry(self.clock())
            t = ManagedTrade(
                tag=f"tsb{uuid.uuid4().hex[:10]}",
                side=side,
                size=size,
                stop_price=stop_price,
                target_price=target_price,
                reason=reason,
                created_at=self.clock(),
                strategy=strategy or self.strategy_name,
                planned_risk=planned_risk,
                ref_price=ref_price,
            )
            self.trade = t
            sl_ticks = tp_ticks = None
            if self.use_native_brackets:
                sl_ticks = max(1, round(self.contract.ticks(ref_price - stop_price)))
                if target_price is not None:
                    tp_ticks = max(1, round(self.contract.ticks(target_price - ref_price)))
            try:
                self._count_action()
                order_id = await self.broker.place_order(
                    self.contract.id,
                    OrderType.LIMIT if limit_price is not None else OrderType.MARKET,
                    side,
                    size,
                    limit_price=limit_price,
                    ioc=limit_price is not None,
                    tag=f"{t.tag}-E",
                    stop_loss_ticks=sl_ticks,
                    take_profit_ticks=tp_ticks,
                )
            except Exception as exc:  # noqa: BLE001
                if not may_have_reached_broker(exc):
                    self.trade = None
                    self._event("error", f"Entry order failed: {exc}")
                    return None
                # A timeout or server error: TopstepX may have accepted (and filled) the order anyway.
                # Forgetting it would leave a position with no stop and let the next entry double it,
                # so the trade stays pending until TopstepX shows a fill (it is then protected as usual)
                # or reconcile finds nothing and abandons it after the entry timeout.
                self._event("warning", f"Entry order may not have gone through ({exc}) - checking with the broker "
                                       "before doing anything else")
                return t
            if t.entry_order_id is None:
                t.entry_order_id = order_id
            self._event(
                "info",
                f"ENTRY {side.label} {size} {self.contract.name} @ "
                + (f"LMT {limit_price}" if limit_price is not None else "MKT")
                + f" | stop {stop_price}"
                + (f" target {target_price}" if target_price is not None else "")
                + f" | {reason}",
            )
            return t

    async def _entry_filled(self, t: ManagedTrade, price: float, qty: int) -> None:
        if qty > t.size:
            self._event("critical", f"Entry filled {qty} contracts but only {t.size} were ordered - an earlier order "
                                    "may have filled into it; CHECK YOUR ACCOUNT")
        t.entry_price = price
        t.filled_size = qty
        t.state = TradeState.OPEN
        t.opened_at = self.clock()
        self._event("info", f"FILLED {t.side.label} {qty} @ {price}")
        if t.exit_requested:
            await self._exit_locked(t, t.exit_reason or "exit requested before fill")
            return
        stop_wrong_side = t.stop_price >= price if t.side == OrderSide.BUY else t.stop_price <= price
        if stop_wrong_side:
            self._event("warning", "Fill price is already beyond the stop - exiting immediately")
            await self._exit_locked(t, "filled beyond stop")
            return
        if self.use_native_brackets:
            return  # server-side brackets; their order ids are discovered by reconcile()
        await self._place_stop(t)
        actual_risk = qty * (self.contract.ticks(price - t.stop_price) * self.contract.tick_value + self.fees_round_turn)
        if t.planned_risk and actual_risk > t.planned_risk * self.max_risk_overrun and t.state == TradeState.OPEN:
            self._event(
                "warning",
                f"Fill at {price} put ${actual_risk:,.0f} at risk vs ${t.planned_risk:,.0f} planned - exiting immediately",
            )
            await self._exit_locked(t, "fill too far from the signal price")
            return
        if t.target_price is not None and t.state == TradeState.OPEN:
            await self._place_target(t)

    async def cancel_unfilled_entry(self, reason: str) -> bool:
        """Cancel an entry that hasn't filled (e.g. its limit price was never reached)."""
        async with self._lock:
            self._version += 1
            t = self.trade
            if t is None or t.state != TradeState.PENDING or t.entry_order_id is None:
                return False
            self._event("info", f"Entry not filled - cancelled ({reason})")
            await self._safe_cancel(t.entry_order_id)
            return True

    async def _place_stop(self, t: ManagedTrade) -> None:
        if t.stop_failures >= MAX_STOP_FAILURES:
            self._event("critical", "Protective stop keeps failing - flattening")
            await self._exit_locked(t, "protective stop failed repeatedly")
            return
        t.stop_seq += 1
        try:
            self._count_action()
            t.stop_order_id = await self.broker.place_order(
                self.contract.id,
                OrderType.STOP,
                t.side.opposite,
                t.filled_size,
                stop_price=t.stop_price,
                tag=f"{t.tag}-S{t.stop_seq}",
            )
        except Exception as exc:  # noqa: BLE001
            t.stop_failures += 1
            if may_have_reached_broker(exc):
                self._unconfirmed_tags.add(f"{t.tag}-S{t.stop_seq}")  # reconcile adopts it if it is working
            self._event("critical", f"FAILED to place protective stop ({exc}) - flattening")
            await self._exit_locked(t, "could not place protective stop")

    async def _place_target(self, t: ManagedTrade) -> None:
        try:
            self._count_action()
            t.target_order_id = await self.broker.place_order(
                self.contract.id,
                OrderType.LIMIT,
                t.side.opposite,
                t.filled_size,
                limit_price=t.target_price,
                tag=f"{t.tag}-T",
            )
        except Exception as exc:  # noqa: BLE001 - the stop still protects the position
            if may_have_reached_broker(exc):
                self._unconfirmed_tags.add(f"{t.tag}-T")  # reconcile adopts it if it is working
            self._event("warning", f"Could not place profit target ({exc}); stop remains active")

    # ------------------------------------------------------------- broker events

    def _is_close(self, order: Order, t: ManagedTrade) -> bool:
        """An untagged market order against the trade: the bot's close (or a close by hand)."""
        return (not _ANY_OWN_TAG.match(order.custom_tag or "") and order.type == OrderType.MARKET
                and order.side == t.side.opposite and order.id != t.entry_order_id)

    async def handle_order(self, order: Order) -> None:
        if order.contract_id != self.contract.id:
            return
        async with self._lock:
            self._version += 1
            t = self.trade
            if t is None:
                return
            role = self._role(order, t)
            if role == "entry":
                t.entry_order_id = order.id
                if t.state != TradeState.PENDING:
                    return
                if order.status == OrderStatus.FILLED:
                    await self._entry_filled(t, order.filled_price or self.last_price or 0.0, order.fill_volume or order.size)
                elif order.status in (OrderStatus.CANCELLED, OrderStatus.EXPIRED, OrderStatus.REJECTED):
                    if order.fill_volume:
                        await self._entry_filled(t, order.filled_price or 0.0, order.fill_volume)
                    else:
                        self.trade = None
                        self._event("warning", f"Entry order {order.status.name.lower()} - no position opened")
            elif role in ("stop", "target"):
                if order.status == OrderStatus.FILLED and t.state in (TradeState.OPEN, TradeState.EXITING):
                    t.note_exit(order.id, order.filled_price or self.last_price or 0.0, order.fill_volume or order.size)
                    await self._exit_order_filled(t, order.id, role)
                elif order.status.is_working:
                    await self._adopt(t, order)
                elif (
                    role == "stop"
                    and order.id == t.stop_order_id
                    and t.state == TradeState.OPEN
                    and order.status in (OrderStatus.CANCELLED, OrderStatus.EXPIRED, OrderStatus.REJECTED)
                ):
                    t.stop_order_id = None
                    t.stop_failures += 1
                    self._event("warning", f"Protective stop was {order.status.name.lower()} - re-placing")
                    await self._place_stop(t)
            elif (order.status == OrderStatus.FILLED and t.state in (TradeState.OPEN, TradeState.EXITING)
                  and self._is_close(order, t)):
                if t.note_exit(order.id, order.filled_price or self.last_price or 0.0, order.fill_volume or order.size):
                    await self._closing_fill(t)

    async def _adopt(self, t: ManagedTrade, order: Order) -> None:
        """A working stop or target of this trade whose id the bot never got (its placement timed out after
        TopstepX carried it out): record it, so it is cancelled and moved like any other. A second working
        stop is cancelled - two would double the exit."""
        if t.state == TradeState.CLOSED or order.id in (t.stop_order_id, t.target_order_id):
            return
        tag = order.custom_tag or ""
        if tag == f"{t.tag}-T":
            if t.target_order_id is None:
                t.target_order_id = order.id
                self._event("info", f"Profit target {order.id} found at TopstepX - managing it")
                return
        elif tag.startswith(f"{t.tag}-S"):
            if t.stop_order_id is None:
                t.stop_order_id = order.id
                if order.stop_price is not None:
                    t.stop_price = order.stop_price
                self._event("info", f"Protective stop {order.id} found at TopstepX - managing it")
                return
        else:
            return
        self._event("warning", f"Cancelling duplicate order {order.id} of the current trade")
        await self._safe_cancel(order.id)

    async def _exit_order_filled(self, t: ManagedTrade, order_id: int, role: str) -> None:
        if not t.exit_reason:
            t.exit_reason = "stop loss" if role == "stop" else "profit target"
        if t.exit_ref is None:
            t.exit_ref = t.stop_price if role == "stop" else t.target_price
        sibling = t.target_order_id if role == "stop" else t.stop_order_id
        if t.state == TradeState.EXITING:
            # A close was already sent: if it filled too (its reports may be lost), this fill reversed the
            # position instead of closing it. Close whatever is there now; a flat report finishes the trade.
            await self._safe_cancel(sibling)
            await self._close_now(t, order_id=order_id)
            return
        if t.exited_size < t.filled_size:
            return  # only part of it so far; the rest of the fill (or the order's own report) follows
        await self._safe_cancel(sibling)
        await self._finalize(t)

    async def _closing_fill(self, t: ManagedTrade) -> None:
        """A close (ours or by hand) filled: once it covers the position, the position is flat even if its
        position update is late or lost. Cancel the protective stop now - left working it would reverse it."""
        if not t.filled_size or t.exited_size < t.filled_size:
            return
        await self._safe_cancel(t.stop_order_id)
        await self._safe_cancel(t.target_order_id)
        self.position = 0
        if not t.exit_reason:
            t.exit_reason = "position closed outside the bot"
        await self._finalize(t)

    async def handle_fill(self, fill: Fill) -> None:
        if fill.contract_id != self.contract.id:
            return
        async with self._lock:
            self._version += 1
            t = self.trade
            if t is None or t.state not in (TradeState.OPEN, TradeState.EXITING) or fill.voided:
                return
            if fill.side != t.side.opposite or fill.order_id == t.entry_order_id:
                return
            if not t.note_exit(fill.order_id, fill.price, fill.size, fill_id=fill.id):
                return
            if fill.order_id in (t.stop_order_id, t.target_order_id):
                await self._exit_order_filled(t, fill.order_id, "stop" if fill.order_id == t.stop_order_id else "target")
            else:
                await self._closing_fill(t)  # e.g. a close-position market order

    async def handle_position(self, pos: Position) -> None:
        if pos.contract_id != self.contract.id:
            return
        async with self._lock:
            self._version += 1
            self.position = pos.size
            self.position_avg = pos.avg_price
            t = self.trade
            if t is None:
                return
            same_side = pos.size != 0 and (pos.size > 0) == (t.side == OrderSide.BUY)
            if pos.size != 0 and not same_side:
                await self._wrong_side(t, pos)
            elif t.state == TradeState.PENDING and same_side:
                await self._entry_filled(t, pos.avg_price, abs(pos.size))
            elif t.state == TradeState.OPEN and same_side and abs(pos.size) > t.filled_size:
                # The rest of a partially filled entry arrived: protect the whole position at once.
                self._event("info", f"Entry fill completed: {abs(pos.size)} contracts @ {pos.avg_price}")
                t.filled_size = abs(pos.size)
                t.entry_price = pos.avg_price
                if not self.use_native_brackets:
                    await self._resize_protection(t)
            elif t.state in (TradeState.OPEN, TradeState.EXITING) and pos.size == 0:
                await self._flat_reported(t)

    async def _flat_reported(self, t: ManagedTrade) -> None:
        await self._safe_cancel(t.stop_order_id)
        await self._safe_cancel(t.target_order_id)
        if not t.exit_reason:
            t.exit_reason = "position closed outside the bot"
        await self._finalize(t)

    async def _wrong_side(self, t: ManagedTrade, pos: Position) -> None:
        """The position is opposite to the trade: an old order filled into it, a stop filled after the close,
        or it was reversed outside the bot. Never protect it with this trade's orders (they are on the wrong
        side and would add to it): cancel them and close it now."""
        if t.state == TradeState.CLOSED:
            return
        self._event("critical", f"Position {pos.size} is on the wrong side for the {t.side.label} trade - closing it")
        await self._safe_cancel(t.stop_order_id)
        if t.state == TradeState.PENDING:
            await self._safe_cancel(t.entry_order_id)
            t.state = TradeState.OPEN  # closed below; its P&L comes from the close
            t.entry_price, t.filled_size = t.entry_price or pos.avg_price, t.filled_size or abs(pos.size)
        if t.state == TradeState.OPEN:
            t.exit_reason = "position on the wrong side"
            await self._exit_locked(t, t.exit_reason)
        else:  # already exiting: the close that was sent did not end it
            await self._safe_cancel(t.target_order_id)
            await self._close_now(t)

    async def _finalize(self, t: ManagedTrade) -> None:
        if t.state == TradeState.CLOSED:
            return
        t.exit_price = t.average_exit()
        if t.exit_price is None:
            t.exit_price = self.last_price if self.last_price is not None else t.entry_price
        t.closed_at = self.clock()
        t.gross_pnl = self.contract.pnl(t.entry_price or 0.0, t.exit_price or 0.0, t.signed_size)
        t.fees = self.fees_round_turn * t.filled_size
        t.state = TradeState.CLOSED
        self.trade = None
        self.last_trade = t
        self._event(
            "info",
            f"EXIT {t.side.label} {t.filled_size} @ {t.exit_price:.{self.contract.price_decimals}f} "
            f"({t.exit_reason}) net ${t.net_pnl:,.2f}",
        )
        if self.on_trade_closed:
            try:
                await self.on_trade_closed(t)
            except Exception:  # noqa: BLE001 - the trade is closed either way; don't break the order path
                log.exception("Could not record the closed trade %s", t.tag)

    # ------------------------------------------------------------------- exits

    async def exit(self, reason: str) -> None:
        async with self._lock:
            self._version += 1
            await self._exit_locked(self.trade, reason)

    async def _exit_locked(self, t: ManagedTrade | None, reason: str) -> None:
        if t is None:
            if self.position != 0:
                self._event("warning", f"Closing untracked position ({reason})")
                self._count_action()
                await self.broker.close_position(self.contract.id)
            return
        if t.state == TradeState.PENDING:
            t.exit_requested = True
            t.exit_reason = reason
            await self._safe_cancel(t.entry_order_id)
            return
        if t.state in (TradeState.CLOSED, TradeState.EXITING):
            return  # a close is already in flight; sending another could reverse the position
        t.state = TradeState.EXITING
        t.exit_sent_at = self.clock()
        if t.exit_ref is None:
            t.exit_ref = self.last_price
        t.exit_reason = t.exit_reason or reason
        await self._safe_cancel(t.target_order_id)
        await self._close_now(t, first=True, failed=("critical", "protective stop left in place"))
        # The protective stop is deliberately kept until the broker reports the position flat
        # (handle_position / reconcile cancel it then), so the trade is never unprotected.

    async def _close_now(self, t: ManagedTrade, order_id: int | None = None, first: bool = False,
                         failed: tuple[str, str] = ("warning", "will retry")) -> bool:
        """Send TopstepX's close-position request (it closes whatever position is there).

        A close asked for because our stop or target filled carries that order's id: one close per order is
        enough, remembered only once it went through (a failed one is retried from the next report of the
        same order). Any other close (a position report, reconcile) is skipped within ``CLOSE_DEBOUNCE`` of
        the last one. Broker timestamps are never compared with this PC's clock."""
        now = self.clock()
        if order_id is not None:
            if order_id in t.close_sent_for:
                return False
        elif not first and t.last_close_at is not None and now - t.last_close_at < CLOSE_DEBOUNCE:
            return False
        try:
            self._count_action()
            await self.broker.close_position(self.contract.id)
        except Exception as exc:  # noqa: BLE001 - the caller decides; reconcile retries
            if may_have_reached_broker(exc):
                t.last_close_at = now
            self._event(failed[0], f"Close position failed ({exc}); {failed[1]}")
            return False
        t.last_close_at = t.exit_sent_at = now
        if order_id is not None:
            t.close_sent_for.add(order_id)
        return True

    async def flatten_all(self, reason: str) -> None:
        """Close any position and cancel every working order on this contract."""
        async with self._lock:
            self._version += 1
            await self._exit_locked(self.trade, reason)
            keep = self.trade.stop_order_id if self.trade else None
            try:
                for order in await self.broker.open_orders():
                    # Leave market orders alone (one may be the closing order) and our protective stop
                    # (cancelled once the position is confirmed flat).
                    if order.contract_id == self.contract.id and order.type != OrderType.MARKET and order.id != keep:
                        await self._safe_cancel(order.id)
            except Exception as exc:  # noqa: BLE001
                self._event("error", f"Could not list open orders while flattening: {exc}")

    async def update_stop(self, new_stop: float) -> bool:
        """Move the protective stop - only ever in the trade's favour."""
        async with self._lock:
            self._version += 1
            t = self.trade
            if t is None or t.state != TradeState.OPEN or t.stop_order_id is None:
                return False
            new_stop = self.contract.round_price(new_stop, "down" if t.side == OrderSide.BUY else "up")
            improves = new_stop > t.stop_price if t.side == OrderSide.BUY else new_stop < t.stop_price
            if not improves:
                return False
            try:
                self._count_action()
                await self.broker.modify_order(t.stop_order_id, stop_price=new_stop)
            except Exception as exc:  # noqa: BLE001
                self._event("warning", f"Stop update failed: {exc}")
                return False
            self._event("info", f"Stop moved {t.stop_price} -> {new_stop}")
            t.stop_price = new_stop
            return True

    # ---------------------------------------------------------- reconciliation

    async def reconcile(self) -> None:
        """Compare our view with the broker's and repair any difference.

        Open orders are listed before positions: a stop that fills in between then looks like "position
        flat" (finished, correctly) rather than "stop missing" (a new stop on a flat account). When only
        one of the two requests succeeds, the half that came back is still acted on, and the error is
        raised afterwards so the caller reports it."""
        async with self._lock:
            version = self._version  # taken while no change is in progress
        orders: list[Order] | None = None
        positions: list[Position] | None = None
        error: Exception | None = None
        try:
            orders = [o for o in await self.broker.open_orders() if o.contract_id == self.contract.id]
        except Exception as exc:  # noqa: BLE001 - raised below, after acting on the other half
            error = exc
        try:
            positions = await self.broker.positions()
        except Exception as exc:  # noqa: BLE001 - raised below, after acting on the other half
            error = error or exc
        if orders is None and positions is None:
            raise error
        async with self._lock:
            if self._version != version:
                log.debug("Reconcile skipped: state changed while fetching the broker's view")
            elif orders is None:
                await self._reconcile_positions(positions)
            elif positions is None:
                await self._reconcile_orders(orders)
            else:
                await self._reconcile_both(positions, orders)
        if error is not None:
            raise error

    def _own_position(self, positions: list[Position]) -> Position | None:
        pos = next((p for p in positions if p.contract_id == self.contract.id and p.size != 0), None)
        self.position = pos.size if pos else 0
        if pos:
            self.position_avg = pos.avg_price
        return pos

    async def _reconcile_positions(self, positions: list[Position]) -> None:
        """Only the positions came back: act on what they prove."""
        pos = self._own_position(positions)
        t = self.trade
        if t is None or t.state == TradeState.PENDING and pos is None:
            return
        if pos is not None and (pos.size > 0) != (t.side == OrderSide.BUY):
            await self._wrong_side(t, pos)
        elif pos is None and t.state in (TradeState.OPEN, TradeState.EXITING):
            await self._flat_reported(t)
        elif pos is not None and t.state == TradeState.EXITING:
            await self._retry_close(t)

    async def _reconcile_orders(self, orders: list[Order]) -> None:
        """Only the open orders came back: adopt or cancel what they show; an exiting trade whose stop is no
        longer working may have been reversed by it, so a close goes out as a precaution."""
        t = self.trade
        await self._cancel_stale_orders(orders, t)
        self._unconfirmed_tags.clear()
        if t is not None and t.state == TradeState.EXITING and t.stop_order_id not in {o.id for o in orders}:
            await self._close_now(t)

    async def _retry_close(self, t: ManagedTrade) -> None:
        if t.exit_sent_at is None or self.clock() - t.exit_sent_at > EXIT_RETRY_AFTER:
            self._event("warning", "Position still open while exiting - retrying close")
            t.exit_sent_at = self.clock()
            await self._close_now(t)

    async def _reconcile_both(self, positions: list[Position], orders: list[Order]) -> None:
        pos = self._own_position(positions)
        t = self.trade
        await self._cancel_stale_orders(orders, t)
        self._unconfirmed_tags.clear()
        if t is None:
            if pos is not None and not self._recover_own_trade(pos, orders):
                await self._handle_orphan(pos, orders)
            return
        if pos is not None and (pos.size > 0) != (t.side == OrderSide.BUY):
            await self._wrong_side(t, pos)
            return
        if t.state == TradeState.PENDING:
            if pos is not None:
                await self._entry_filled(t, pos.avg_price, abs(pos.size))
            elif self.clock() - t.created_at > self.entry_timeout:
                if t.entry_order_id is None:  # placing it failed: find it by its tag, if it got through
                    t.entry_order_id = next((o.id for o in orders if o.custom_tag == f"{t.tag}-E"), None)
                order = await self.broker.get_order(t.entry_order_id, t.created_at) if t.entry_order_id else None
                if order and order.status == OrderStatus.FILLED:
                    self._event("warning", "Entry filled but no position reported yet; waiting")
                else:
                    await self._safe_cancel(t.entry_order_id)
                    self.trade = None
                    self._event("warning", "Entry not filled in time - abandoned")
            return
        if pos is None:
            await self._flat_reported(t)
            return
        if t.state == TradeState.EXITING:
            await self._retry_close(t)
            return
        working_ids = {o.id for o in orders}
        if self.use_native_brackets:
            for o in orders:
                if o.side == t.side.opposite and o.type == OrderType.STOP:
                    t.stop_order_id = o.id
                elif o.side == t.side.opposite and o.type == OrderType.LIMIT:
                    t.target_order_id = o.id
        t.filled_size = abs(pos.size)
        if t.stop_order_id not in working_ids:
            self._event("warning", "Protective stop missing at broker - re-placing")
            # Cancel the old one first: "not working" in a snapshot may only mean it was being changed.
            await self._safe_cancel(t.stop_order_id)
            t.stop_order_id = None
            await self._place_stop(t)
            return
        sizes = {o.id: o.size for o in orders}
        if any(sizes.get(oid, t.filled_size) != t.filled_size for oid in (t.stop_order_id, t.target_order_id)):
            await self._resize_protection(t, sizes)

    async def _cancel_stale_orders(self, orders: list[Order], t: ManagedTrade | None) -> None:
        """Cancel working bot orders that don't belong to the current trade (e.g. a stop whose cancel failed),
        and adopt the current trade's own stop or target if the bot never got its id."""
        working = {o.id for o in orders}
        self._leftovers &= working
        for o in orders:
            match = _ANY_OWN_TAG.match(o.custom_tag or "")
            if match is None and o.id not in self._leftovers:
                continue
            if t is not None and ((match and match.group(1) == t.tag) or o.id in (t.entry_order_id, t.stop_order_id,
                                                                                   t.target_order_id)):
                if match and _OWN_TAG.match(o.custom_tag or ""):
                    await self._adopt(t, o)
                continue
            if t is None and match and _OWN_TAG.match(o.custom_tag or "") and self.position != 0:
                continue  # may be the stop that protects a position recovered after a restart
            self._event("warning", f"Cancelling stale bot order {o.id}")
            await self._safe_cancel(o.id)

    async def _resize_protection(self, t: ManagedTrade, sizes: dict[int, int] | None = None) -> None:
        """Make the stop and target cover exactly the position (e.g. after a partial entry fill)."""
        for oid in (t.stop_order_id, t.target_order_id):
            if oid is None or (sizes is not None and sizes.get(oid, t.filled_size) == t.filled_size):
                continue
            try:
                self._count_action()
                await self.broker.modify_order(oid, size=t.filled_size)
            except Exception as exc:  # noqa: BLE001 - reconcile compares sizes again and retries
                self._event("warning", f"Could not resize order {oid} to {t.filled_size}: {exc}")

    def _recover_own_trade(self, pos: Position, orders: list[Order]) -> bool:
        """After a crash/restart, re-adopt a position that is still protected by one of OUR stops."""
        side = OrderSide.BUY if pos.size > 0 else OrderSide.SELL
        stops, targets = {}, {}
        for o in orders:
            match = _OWN_TAG.match(o.custom_tag or "")
            if not match or o.side != side.opposite:
                continue
            base, kind = match.group(1), match.group(2)
            if kind.startswith("S") and o.type == OrderType.STOP:
                stops[base] = (int(kind[1:]), o)
            elif kind == "T" and o.type == OrderType.LIMIT:
                targets[base] = o
        if not stops:
            return False
        base, (seq, stop) = max(stops.items(), key=lambda kv: kv[1][0])
        target = targets.get(base)
        self.trade = ManagedTrade(
            tag=base,
            side=side,
            size=abs(pos.size),
            stop_price=stop.stop_price,
            target_price=target.limit_price if target else None,
            reason="recovered after restart",
            created_at=self.clock(),
            strategy=self.strategy_name,
            state=TradeState.OPEN,
            entry_price=pos.avg_price,
            filled_size=abs(pos.size),
            opened_at=self.clock(),
            stop_order_id=stop.id,
            target_order_id=target.id if target else None,
            stop_seq=seq,
        )
        self._event(
            "warning",
            f"Recovered open {side.label} {abs(pos.size)} @ {pos.avg_price} after restart "
            f"(stop {stop.stop_price}" + (f", target {target.limit_price})" if target else ")"),
        )
        return True

    async def _handle_orphan(self, pos: Position, orders: list[Order]) -> None:
        if self.orphan_policy == "ignore":
            log.debug("Ignoring position not opened by the bot: %s", pos)
            return
        if self.orphan_policy == "flatten":
            self._event("warning", f"Flattening position not opened by the bot ({pos.size} @ {pos.avg_price})")
            self._count_action()
            await self.broker.close_position(self.contract.id)
            for o in orders:
                if o.type != OrderType.MARKET:
                    await self._safe_cancel(o.id)
            return
        # adopt: manage it, keeping its existing stop or adding one 40 ticks from the entry
        side = OrderSide.BUY if pos.size > 0 else OrderSide.SELL
        existing_stop = next((o for o in orders if o.type == OrderType.STOP and o.side == side.opposite), None)
        stop = existing_stop.stop_price if existing_stop else None
        if stop is None:
            stop = self.contract.round_price(pos.avg_price - side.sign * 40 * self.contract.tick_size)
        t = ManagedTrade(
            tag=f"tsb{uuid.uuid4().hex[:10]}",
            side=side,
            size=abs(pos.size),
            stop_price=stop,
            target_price=None,
            reason="adopted existing position",
            created_at=self.clock(),
            strategy=self.strategy_name,
            state=TradeState.OPEN,
            entry_price=pos.avg_price,
            filled_size=abs(pos.size),
            opened_at=self.clock(),
            stop_order_id=existing_stop.id if existing_stop else None,
        )
        self.trade = t
        self._event("warning", f"Adopted existing {side.label} position of {abs(pos.size)}; stop {stop}")
        if existing_stop is None:
            await self._place_stop(t)
