"""Command-line interface.

With no arguments (double-clicking start.bat) it starts the server: the dashboard opens in your
browser and everything else - setup, paper or live, start and stop - happens there. The text
menu from earlier versions is still here: ``topstep-bot menu`` (start.bat menu)."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import sys
import webbrowser
from datetime import datetime, timedelta, timezone
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.table import Table

from topstep_bot import __version__
from topstep_bot.config import BotConfig, ConfigError, load_config, load_secrets

console = Console()
UTC = timezone.utc
log = logging.getLogger("topstep_bot")
LOG_DIR = Path("logs")


# --------------------------------------------------------------------- helpers

def setup_logging(cfg: BotConfig, command: str, console_level: str | None = None, file_prefix: str = "bot") -> Path:
    """Log to files in cfg.log_dir (see logging_setup.py) and to this window."""
    from topstep_bot.logging_setup import log_startup
    from topstep_bot.logging_setup import setup_logging as _setup

    s = load_secrets()
    log_dir = _setup(
        cfg.log_dir, console_level or cfg.log_level, console=console, retention_days=cfg.log_retention_days,
        secrets=[s.api_key, s.telegram_bot_token, s.discord_webhook_url, s.github_token], file_prefix=file_prefix,
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
        console.print("[red]No TopstepX credentials found.[/] Add them on the dashboard's Setup tab "
                      "(double-click start.bat), or run [bold]start.bat setup[/].")
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


def cmd_rules(args: argparse.Namespace) -> int:
    from topstep_bot.risk.summary import rule_rows

    cfg = _load(args)
    table = Table(title=f"Topstep rules for your {cfg.account.plan} {cfg.account.stage} account", show_lines=True)
    table.add_column("Rule", style="bold")
    table.add_column("Topstep")
    table.add_column("What the bot does", style="green")
    for row in rule_rows(cfg):
        table.add_row(*row)
    console.print(table)
    console.print("[dim]Checked against the Topstep Help Center in October 2026 - confirm in your TopstepX Risk Settings. "
                  "Details: docs/TOPSTEP_RULES.md[/]")
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


def _history(cfg: BotConfig, args: argparse.Namespace, *, allow_synthetic: bool = True):
    """Price history for a backtest or training run: --data file, cached download, fresh download from
    TopstepX, or (only if allowed) synthetic data. Returns (bars, synthetic?)."""
    from topstep_bot.backtest.data import load_csv, synthetic_bars

    data_file = args.data or cfg.backtest.data_file
    synthetic = args.synthetic
    if not data_file and not synthetic:
        cached = cfg.data_path / f"{cfg.instrument.symbol}_1m.csv"
        if args.download or (not cached.exists() and load_secrets().has_credentials):
            data_file = str(asyncio.run(_download(cfg, args.days, 1)))
        elif cached.exists():
            data_file = str(cached)
        elif allow_synthetic:
            synthetic = True
        else:
            raise ValueError("No price history yet. Run setup (API key) and then: topstep-bot download --days 365")
    if synthetic:
        console.print(
            "[yellow]Using SYNTHETIC random data - good for seeing how the bot works, meaningless for judging a "
            "strategy. Run setup and 'topstep-bot download' to use real data.[/]"
        )
        return synthetic_bars(cfg.instrument.symbol, days=args.days, seed=args.seed), True
    bars = load_csv(data_file, naive_tz=args.tz)
    if bars:
        console.print(f"Loaded {len(bars):,} bars from {data_file} ({bars[0].ts:%Y-%m-%d} to {bars[-1].ts:%Y-%m-%d})")
    return bars, False


def _apply_overrides(cfg: BotConfig, args: argparse.Namespace) -> None:
    if getattr(args, "strategy", None):
        cfg.strategy.name = args.strategy
        cfg.strategy.params = {}
    if getattr(args, "symbol", None):
        cfg.instrument.symbol = args.symbol.upper()
    if getattr(args, "timeframe", None):
        cfg.instrument.timeframe_minutes = args.timeframe


def cmd_backtest(args: argparse.Namespace) -> int:
    from topstep_bot.backtest.metrics import combine_statistics, compute_metrics
    from topstep_bot.backtest.report import write_report
    from topstep_bot.backtest.runner import run_backtest
    from topstep_bot.instruments import offline_contract
    from topstep_bot.risk.topstep import PLANS

    cfg = _load(args)
    _apply_overrides(cfg, args)
    bars, _ = _history(cfg, args)

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


def _load_bars(cfg: BotConfig, args: argparse.Namespace):
    """Bars for training/backtesting: --data, else a fresh download (with credentials), else the cached CSV."""
    from topstep_bot.backtest.data import load_csv

    if args.data:
        bars = load_csv(args.data, naive_tz=args.tz)
        console.print(f"Loaded {len(bars):,} bars from {args.data}")
        return bars
    cached = cfg.data_path / f"{cfg.instrument.symbol}_1m.csv"
    if load_secrets().has_credentials:
        return load_csv(asyncio.run(_download(cfg, args.days, 1)))
    if cached.exists():
        console.print(f"[yellow]No credentials - using cached history {cached}[/]")
        return load_csv(cached)
    raise SystemExit("No history available: run setup (for downloads) or pass --data <csv>.")


def cmd_train(args: argparse.Namespace) -> int:
    """Teach the bot which strategy works at which time of day, from recent real data."""
    from topstep_bot.instruments import offline_contract
    from topstep_bot.knowledge import KnowledgeBase, train_from_bars
    from topstep_bot.strategies import BASE_STRATEGIES, STRATEGIES

    cfg = _load(args)
    args.days = args.days or cfg.knowledge.history_days
    bars = _load_bars(cfg, args)
    contract = offline_contract(cfg.instrument.symbol)
    kb = KnowledgeBase.from_config(cfg, cfg.knowledge_path)
    with console.status("Replaying every strategy through the history...") as status:
        result = asyncio.run(train_from_bars(cfg, contract, bars, kb, progress=lambda f: status.update(f"Training... {f:.0%}")))
    console.print(f"[green]Trained on {result['days']} trading days ({result['from']} to {result['to']}, "
                  f"{result['bars']:,} bars): {result['observations']} observations.[/]")
    print_knowledge_table(kb.summary([(n, STRATEGIES[n].title) for n in BASE_STRATEGIES]),
                          f"{contract.name} {cfg.instrument.timeframe_minutes}m")
    console.print(f"[dim]Saved to {cfg.knowledge_path}. The running bot keeps adding what it sees and retrains daily.[/]")
    return 0


def print_knowledge_table(summary: dict, what: str) -> None:
    from topstep_bot.knowledge import REGIMES, SLOT_NAMES

    table = Table(title=f"What works when on {what}  (average R per signal, sample size)", show_lines=True)
    table.add_column("Strategy", no_wrap=True)
    for sl in SLOT_NAMES:
        table.add_column(sl, justify="left", no_wrap=True)
    table.add_column("overall", justify="right", no_wrap=True)
    for row in summary["strategies"]:
        cells = []
        for sl in SLOT_NAMES:
            lines = []
            for rg in REGIMES:
                c = row["cells"][f"{sl}|{rg}"]
                mark = "[green]✔[/]" if c["allowed"] else ("[red]✘[/]" if c["level"] != "unproven" else "[dim]?[/]")
                lines.append(f"{mark} {rg[:4]} {c['mean_r']:+.2f} ({c['cell_n']})" if c["cell_n"] else f"[dim]- {rg[:4]}[/]")
            cells.append("\n".join(lines))
        o = row["overall"]
        table.add_row(row["title"], *cells, f"{o['mean_r']:+.2f} ({o['n']})")
    console.print(table)
    console.print("[dim]✔ = the adaptive strategy trades this strategy then, ✘ = switched off (losing), ? = not enough evidence. "
                  "calm/vola = volatility regime. Chicago time: open 08:30-10:00, midday 10:00-13:00, close 13:00-15:10.[/]")


def cmd_insights(args: argparse.Namespace) -> int:
    """What the bot has learned: results after costs, real fills against simulated ones, market conditions."""
    from topstep_bot.insights import build_report, execution_line, export_csv, hint_line
    from topstep_bot.knowledge import KnowledgeBase
    from topstep_bot.strategies import BASE_STRATEGIES, STRATEGIES

    cfg = _load(args)
    path = cfg.knowledge_path
    if not path.exists():
        console.print(f"[yellow]No knowledge base yet ({path}). Run train (menu 5) first, or let the bot run.[/]")
        return 1
    kb = KnowledgeBase.from_config(cfg, path)
    if args.csv:
        out = Path(args.csv)
        n = export_csv(kb, out)
        console.print(f"[green]Saved {n:,} observations to {out.resolve()}[/] - opens in Excel, one row per signal or trade.")
        return 0
    rep = build_report(kb, [(n, STRATEGIES[n].title) for n in BASE_STRATEGIES], slippage_ticks=cfg.risk.slippage_ticks)
    cov = rep["coverage"]
    console.print(f"[bold]What the bot learned[/] on {cfg.instrument.symbol} {cfg.instrument.timeframe_minutes}m: "
                  f"{cov['total']:,} observations ({cov['first_day']} to {cov['last_day']}), {cov['with_context']:,} with "
                  f"market context, {cov['with_path']:,} with their price path.")

    def r(v: float | None) -> str:
        return "-" if v is None else f"[{'green' if v > 0 else 'red' if v < 0 else 'white'}]{v:+.2f}[/]"

    table = Table(title="Average signal, in R (1R = what the trade risked)", show_lines=False)
    for col, just in (("Strategy", "left"), ("Signals", "right"), ("Won", "right"), ("Before costs", "right"),
                      ("Costs", "right"), ("After costs", "right"), ("Best point", "right"), ("Worst point", "right"),
                      ("Losers up 1R first", "right"), ("Real trades", "right")):
        table.add_column(col, justify=just, no_wrap=True)
    rows = sorted([*rep["strategies"], *([rep["manual"]] if rep["manual"] else [])], key=lambda x: x["net_r"] or -9, reverse=True)
    for row in rows:
        gave = "-" if row["gave_back"] is None else f"{row['gave_back']:.0%} of {row['n_losers']}"
        real = f"{r(row['real_net_r'])} ({row['real_n']})" if row["real_n"] else "-"
        table.add_row(row["title"], str(row["n"]), f"{row['win_rate']:.0%}" if row["win_rate"] is not None else "-",
                      r(row["gross_r"]), "-" if row["cost_r"] is None else f"{row['cost_r']:.2f}", r(row["net_r"]),
                      r(row["mfe_r"]), r(row["mae_r"]), gave, real)
    console.print(table)
    console.print("[dim]Best / worst point: how far a signal went for and against it on average before it ended. "
                  "Costs are fees plus the assumed slippage for ideas, fees for real fills (their slippage is in the prices). "
                  "Observations recorded before costs were measured count before costs.[/]")
    ex = execution_line(rep["execution"])
    console.print(ex or "[dim]No real fills with slippage data yet - they appear after the bot's first trades.[/]")
    if rep["hints"]:
        console.print("\n[bold]Conditions worth testing[/] [dim](hints, not proven: with this many comparisons some are luck)[/]")
        for h in rep["hints"]:
            console.print(f"  - {hint_line(h)}")
    else:
        console.print("[dim]Not enough observations with market context yet to compare conditions - they build up as the bot runs "
                      "and with each retraining.[/]")
    console.print("[dim]Export everything to Excel: topstep-bot insights --csv knowledge.csv[/]")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    """The trading bot itself, without a dashboard. Normally launched by the controller ('start')."""
    import os

    from topstep_bot.live import Controls, LiveRunner, SetupError
    from topstep_bot.service import RESTART_EXIT_CODE, write_exit_note

    cfg = _load(args)
    log_dir = setup_logging(cfg, f"bot ({cfg.mode})")
    secrets = load_secrets()
    supervised = os.environ.get("TOPSTEP_BOT_SUPERVISED") == "1"
    if cfg.mode == "live" and not args.yes and not supervised:
        console.print(
            Panel(
                "[bold red]LIVE MODE[/] - the bot will place REAL orders on your TopstepX account.\n"
                "Rule violations and losses are real. Run 'preflight' first (menu: Start trading today) and watch the\n"
                "first trades with TopstepX open alongside.\n"
                "Stop any time with Ctrl+C, the dashboard's Stop button, Telegram /stop, or by creating a file named KILL here.",
                border_style="red",
            )
        )
        if Prompt.ask("Type LIVE to continue") != "LIVE":
            console.print("Cancelled.")
            return 1
    if not secrets.has_credentials:
        console.print("[red]No TopstepX credentials found.[/] Add them on the dashboard's Setup tab first.")
        write_exit_note(2, "no TopstepX login yet - add it on the Setup tab")
        return 2

    controls = Controls()
    runner = LiveRunner(cfg, secrets, controls)
    servers: list = []

    async def on_ready(core) -> None:
        from topstep_bot.control import BotActions
        from topstep_bot.worker_api import start_worker_api

        server = await start_worker_api(BotActions(core, controls, retrain=runner.retrain), core.snapshot)
        if server:
            servers.append(server)

    async def main() -> None:
        try:
            await runner.run(on_ready)
        finally:
            for server in servers:
                await server.stop()

    from topstep_bot.keepawake import console_stays_responsive, keep_awake

    try:
        with keep_awake(cfg.service.keep_awake), console_stays_responsive():
            asyncio.run(main())
    except SetupError as exc:
        log.error("Could not start: %s", exc)
        write_exit_note(2, str(exc))
        return 2
    except KeyboardInterrupt:
        controls.stop_reason = controls.stop_reason or "Ctrl+C"
    except Exception as exc:
        log.critical("The bot crashed. Details are in %s (errors.log and crash_*.txt)", log_dir, exc_info=True)
        write_exit_note(1, f"crashed: {type(exc).__name__}: {exc}")
        raise
    reason = controls.stop_reason or "normal stop"
    log.info("Bot exited. Reason: %s", reason)
    if controls.failure:
        # Exit code 1: the controller restarts the bot (with backoff) and sends the alert.
        write_exit_note(1, f"stopped after an internal error: {controls.failure}")
        if not supervised:
            console.print(f"[red]The bot stopped itself after an internal error:[/] {controls.failure}. "
                          f"Details: {log_dir} (errors.log)")
        return 1
    code = RESTART_EXIT_CODE if controls.restart_requested else 0
    write_exit_note(code, reason)
    if not supervised:
        console.print(f"Stopped ({reason}). Logs: {log_dir}")
    return code


def _confirm_live(cfg: BotConfig, args: argparse.Namespace, what: str) -> bool:
    if cfg.mode != "live" or getattr(args, "yes", False):
        return True
    console.print(Panel(f"[bold red]LIVE MODE[/] - {what} will place REAL orders on your TopstepX account, unattended.",
                        border_style="red"))
    return Prompt.ask("Type LIVE to continue") == "LIVE"


def cmd_start(args: argparse.Namespace) -> int:
    """Start the controller: dashboard + Telegram, which run and supervise the trading bot 24/7.

    It opens even when config.yaml is missing or has a problem: the dashboard's Setup tab shows
    what is wrong and fixes it. If it is already running, the dashboard is just opened again."""
    from topstep_bot.controller import resolve_mode, run_controller
    from topstep_bot.setup_service import load_for_server

    cfg, problem = load_for_server(args.config)
    if _controller_running(cfg):
        url = f"http://127.0.0.1:{cfg.dashboard.port}/"
        console.print(f"Topstep Bot is already running (or something else uses port {cfg.dashboard.port}). "
                      f"Opening the dashboard: [bold]{url}[/]")
        if not args.no_browser:
            webbrowser.open(url)
        return 0
    cfg.mode = resolve_mode(cfg, args.mode)
    start_bot = not args.no_bot
    if start_bot and not _confirm_live(cfg, args, "the bot"):
        console.print("Cancelled.")
        return 1
    return run_controller(cfg, load_secrets(), config_path=args.config, mode=cfg.mode,
                          start_bot=start_bot, open_browser=not args.no_browser, config_error=problem)


def cmd_server(args: argparse.Namespace) -> int:
    """What double-clicking start.bat does: start the server and open the dashboard. The bot itself
    is started from the dashboard (Paper or Live), so nothing is asked here."""
    args.mode, args.yes, args.no_bot = None, True, True
    args.no_browser = getattr(args, "no_browser", False)
    return cmd_start(args)


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
    from topstep_bot.controller import run_controller

    return run_controller(cfg, load_secrets(), config_path=args.config, mode="live")


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


def _pct(v: float | None) -> str:
    return "-" if v is None else f"{v * 100:.0f}%"


def _pf(v: float) -> str:
    return "∞" if v == float("inf") else f"{v:.2f}"


def _print_tuning(report, cfg: BotConfig) -> None:
    f0, f1 = report.folds[0], report.folds[-1]
    console.print(
        f"\nTested {report.first_day} → {report.last_day}: {report.candidates} candidate settings, "
        f"{len(report.folds)} walk-forward windows. Settings were chosen on each train window "
        f"(e.g. {f0.train[0]} → {f0.train[1]}) and judged only on the unseen test window after it "
        f"({f0.test[0]} → {f0.test[1]}, ... , {f1.test[0]} → {f1.test[1]})."
    )
    from rich import box

    table = Table(title="Out-of-sample results (test windows only)", box=box.SIMPLE_HEAD)
    for col in ("Strategy", "Net $", "Trades", "PF", "Max DD", "Pass", "Verdict"):
        table.add_column(col, justify="left" if col in ("Strategy", "Verdict") else "right", no_wrap=True)
    rows = sorted(report.results, key=lambda r: (r.eligible, r.oos.sharpe), reverse=True)
    for r in rows:
        o = r.oos
        name = r.strategy + ("*" if r.strategy == cfg.strategy.name else "") + (" ◀" if report.recommended is r else "")
        table.add_row(name, f"{o.net:+,.0f}", str(o.trades), _pf(o.profit_factor), f"{o.max_drawdown:,.0f}",
                      _pct(o.combine_pass_rate), f"[green]✔ {r.reason}[/]" if r.eligible else f"[yellow]✘ {r.reason}[/]")
    console.print(table)
    console.print("[dim]PF = profit factor (above 1 = profitable). Pass = Combine pass rate. * = your current strategy. "
                  "◀ = recommended.[/]")
    if report.current is not None:
        c = report.current
        console.print(f"Your current setting ({report.current_label}) on the same test windows: {c.net:+,.0f} over "
                      f"{c.trades} trades, PF {_pf(c.profit_factor)}, max drawdown {c.max_drawdown:,.0f}.")
    rec = report.recommended
    if rec is None or rec.final is None:
        console.print(Panel(
            "[bold yellow]No strategy held up on data it hadn't seen.[/] Don't go live on these results.\n"
            "Try more history (--days 730), another symbol, or keep paper trading.", border_style="yellow"))
        return
    choices = ", ".join(f"{f.test[0]:%b %Y}: {c.label.removeprefix(rec.strategy).strip() or 'defaults'}"
                        for f, c, _ in rec.choices)
    settings = ", ".join(f"{k}={v}" for k, v in rec.final.params) or "its default settings"
    console.print(Panel(
        f"[bold green]Best out-of-sample: {rec.strategy}[/] - {settings}\n"
        f"Out-of-sample: {rec.oos.net:+,.0f} over {rec.oos.trades} trades, profit factor {_pf(rec.oos.profit_factor)}, "
        f"Sharpe {rec.oos.sharpe:.2f}, max drawdown {rec.oos.max_drawdown:,.0f}"
        + (f", Combine pass rate {_pct(rec.oos.combine_pass_rate)}" if rec.oos.combine_pass_rate is not None else "")
        + f".\nChosen per window: {choices}.\n"
        + ("Only these settings were tested (nothing was tuned)." if rec.n_candidates == 1 else
           f"The same settings were picked in {rec.stability:.0%} of windows"
           + (" - stable." if rec.stability >= 0.5 else " - they shift over time, so expect results to vary."))
        + "\n[dim]Past results, even out-of-sample, don't guarantee future ones. Start with reduced size (ramp-up).[/]",
        title="Recommendation", border_style="green"))


def cmd_tune(args: argparse.Namespace) -> int:
    """Walk-forward tuning: which strategy and settings held up on data they never saw."""
    import json
    import os

    from topstep_bot.instruments import offline_contract
    from topstep_bot.training import save_strategy, train

    cfg = _load(args)
    _apply_overrides(cfg, args)
    bars, synthetic = _history(cfg, args, allow_synthetic=args.synthetic)
    if synthetic:
        console.print("[yellow]Tuning on synthetic data only demonstrates the process - never save its result.[/]")
    contract = offline_contract(cfg.instrument.symbol)
    strategies = args.strategies.split(",") if args.strategies else None
    workers = args.workers or max(1, min(4, (os.cpu_count() or 2) - 1))
    with console.status("Preparing...") as status:
        report = train(cfg, bars, contract, strategies=strategies, folds=args.folds, workers=workers,
                       progress=lambda i, n: status.update(f"Backtesting candidate settings {i}/{n}..."))
    _print_tuning(report, cfg)
    out = Path(cfg.backtest.report_dir)
    out.mkdir(parents=True, exist_ok=True)
    from topstep_bot.backtest.train_report import write_training_report

    page = write_training_report(report, out, cfg.instrument.symbol, cfg.instrument.timeframe_minutes)
    page.with_suffix(".json").write_text(json.dumps(report.to_dict(), indent=1), encoding="utf-8")
    console.print(f"Report: [bold]{page.resolve()}[/] (details: {page.with_suffix('.json').name})")
    if not args.no_open:
        webbrowser.open(page.resolve().as_uri())
    for err in report.errors[:3]:
        console.print(f"[dim]Skipped: {err}[/]")

    rec = report.recommended
    if rec is None or rec.final is None or synthetic:
        return 0
    if args.save is None and sys.stdin.isatty():
        args.save = Prompt.ask("Save the recommended strategy and settings to config.yaml?", choices=["y", "n"],
                               default="n") == "y"
    if args.save:
        cfg_path = Path(args.config or "config.yaml")
        backup = save_strategy(cfg_path, rec.strategy, rec.final.params_dict,
                               note=f"Tuned {datetime.now():%Y-%m-%d} on {report.first_day}..{report.last_day} "
                                    f"(topstep-bot tune); previous config saved as {cfg_path.name}.bak")
        console.print(f"[green]Saved to {cfg_path}[/] (previous version: {backup}). Backtest it with: topstep-bot backtest")
    else:
        console.print("Not saved. To use it, run tune again with --save, or edit strategy: in config.yaml.")
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


def _controller_running(cfg: BotConfig) -> bool:
    """Is the dashboard/controller already running on this PC?"""
    import socket

    with contextlib.suppress(OSError), socket.create_connection(("127.0.0.1", cfg.dashboard.port), timeout=0.5):
        return True
    return False


def _print_update(info, method: str) -> None:
    from topstep_bot.updater import version_label

    if info.error:
        console.print(f"[red]Could not check for updates:[/] {info.error}")
        return
    where = "git" if method == "git" else "GitHub download"
    console.print(f"Installed: [bold]{version_label(info.version, info.current)}[/]"
                  + (f" ({info.current_date[:10]})" if info.current_date else "")
                  + f"   ·   following [bold]{info.branch}[/] ({where})")
    if not info.available:
        console.print("[green]Topstep Bot is up to date.[/]")
        return
    console.print(f"[bold cyan]{info.headline()}[/]")
    if not info.exact:
        console.print("[dim]This copy was downloaded as a ZIP, so its exact version is unknown: installing makes it "
                      f"identical to the latest {info.branch} ({info.file_count} files differ). The latest changes:[/]")
    table = Table(show_header=True, header_style="bold", box=None, pad_edge=False)
    table.add_column("Date", no_wrap=True)
    table.add_column("Change")
    table.add_column("Commit", style="dim", no_wrap=True)
    for c in info.changes[:15]:
        table.add_row(c.date[:10], c.title, c.short)
    console.print(table)
    if info.change_count > 15 and info.exact:
        console.print(f"[dim]...and {info.change_count - 15} more.[/]")
    if info.dependencies:
        console.print("[yellow]It also installs new Python packages (needs the internet for a minute).[/]")
    if info.problem:
        console.print(Panel(f"Can't install it yet: {info.problem}.", border_style="yellow"))


def cmd_update(args: argparse.Namespace) -> int:
    """Check GitHub for a newer version of the bot, show what changed, and install it if you say so."""
    from topstep_bot.updater import UpdateError, Updater, load_state
    from topstep_bot.wizard import setup_github_token

    cfg = _load(args)
    env_path = Path(args.config or "config.yaml").parent / ".env"
    if args.token and not setup_github_token(env_path, cfg.updates.repo):
        return 1
    updater = Updater.for_config(cfg, token=load_secrets().github_token)
    if updater is None:
        console.print("[yellow]This copy was installed as a Python package, not from the bot folder - update it with pip.[/]")
        return 1
    try:
        if args.undo:
            last = load_state(Path(cfg.data_dir)).get("last_install")
            if not last or last.get("status") == "rolled_back":
                console.print("There is no update to undo.")
                return 0
            if _controller_running(cfg):
                console.print("[yellow]Close the bot (its window) first, then run this again.[/]")
                return 1
            if not args.yes and not Confirm.ask(f"Go back to the version from before the update of {last.get('at', '?')[:16]}?",
                                                default=False):
                return 0
            rec = updater.rollback()
            console.print(f"[green]Done - back to {(rec.get('from') or 'the previous files')[:7]}.[/] "
                          "Double-click start.bat to open the dashboard again.")
            return 0
        with console.status("Checking GitHub for updates..."):
            info = updater.check()
        if info.needs_token and not args.check and sys.stdin.isatty():
            console.print(f"[yellow]{info.error}.[/]")
            if Confirm.ask("Set up a GitHub token now?", default=True) and setup_github_token(env_path, cfg.updates.repo):
                updater.close()
                updater = Updater.for_config(cfg, token=load_secrets().github_token)
                with console.status("Checking GitHub for updates..."):
                    info = updater.check()
        _print_update(info, updater.method)
        if info.error or info.problem:
            return 1
        if not info.available or args.check:
            return 0
        if _controller_running(cfg):
            console.print(Panel(
                "The bot is running. Install the update from the dashboard ([bold]Settings[/] tab -> Updates) or with "
                "[bold]/update[/] in Telegram: they install it only while no trade is open, then restart the bot with "
                "the new version by themselves.", border_style="cyan"))
            return 0
        if not args.yes and not Confirm.ask("Install it now?", default=True):
            console.print("Not installed. Run this again any time.")
            return 0
        with console.status("Installing...") as status:
            record = updater.install(info, args.config, progress=lambda m: status.update(m))
        console.print(Panel(
            f"[green bold]Updated to {record['to'][:7]}.[/]\n"
            "Your settings, API keys, trades and what the bot has learned were kept.\n"
            "Double-click start.bat to open the dashboard again. Changed your mind? start.bat update --undo",
            border_style="green"))
        return 0
    except UpdateError as exc:
        console.print(f"[red]{exc}[/]")
        return 1
    finally:
        updater.close()


# ------------------------------------------------------------------------ menu

MENU = [
    ("setup", "Set up / change settings (API key, account, strategy, risk, Telegram)"),
    ("go-live", "START TRADING TODAY: run all preflight checks, then go live"),
    ("check", "Test the connection to TopstepX"),
    ("backtest", "Backtest the configured strategy and open the report"),
    ("train", "TRAIN the bot on recent real data: which strategy works at which time of day"),
    ("paper", "START in PAPER mode (real prices, simulated orders) - dashboard + Telegram"),
    ("live", "START in LIVE mode on your TopstepX account - dashboard + Telegram"),
    ("flatten", "EMERGENCY: close all positions and cancel all orders"),
    ("journal", "Show recent trades and daily results"),
    ("strategies", "Describe the available strategies"),
    ("demo", "Quick demo backtest on synthetic data (no account needed)"),
    ("telegram-test", "Send a test message to your Telegram bot"),
    ("autostart", "Start the 24/7 service automatically when Windows starts"),
    ("logs", "Show recent errors and where the log files are"),
    ("tune", "TUNE: test every strategy on unseen real data and pick the settings that held up"),
    ("rules", "Show Topstep's rules for your account and how the bot stays inside them"),
    ("insights", "What the bot has LEARNED: results after costs, real fills, market conditions"),
    ("update", "UPDATE: check GitHub for a newer version of the bot and install it"),
]


def _menu_update_line(cfg: BotConfig) -> None:
    """'Update available' from the last check (no network: the menu must open instantly)."""
    from topstep_bot.updater import cached_info

    info = cached_info(Path(cfg.data_dir))
    if info and info.available and not info.error:
        number = next(i for i, (name, _) in enumerate(MENU, start=1) if name == "update")
        console.print(f"[cyan]⬆ {info.headline()} Choose {number} to see what changed.[/]")


def interactive_menu(parser: argparse.ArgumentParser) -> int:
    console.print(Panel.fit(f"[bold]Topstep Bot[/] v{__version__} - text menu\n"
                            "[dim]Double-click start.bat instead to use the dashboard: setup, paper/live, start and stop "
                            "are all there.[/]", border_style="cyan"))
    if not Path("config.yaml").exists():
        demo = next(i for i, (name, _) in enumerate(MENU, start=1) if name == "demo")
        console.print(f"[yellow]No config.yaml yet - start with option 1 (setup), or {demo} for a demo.[/]")
    else:
        try:
            cfg = load_config()
            console.print(f"[dim]Your setup: {cfg.account.plan} {cfg.account.stage}, {cfg.instrument.symbol}, "
                          f"strategy {cfg.strategy.name}, {cfg.mode.upper()} mode, ${cfg.risk.risk_per_trade:,.0f} per trade[/]")
        except Exception as exc:  # noqa: BLE001 - the menu must open even with a broken config
            console.print(f"[red]config.yaml has a problem:[/] {exc}\n[yellow]Fix it or run option 1 (setup) again.[/]")
        else:
            _menu_update_line(cfg)
    for i, (_, desc) in enumerate(MENU, start=1):
        console.print(f"  [bold]{i}[/]  {desc}")
    console.print("  [bold]0[/]  Quit")
    choice = Prompt.ask("Choose", choices=[str(i) for i in range(len(MENU) + 1)], default="0")
    if choice == "0":
        return 0
    name = MENU[int(choice) - 1][0]
    argv = {"paper": ["start", "--mode", "paper"], "live": ["start", "--mode", "live"],
            "autostart": ["autostart", "on"]}.get(name, [name])
    args = parser.parse_args(argv)
    return dispatch(args)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="topstep-bot", description="Automated trading bot for Topstep (TopstepX API).")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("-c", "--config", default=None, help="config file (default: config.yaml)")
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("server", help="start the server and open the dashboard (what start.bat does with no arguments)")
    p.add_argument("--no-browser", action="store_true", help="don't open the dashboard in a browser")
    p.set_defaults(func=cmd_server)
    sub.add_parser("menu", help="the text menu from earlier versions").set_defaults(
        func=lambda args: interactive_menu(parser))
    sub.add_parser("setup", help="setup wizard in this window (the dashboard's Setup tab does the same)").set_defaults(
        func=cmd_setup)
    sub.add_parser("check", help="test credentials, list accounts, resolve the contract").set_defaults(func=cmd_check)
    sub.add_parser("strategies", help="list strategies and their parameters").set_defaults(func=cmd_strategies)
    sub.add_parser("rules", help="Topstep's rules for your account and how the bot enforces them").set_defaults(func=cmd_rules)
    sub.add_parser("telegram-test", help="send a test message to your Telegram bot").set_defaults(func=cmd_telegram_test)

    for name, helptext in (("start", "start the dashboard + Telegram, which run the bot 24/7 (recommended)"),
                           ("service", "same as 'start' (kept for older shortcuts)")):
        p = sub.add_parser(name, help=helptext)
        p.add_argument("--mode", choices=["paper", "live"], help="paper or live (default: last used, else config)")
        p.add_argument("--yes", action="store_true", help="skip the live-mode confirmation prompt")
        p.add_argument("--no-bot", action="store_true", help="open the dashboard without starting the bot")
        p.add_argument("--no-browser", action="store_true", help="don't open the dashboard in a browser")
        p.set_defaults(func=cmd_start)

    p = sub.add_parser("run", help="run only the trading bot, without dashboard (normally started by 'start')")
    p.add_argument("--mode", choices=["paper", "live"], help="override mode from config")
    p.add_argument("--yes", action="store_true", help="skip the live-mode confirmation prompt")
    p.set_defaults(func=cmd_run)

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

    p = sub.add_parser("train", help="learn which strategy works at which time of day from recent real data")
    p.add_argument("--data", help="CSV of bars to learn from (default: download fresh history)")
    p.add_argument("--tz", default="UTC", help="timezone for CSV times without one")
    p.add_argument("--days", type=int, default=None, help="days of history to download (default: knowledge.history_days)")
    p.set_defaults(func=cmd_train)

    p = sub.add_parser("tune", help="walk-forward test: pick the strategy/settings that held up on unseen data")
    p.add_argument("--data", help="CSV of 1-minute bars (default: data/<SYMBOL>_1m.csv, else download)")
    p.add_argument("--tz", default="UTC", help="timezone for CSV times without one")
    p.add_argument("--download", action="store_true", help="download fresh history from TopstepX first")
    p.add_argument("--days", type=int, default=365, help="days of history to download (default 365; more is better)")
    p.add_argument("--strategies", help="comma-separated strategies to test (default: all)")
    p.add_argument("--symbol", help="override the symbol")
    p.add_argument("--timeframe", type=int, help="override the bar timeframe in minutes")
    p.add_argument("--folds", type=int, default=4, help="walk-forward windows (default 4)")
    p.add_argument("--workers", type=int, default=0, help="parallel processes (default: CPUs - 1, max 4)")
    p.add_argument("--save", action="store_true", default=None, help="save the recommendation to config.yaml")
    p.add_argument("--no-save", dest="save", action="store_false", help="never ask to save")
    p.add_argument("--synthetic", action="store_true", help="demo the process on synthetic data (never saved)")
    p.add_argument("--no-open", action="store_true", help="don't open the report in a browser")
    p.add_argument("--seed", type=int, default=7, help=argparse.SUPPRESS)
    p.set_defaults(func=cmd_tune, strategy=None)

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

    p = sub.add_parser("insights", help="what the bot has learned: results after costs, real fills, market conditions")
    p.add_argument("--csv", metavar="FILE", help="save every observation to a CSV file (opens in Excel) instead")
    p.set_defaults(func=cmd_insights, csv=None)

    p = sub.add_parser("update", help="check GitHub for a newer version of the bot and install it")
    p.add_argument("--check", action="store_true", help="only show whether an update is available")
    p.add_argument("--yes", action="store_true", help="install without asking")
    p.add_argument("--token", action="store_true", help="set up (or replace) a GitHub token for update checks (only needed for a private repository)")
    p.add_argument("--undo", action="store_true", help="go back to the version from before the last update")
    p.set_defaults(func=cmd_update)

    p = sub.add_parser("journal", help="show recent trades and daily results")
    p.add_argument("--mode", choices=["paper", "live"])
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=cmd_journal)
    return parser


def dispatch(args: argparse.Namespace) -> int:
    """Run one command. Every failure is shown in plain words and written to a log file, so a
    problem can always be diagnosed afterwards - even if the window has closed."""
    # The bot and the controller set up their own logging (bot.log / controller.log). Other commands log to
    # commands.log, so they never rotate bot.log while the bot is writing it (Windows can't do that).
    log_dir = LOG_DIR
    if args.command not in ("run", "logs", "service", "start", "server", "go-live", "menu"):
        try:
            log_dir = setup_logging(_load(args), args.command, console_level="WARNING", file_prefix="commands")
        except Exception:  # noqa: BLE001 - never block a command because logging failed (e.g. bad config)
            logging.basicConfig(level=logging.WARNING)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        return 130
    except FileNotFoundError as exc:
        console.print(f"[red]{exc}[/]")
        return 2
    except ConfigError as exc:
        console.print(f"[red]Configuration problem:[/] {exc}\nFix it on the dashboard's Setup tab (double-click start.bat), or in config.yaml with Notepad.")
        return 2
    except ValueError as exc:  # a problem the user can fix (unknown symbol, not enough data, ...)
        log.warning("%s failed: %s", args.command, exc, exc_info=True)
        console.print(f"[red]{exc}[/]")
        return 2
    except Exception as exc:  # noqa: BLE001 - last line of defence: record it, explain it
        log.exception("Unexpected error in '%s'", args.command)
        console.print(f"[red]Unexpected error:[/] {exc!r}\nThe full details were saved in [bold]{Path(log_dir).resolve()}[/] "
                      "(errors.log) - 'topstep-bot logs --bundle' zips them for support (no passwords or keys).")
        return 1


def main(argv: list[str] | None = None) -> int:
    if sys.platform == "win32":
        with contextlib.suppress(AttributeError, ValueError):
            sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        args = parser.parse_args([*(["-c", args.config] if args.config else []), "server"])
    if args.command == "menu":
        try:
            return interactive_menu(parser)
        except KeyboardInterrupt:
            return 0
    return dispatch(args)
