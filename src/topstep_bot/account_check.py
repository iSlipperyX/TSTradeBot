"""Everything TopstepX says about your accounts, checked with the API keys stored on this PC.

The dashboard's Setup tab (Accounts) and ``topstep-bot accounts --check`` use it. For every stored
key it logs in and lists the accounts that key can see; for every account in config.yaml it then
reports:

  * whether its key works and can see the account (closed, failed or on another login if not)
  * balance, whether TopstepX lets it trade right now, and whether the API may trade it at all
    (Topstep doesn't allow Live Funded Accounts on the API)
  * the plan and stage its name and balance point to, compared with your settings
  * open positions and working orders, and today's closed trades and net P&L
  * profit toward the Combine target, and the market the account trades (or the bot's pick)

Accounts the keys can see but config.yaml doesn't list are returned too, ready to be added.
Nothing here changes anything: it only reads.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from topstep_bot.config import BotConfig, Secrets
    from topstep_bot.models import Account

log = logging.getLogger(__name__)

MAIN_KEY = "main"


def mask(key: str | None) -> str:
    return f"••••{key[-4:]}" if key and len(key) > 4 else ("••••" if key else "-")


def account_hints(account: Account) -> tuple[str | None, str | None]:
    """(plan, stage) from the account's name, and from its balance when the name doesn't say."""
    from topstep_bot.preflight import account_hints as from_name
    from topstep_bot.risk.topstep import PLANS

    plan, stage = from_name(account.name)
    if plan is None and stage in (None, "combine", "practice"):
        near = [p for p in PLANS.values() if abs(account.balance - p.account_size) <= p.max_loss_limit]
        if len(near) == 1:
            plan = near[0].name
    return plan, stage


def contract_label(contract_id: str) -> str:
    """'CON.F.US.MNQ.Z26' -> 'MNQ Z26'."""
    parts = contract_id.split(".")
    return f"{parts[3]} {parts[4]}" if len(parts) >= 5 else contract_id


def _account_row(a: Account, key: str) -> dict[str, Any]:
    plan, stage = account_hints(a)
    return {"id": a.id, "name": a.name, "balance": a.balance, "can_trade": a.can_trade, "simulated": a.simulated,
            "plan": plan, "stage": stage, "dll": "DLL" in a.name.upper(), "key": key}


async def _login(username: str | None, api_key: str | None, cfg: BotConfig) -> tuple[list[Account] | None, str | None]:
    from topstep_bot.api import rest

    if not (username and api_key):
        return None, "no username" if api_key else "no API key"
    try:
        async with rest.ProjectXClient(username, api_key, cfg.api.base_url, cfg.api.timeout_seconds) as client:
            return await client.search_accounts(only_active=True), None
    except rest.ProjectXError as exc:
        return None, f"TopstepX did not accept it: {exc}"
    except Exception as exc:  # noqa: BLE001 - network trouble: say so, keep checking the others
        return None, f"could not reach TopstepX ({type(exc).__name__})"


async def _details(cfg: BotConfig, username: str, api_key: str, account: Account) -> dict[str, Any]:
    """Positions, working orders and today's closed trades of one account."""
    from topstep_bot.api import rest
    from topstep_bot.sessions import SessionSchedule, session_open_for

    schedule = SessionSchedule(cfg.session)
    since = session_open_for(schedule.trading_day(datetime.now(UTC)), schedule.tz)
    out: dict[str, Any] = {}
    try:
        async with rest.ProjectXClient(username, api_key, cfg.api.base_url, cfg.api.timeout_seconds) as client:
            positions, orders, fills = await asyncio.gather(
                client.search_open_positions(account.id), client.search_open_orders(account.id),
                client.search_trades(account.id, since))
    except Exception as exc:  # noqa: BLE001
        return {"error": f"could not read positions and trades: {exc}"[:160]}
    out["positions"] = [f"{'long' if p.size > 0 else 'short'} {abs(p.size)} {contract_label(p.contract_id)} @ {p.avg_price:,.2f}"
                        for p in positions if p.size]
    out["orders"] = [f"{o.side.name.lower()} {o.size} {contract_label(o.contract_id)}"
                     + (f" @ {o.limit_price or o.stop_price:,.2f}" if (o.limit_price or o.stop_price) else "")
                     for o in orders]
    live = [f for f in fills if not f.voided]
    out["today"] = {"trades": sum(1 for f in live if f.pnl is not None),
                    "pnl": round(sum(f.pnl or 0.0 for f in live) - sum(f.fees for f in live), 2),
                    "fees": round(sum(f.fees for f in live), 2)}
    return out


async def check(cfg: BotConfig, secrets: Secrets) -> dict[str, Any]:
    """The full report (see the module docstring)."""
    from topstep_bot.factory import starting_balance
    from topstep_bot.market_pick import current_symbol
    from topstep_bot.risk.topstep import PLANS

    keys: list[tuple[str, str | None, str | None]] = [(MAIN_KEY, secrets.username, secrets.api_key)]
    keys += [(name, login.username or secrets.username, login.api_key) for name, login in sorted(secrets.logins.items())]
    results = await asyncio.gather(*(_login(u, k, cfg) for _, u, k in keys))
    seen: dict[str, list[Account]] = {}
    key_rows = []
    for (name, username, api_key), (accounts, error) in zip(keys, results, strict=True):
        if not api_key and name == MAIN_KEY and not secrets.username:
            continue  # nothing stored yet
        key_rows.append({"name": name, "username": username or "", "key": mask(api_key), "ok": error is None,
                         "error": error, "accounts": len(accounts or [])})
        if accounts is not None:
            seen[name] = accounts
    creds = {name: (u, k) for name, u, k in keys}

    names = cfg.account_names(enabled_only=False) if cfg.multi else [None]
    rows, used_ids = [], set()
    detail_jobs = []
    for name in names:
        try:
            acfg = cfg.for_account(name)
            key = secrets.login_for(acfg) or MAIN_KEY
        except ValueError as exc:
            rows.append({"name": name or MAIN_KEY, "status": "error", "issues": [str(exc)], "notes": []})
            continue
        a = acfg.account
        plan = PLANS[a.plan]
        row: dict[str, Any] = {
            "name": name or MAIN_KEY, "enabled": cfg.profile(name).enabled if name else True, "key": key,
            "plan": a.plan, "stage": a.stage, "account_id": a.account_id, "account_name": a.account_name,
            "market": current_symbol(acfg), "auto_pick": acfg.instrument.auto_pick, "fallback": acfg.instrument.symbol,
            "strategy": acfg.strategy.name, "follow": cfg.profile(name).follow if name else None,
            "risk_per_trade": acfg.risk.risk_per_trade, "issues": [], "notes": [], "topstepx": None,
        }
        rows.append(row)
        if key != MAIN_KEY and key not in secrets.logins:
            row["issues"].append(f"uses API key '{key}', which isn't stored on this PC")
            continue
        if key not in seen:
            row["issues"].append(f"API key '{key}' could not log in" if creds.get(key, (None, None))[1] else
                                 "no API key stored yet")
            continue
        if a.account_id is None and not a.account_name:
            row["issues"].append("no TopstepX account chosen yet")
            continue
        match = next((x for x in seen[key] if (a.account_id is not None and x.id == a.account_id)
                      or (a.account_id is None and x.name == a.account_name)), None)
        if match is None:
            row["issues"].append(f"key '{key}' can't see account {a.account_id or a.account_name}: it may be closed, "
                                 "failed, or on another TopstepX login")
            continue
        used_ids.add(match.id)
        info = _account_row(match, key)
        row["topstepx"] = info
        if not match.simulated:
            row["issues"].append("a Live Funded Account: Topstep doesn't allow trading it through the API")
        if not match.can_trade:
            row["issues"].append("TopstepX says it can't trade right now (rule breach, payout review or closed)")
        if info["plan"] and info["plan"] != a.plan:
            row["issues"].append(f"its name/balance point to a {info['plan']} account, but the settings say {a.plan}")
        if info["stage"] and info["stage"] != a.stage:
            row["issues"].append(f"its name points to {info['stage']}, but the settings say {a.stage}")
        if info["dll"] and a.topstep_daily_loss_limit is None:
            row["notes"].append(f"has Topstep's Daily Loss Limit: the bot enforces ${plan.daily_loss_limit:,.0f} itself")
        if a.stage == "combine":
            profit = match.balance - starting_balance(acfg)
            row["progress"] = {"profit": round(profit, 2), "target": plan.profit_target,
                               "remaining": round(max(0.0, plan.profit_target - profit), 2)}
        username, api_key = creds[key]
        detail_jobs.append((row, acfg, username, api_key, match))

    details = await asyncio.gather(*(_details(acfg, u, k, m) for _, acfg, u, k, m in detail_jobs))
    for (row, *_), extra in zip(detail_jobs, details, strict=True):
        row.update(extra)
        if extra.get("error"):
            row["notes"].append(extra["error"])
    for row in rows:
        row["status"] = "error" if row["issues"] else "ok"

    available, excluded = [], []
    for key, accounts in seen.items():
        for x in accounts:
            if x.id in used_ids or any(r["id"] == x.id for r in available + excluded):
                continue  # already set up, or seen with another key
            (available if x.simulated else excluded).append(_account_row(x, key))
    return {"checked_at": datetime.now(UTC).isoformat(timespec="seconds"), "keys": key_rows, "accounts": rows,
            "available": available, "excluded": [x["name"] for x in excluded]}


def text(report: dict[str, Any]) -> str:
    """The report in plain lines (Telegram, the console)."""
    lines = []
    for k in report["keys"]:
        lines.append(f"Key {k['name']} ({k['username']}, {k['key']}): "
                     + (f"OK, sees {k['accounts']} account(s)" if k["ok"] else f"FAILED - {k['error']}"))
    for r in report["accounts"]:
        t = r.get("topstepx")
        head = f"{r['name']}: " + (f"{t['name']} ${t['balance']:,.2f}" if t else "not connected")
        if t:
            head += f", {'can trade' if t['can_trade'] else 'CANNOT trade'}, {r['plan']} {r['stage']}, " \
                    f"market {r['market']}{' (picked)' if r.get('auto_pick') else ''}"
        lines.append(head)
        if r.get("positions") is not None:
            lines.append("  open: " + (", ".join(r["positions"]) or "flat") + (f"; orders: {', '.join(r['orders'])}"
                                                                                if r.get("orders") else ""))
        if r.get("today"):
            lines.append(f"  today: {r['today']['trades']} trade(s), net ${r['today']['pnl']:,.2f}")
        lines += [f"  ! {i}" for i in r["issues"]] + [f"  - {n}" for n in r["notes"]]
    if report["available"]:
        lines.append("Not set up yet: " + ", ".join(f"{a['name']} (${a['balance']:,.0f})" for a in report["available"]))
    return "\n".join(lines)
