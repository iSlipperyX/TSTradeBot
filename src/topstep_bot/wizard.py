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


def remove_env_keys(path: Path, keys: set[str]) -> bool:
    """Delete keys from a .env file, keeping every other line. Returns True if anything was removed."""
    if not path.exists():
        return False
    lines = path.read_text(encoding="utf-8").splitlines()
    kept = [ln for ln in lines if not ("=" in ln and not ln.lstrip().startswith("#") and ln.split("=", 1)[0].strip() in keys)]
    if len(kept) == len(lines):
        return False
    path.write_text("\n".join(kept) + "\n", encoding="utf-8")
    return True


def render_config(
    *,
    mode: str = "paper",
    plan: str = "50K",
    stage: str = "combine",
    account_id: int | None = None,
    symbol: str = "MNQ",
    timeframe: int = 5,
    strategy: str = "adaptive",
    risk_per_trade: float = 150,
    daily_loss: float = 500,
    max_trades: int = 4,
    accounts: list[dict] | None = None,
) -> str:
    account_line = f"  account_id: {account_id}" if account_id else "  # account_id: 123456          # set by 'topstep-bot setup'"
    if accounts:
        account_line = "  # account_id: set per account in the 'accounts' list at the end of this file"
    return f"""# Topstep Bot configuration. Every setting has a safe default; see README.md for the full list.

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
  name: {strategy}                # adaptive (all strategies, all day, guided by what the bot has learned),
  params: {{}}                 #   or one of: orb, noise_breakout, ema_trend, vwap_reversion, vwap_pullback
                              # override strategy defaults under params, e.g. {{target_r: 1.5}}

knowledge:                      # what the bot learns while it runs (drives the adaptive strategy)
  auto_train: true              # retrain on the last 60 days of history at startup when stale (daily)
  history_days: 60

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
""" + render_accounts(accounts or [])


def render_accounts(accounts: list[dict]) -> str:
    """The 'accounts' section: one bot per account, each inheriting everything above unless it says otherwise."""
    if not accounts:
        return """
# Trading several accounts? List them here - see docs/HOW_TO_USE.md ('Trading several accounts').
# accounts:
#   - name: c1                  # short name for the dashboard and Telegram (/pause c1)
#     account_id: 111111
#   - name: c2
#     account_id: 222222
#     strategy: {name: orb}     # override anything that should differ; the rest is shared
#     risk: {risk_per_trade: 100}
"""
    lines = ["", "# One bot per account. Each account uses every setting above unless it overrides it here,",
             "# so accounts that don't override strategy/instrument take the same trade calls (each sized by its",
             "# own risk). 'follow: c1' copies c1's calls. See docs/HOW_TO_USE.md ('Trading several accounts').",
             "multi_account:", "  hedge_guard: correlated       # never hold opposite positions across accounts (Topstep rule)",
             "accounts:"]
    for a in accounts:
        lines.append(f"  - name: {a['name']}")
        lines.append(f"    account_id: {a['account_id']}" + (f"        # {a['label']}" if a.get("label") else ""))
        if a.get("login"):
            lines.append(f"    login: {a['login']}              # API key stored as TOPSTEPX_API_KEY_{a['login'].upper()}")
        for key in ("plan", "stage"):
            if a.get(key):
                lines.append(f'    {key}: "{a[key]}"' if key == "plan" else f"    {key}: {a[key]}")
        if a.get("strategy"):
            lines.append(f"    strategy: {{name: {a['strategy']}}}")
    return "\n".join(lines) + "\n"


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


def _pick_accounts(accounts: list) -> list:
    """Let the user choose one or several accounts (e.g. '2' or '1,3' or 'all')."""
    table = Table(show_header=False, box=None, padding=(0, 2))
    for i, a in enumerate(accounts, start=1):
        table.add_row(f"[bold]{i}[/]", a.name, f"balance ${a.balance:,.2f}", "can trade" if a.can_trade else "[red]NOT tradable[/]")
    console.print("\n[bold cyan]Which account(s) should the bot trade?[/]  Several = one bot per account, run together.")
    console.print(table)
    while True:
        answer = Prompt.ask("Numbers separated by commas, or 'all'", default="1").strip().lower()
        try:
            picks = range(1, len(accounts) + 1) if answer == "all" else sorted({int(x) for x in answer.split(",") if x.strip()})
            chosen = [accounts[i - 1] for i in picks]
        except (ValueError, IndexError):
            console.print("[red]Use numbers from the list, e.g. 1,3[/]")
            continue
        if chosen:
            return chosen


def _test_key(username: str, api_key: str) -> list | None:
    try:
        with console.status("Testing the key..."):
            return asyncio.run(_fetch_accounts(username, api_key))
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]Login failed:[/] {exc}")
        return None


def _add_other_logins(env_path: Path, main_user: str, accounts: list, other_logins: dict[int, str]) -> None:
    """Store API keys for other TopstepX logins and add their accounts to the list to choose from."""
    from topstep_bot.config import login_env_names, login_key

    while True:
        name = login_key(Prompt.ask("Short name for this login (e.g. second)", default="second"))
        username = Prompt.ask("Its TopstepX username").strip()
        api_key = Prompt.ask("Its API key (input hidden)", password=True).strip()
        found = _test_key(username, api_key) if username and api_key else None
        if found is not None:
            user_var, key_var = login_env_names(name)
            update_env_file(env_path, {user_var: username, key_var: api_key} if username != main_user else {key_var: api_key})
            known = {a.id for a in accounts}
            new = [a for a in found if a.id not in known]
            accounts.extend(new)
            other_logins.update({a.id: name for a in new})
            console.print(f"[green]Saved login '{name}'[/] with {len(found)} account(s).")
        if not Confirm.ask("Add another login?", default=False):
            return


def _offer_own_keys(env_path: Path, profiles: list[dict]) -> None:
    """Optionally give each account its own API key (stored under the account's name, used automatically)."""
    from topstep_bot.config import login_env_names

    console.print("\n[bold cyan]5c. API keys[/]  All accounts can share your one API key. A separate key per account "
                  "keeps their connections apart (one account's connection problem can't affect another).")
    if not Confirm.ask("Give each account its own API key?", default=False):
        return
    for prof in profiles:
        if prof.get("login"):
            continue
        api_key = Prompt.ask(f"API key for {prof['name']} ({prof['label']}) - Enter to use the main key",
                             password=True, default="", show_default=False).strip()
        if api_key and _test_key(_main_username(env_path), api_key) is not None:
            update_env_file(env_path, {login_env_names(prof["name"])[1]: api_key})
            console.print(f"[green]{prof['name']} will use its own key.[/]")


def _main_username(env_path: Path) -> str:
    for line in env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []:
        if line.startswith("TOPSTEPX_USERNAME="):
            return line.split("=", 1)[1].strip()
    return ""


def _account_profiles(chosen: list, plan: str, stage: str, strategy: str,
                      other_logins: dict[int, str] | None = None) -> list[dict]:
    """Names, plan/type guesses and (optionally) a strategy per account for the 'accounts' list."""
    from topstep_bot.preflight import account_hints

    console.print(
        "\n[bold cyan]5b. Same calls or different calls?[/]\n"
        "  [bold]Same[/]: every account trades the strategy above and takes the same calls at the same time\n"
        "        (each one sized and limited by its own risk rules).\n"
        "  [bold]Different[/]: choose a strategy per account, e.g. adaptive on one Combine and ORB on another.\n"
        "  Either way the bot never holds opposite positions on two accounts (Topstep forbids hedging)."
    )
    same = Confirm.ask("Should all these accounts take the same calls?", default=True)
    counts: dict[str, int] = {}
    profiles = []
    for a in chosen:
        hint_plan, hint_stage = account_hints(a.name)
        prefix = {"express": "xfa", "practice": "p"}.get(hint_stage or stage, "c")
        counts[prefix] = counts.get(prefix, 0) + 1
        prof = {"name": f"{prefix}{counts[prefix]}", "account_id": a.id, "label": a.name}
        if other_logins and a.id in other_logins:
            prof["login"] = other_logins[a.id]
        if hint_plan and hint_plan != plan:
            prof["plan"] = hint_plan
        if hint_stage and hint_stage != stage:
            prof["stage"] = hint_stage
        if not same:
            pick = _pick(f"Strategy for {prof['name']} ({a.name})",
                         [(cls.name, cls.title) for cls in STRATEGIES.values()], strategy)
            if pick != strategy:
                prof["strategy"] = pick
        profiles.append(prof)
    console.print("Accounts: " + ", ".join(f"[bold]{p['name']}[/] = {p['label']}" for p in profiles)
                  + "\nUse these names on the dashboard and in Telegram (e.g. /pause c1). Fine-tune each account "
                  "(risk, symbol...) in config.yaml - see 'topstep-bot accounts'.")
    return profiles


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
    chosen: list = []
    other_logins: dict[int, str] = {}  # account id -> stored login name, for accounts under another login
    if Confirm.ask("Enter API credentials now? (No = skip; you can still backtest on sample data)", default=True):
        username = Prompt.ask("TopstepX username").strip()
        api_key = Prompt.ask("API key (input hidden)", password=True).strip()
        update_env_file(env_path, {"TOPSTEPX_USERNAME": username, "TOPSTEPX_API_KEY": api_key})
        console.print(f"[green]Saved to {env_path}[/]")
        try:
            with console.status("Testing login..."):
                accounts = asyncio.run(_fetch_accounts(username, api_key))
            console.print(f"[green]Login OK.[/] Found {len(accounts)} active account(s).")
            if Confirm.ask("Do you also have accounts under another TopstepX login?", default=False):
                _add_other_logins(env_path, username, accounts, other_logins)
            if len(accounts) > 1:
                chosen = _pick_accounts(accounts)
                account_id = chosen[0].id if len(chosen) == 1 else None
            elif accounts:
                chosen, account_id = accounts, accounts[0].id
        except ProjectXError as exc:
            console.print(f"[red]Login failed:[/] {exc}\nCheck the username/API key; you can fix them in {env_path} later.")
        except Exception as exc:  # noqa: BLE001
            console.print(f"[red]Could not reach TopstepX:[/] {exc}")

    # ---- account
    plan = _pick(
        "2. Account size",
        [(p.name, f"profit target ${p.profit_target:,.0f}, max loss limit ${p.max_loss_limit:,.0f}, max {p.max_minis} minis") for p in PLANS.values()],
        "50K",
    )
    stage = _pick(
        "3. Account type",
        [("combine", "Trading Combine (evaluation)"), ("express", "Express Funded Account (XFA)"), ("practice", "Practice account")],
        "combine",
    )

    # ---- instrument & strategy
    symbol = _pick(
        "4. What to trade (micros are strongly recommended to start)",
        [(s, f"{SPECS[s].description} - ${SPECS[s].tick_value:g}/tick") for s in SYMBOL_CHOICES],
        "MNQ",
    )
    strategy = _pick("5. Strategy (adaptive is recommended: it trades all day and learns what works)",
                     [(cls.name, f"{cls.title}: {cls.description}") for cls in STRATEGIES.values()], "adaptive")
    profiles = _account_profiles(chosen, plan, stage, strategy, other_logins) if len(chosen) > 1 else []
    if len(profiles) > 1:
        _offer_own_keys(env_path, profiles)

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
            risk_per_trade=risk, daily_loss=daily, accounts=profiles,
        ),
        encoding="utf-8",
    )
    console.print(
        Panel.fit(
            f"[green bold]Saved {config_path}[/]\n\n"
            "Next steps:\n"
            "  1. [bold]Train[/] the bot (menu 5): it learns which strategy works when from recent real data.\n"
            "  2. [bold]Backtest[/] (menu 4) and read the report.\n"
            "  3. [bold]Start in paper mode[/] (menu 6): real prices, simulated orders, with the dashboard.\n"
            "  4. When you're ready, flip the dashboard's [bold]Paper | Live[/] switch - start small.",
            border_style="green",
        )
    )
    return True
