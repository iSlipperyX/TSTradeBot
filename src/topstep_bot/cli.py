"""Command-line interface. Run with no arguments for an interactive menu."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import webbrowser
from datetime import datetime, timedelta, timezone
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt
from rich.table import Table

from topstep_bot import __version__
from topstep_bot.config import BotConfig, Secrets, load_config, load_secrets

console = Console()
UTC = timezone.utc
log = logging.getLogger("topstep_bot")


# --------------------------------------------------------------------- helpers

def setup_logging(cfg: BotConfig, command: str, console_level: str | None = None) -> Path:
    """Log to files in cfg.log_dir (see logging_setup.py) and to this window."""
    from topstep_bot.logging_setup import log_startup
    from topstep_bot.logging_setup import setup_logging as _setup

    s = load_secrets()
    log_dir = _setup(
        cfg.log_dir, console_level or cfg.log_level, console=console, retention_days=cfg.log_retention_days,
        secrets=[s.api_key, s.telegram_bot_token, s.discord_webhook_url],
    )
    log_startup(cfg, command)
    return log_dir


def _load(args: argparse.Namespace) -> BotConfig:
    cfg = load_config(args.config)
    if getattr(args, "mode", None):
        cfg.mode = args.mode
    return cfg


def _client(cfg: BotConfig):
    from topstep_bot.api.rest import ProjectXClient

    secrets = load_secrets()
    if not secrets.has_credentials:
        console.print("[red]No TopstepX credentials found.[/] Run [bold]topstep-bot setup[/] first.")
        raise SystemExit(2)
    return ProjectXClient(secrets.username, secrets.api_key, cfg.api.base_url, cfg.api.timeout_seconds)


def _money(v: float) -> str:
    return f"-${abs(v):,.2f}" if v < 0 else f"${v:,.2f}"


# -------------------------------------------------------------------- commands

def cmd_setup(args: argparse.Namespace) -> int:
    from topstep_bot.wizard import run_wizard

    cfg_path = Path(args.config or "config.yaml")
    run_wizard(cfg_path, cfg_path.parent / ".env")
    return 0


def cmd_strategies(args: argparse.Namespace) -> int:
    from topstep_bot.strategies import STRATEGIES

    for cls in STRATEGIES.values():
        params = ", ".join(f"{k}={v}" for k, v in cls.defaults.items())
        console.print(Panel(f"{cls.description}\n\n[dim]Parameters: {params}[/]", title=f"[bold]{cls.name}[/] - {cls.title}"))
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    from topstep_bot.factory import fees_for
    from topstep_bot.live import resolve_contract
    from topstep_bot.risk.topstep import PLANS

    cfg = _load(args)

    async def go() -> None:
        async with _client(cfg) as client:
            with console.status("Logging in..."):
                accounts = await client.search_accounts(only_active=True)
            table = Table(title="Active accounts")
            for col in ("ID", "Name", "Balance", "Can trade", "Selected"):
                table.add_column(col)
            for a in accounts:
                table.add_row(str(a.id), a.name, _money(a.balance), "yes" if a.can_trade else "NO",
                              "◀" if a.id == cfg.account.account_id else "")
            console.print(table)
            contract = await resolve_contract(client, cfg)
            console.print(
                f"Contract: [bold]{contract.name}[/] ({contract.description}) id={contract.id} "
                f"tick {contract.tick_size} = ${contract.tick_value}, est. fees ${fees_for(cfg, contract):.2f}/round turn"
            )
    try:
        asyncio.run(go())
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]Check failed:[/] {exc}")
        return 1
    plan = PLANS[cfg.account.plan]
    console.print(
        f"[green]Connection OK.[/] Mode: [bold]{cfg.mode}[/]. Plan {plan.name} {cfg.account.stage}: "
        f"MLL ${plan.max_loss_limit:,.0f}, max {plan.max_minis} minis. Strategy: {cfg.strategy.name} "
        f"on {cfg.instrument.timeframe_minutes}m bars, risk ${cfg.risk.risk_per_trade:,.0f}/trade."
    )
    secrets = load_secrets()
    if secrets.telegram_bot_token and secrets.telegram_chat_id:
        state = "on" if cfg.telegram.control_enabled else "off (telegram.control_enabled: false)"
        console.print(f"Telegram alerts: on. Telegram remote control: {state}.")
    return 0


async def _download(cfg: BotConfig, days: int, tf: int) -> Path:
    from topstep_bot.backtest.data import save_csv
    from topstep_bot.live import resolve_contract
    from topstep_bot.models import BarUnit

    async with _client(cfg) as client:
        contract = await resolve_contract(client, cfg)
        end = datetime.now(UTC)
        with console.status(f"Downloading {days} days of {tf}-minute {contract.name} bars..."):
            bars = await client.retrieve_bars_range(
                contract.id, end - timedelta(days=days), end, BarUnit.MINUTE, tf, live=cfg.data.live_market_data
            )
    path = cfg.data_path / f"{cfg.instrument.symbol}_{tf}m.csv"
    save_csv(bars, path)
    console.print(f"[green]Saved {len(bars):,} bars to {path}[/]")
    return path


def cmd_download(args: argparse.Namespace) -> int:
    cfg = _load(args)
    asyncio.run(_download(cfg, args.days, args.tf))
    return 0


def cmd_backtest(args: argparse.Namespace) -> int:
    from topstep_bot.backtest.data import load_csv, synthetic_bars
    from topstep_bot.backtest.metrics import combine_statistics, compute_metrics
    from topstep_bot.backtest.report import write_report
    from topstep_bot.backtest.runner import run_backtest
    from topstep_bot.instruments import offline_contract
    from topstep_bot.risk.topstep import PLANS

    cfg = _load(args)
    if args.strategy:
        cfg.strategy.name = args.strategy
        cfg.strategy.params = {}
    if args.symbol:
        cfg.instrument.symbol = args.symbol.upper()
    if args.timeframe:
        cfg.instrument.timeframe_minutes = args.timeframe

    data_file = args.data or cfg.backtest.data_file
    synthetic = args.synthetic
    if not data_file and not synthetic:
        cached = cfg.data_path / f"{cfg.instrument.symbol}_1m.csv"
        if args.download or (not cached.exists() and load_secrets().has_credentials):
            data_file = str(asyncio.run(_download(cfg, args.days, 1)))
        elif cached.exists():
            data_file = str(cached)
        else:
            synthetic = True
    if synthetic:
        console.print(
            "[yellow]Using SYNTHETIC random data - good for seeing how the bot works, meaningless for judging a "
            "strategy. Run setup and 'topstep-bot download' to backtest on real data.[/]"
        )
        bars = synthetic_bars(cfg.instrument.symbol, days=args.days, seed=args.seed)
    else:
        bars = load_csv(data_file, naive_tz=args.tz)
        console.print(f"Loaded {len(bars):,} bars from {data_file}")

    contract = offline_contract(cfg.instrument.symbol)
    with console.status("Running backtest..."):
        res = asyncio.run(run_backtest(cfg, bars, contract))
    m = compute_metrics(res.trades, res.days, res.starting_balance)

    table = Table(title=f"Backtest: {cfg.strategy.name} on {contract.name} {cfg.instrument.timeframe_minutes}m "
                        f"({res.first_day} → {res.last_day})")
    table.add_column("Metric")
    table.add_column("Value", justify="right")
    pf = m["profit_factor"]
    rows = [
        ("Net P&L", _money(m["net_pnl"])),
        ("Trades", f"{m['trades']} ({m['longs']} long / {m['shorts']} short)"),
        ("Win rate", f"{m['win_rate'] * 100:.1f}%"),
        ("Profit factor", "∞" if pf == float("inf") else f"{pf:.2f}"),
        ("Avg R / trade", f"{m['avg_r']:.2f}"),
        ("Max drawdown", _money(m["max_drawdown"])),
        ("Best / worst day", f"{_money(m['best_day'])} / {_money(m['worst_day'])}"),
        ("Fees paid", _money(m["fees"])),
    ]
    if cfg.account.stage == "combine":
        cs = combine_statistics(res.days, PLANS[cfg.account.plan])
        first = cs["from_first_day"]
        if first:
            rows.append(("Combine (from day 1)", f"{first.status.upper()} - {first.detail}"))
        if cs["pass_rate"] is not None:
            rows.append(("Combine pass rate", f"{cs['pass_rate'] * 100:.0f}% of {cs['attempts']} simulated starts"))
    for row in rows:
        table.add_row(*row)
    console.print(table)
    if res.breaches:
        console.print(f"[red]Warning: the Maximum Loss Limit was touched {len(res.breaches)} time(s).[/]")
    path = write_report(res, cfg.backtest.report_dir)
    console.print(f"Report: [bold]{path.resolve()}[/]")
    if not args.no_open:
        webbrowser.open(path.resolve().as_uri())
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    from topstep_bot.dashboard import DashboardServer
    from topstep_bot.live import Controls, LiveRunner, SetupError

    cfg = _load(args)
    log_dir = setup_logging(cfg, f"run ({cfg.mode})")
    secrets = load_secrets()
    if cfg.mode == "live":
        console.print(
            Panel(
                "[bold red]LIVE MODE[/] - the bot will place REAL orders on your TopstepX account.\n"
                "Rule violations and losses are real. Make sure you have paper traded this exact configuration.\n"
                "Stop any time with Ctrl+C, the dashboard's Stop button, Telegram /stop, or by creating a file named KILL here.",
                border_style="red",
            )
        )
        if not args.yes and Prompt.ask("Type LIVE to continue") != "LIVE":
            console.print("Cancelled.")
            return 1

    controls = Controls()
    runner = LiveRunner(cfg, secrets, controls) if secrets.has_credentials else None
    if runner is None:
        console.print("[red]No TopstepX credentials found.[/] Run [bold]topstep-bot setup[/] first.")
        return 2
    dashboard: list[DashboardServer] = []
    telegram: list = []
    background: list[asyncio.Task] = []

    async def on_ready(core) -> None:
        from topstep_bot.control import BotActions

        actions = BotActions(core, controls)
        await _start_telegram(cfg, secrets, actions, telegram, background, announce=not runner.quiet_start)
        if args.no_dashboard or not cfg.dashboard.enabled:
            return
        server = DashboardServer(
            cfg.dashboard.host,
            cfg.dashboard.port,
            core.snapshot,
            {
                **{name: (lambda payload, fn=getattr(actions, name): fn("dashboard"))
                   for name in ("pause", "resume", "flatten", "stop")},
                "preview_setting": lambda p: actions.preview_setting(p.get("key", ""), p.get("value")),
                "set_setting": lambda p: actions.change_setting(p.get("key", ""), p.get("value"), "dashboard"),
                "reset_settings": lambda p: actions.reset_settings("dashboard"),
                "take_idea": lambda p: actions.take_idea(str(p.get("id", "")), "dashboard",
                                                         int(p["size"]) if p.get("size") else None),
            },
            log_stats=True,
        )
        try:
            await server.start()
        except OSError as exc:
            log.warning("Dashboard could not start on port %s: %s", cfg.dashboard.port, exc)
            return
        dashboard.append(server)
        console.print(f"Dashboard: [bold]{server.url}[/]")
        if cfg.dashboard.open_browser and not args.no_browser:
            webbrowser.open(server.url)

    async def main() -> None:
        try:
            await runner.run(on_ready)
        finally:
            for task in background:
                task.cancel()
            for controller in telegram:
                if not controls.restart_requested:
                    await controller.send("⏹ Topstep Bot has stopped. Restart it on your PC to resume.")
                await controller.close()
            for server in dashboard:
                await server.stop()

    from topstep_bot.keepawake import keep_awake
    from topstep_bot.service import RESTART_EXIT_CODE

    try:
        with keep_awake(cfg.service.keep_awake):
            asyncio.run(main())
    except SetupError as exc:
        log.error("Could not start: %s", exc)
        return 2
    except KeyboardInterrupt:
        controls.stop_reason = controls.stop_reason or "Ctrl+C"
        console.print("Stopped.")
    except Exception:
        log.critical("The bot crashed. Details are in %s (errors.log and crash_*.txt)", log_dir, exc_info=True)
        raise
    log.info("Bot exited. Reason: %s", controls.stop_reason or "normal stop")
    console.print(f"Stopped ({controls.stop_reason or 'normal stop'}). Logs: {log_dir}")
    return RESTART_EXIT_CODE if controls.restart_requested else 0


async def _start_telegram(
    cfg: BotConfig, secrets: Secrets, actions, telegram: list, background: list, announce: bool = True
) -> None:
    """Start two-way Telegram control if a bot token and chat ID are configured."""
    if not (cfg.telegram.control_enabled and secrets.telegram_bot_token and secrets.telegram_chat_id):
        return
    from topstep_bot.telegram_control import TelegramController

    controller = TelegramController(secrets.telegram_bot_token, secrets.telegram_chat_id, actions, cfg.telegram)
    try:
        await controller.start(announce=announce)
    except Exception as exc:  # noqa: BLE001 - trading continues without remote control
        log.warning("Telegram control could not start: %s", exc)
        await controller.close()
        return
    telegram.append(controller)
    from topstep_bot.logging_setup import spawn

    background.append(spawn(controller.run(), name="telegram"))
    console.print("Telegram control: [bold]on[/] - send /help to your bot")


def _confirm_live(cfg: BotConfig, args: argparse.Namespace, what: str) -> bool:
    if cfg.mode != "live" or getattr(args, "yes", False):
        return True
    console.print(Panel(f"[bold red]LIVE MODE[/] - {what} will place REAL orders on your TopstepX account, unattended.",
                        border_style="red"))
    return Prompt.ask("Type LIVE to continue") == "LIVE"


def cmd_service(args: argparse.Namespace) -> int:
    """Run the bot 24/7 on this computer: auto-restart, keep awake, daily maintenance restart."""
    from topstep_bot.service import run_service

    cfg = _load(args)
    if not _confirm_live(cfg, args, "the 24/7 service"):
        console.print("Cancelled.")
        return 1
    return run_service(cfg, load_secrets(), config_path=args.config, mode=args.mode)


def cmd_autostart(args: argparse.Namespace) -> int:
    from topstep_bot import autostart

    if args.action == "status":
        try:
            enabled = autostart.is_enabled()
        except OSError as exc:
            console.print(str(exc))
            return 1
        console.print(f"Automatic start at Windows sign-in: [bold]{'ON' if enabled else 'off'}[/]"
                      + (f" ({autostart.script_path()})" if enabled else ""))
        return 0
    if args.action == "off":
        console.print("Automatic start removed." if autostart.disable() else "Automatic start was not enabled.")
        return 0
    cfg_path = Path(args.config or "config.yaml")
    if not cfg_path.exists():
        console.print("[red]No config.yaml yet.[/] Run setup first.")
        return 2
    cfg = _load(args)
    if not _confirm_live(cfg, args, "starting automatically at every sign-in"):
        console.print("Cancelled.")
        return 1
    try:
        path = autostart.enable(cfg_path)
    except OSError as exc:
        console.print(f"[red]{exc}[/]")
        return 1
    console.print(f"[green]Automatic start enabled.[/] The 24/7 service ({cfg.mode} mode) will start about 30s after "
                  f"you sign in to Windows.\nStartup script: {path}\nTurn off with: topstep-bot autostart off")
    return 0


def _print_preflight(report) -> None:
    from topstep_bot.preflight import FAIL, INFO, OK, WARN

    icons = {OK: "[green]✔[/]", WARN: "[yellow]![/]", FAIL: "[red]✘[/]", INFO: "[cyan]i[/]"}
    table = Table(title="Preflight check", show_lines=False)
    table.add_column("")
    table.add_column("Check")
    table.add_column("Result")
    for c in report.checks:
        table.add_row(icons[c.status], c.name, c.detail)
    console.print(table)
    if report.strategy_rows:
        st = Table(title="Strategies on recent real data (same risk settings)")
        for col in ("Strategy", "Net", "Trades", "Win%", "PF", "Max DD", "Pass", "MLL"):
            st.add_column(col, justify="left" if col == "Strategy" else "right", no_wrap=True)
        for r in report.strategy_rows:
            name = f"{r['name']}{' ◀' if r['configured'] else ''}"
            if "error" in r:
                st.add_row(name, r["error"], "", "", "", "", "", "")
                continue
            pf = "∞" if r["pf"] == float("inf") else f"{r['pf']:.2f}"
            st.add_row(name, f"{r['net']:+,.0f}", str(r["trades"]), f"{r['win_rate'] * 100:.0f}", pf, f"{r['max_dd']:,.0f}",
                       "-" if r["pass_rate"] is None else f"{r['pass_rate'] * 100:.0f}%", str(r["breaches"]))
        console.print(st)
        console.print("[dim]◀ = configured. Net and Max DD in $. Pass = Combine pass rate. MLL = times the Max Loss Limit was touched.\n"
                      "Past results on recent data are a sanity check, not a forecast. Switching to whichever "
                      "strategy did best last quarter is a common way to lose money.[/]")
    colour = {"READY": "green", "READY WITH WARNINGS": "yellow", "NOT READY": "red"}[report.verdict]
    console.print(Panel(f"[bold {colour}]{report.verdict}[/]  ({report.failures} problem(s), {report.warnings} warning(s))",
                        border_style=colour))


def _run_preflight(cfg: BotConfig, args: argparse.Namespace):
    from topstep_bot.preflight import run_preflight

    with console.status("Running preflight checks...") as status:
        def ask(prompt: str) -> str:
            status.stop()
            try:
                return Prompt.ask(prompt, default="")
            finally:
                status.start()

        return asyncio.run(run_preflight(
            cfg, load_secrets(), ask_mll=ask, backtest_days=args.days,
            run_backtests=not args.skip_backtest, progress=lambda m: status.update(f"{m}..."),
        ))


def cmd_preflight(args: argparse.Namespace) -> int:
    """Check everything the bot needs to know before trading live."""
    cfg = _load(args)
    report = _run_preflight(cfg, args)
    _print_preflight(report)
    return 1 if report.failures else 0


def cmd_go_live(args: argparse.Namespace) -> int:
    """Same-day start: preflight, then launch live trading."""
    cfg = _load(args)
    cfg.mode = "live"
    console.print(Panel(
        "Starting today without paper trading. To protect you while the bot proves itself, it will:\n"
        f"  - risk {cfg.risk.ramp_up_risk_fraction:.0%} of your normal risk for the first {cfg.risk.ramp_up_days} live day(s)\n"
        "  - keep every Topstep and personal loss limit active, and skip high-impact news\n"
        "Watch the first trades on the dashboard or Telegram, with TopstepX open alongside.",
        title="Start trading today", border_style="cyan"))
    report = _run_preflight(cfg, args)
    _print_preflight(report)
    if report.failures:
        console.print("[red]Fix the problems marked ✘ and run this again.[/]")
        return 1
    if Prompt.ask(f"Start LIVE trading on {report.account_name}? Type LIVE", default="") != "LIVE":
        console.print("Cancelled - nothing was started.")
        return 1
    how = Prompt.ask("Run 24/7 with auto-restart (recommended), or just this session?", choices=["24/7", "session"], default="24/7")
    if how == "24/7":
        from topstep_bot.service import run_service

        return run_service(cfg, load_secrets(), config_path=args.config, mode="live")
    run_args = build_parser().parse_args((["-c", args.config] if args.config else []) + ["run", "--mode", "live", "--yes"])
    return cmd_run(run_args)


def cmd_logs(args: argparse.Namespace) -> int:
    """Show recent problems, open the log folder, or zip logs for support."""
    import os
    import zipfile

    from topstep_bot.logging_setup import tail

    cfg = _load(args)
    log_dir = Path(cfg.log_dir)
    if not log_dir.exists():
        console.print(f"No logs yet ({log_dir}). They are created the first time the bot runs.")
        return 0
    if args.open:
        if sys.platform == "win32":
            os.startfile(log_dir)  # noqa: S606 - opens the folder in Explorer
        else:
            webbrowser.open(log_dir.resolve().as_uri())
        return 0
    if args.bundle:
        path = log_dir / f"support_bundle_{datetime.now():%Y%m%d_%H%M%S}.zip"
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
            for f in sorted(log_dir.iterdir()):
                if f.is_file() and f.suffix != ".zip":
                    zf.write(f, f.name)
            cfg_file = Path(args.config or "config.yaml")
            if cfg_file.exists():  # config.yaml holds no secrets (they live in .env, which is never included)
                zf.write(cfg_file, "config.yaml")
        console.print(f"[green]Support bundle created:[/] {path}\nIt contains logs and config.yaml - never your .env.")
        return 0
    crashes = sorted(log_dir.glob("crash_*.txt"))
    if crashes:
        console.print(f"[red]{len(crashes)} crash report(s).[/] Latest: {crashes[-1]}")
        console.print("".join(tail(crashes[-1], 25)))
    errors = tail(log_dir / "errors.log", args.lines)
    console.print(Panel("".join(errors) or "No warnings or errors logged.", title="Recent warnings & errors (errors.log)"))
    if args.all:
        console.print(Panel("".join(tail(log_dir / "bot.log", args.lines)), title="Recent activity (bot.log)"))
    console.print(f"[dim]Log folder: {log_dir}  -  'logs --open' opens it, 'logs --bundle' zips it for support.[/]")
    return 0


def cmd_flatten(args: argparse.Namespace) -> int:
    """Emergency: cancel all open orders and close all positions on the configured account."""
    from topstep_bot.live import select_account

    cfg = _load(args)

    async def go() -> None:
        async with _client(cfg) as client:
            account = await select_account(client, cfg)
            positions = await client.search_open_positions(account.id)
            orders = await client.search_open_orders(account.id)
            console.print(f"Account {account.name}: {len(positions)} open position(s), {len(orders)} open order(s).")
            if not positions and not orders:
                return
            if not args.yes and Prompt.ask("Type FLATTEN to close everything") != "FLATTEN":
                console.print("Cancelled.")
                return
            for order in orders:
                await client.cancel_order(account.id, order.id)
            for pos in positions:
                await client.close_position(account.id, pos.contract_id)
            console.print("[green]Flatten requests sent.[/] Verify in TopstepX.")

    asyncio.run(go())
    return 0


def cmd_journal(args: argparse.Namespace) -> int:
    from topstep_bot.journal import Journal

    cfg = _load(args)
    if not cfg.journal_path.exists():
        console.print(f"No journal yet ({cfg.journal_path}).")
        return 0
    journal = Journal(cfg.journal_path)
    days = journal.daily()[-10:]
    table = Table(title=f"Daily results ({cfg.mode})")
    for col in ("Day", "Account", "Trades", "P&L", "End balance", "MLL floor"):
        table.add_column(col)
    for d in days:
        table.add_row(d["trading_day"], d["account"], str(d["trades"]), _money(d["net_pnl"]),
                      _money(d["end_balance"]), _money(d["mll_floor"]))
    console.print(table)
    trades = Table(title="Recent trades")
    for col in ("Exit time", "Side", "Qty", "Entry", "Exit", "Net", "Exit reason"):
        trades.add_column(col)
    for t in journal.trades(limit=args.limit):
        trades.add_row((t["exit_time"] or "")[:16], t["side"], str(t["size"]), str(t["entry_price"]),
                       str(t["exit_price"]), _money(t["net_pnl"] or 0), t["exit_reason"] or "")
    console.print(trades)
    journal.close()
    return 0


def cmd_demo(args: argparse.Namespace) -> int:
    args.synthetic, args.data, args.download = True, None, False
    return cmd_backtest(args)


def cmd_telegram_test(args: argparse.Namespace) -> int:
    """Check the Telegram bot token and chat ID by sending a test message."""
    from topstep_bot.telegram_control import KEYBOARD, TelegramController, help_text

    cfg = _load(args)
    secrets = load_secrets()
    if not (secrets.telegram_bot_token and secrets.telegram_chat_id):
        console.print("[red]TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are not set.[/] Run setup or add them to .env.")
        return 2

    async def go() -> bool:
        controller = TelegramController(secrets.telegram_bot_token, secrets.telegram_chat_id, None, cfg.telegram)  # type: ignore[arg-type]
        try:
            me = await controller._call("getMe")
            sent = await controller.send(
                "✅ Test message from Topstep Bot. While the bot is running you can control it from this chat.\n\n"
                + help_text(),
                KEYBOARD,
            )
            if sent is None:
                console.print(f"[red]Bot @{me.get('username')} could not message chat {secrets.telegram_chat_id}.[/] "
                              "Send your bot a message first, and check TELEGRAM_CHAT_ID.")
                return False
            console.print(f"[green]Bot @{me.get('username')} sent a test message to your chat.[/]")
            return True
        finally:
            await controller.close()

    try:
        return 0 if asyncio.run(go()) else 1
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]Telegram test failed:[/] {exc}")
        return 1


# ------------------------------------------------------------------------ menu

MENU = [
    ("setup", "Set up / change settings (API key, account, strategy, risk, Telegram)"),
    ("go-live", "START TRADING TODAY: run all preflight checks, then go live"),
    ("check", "Test the connection to TopstepX"),
    ("backtest", "Backtest the configured strategy and open the report"),
    ("paper", "Paper trade (real prices, simulated orders) with the dashboard"),
    ("live", "Trade LIVE on your TopstepX account"),
    ("flatten", "EMERGENCY: close all positions and cancel all orders"),
    ("journal", "Show recent trades and daily results"),
    ("strategies", "Describe the available strategies"),
    ("demo", "Quick demo backtest on synthetic data (no account needed)"),
    ("telegram-test", "Send a test message to your Telegram bot"),
    ("service", "Run 24/7 (auto-restart, keeps the PC awake, daily maintenance restart)"),
    ("autostart", "Start the 24/7 service automatically when Windows starts"),
    ("logs", "Show recent errors and where the log files are"),
]


def interactive_menu(parser: argparse.ArgumentParser) -> int:
    console.print(Panel.fit(f"[bold]Topstep Bot[/] v{__version__}", border_style="cyan"))
    if not Path("config.yaml").exists():
        console.print("[yellow]No config.yaml yet - start with option 1 (setup), or 10 for a demo.[/]")
    for i, (_, desc) in enumerate(MENU, start=1):
        console.print(f"  [bold]{i}[/]  {desc}")
    console.print("  [bold]0[/]  Quit")
    choice = Prompt.ask("Choose", choices=[str(i) for i in range(len(MENU) + 1)], default="0")
    if choice == "0":
        return 0
    name = MENU[int(choice) - 1][0]
    argv = {"paper": ["run", "--mode", "paper"], "live": ["run", "--mode", "live"],
            "autostart": ["autostart", "on"]}.get(name, [name])
    args = parser.parse_args(argv)
    return args.func(args)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="topstep-bot", description="Automated trading bot for Topstep (TopstepX API).")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("-c", "--config", default=None, help="config file (default: config.yaml)")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("setup", help="interactive setup wizard").set_defaults(func=cmd_setup)
    sub.add_parser("check", help="test credentials, list accounts, resolve the contract").set_defaults(func=cmd_check)
    sub.add_parser("strategies", help="list strategies and their parameters").set_defaults(func=cmd_strategies)
    sub.add_parser("telegram-test", help="send a test message to your Telegram bot").set_defaults(func=cmd_telegram_test)

    p = sub.add_parser("run", help="start the bot (paper or live)")
    p.add_argument("--mode", choices=["paper", "live"], help="override mode from config")
    p.add_argument("--yes", action="store_true", help="skip the live-mode confirmation prompt")
    p.add_argument("--no-dashboard", action="store_true", help="don't start the web dashboard")
    p.add_argument("--no-browser", action="store_true", help="don't open the dashboard in a browser")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("service", help="run 24/7: auto-restart, keep awake, daily maintenance restart")
    p.add_argument("--mode", choices=["paper", "live"], help="override mode from config")
    p.add_argument("--yes", action="store_true", help="skip the live-mode confirmation prompt")
    p.set_defaults(func=cmd_service)

    p = sub.add_parser("autostart", help="start the 24/7 service when you sign in to Windows")
    p.add_argument("action", choices=["on", "off", "status"])
    p.add_argument("--yes", action="store_true", help="skip the live-mode confirmation prompt")
    p.set_defaults(func=cmd_autostart)

    for name, func, helptext in (("backtest", cmd_backtest, "backtest a strategy"), ("demo", cmd_demo, "demo backtest on synthetic data")):
        p = sub.add_parser(name, help=helptext)
        p.add_argument("--data", help="CSV of bars (time, open, high, low, close[, volume])")
        p.add_argument("--tz", default="UTC", help="timezone for CSV times without one (e.g. America/Chicago)")
        p.add_argument("--download", action="store_true", help="download fresh history from TopstepX first")
        p.add_argument("--synthetic", action="store_true", help="use synthetic random data")
        p.add_argument("--days", type=int, default=90, help="days of history to download/generate (default 90)")
        p.add_argument("--seed", type=int, default=7, help="random seed for synthetic data")
        p.add_argument("--strategy", help="override the strategy (uses its default parameters)")
        p.add_argument("--symbol", help="override the symbol")
        p.add_argument("--timeframe", type=int, help="override the bar timeframe in minutes")
        p.add_argument("--no-open", action="store_true", help="don't open the report in a browser")
        p.set_defaults(func=func)

    for name, func, helptext in (
        ("preflight", cmd_preflight, "check everything the bot needs before trading live"),
        ("go-live", cmd_go_live, "same-day start: preflight checks, then live trading"),
    ):
        p = sub.add_parser(name, help=helptext)
        p.add_argument("--days", type=int, default=90, help="days of real data for the strategy check")
        p.add_argument("--skip-backtest", action="store_true", help="skip the strategy check")
        p.set_defaults(func=func)

    p = sub.add_parser("download", help="download historical bars to a CSV")
    p.add_argument("--days", type=int, default=90)
    p.add_argument("--tf", type=int, default=1, help="bar size in minutes (default 1)")
    p.set_defaults(func=cmd_download)

    p = sub.add_parser("flatten", help="EMERGENCY: close all positions and cancel all orders")
    p.add_argument("--yes", action="store_true")
    p.set_defaults(func=cmd_flatten)

    p = sub.add_parser("logs", help="show recent errors, open the log folder, or zip logs for support")
    p.add_argument("--lines", type=int, default=40)
    p.add_argument("--all", action="store_true", help="also show recent general activity")
    p.add_argument("--open", action="store_true", help="open the log folder")
    p.add_argument("--bundle", action="store_true", help="zip the logs (and config.yaml, never .env) for support")
    p.set_defaults(func=cmd_logs)

    p = sub.add_parser("journal", help="show recent trades and daily results")
    p.add_argument("--mode", choices=["paper", "live"])
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=cmd_journal)
    return parser


def main(argv: list[str] | None = None) -> int:
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        try:
            return interactive_menu(parser)
        except KeyboardInterrupt:
            return 0
    if args.command not in ("run", "logs", "service"):
        try:
            setup_logging(_load(args), args.command, console_level="WARNING")
        except Exception:  # noqa: BLE001 - never block a command because logging failed
            logging.basicConfig(level=logging.WARNING)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        return 130
    except FileNotFoundError as exc:
        console.print(f"[red]{exc}[/]")
        return 2
    except ValueError as exc:
        console.print(f"[red]Configuration problem:[/] {exc}")
        return 2
