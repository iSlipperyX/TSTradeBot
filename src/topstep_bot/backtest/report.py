"""Self-contained HTML backtest report (no internet needed to view it)."""

from __future__ import annotations

import html
import json
import math
from datetime import datetime
from pathlib import Path

from topstep_bot.backtest.metrics import combine_statistics, compute_metrics
from topstep_bot.backtest.runner import BacktestResult
from topstep_bot.risk.topstep import PLANS, LossLimitTracker
from topstep_bot.strategies import STRATEGIES


def _money(v: float | None) -> str:
    if v is None:
        return "-"
    return f"-${abs(v):,.2f}" if v < 0 else f"${v:,.2f}"


def _pct(v: float | None) -> str:
    return "-" if v is None else f"{v * 100:.1f}%"


def _num(v: float | None, digits: int = 2) -> str:
    if v is None:
        return "-"
    if math.isinf(v):
        return "∞"
    return f"{v:.{digits}f}"


def build_report(res: BacktestResult) -> str:
    cfg = res.cfg
    plan = PLANS[cfg.account.plan]
    m = compute_metrics(res.trades, res.days, res.starting_balance)
    combine = combine_statistics(res.days, plan) if cfg.account.stage == "combine" else None
    strat = STRATEGIES[cfg.strategy.name]

    tracker = LossLimitTracker(res.starting_balance, plan.max_loss_limit)
    series = []
    for d in res.days:
        series.append(
            {"day": d.day.isoformat(), "balance": round(d.end_balance, 2), "floor": round(tracker.floor, 2),
             "pnl": round(d.pnl, 2), "trades": d.trades}
        )
        tracker.end_of_day(d.end_balance)

    first = combine["from_first_day"] if combine else None
    if first is None:
        verdict = ("neutral", "Topstep Combine simulation", "Not applicable for this account stage.")
    elif first.status == "passed":
        verdict = ("good", "Combine PASSED", f"Starting on day one: {first.detail} after {first.days_used} trading days.")
    elif first.status == "failed":
        verdict = ("bad", "Combine FAILED", f"Starting on day one: {first.detail} after {first.days_used} trading days.")
    else:
        verdict = ("neutral", "Combine not finished", f"Profit {_money(first.profit)} after {first.days_used} days - target not reached yet.")

    cards = [
        ("Net P&L", _money(m["net_pnl"]), "after fees and slippage"),
        ("Trades", str(m["trades"]), f"{m['longs']} long / {m['shorts']} short"),
        ("Win rate", _pct(m["win_rate"]), f"avg win {_money(m['avg_win'])} / loss {_money(m['avg_loss'])}"),
        ("Profit factor", _num(m["profit_factor"]), f"avg {_num(m['avg_r'])}R per trade"),
        ("Max drawdown", _money(m["max_drawdown"]), f"longest losing streak {m['max_losing_streak']}"),
        ("Sharpe (daily)", _num(m["sharpe"]), f"{m['green_days']}/{m['traded_days']} green trading days"),
    ]
    if combine and combine["attempts"]:
        cards.append(
            ("Combine pass rate", _pct(combine["pass_rate"]),
             f"{combine['attempts']} simulated starts; median {_num(combine['median_days_to_pass'], 0)} days to pass")
        )
    elif combine:
        cards.append(("Combine pass rate", "n/a", "no simulated start reached the target or the MLL - use more data"))

    trade_rows = "\n".join(
        "<tr>"
        f"<td>{html.escape((t.opened_at or t.created_at).strftime('%Y-%m-%d %H:%M'))}</td>"
        f"<td>{t.side.label}</td><td class='num'>{t.filled_size}</td>"
        f"<td class='num'>{t.entry_price}</td><td class='num'>{_num(t.exit_price, res.contract.price_decimals)}</td>"
        f"<td class='num {'pos' if t.net_pnl > 0 else 'neg'}'>{_money(t.net_pnl)}</td>"
        f"<td class='num'>{_num(t.r_multiple())}</td>"
        f"<td>{html.escape(t.exit_reason)}</td><td class='muted'>{html.escape(t.reason)}</td>"
        "</tr>"
        for t in reversed(res.trades[-300:])
    )
    exit_rows = "".join(f"<li>{html.escape(k)}: <b>{v}</b></li>" for k, v in m["exit_reasons"].items())
    params = {**strat.defaults, **cfg.strategy.params}
    param_rows = "".join(f"<li>{html.escape(k)}: <b>{html.escape(str(v))}</b></li>" for k, v in params.items())
    breach_note = ""
    if res.breaches:
        ts, eq, fl = res.breaches[0]
        breach_note = (
            f"<p class='alert'>⚠ The Maximum Loss Limit would have been touched {len(res.breaches)} time(s); first on "
            f"{ts:%Y-%m-%d %H:%M} UTC (equity {_money(eq)} vs floor {_money(fl)}). Topstep liquidates at that point.</p>"
        )

    card_html = "".join(
        f"<div class='card'><div class='label'>{html.escape(a)}</div><div class='value'>{html.escape(b)}</div>"
        f"<div class='sub'>{html.escape(c)}</div></div>"
        for a, b, c in cards
    )
    return TEMPLATE.format(
        title=html.escape(f"{strat.title} · {res.contract.name}"),
        subtitle=html.escape(
            f"{cfg.instrument.timeframe_minutes}-minute bars · {res.first_day} → {res.last_day} · "
            f"{plan.name} {cfg.account.stage} · risk {_money(cfg.risk.risk_per_trade)}/trade"
        ),
        generated=datetime.now().strftime("%Y-%m-%d %H:%M"),
        verdict_class=verdict[0],
        verdict_title=html.escape(verdict[1]),
        verdict_text=html.escape(verdict[2]),
        cards=card_html,
        breach_note=breach_note,
        trade_rows=trade_rows or "<tr><td colspan='9'>No trades.</td></tr>",
        exit_rows=exit_rows or "<li>None</li>",
        param_rows=param_rows,
        data=json.dumps(series),
        fees=_money(m["fees"]),
        description=html.escape(strat.description),
    )


def write_report(res: BacktestResult, out_dir: Path | str) -> Path:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = out / f"backtest_{res.cfg.strategy.name}_{res.contract.name}_{stamp}.html"
    path.write_text(build_report(res), encoding="utf-8")
    return path


TEMPLATE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Backtest Report</title>
<style>
:root {{ color-scheme: light; --bg:#f6f6f4; --surface:#fcfcfb; --border:#e4e3df; --text:#0b0b0b; --text2:#52514e;
  --muted:#8a8984; --grid:#ecebe7; --s1:#2a78d6; --floor:#d03b3b; --pos:#2a78d6; --neg:#e34948;
  --good-bg:#e7f5e7; --good:#006300; --bad-bg:#fbeaea; --bad:#a32a2a; }}
@media (prefers-color-scheme: dark) {{ :root:not([data-theme="light"]) {{ color-scheme: dark; --bg:#121211;
  --surface:#1a1a19; --border:#2e2e2b; --text:#ffffff; --text2:#c3c2b7; --muted:#8f8e86; --grid:#2a2a27;
  --s1:#3987e5; --floor:#e66767; --pos:#3987e5; --neg:#e66767; --good-bg:#15301a; --good:#7ddc7d;
  --bad-bg:#3a1a1a; --bad:#ff9b9b; }} }}
:root[data-theme="dark"] {{ color-scheme: dark; --bg:#121211; --surface:#1a1a19; --border:#2e2e2b; --text:#ffffff;
  --text2:#c3c2b7; --muted:#8f8e86; --grid:#2a2a27; --s1:#3987e5; --floor:#e66767; --pos:#3987e5; --neg:#e66767;
  --good-bg:#15301a; --good:#7ddc7d; --bad-bg:#3a1a1a; --bad:#ff9b9b; }}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--bg); color:var(--text); font:14px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif; }}
main {{ max-width:1100px; margin:0 auto; padding:24px 16px 48px; }}
h1 {{ font-size:24px; margin:0; }} h2 {{ font-size:16px; margin:0 0 12px; }}
.sub, .muted {{ color:var(--text2); }} .muted {{ font-size:12px; }}
.panel {{ background:var(--surface); border:1px solid var(--border); border-radius:12px; padding:16px; margin-top:16px; }}
.cards {{ display:grid; grid-template-columns:repeat(auto-fill,minmax(150px,1fr)); gap:12px; margin-top:16px; }}
.card {{ background:var(--surface); border:1px solid var(--border); border-radius:12px; padding:12px 14px; }}
.card .label {{ color:var(--text2); font-size:12px; }} .card .value {{ font-size:22px; font-weight:650; margin:2px 0; }}
.card .sub {{ font-size:12px; }}
.verdict {{ border-radius:12px; padding:14px 16px; margin-top:16px; border:1px solid var(--border); background:var(--surface); }}
.verdict.good {{ background:var(--good-bg); color:var(--good); }} .verdict.bad {{ background:var(--bad-bg); color:var(--bad); }}
.verdict b {{ font-size:16px; }}
.alert {{ background:var(--bad-bg); color:var(--bad); padding:10px 14px; border-radius:10px; }}
.legend {{ display:flex; gap:16px; font-size:12px; color:var(--text2); margin-bottom:6px; }}
.legend i {{ display:inline-block; width:18px; height:0; border-top:2px solid; vertical-align:middle; margin-right:6px; }}
.chart {{ position:relative; }} svg {{ display:block; width:100%; height:auto; }}
.tip {{ position:absolute; pointer-events:none; background:var(--surface); border:1px solid var(--border);
  border-radius:8px; padding:6px 10px; font-size:12px; box-shadow:0 4px 12px rgba(0,0,0,.12); display:none; white-space:nowrap; }}
.grid2 {{ display:grid; grid-template-columns:1fr 1fr; gap:16px; }}
@media (max-width:720px) {{ .grid2 {{ grid-template-columns:1fr; }} }}
.tablewrap {{ overflow-x:auto; }}
table {{ border-collapse:collapse; width:100%; font-size:12.5px; }}
th, td {{ text-align:left; padding:6px 8px; border-bottom:1px solid var(--border); }}
th {{ color:var(--text2); font-weight:600; }} td.num, th.num {{ text-align:right; font-variant-numeric:tabular-nums; }}
.pos {{ color:var(--text); }} .neg {{ color:var(--bad); }}
ul {{ margin:0; padding-left:18px; }}
footer {{ margin-top:24px; color:var(--muted); font-size:12px; }}
</style></head>
<body><main>
<h1>{title}</h1>
<div class="sub">{subtitle}</div>
<div class="verdict {verdict_class}"><b>{verdict_title}</b><div>{verdict_text}</div></div>
{breach_note}
<div class="cards">{cards}</div>

<section class="panel">
  <h2>Account balance vs. Maximum Loss Limit</h2>
  <div class="legend"><span><i style="border-color:var(--s1)"></i>End-of-day balance</span>
  <span><i style="border-color:var(--floor);border-top-style:dashed"></i>MLL floor (trailing)</span></div>
  <div class="chart" id="eq"><div class="tip"></div></div>
</section>
<section class="panel">
  <h2>Daily P&amp;L</h2>
  <div class="chart" id="daily"><div class="tip"></div></div>
</section>

<div class="grid2">
  <section class="panel"><h2>Strategy</h2><p class="sub">{description}</p><ul>{param_rows}</ul></section>
  <section class="panel"><h2>How trades ended</h2><ul>{exit_rows}</ul>
    <p class="muted">Total fees: {fees}. Fills are simulated conservatively: market orders fill at the next bar's
    open plus slippage; limit orders need price to trade through; if a stop and target are both hit in one bar, the
    stop is assumed first.</p></section>
</div>

<section class="panel"><h2>Trades (most recent first)</h2><div class="tablewrap"><table>
<thead><tr><th>Entry time (UTC)</th><th>Side</th><th class="num">Qty</th><th class="num">Entry</th><th class="num">Exit</th>
<th class="num">Net P&amp;L</th><th class="num">R</th><th>Exit</th><th>Signal</th></tr></thead>
<tbody>{trade_rows}</tbody></table></div></section>

<footer>Generated {generated}. Hypothetical results from historical simulation - they do not account for every
real-world factor (queue position, outages, data differences) and are not a guarantee of future results.</footer>
</main>
<script>
const DATA = {data};
const css = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
const fmt = v => (v < 0 ? "-$" : "$") + Math.abs(v).toLocaleString(undefined, {{minimumFractionDigits:2, maximumFractionDigits:2}});
const NS = "http://www.w3.org/2000/svg";
function el(tag, attrs, parent) {{ const e = document.createElementNS(NS, tag);
  for (const k in attrs) e.setAttribute(k, attrs[k]); if (parent) parent.appendChild(e); return e; }}
function niceTicks(lo, hi, n) {{ const span = hi - lo || 1; const step0 = span / n;
  const mag = Math.pow(10, Math.floor(Math.log10(step0))); const step = [1,2,2.5,5,10].map(m => m*mag).find(s => span/s <= n) || mag*10;
  const out = []; for (let v = Math.ceil(lo/step)*step; v <= hi + 1e-9; v += step) out.push(v); return out; }}
function frame(id, H) {{ const box = document.getElementById(id); const W = Math.max(box.clientWidth, 320);
  const svg = el("svg", {{viewBox:`0 0 ${{W}} ${{H}}`, role:"img"}}); box.insertBefore(svg, box.firstChild);
  return {{box, svg, W, H, L:64, R:16, T:10, B:26, tip: box.querySelector(".tip")}}; }}
function axes(f, lo, hi) {{ const y = v => f.T + (hi - v) / (hi - lo || 1) * (f.H - f.T - f.B);
  for (const t of niceTicks(lo, hi, 5)) {{ el("line", {{x1:f.L, x2:f.W-f.R, y1:y(t), y2:y(t), stroke:css("--grid")}}, f.svg);
    const tx = el("text", {{x:f.L-8, y:y(t)+4, "text-anchor":"end", "font-size":11, fill:css("--text2")}}, f.svg);
    tx.textContent = (t < 0 ? "-$" : "$") + Math.abs(t).toLocaleString(); }}
  return y; }}
function xLabels(f, x) {{ if (!DATA.length) return; const idx = [0, Math.floor((DATA.length-1)/2), DATA.length-1];
  [...new Set(idx)].forEach((i, k) => {{ const t = el("text", {{x:x(i), y:f.H-6, "font-size":11, fill:css("--text2"),
    "text-anchor": k===0 ? "start" : (i===DATA.length-1 ? "end" : "middle")}}, f.svg); t.textContent = DATA[i].day; }}); }}
function showTip(f, html, px, py) {{ f.tip.innerHTML = html; f.tip.style.display = "block";
  const w = f.tip.offsetWidth; f.tip.style.left = Math.min(Math.max(px - w/2, 0), f.box.clientWidth - w) + "px";
  f.tip.style.top = Math.max(py - 56, 0) + "px"; }}
function drawEquity() {{ const f = frame("eq", 280); f.R = 78; if (!DATA.length) return;
  const vals = DATA.flatMap(d => [d.balance, d.floor]); const pad = (Math.max(...vals) - Math.min(...vals)) * 0.08 || 100;
  const lo = Math.min(...vals) - pad, hi = Math.max(...vals) + pad; const y = axes(f, lo, hi);
  const x = i => f.L + (DATA.length === 1 ? 0 : i / (DATA.length - 1)) * (f.W - f.L - f.R);
  const path = key => DATA.map((d, i) => (i ? "L" : "M") + x(i) + "," + y(d[key])).join("");
  el("path", {{d:path("floor"), fill:"none", stroke:css("--floor"), "stroke-width":2, "stroke-dasharray":"6 4"}}, f.svg);
  el("path", {{d:path("balance"), fill:"none", stroke:css("--s1"), "stroke-width":2, "stroke-linejoin":"round"}}, f.svg);
  const last = DATA.length - 1;
  [["balance", "Balance"], ["floor", "MLL floor"]].forEach(([k, label]) => {{
    const t = el("text", {{x:x(last)+8, y:y(DATA[last][k]) + 4, "text-anchor":"start", "font-size":11, fill:css("--text2")}}, f.svg);
    t.textContent = label; }});
  xLabels(f, x);
  const cross = el("line", {{y1:f.T, y2:f.H-f.B, stroke:css("--muted"), "stroke-width":1, visibility:"hidden"}}, f.svg);
  const dot = el("circle", {{r:4, fill:css("--s1"), stroke:css("--surface"), "stroke-width":2, visibility:"hidden"}}, f.svg);
  el("rect", {{x:f.L, y:f.T, width:f.W-f.L-f.R, height:f.H-f.T-f.B, fill:"transparent"}}, f.svg)
    .addEventListener("mousemove", ev => {{ const r = f.svg.getBoundingClientRect(); const px = (ev.clientX - r.left) * f.W / r.width;
      const i = Math.round((px - f.L) / (f.W - f.L - f.R) * (DATA.length - 1)); const d = DATA[Math.max(0, Math.min(last, i))];
      const xi = x(DATA.indexOf(d)); cross.setAttribute("x1", xi); cross.setAttribute("x2", xi); cross.setAttribute("visibility", "visible");
      dot.setAttribute("cx", xi); dot.setAttribute("cy", y(d.balance)); dot.setAttribute("visibility", "visible");
      showTip(f, `<b>${{d.day}}</b><br>Balance ${{fmt(d.balance)}}<br>MLL floor ${{fmt(d.floor)}}<br>Room ${{fmt(d.balance - d.floor)}}`,
        xi * r.width / f.W, y(d.balance) * r.height / f.H); }});
  f.svg.addEventListener("mouseleave", () => {{ f.tip.style.display = "none"; cross.setAttribute("visibility", "hidden"); dot.setAttribute("visibility", "hidden"); }}); }}
function drawDaily() {{ const f = frame("daily", 220); if (!DATA.length) return;
  const vals = DATA.map(d => d.pnl); const lo = Math.min(0, ...vals), hi = Math.max(0, ...vals); const y = axes(f, lo, hi || 1);
  const n = DATA.length, slot = (f.W - f.L - f.R) / n, bw = Math.max(Math.min(slot - 2, 18), 1);
  const x = i => f.L + slot * i + slot / 2;
  DATA.forEach((d, i) => {{ const top = y(Math.max(d.pnl, 0)), bot = y(Math.min(d.pnl, 0));
    const h = Math.max(bot - top, d.pnl === 0 ? 0 : 1);
    const r = el("rect", {{x:x(i) - bw/2, y:top, width:bw, height:h, rx:Math.min(2, bw/2), fill: d.pnl >= 0 ? css("--pos") : css("--neg")}}, f.svg);
    const hit = el("rect", {{x:f.L + slot*i, y:f.T, width:slot, height:f.H-f.T-f.B, fill:"transparent"}}, f.svg);
    hit.addEventListener("mousemove", () => {{ const b = f.svg.getBoundingClientRect();
      showTip(f, `<b>${{d.day}}</b><br>P&amp;L ${{fmt(d.pnl)}}<br>${{d.trades}} trade(s)`, x(i) * b.width / f.W, top * b.height / f.H); }});
    hit.addEventListener("mouseleave", () => f.tip.style.display = "none"); }});
  el("line", {{x1:f.L, x2:f.W-f.R, y1:y(0), y2:y(0), stroke:css("--muted")}}, f.svg);
  xLabels(f, x); }}
drawEquity(); drawDaily();
</script></body></html>
"""
