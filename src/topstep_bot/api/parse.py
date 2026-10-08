"""Convert ProjectX JSON payloads (REST and realtime share field names) into domain models."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Iterator

from topstep_bot.models import (
    Account,
    Bar,
    Contract,
    Fill,
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
    Position,
    PositionType,
)

UTC = timezone.utc
_FRACTION = re.compile(r"(\.\d{6})\d+")
_ROOT = re.compile(r"^([A-Z0-9]+?)[FGHJKMNQUVXZ]\d{1,2}$")


def parse_ts(value: Any) -> datetime | None:
    """Parse ISO-8601 timestamps from the API (handles 'Z' and .NET 7-digit fractions)."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    text = _FRACTION.sub(r"\1", str(value).strip()).replace("Z", "+00:00")
    ts = datetime.fromisoformat(text)
    return ts if ts.tzinfo else ts.replace(tzinfo=UTC)


def format_ts(ts: datetime) -> str:
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return ts.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def root_from_name(name: str) -> str:
    match = _ROOT.match(name.upper())
    return match.group(1) if match else name.upper()


def unwrap(payload: Any) -> Iterator[dict]:
    """Realtime payloads may be a dict, a list of dicts, or {'action':..,'data':{..}} wrappers."""
    if isinstance(payload, list):
        for item in payload:
            yield from unwrap(item)
    elif isinstance(payload, dict):
        inner = payload.get("data")
        if isinstance(inner, (dict, list)) and "id" not in payload and "price" not in payload:
            yield from unwrap(inner)
        else:
            yield payload


def _f(value: Any) -> float | None:
    return None if value is None else float(value)


def account(d: dict) -> Account:
    return Account(
        id=int(d["id"]),
        name=str(d.get("name", "")),
        balance=float(d.get("balance") or 0.0),
        can_trade=bool(d.get("canTrade", True)),
        is_visible=bool(d.get("isVisible", True)),
        simulated=bool(d.get("simulated", True)),
    )


def contract(d: dict) -> Contract:
    name = str(d.get("name", ""))
    return Contract(
        id=str(d["id"]),
        name=name,
        tick_size=float(d["tickSize"]),
        tick_value=float(d["tickValue"]),
        description=str(d.get("description", "")),
        symbol_id=str(d.get("symbolId", "")),
        active=bool(d.get("activeContract", True)),
        root=root_from_name(name),
    )


def bar(d: dict) -> Bar:
    return Bar(
        ts=parse_ts(d["t"]),
        open=float(d["o"]),
        high=float(d["h"]),
        low=float(d["l"]),
        close=float(d["c"]),
        volume=float(d.get("v") or 0.0),
    )


def order(d: dict) -> Order:
    return Order(
        id=int(d["id"]),
        account_id=int(d.get("accountId", 0)),
        contract_id=str(d.get("contractId", "")),
        type=OrderType(int(d.get("type", 0))),
        side=OrderSide(int(d.get("side", 0))),
        size=int(d.get("size", 0)),
        status=OrderStatus(int(d.get("status", 0))),
        limit_price=_f(d.get("limitPrice")),
        stop_price=_f(d.get("stopPrice")),
        filled_price=_f(d.get("filledPrice")),
        fill_volume=int(d.get("fillVolume") or 0),
        custom_tag=d.get("customTag"),
        created_at=parse_ts(d.get("creationTimestamp")),
        updated_at=parse_ts(d.get("updateTimestamp")),
    )


def position(d: dict) -> Position:
    size = int(d.get("size") or 0)
    if int(d.get("type", 0)) == PositionType.SHORT:
        size = -size
    return Position(
        account_id=int(d.get("accountId", 0)),
        contract_id=str(d.get("contractId", "")),
        size=size,
        avg_price=float(d.get("averagePrice") or 0.0),
    )


def fill(d: dict) -> Fill:
    return Fill(
        id=int(d["id"]),
        order_id=int(d.get("orderId", 0)),
        contract_id=str(d.get("contractId", "")),
        side=OrderSide(int(d.get("side", 0))),
        size=int(d.get("size", 0)),
        price=float(d.get("price", 0.0)),
        ts=parse_ts(d.get("creationTimestamp")) or datetime.now(UTC),
        pnl=_f(d.get("profitAndLoss")),
        fees=float(d.get("fees") or 0.0),
        voided=bool(d.get("voided", False)),
    )
