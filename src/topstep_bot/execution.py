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
_OWN_TAG = re.compile(r"^(tsb[0-9a-f]{10})-(S\d+|T)$")


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
    exit_fills: list[tuple[float, int]] = field(default_factory=list)
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

    def __post_init__(self) -> None:
        if not self.initial_stop:
            self.initial_stop = self.stop_price

    @property
    def net_pnl(self) -> float:
        return self.gross_pnl - self.fees

    @property
    def signed_size(self) -> int:
        return self.filled_size * self.side.sign

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
            self.on_event(level, message)

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
            log.debug("cancel %s ignored: %s", order_id, exc)

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
                self.trade = None
                self._event("error", f"Entry order failed: {exc}")
                return None
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
            self._event("warning", f"Could not place profit target ({exc}); stop remains active")

    # ------------------------------------------------------------- broker events

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
                    await self._exit_order_filled(t, order, role)
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

    async def _exit_order_filled(self, t: ManagedTrade, order: Order, role: str) -> None:
        qty = order.fill_volume or order.size
        t.exit_fills.append((order.filled_price or self.last_price or 0.0, qty))
        if not t.exit_reason:
            t.exit_reason = "stop loss" if role == "stop" else "profit target"
        if t.exit_ref is None:
            t.exit_ref = t.stop_price if role == "stop" else t.target_price
        sibling = t.target_order_id if role == "stop" else t.stop_order_id
        await self._safe_cancel(sibling)
        await self._finalize(t)

    async def handle_fill(self, fill: Fill) -> None:
        if fill.contract_id != self.contract.id:
            return
        async with self._lock:
            self._version += 1
            t = self.trade
            if t is None or t.state not in (TradeState.OPEN, TradeState.EXITING):
                return
            ours = (t.entry_order_id, t.stop_order_id, t.target_order_id)
            if fill.side == t.side.opposite and fill.order_id not in ours:
                t.exit_fills.append((fill.price, fill.size))  # e.g. a close-position market order

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
            if t.state == TradeState.PENDING and same_side:
                await self._entry_filled(t, pos.avg_price, abs(pos.size))
            elif t.state == TradeState.OPEN and same_side and abs(pos.size) > t.filled_size:
                # The rest of a partially filled entry arrived: protect the whole position at once.
                self._event("info", f"Entry fill completed: {abs(pos.size)} contracts @ {pos.avg_price}")
                t.filled_size = abs(pos.size)
                t.entry_price = pos.avg_price
                if not self.use_native_brackets:
                    await self._resize_protection(t)
            elif t.state in (TradeState.OPEN, TradeState.EXITING) and pos.size == 0:
                await self._safe_cancel(t.stop_order_id)
                await self._safe_cancel(t.target_order_id)
                if not t.exit_reason:
                    t.exit_reason = "position closed outside the bot"
                await self._finalize(t)

    async def _finalize(self, t: ManagedTrade) -> None:
        if t.state == TradeState.CLOSED:
            return
        if t.exit_fills:
            qty = sum(q for _, q in t.exit_fills)
            t.exit_price = sum(p * q for p, q in t.exit_fills) / qty
        else:
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
            await self.on_trade_closed(t)

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
        try:
            self._count_action()
            await self.broker.close_position(self.contract.id)
        except Exception as exc:  # noqa: BLE001 - the stop stays in place; reconcile retries
            self._event("critical", f"Close position failed ({exc}); protective stop left in place")
        # The protective stop is deliberately kept until the broker reports the position flat
        # (handle_position / reconcile cancel it then), so the trade is never unprotected.

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
        """Compare our view with the broker's and repair any difference."""
        async with self._lock:
            version = self._version  # taken while no change is in progress
        positions = await self.broker.positions()
        orders = [o for o in await self.broker.open_orders() if o.contract_id == self.contract.id]
        pos = next((p for p in positions if p.contract_id == self.contract.id and p.size != 0), None)
        async with self._lock:
            if self._version != version:
                log.debug("Reconcile skipped: state changed while fetching the broker's view")
                return
            self.position = pos.size if pos else 0
            if pos:
                self.position_avg = pos.avg_price
            t = self.trade
            if t is None:
                if pos is not None:
                    if not self._recover_own_trade(pos, orders):
                        await self._handle_orphan(pos, orders)
                else:
                    for o in orders:
                        if (o.custom_tag or "").startswith("tsb"):
                            self._event("warning", f"Cancelling stale bot order {o.id}")
                            await self._safe_cancel(o.id)
                return
            if t.state == TradeState.PENDING:
                if pos is not None:
                    await self._entry_filled(t, pos.avg_price, abs(pos.size))
                elif self.clock() - t.created_at > self.entry_timeout:
                    order = await self.broker.get_order(t.entry_order_id, t.created_at) if t.entry_order_id else None
                    if order and order.status == OrderStatus.FILLED:
                        self._event("warning", "Entry filled but no position reported yet; waiting")
                    else:
                        await self._safe_cancel(t.entry_order_id)
                        self.trade = None
                        self._event("warning", "Entry not filled in time - abandoned")
                return
            if pos is None:
                await self._safe_cancel(t.stop_order_id)
                await self._safe_cancel(t.target_order_id)
                t.exit_reason = t.exit_reason or "position closed outside the bot"
                await self._finalize(t)
                return
            if t.state == TradeState.EXITING:
                if t.exit_sent_at is None or self.clock() - t.exit_sent_at > EXIT_RETRY_AFTER:
                    self._event("warning", "Position still open while exiting - retrying close")
                    t.exit_sent_at = self.clock()
                    self._count_action()
                    await self.broker.close_position(self.contract.id)
                return
            working_ids = {o.id for o in orders}
            if self.use_native_brackets:
                for o in orders:
                    if o.side == t.side.opposite and o.type == OrderType.STOP:
                        t.stop_order_id = o.id
                    elif o.side == t.side.opposite and o.type == OrderType.LIMIT:
                        t.target_order_id = o.id
                working_ids = {o.id for o in orders}
            t.filled_size = abs(pos.size)
            if t.stop_order_id not in working_ids:
                self._event("warning", "Protective stop missing at broker - re-placing")
                await self._place_stop(t)
                return
            sizes = {o.id: o.size for o in orders}
            if any(sizes.get(oid, t.filled_size) != t.filled_size for oid in (t.stop_order_id, t.target_order_id)):
                await self._resize_protection(t, sizes)

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
