"""Interactive first-time setup: credentials, account, instrument, strategy and risk."""

from __future__ import annotations

import asyncio
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, FloatPrompt, Prompt
from rich.table import Table

from topstep_bot.api.rest import ProjectXClient, ProjectXError
from topstep_bot.instruments import SPECS
from topstep_bot.risk.topstep import PLANS
from topstep_bot.strategies import STRATEGIES

console = Console()

SYMBOL_CHOICES = ["MNQ", "MES", "NQ", "ES", "M2K", "RTY", "MYM", "YM", "MGC", "GC", "MCL", "CL"]


def update_env_file(path: Path, values: dict[str, str]) -> None:
    """Set keys in a .env file, keeping any other lines intact."""
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    remaining = dict(values)
    out = []
    for line in lines:
        key = line.split("=", 1)[0].strip() if "=" in line and not line.lstrip().startswith("#") else None
        if key in remaining:
            out.append(f"{key}={remaining.pop(key)}")
        else:
            out.append(line)
    if not lines:
        out.append("# TopstepX API credentials - keep this file private. Never share or commit it.")
    out.extend(f"{k}={v}" for k, v in remaining.items())
    path.write_text("\n".join(out) + "\n", encoding="utf-8")


def render_config(
    *,
    mode: str = "paper",
    plan: str = "50K",
    stage: str = "combine",
    account_id: int | None = None,
    symbol: str = "MNQ",
    timeframe: int = 5,
    strategy: str = "noise_breakout",
    risk_per_trade: float = 150,
    daily_loss: float = 500,
    max_trades: int = 4,
) -> str:
    account_line = f"  account_id: {account_id}" if account_id else "  # account_id: 123456          # set by 'topstep-bot setup'"
    return f"""# Topstep Bot configuration. Every setting has a safe default; the full list is in
# docs/HOW_TO_USE.md (section "Full configuration reference"). Edit with Notepad; spaces matter.

# paper = simulated fills on real market data (no orders sent). live = real orders on your TopstepX account.
mode: {mode}

account:
  plan: "{plan}"                # 50K, 100K or 150K
  stage: {stage}            # combine, express or practice
{account_line}
  # mll_floor_override: 48500    # copy your current Max Loss Limit from the Topstep dashboard to sync the bot

instrument:
  symbol: {symbol}                # MNQ, MES, NQ, ES, M2K, RTY, MYM, YM, MGC, GC, MCL, CL
  timeframe_minutes: {timeframe}

strategy:
  name: {strategy}                # noise_breakout, orb_momentum, orb, ema_trend, late_day_momentum, vwap_reversion - or let 'train' pick
  params: {{}}                 # override strategy defaults here, e.g. {{target_r: 1.5}}

risk:
  risk_per_trade: {risk_per_trade:g}          # $ lost if a trade hits its stop (position size is calculated from this)
  personal_daily_loss_limit: {daily_loss:g} # stop trading for the day after losing this much (incl. open P&L)
  max_trades_per_day: {max_trades}
  max_consecutive_losses: 2
  mll_buffer: 200               # keep this much cushion above Topstep's Maximum Loss Limit
  # daily_profit_target: 1200   # combine default: 40% of the profit target (protects the consistency rule)
  # max_contracts: 2            # optional extra cap (Topstep's own cap is always enforced)
  # breakeven_at_r: 1.0         # move stop to breakeven after 1R of profit
  # trail_atr_multiple: 2.0     # ATR trailing stop
  # min_stop_atr: 0.5           # never place a stop closer than half an ATR
  # ramp_up_days: 3             # first live trading days on a new account risk half as much

session:                        # times are US Central (exchange) time
  trade_start: "08:30"
  last_entry: "14:30"
  flatten_at: "15:00"           # Topstep requires flat by 15:10 CT
  blackout_windows: []          # e.g. [{{start: "07:25", end: "07:40", label: "CPI"}}]

telegram:                       # control the bot from Telegram (token + chat ID are stored in .env)
  control_enabled: true
  confirm_dangerous: true       # /flatten and /stop need a confirmation tap
  allowed_user_ids: []          # optionally restrict to specific Telegram user IDs

dashboard:
  enabled: true
  port: 8765
  open_browser: true
"""


def _pick(title: str, options: list[tuple[str, str]], default: str) -> str:
    table = Table(show_header=False, box=None, padding=(0, 2))
    for i, (key, desc) in enumerate(options, start=1):
        table.add_row(f"[bold]{i}[/]", f"[bold]{key}[/]", desc)
    console.print(f"\n[bold cyan]{title}[/]")
    console.print(table)
    keys = [k for k, _ in options]
    default_index = str(keys.index(default) + 1)
    choice = Prompt.ask("Choose", choices=[str(i) for i in range(1, len(keys) + 1)], default=default_index)
    return keys[int(choice) - 1]


async def _fetch_accounts(username: str, api_key: str) -> list:
    async with ProjectXClient(username, api_key) as client:
        return await client.search_accounts(only_active=True)


async def detect_telegram_chat(token: str) -> list[tuple[str, str]]:
    """Chats that recently messaged the bot, as (chat_id, description). Also validates the token."""
    import httpx

    async with httpx.AsyncClient(base_url=f"https://api.telegram.org/bot{token}/", timeout=15) as client:
        me = (await client.post("getMe")).json()
        if not me.get("ok"):
            raise ValueError(me.get("description", "invalid bot token"))
        data = (await client.post("getUpdates", json={"timeout": 0, "allowed_updates": ["message"]})).json()
    chats: dict[str, str] = {}
    for upd in data.get("result") or []:
        msg = upd.get("message") or {}
        chat = msg.get("chat") or {}
        if "id" in chat:
            who = chat.get("username") or chat.get("title") or chat.get("first_name") or ""
            chats[str(chat["id"])] = f"{who} ({chat.get('type', 'chat')})"
    return list(chats.items())


def setup_telegram(env_path: Path) -> bool:
    console.print(
        "1. In Telegram, open [bold]@BotFather[/], send [bold]/newbot[/] and follow the prompts.\n"
        "2. Copy the bot token it gives you (looks like 123456789:AA...).\n"
        "Only YOUR chat will be able to control the bot; messages from anyone else are ignored."
    )
    token = Prompt.ask("Bot token (input hidden)", password=True).strip()
    if not token:
        return False
    chat_id = ""
    for _ in range(3):
        Prompt.ask("3. Now open your new bot in Telegram, press Start (or send it any message), then press Enter here",
                   default="", show_default=False)
        try:
            chats = asyncio.run(detect_telegram_chat(token))
        except Exception as exc:  # noqa: BLE001
            console.print(f"[red]Telegram rejected the token:[/] {exc}")
            return False
        if len(chats) == 1:
            chat_id = chats[0][0]
            console.print(f"[green]Found your chat:[/] {chats[0][1]}")
            break
        if len(chats) > 1:
            chat_id = _pick("Which chat should control the bot?", chats, chats[0][0])
            break
        console.print("[yellow]No message found yet - make sure you messaged the new bot.[/]")
    if not chat_id:
        chat_id = Prompt.ask("Enter your chat ID manually (leave empty to skip)", default="").strip()
    if not chat_id:
        return False
    update_env_file(env_path, {"TELEGRAM_BOT_TOKEN": token, "TELEGRAM_CHAT_ID": chat_id})
    console.print("[green]Telegram saved.[/] Test it any time with: topstep-bot telegram-test")
    return True


def run_wizard(config_path: Path, env_path: Path) -> bool:
    console.print(
        Panel.fit(
            "[bold]Topstep Bot setup[/]\n\n"
            "This wizard creates [bold]config.yaml[/] and stores your API key in [bold].env[/] on this computer.\n"
            "You can re-run it any time. Press Ctrl+C to quit.\n\n"
            "[yellow]Trading futures involves substantial risk of loss. This software comes with no guarantee of "
            "profit.\nTopstep rules require automated trading to run from your own computer (no VPS/VPN).[/]",
            border_style="cyan",
        )
    )
    if config_path.exists() and not Confirm.ask(f"{config_path} already exists. Overwrite it?", default=False):
        return False

    # ---- credentials
    console.print(
        "\n[bold cyan]1. TopstepX API access[/]\n"
        "You need an API subscription (TopstepX -> Settings -> API; Topstep traders get 50% off with code 'topstep').\n"
        "Your [bold]username[/] is your TopstepX login name (not your email). Create an API key in the same API page."
    )
    account_id = None
    hint_plan, hint_stage = None, None
    if Confirm.ask("Enter API credentials now? (No = skip; you can still backtest on sample data)", default=True):
        username = Prompt.ask("TopstepX username").strip()
        api_key = Prompt.ask("API key (input hidden)", password=True).strip()
        update_env_file(env_path, {"TOPSTEPX_USERNAME": username, "TOPSTEPX_API_KEY": api_key})
        console.print(f"[green]Saved to {env_path}[/]")
        try:
            with console.status("Testing login..."):
                accounts = asyncio.run(_fetch_accounts(username, api_key))
            console.print(f"[green]Login OK.[/] Found {len(accounts)} active account(s).")
            if accounts:
                options = [(str(a.id), f"{a.name}  balance ${a.balance:,.2f}  {'can trade' if a.can_trade else 'NOT tradable'}") for a in accounts]
                account_id = int(_pick("Which account should the bot use?", options, options[0][0]))
                from topstep_bot.preflight import account_hints

                chosen = next(a for a in accounts if a.id == account_id)
                hint_plan, hint_stage = account_hints(chosen.name)
        except ProjectXError as exc:
            console.print(f"[red]Login failed:[/] {exc}\nCheck the username/API key; you can fix them in {env_path} later.")
        except Exception as exc:  # noqa: BLE001
            console.print(f"[red]Could not reach TopstepX:[/] {exc}")

    # ---- account
    if hint_plan or hint_stage:
        console.print(f"[dim]From the account name, the defaults below are set to {hint_plan or '?'} {hint_stage or '?'} - "
                      "just press Enter if that's right.[/]")
    plan = _pick(
        "2. Account size",
        [(p.name, f"profit target ${p.profit_target:,.0f}, max loss limit ${p.max_loss_limit:,.0f}, max {p.max_minis} minis") for p in PLANS.values()],
        hint_plan or "50K",
    )
    stage = _pick(
        "3. Account type",
        [("combine", "Trading Combine (evaluation)"), ("express", "Express Funded Account (XFA)"), ("practice", "Practice account")],
        hint_stage or "combine",
    )

    # ---- instrument & strategy
    symbol = _pick(
        "4. What to trade (micros are strongly recommended to start)",
        [(s, f"{SPECS[s].description} - ${SPECS[s].tick_value:g}/tick") for s in SYMBOL_CHOICES],
        "MNQ",
    )
    strategy = _pick("5. Strategy", [(cls.name, f"{cls.title}: {cls.description}") for cls in STRATEGIES.values()], "noise_breakout")

    # ---- risk
    mll = PLANS[plan].max_loss_limit
    console.print(
        f"\n[bold cyan]6. Risk[/]\nYour Maximum Loss Limit is ${mll:,.0f}. Suggested: risk about {7.5:g}% of it per trade "
        f"and stop for the day after losing 25% of it."
    )
    risk = FloatPrompt.ask("Dollars to risk per trade", default=round(mll * 0.075))
    daily = FloatPrompt.ask("Personal daily loss limit ($)", default=round(mll * 0.25))
    if risk > daily:
        console.print("[yellow]Risk per trade is larger than the daily limit; capping it at the daily limit.[/]")
        risk = daily

    # ---- notifications & remote control
    console.print("\n[bold cyan]7. Alerts and remote control (optional)[/]")
    if Confirm.ask("Use Telegram for alerts AND to control the bot from your phone?", default=False):
        setup_telegram(env_path)
    if Confirm.ask("Send trade alerts to a Discord channel?", default=False):
        url = Prompt.ask("Discord webhook URL").strip()
        if url:
            update_env_file(env_path, {"DISCORD_WEBHOOK_URL": url})

    config_path.write_text(
        render_config(
            plan=plan, stage=stage, account_id=account_id, symbol=symbol, strategy=strategy,
            risk_per_trade=risk, daily_loss=daily,
        ),
        encoding="utf-8",
    )
    console.print(
        Panel.fit(
            f"[green bold]Saved {config_path}[/]\n\n"
            "Next steps:\n"
            "  1. [bold]Train[/] (menu) - tests every strategy on real recent data, on days it never saw while\n"
            "     tuning, and can save the best settings. Or just [bold]backtest[/] the one you picked.\n"
            "  2. [bold]Start trading today[/] (menu) - runs every safety check, then goes live. The first live\n"
            "     days trade at reduced risk. Paper trading first is optional, not required.\n"
            "  Start on a Combine or practice account, never one you can't afford to lose.",
            border_style="green",
        )
    )
    return True
