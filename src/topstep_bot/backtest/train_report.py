"""Self-contained HTML report for `topstep-bot tune` (opens offline, light and dark mode)."""

from __future__ import annotations

import html
import json
import math
from datetime import datetime
from pathlib import Path

from topstep_bot.training import TrainingReport

# Categorical slots in fixed order (validated for colour-vision deficiency as adjacent line pairs).
LIGHT = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948"]
DARK = ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#008300", "#9085e9", "#e66767"]


def _money(v: float | None) -> str:
    if v is None:
        return "-"
    return f"-${abs(v):,.0f}" if v < 0 else f"${v:,.0f}"


def _pct(v: float | None) -> str:
    return "-" if v is None else f"{v * 100:.0f}%"


def _pf(v: float) -> str:
    return "∞" if isinstance(v, float) and math.isinf(v) else f"{v:.2f}"


def build_training_report(report: TrainingReport, symbol: str, timeframe: int) -> str:
    # Series keep a fixed slot per strategy (alphabetical), so colours never depend on ranking.
    order = sorted(r.strategy for r in report.results)
    slot = {name: i % len(LIGHT) for i, name in enumerate(order)}
    series = []
    for r in report.results:
        eq, points = 0.0, []
        for d, pnl in r.oos_daily:
            eq += pnl
            points.append([d.isoformat(), round(eq, 2)])
        series.append({"name": r.strategy, "slot": slot[r.strategy], "points": points,
                       "recommended": report.recommended is r})
    folds = [{"test_start": f.test[0].isoformat(), "test_end": f.test[1].isoformat(),
              "train_start": f.train[0].isoformat(), "train_end": f.train[1].isoformat()} for f in report.folds]

    rows = []
    for r in sorted(report.results, key=lambda r: (r.eligible, r.oos.sharpe), reverse=True):
        o = r.oos
        verdict = ("good", "✔ " + r.reason) if r.eligible else ("warn", "✘ " + r.reason)
        mark = " <span class='pill'>recommended</span>" if report.recommended is r else ""
        final = html.escape(", ".join(f"{k}={v}" for k, v in r.final.params) or "defaults") if r.final else "-"
        rows.append(
            f"<tr><td><i class='sw' style='background:var(--s{slot[r.strategy] + 1})'></i>{html.escape(r.strategy)}{mark}</td>"
            f"<td class='num {'neg' if o.net < 0 else ''}'>{_money(o.net)}</td><td class='num'>{o.trades}</td>"
            f"<td class='num'>{_pf(o.profit_factor)}</td><td class='num'>{_pct(o.win_rate)}</td>"
            f"<td class='num'>{o.avg_r:+.2f}</td><td class='num'>{o.sharpe:.2f}</td>"
            f"<td class='num'>{_money(o.max_drawdown)}</td><td class='num'>{_pct(o.combine_pass_rate)}</td>"
            f"<td class='{verdict[0]}'>{html.escape(verdict[1])}</td><td class='muted'>{final}</td></tr>"
        )

    window_rows = []
    for r in report.results:
        chosen = {f: c for f, c, _ in r.choices}
        cells = "".join(
            f"<td>{html.escape(chosen[f].label.removeprefix(r.strategy).strip() or 'defaults')}</td>" if f in chosen
            else "<td class='muted'>– (too few trades)</td>"
            for f in report.folds
        )
        note = " <span class='muted'>(only your current settings tested)</span>" if r.n_candidates == 1 else ""
        window_rows.append(f"<tr><th>{html.escape(r.strategy)}{note}</th>{cells}</tr>")
    window_head = "".join(f"<th>{f.test[0]:%Y-%m-%d} → {f.test[1]:%Y-%m-%d}</th>" for f in report.folds)

    rec = report.recommended
    if rec and rec.final:
        settings = ", ".join(f"{k}={v}" for k, v in rec.final.params) or "default settings"
        stable = ("Only these settings were tested, so nothing was tuned." if rec.n_candidates == 1 else
                  f"The same settings were chosen in {rec.stability:.0%} of windows.")
        verdict = ("good", f"Recommended: {rec.strategy}",
                   f"With {settings}. Out-of-sample {_money(rec.oos.net)} over {rec.oos.trades} trades, profit factor "
                   f"{_pf(rec.oos.profit_factor)}, max drawdown {_money(rec.oos.max_drawdown)}. {stable}")
    else:
        verdict = ("bad", "No strategy held up out-of-sample",
                   "None made money on the test windows it had never seen. Don't go live on these results.")
    current = ""
    if report.current is not None:
        c = report.current
        current = (f"<p class='sub'>Your current setting ({html.escape(report.current_label)}) on the same test windows: "
                   f"{_money(c.net)} over {c.trades} trades, profit factor {_pf(c.profit_factor)}.</p>")
    return TEMPLATE.format(
        title=html.escape(f"Walk-forward tuning · {symbol} {timeframe}-minute bars"),
        subtitle=html.escape(f"{report.first_day} → {report.last_day} · {report.candidates} candidate settings · "
                             f"{len(report.folds)} walk-forward windows"),
        verdict_class=verdict[0], verdict_title=html.escape(verdict[1]), verdict_text=html.escape(verdict[2]),
        current=current, rows="".join(rows), window_head=window_head, window_rows="".join(window_rows),
        data=json.dumps({"series": series, "folds": folds}), generated=datetime.now().strftime("%Y-%m-%d %H:%M"),
        light_vars="".join(f"--s{i + 1}:{c};" for i, c in enumerate(LIGHT)),
        dark_vars="".join(f"--s{i + 1}:{c};" for i, c in enumerate(DARK)),
    )


def write_training_report(report: TrainingReport, out_dir: Path | str, symbol: str, timeframe: int) -> Path:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"training_{symbol}_{datetime.now():%Y%m%d_%H%M%S}.html"
    path.write_text(build_training_report(report, symbol, timeframe), encoding="utf-8")
    return path


TEMPLATE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Walk-forward Tuning Report</title>
<style>
:root {{ color-scheme: light; --bg:#f9f9f7; --surface:#fcfcfb; --border:rgba(11,11,11,.10); --text:#0b0b0b;
  --text2:#52514e; --muted:#898781; --grid:#e1e0d9; --axis:#c3c2b7; --good:#006300; --good-bg:#e7f5e7;
  --bad:#a32a2a; --bad-bg:#fbeaea; --warn:#7a5200; {light_vars} }}
@media (prefers-color-scheme: dark) {{ :root:where(:not([data-theme="light"])) {{ color-scheme: dark; --bg:#0d0d0d;
  --surface:#1a1a19; --border:rgba(255,255,255,.10); --text:#ffffff; --text2:#c3c2b7; --muted:#898781;
  --grid:#2c2c2a; --axis:#383835; --good:#0ca30c; --good-bg:#15301a; --bad:#ff9b9b; --bad-bg:#3a1a1a;
  --warn:#fab219; {dark_vars} }} }}
:root[data-theme="dark"] {{ color-scheme: dark; --bg:#0d0d0d; --surface:#1a1a19; --border:rgba(255,255,255,.10);
  --text:#ffffff; --text2:#c3c2b7; --muted:#898781; --grid:#2c2c2a; --axis:#383835; --good:#0ca30c;
  --good-bg:#15301a; --bad:#ff9b9b; --bad-bg:#3a1a1a; --warn:#fab219; {dark_vars} }}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--bg); color:var(--text); font:14px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif; }}
main {{ max-width:1100px; margin:0 auto; padding:24px 16px 48px; }}
h1 {{ font-size:24px; margin:0; }} h2 {{ font-size:16px; margin:0 0 4px; }}
.sub {{ color:var(--text2); }} .muted {{ color:var(--text2); font-size:12px; }}
.panel {{ background:var(--surface); border:1px solid var(--border); border-radius:12px; padding:16px; margin-top:16px; }}
.verdict {{ border-radius:12px; padding:14px 16px; margin-top:16px; border:1px solid var(--border); background:var(--surface); }}
.verdict.good {{ background:var(--good-bg); color:var(--good); }} .verdict.bad {{ background:var(--bad-bg); color:var(--bad); }}
.verdict b {{ font-size:16px; }}
.legend {{ display:flex; flex-wrap:wrap; gap:6px 16px; font-size:12px; color:var(--text2); margin:8px 0; }}
.legend span {{ display:inline-flex; align-items:center; gap:6px; }}
.legend i, .sw {{ display:inline-block; width:14px; height:3px; border-radius:2px; vertical-align:middle; margin-right:6px; }}
.chart {{ position:relative; }} svg {{ display:block; width:100%; height:auto; }}
.tip {{ position:absolute; pointer-events:none; background:var(--surface); border:1px solid var(--border);
  border-radius:8px; padding:6px 10px; font-size:12px; box-shadow:0 4px 12px rgba(0,0,0,.12); display:none; white-space:nowrap; }}
.tip i {{ display:inline-block; width:10px; height:3px; border-radius:2px; margin-right:6px; vertical-align:middle; }}
.tablewrap {{ overflow-x:auto; }}
table {{ border-collapse:collapse; width:100%; font-size:12.5px; }}
th, td {{ text-align:left; padding:6px 8px; border-bottom:1px solid var(--border); vertical-align:top; }}
th {{ color:var(--text2); font-weight:600; white-space:nowrap; }}
td.num {{ text-align:right; font-variant-numeric:tabular-nums; white-space:nowrap; }}
td.neg {{ color:var(--bad); }} td.good {{ color:var(--good); white-space:nowrap; }} td.warn {{ color:var(--warn); white-space:nowrap; }}
.pill {{ font-size:11px; font-weight:700; padding:1px 7px; border-radius:999px; border:1px solid var(--good); color:var(--good); margin-left:6px; }}
footer {{ margin-top:24px; color:var(--muted); font-size:12px; }}
</style></head>
<body><main>
<h1>{title}</h1>
<div class="sub">{subtitle}</div>
<div class="verdict {verdict_class}"><b>{verdict_title}</b><div>{verdict_text}</div></div>
{current}
<section class="panel">
  <h2>Out-of-sample P&amp;L by strategy</h2>
  <div class="muted">Each strategy's settings were chosen on a train window, then traded "blind" on the next test window.
  Only those unseen test days are plotted, one after another; dashed lines separate the windows. Dollars at your
  configured risk per trade.</div>
  <div class="legend" id="legend"></div>
  <div class="chart" id="eq"><div class="tip"></div></div>
</section>
<section class="panel"><h2>Results on unseen data</h2><div class="tablewrap"><table>
<thead><tr><th>Strategy</th><th>Net</th><th>Trades</th><th>PF</th><th>Win</th><th>Avg R</th><th>Sharpe</th>
<th>Max DD</th><th>Pass</th><th>Verdict</th><th>Settings to trade now</th></tr></thead>
<tbody>{rows}</tbody></table></div>
<p class="muted">PF = profit factor (gross wins ÷ gross losses; above 1 is profitable). Avg R = average result in units of
the risk taken. Pass = how often a Topstep Combine started on a test day would have passed. "Settings to trade now" were
chosen on the most recent train window.</p></section>
<section class="panel"><h2>Settings chosen in each window</h2><div class="tablewrap"><table>
<thead><tr><th>Strategy</th>{window_head}</tr></thead><tbody>{window_rows}</tbody></table></div>
<p class="muted">If a strategy keeps choosing similar settings, its edge is more likely to be real than lucky.</p></section>
<footer>Generated {generated} by topstep-bot tune. Hypothetical results from historical simulation, judged only on
data the selection never saw - still no guarantee of future results.</footer>
</main>
<script>
const DATA = {data};
const css = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
const fmt = v => (v < 0 ? "-$" : "$") + Math.abs(v).toLocaleString(undefined, {{maximumFractionDigits:0}});
const NS = "http://www.w3.org/2000/svg";
function el(tag, attrs, parent) {{ const e = document.createElementNS(NS, tag);
  for (const k in attrs) e.setAttribute(k, attrs[k]); if (parent) parent.appendChild(e); return e; }}
function niceTicks(lo, hi, n) {{ const span = hi - lo || 1; const step0 = span / n;
  const mag = Math.pow(10, Math.floor(Math.log10(step0))); const step = [1,2,2.5,5,10].map(m => m*mag).find(s => span/s <= n) || mag*10;
  const out = []; for (let v = Math.ceil(lo/step)*step; v <= hi + 1e-9; v += step) out.push(v); return out; }}
const legend = document.getElementById("legend");
DATA.series.forEach(s => {{ const sp = document.createElement("span");
  sp.innerHTML = `<i style="background:var(--s${{s.slot + 1}})"></i>${{s.name}}${{s.recommended ? " (recommended)" : ""}}`;
  legend.appendChild(sp); }});
function draw() {{
  const box = document.getElementById("eq"); box.querySelectorAll("svg").forEach(n => n.remove());
  const days = [...new Set(DATA.series.flatMap(s => s.points.map(p => p[0])))].sort();
  if (!days.length) return;
  const idx = new Map(days.map((d, i) => [d, i]));
  const W = Math.max(box.clientWidth, 320), H = 320, L = 64, R = 16, T = 12, B = 28;
  const svg = el("svg", {{viewBox:`0 0 ${{W}} ${{H}}`, role:"img", "aria-label":"Cumulative out-of-sample profit and loss per strategy"}});
  box.insertBefore(svg, box.firstChild);
  const vals = DATA.series.flatMap(s => s.points.map(p => p[1])).concat([0]);
  let lo = Math.min(...vals), hi = Math.max(...vals); const pad = (hi - lo) * 0.06 || 100; lo -= pad; hi += pad;
  const x = i => L + (days.length === 1 ? 0 : i / (days.length - 1)) * (W - L - R);
  const y = v => T + (hi - v) / (hi - lo) * (H - T - B);
  DATA.folds.forEach((f, k) => {{ const a = idx.get(f.test_start); if (a === undefined) return;
    if (k > 0) el("line", {{x1:x(a), x2:x(a), y1:T, y2:H - B, stroke:css("--axis"), "stroke-dasharray":"4 4"}}, svg);
    const t = el("text", {{x:x(a) + 6, y:T + 12, "font-size":11, fill:css("--muted")}}, svg); t.textContent = `window ${{k + 1}}`; }});
  for (const t of niceTicks(lo, hi, 5)) {{ el("line", {{x1:L, x2:W - R, y1:y(t), y2:y(t), stroke:css("--grid")}}, svg);
    const tx = el("text", {{x:L - 8, y:y(t) + 4, "text-anchor":"end", "font-size":11, fill:css("--muted")}}, svg); tx.textContent = fmt(t); }}
  el("line", {{x1:L, x2:W - R, y1:y(0), y2:y(0), stroke:css("--axis")}}, svg);
  [0, Math.floor((days.length - 1) / 2), days.length - 1].forEach((i, k) => {{
    const t = el("text", {{x:x(i), y:H - 8, "font-size":11, fill:css("--muted"), "text-anchor": k === 0 ? "start" : (k === 2 ? "end" : "middle")}}, svg);
    t.textContent = days[i]; }});
  DATA.series.forEach(s => {{ if (!s.points.length) return;
    const d = s.points.map((p, i) => (i ? "L" : "M") + x(idx.get(p[0])) + "," + y(p[1])).join("");
    el("path", {{d, fill:"none", stroke:css(`--s${{s.slot + 1}}`), "stroke-width":2, "stroke-linejoin":"round"}}, svg); }});
  const cross = el("line", {{y1:T, y2:H - B, stroke:css("--muted"), "stroke-width":1, visibility:"hidden"}}, svg);
  const dots = DATA.series.map(s => el("circle", {{r:4, fill:css(`--s${{s.slot + 1}}`), stroke:css("--surface"), "stroke-width":2, visibility:"hidden"}}, svg));
  const lookup = DATA.series.map(s => new Map(s.points.map(p => [p[0], p[1]])));
  const tip = box.querySelector(".tip");
  el("rect", {{x:L, y:T, width:W - L - R, height:H - T - B, fill:"transparent"}}, svg).addEventListener("mousemove", ev => {{
    const r = svg.getBoundingClientRect(); const px = (ev.clientX - r.left) * W / r.width;
    const i = Math.max(0, Math.min(days.length - 1, Math.round((px - L) / (W - L - R) * (days.length - 1))));
    const day = days[i]; const xi = x(i);
    cross.setAttribute("x1", xi); cross.setAttribute("x2", xi); cross.setAttribute("visibility", "visible");
    let rowsHtml = "";
    DATA.series.forEach((s, k) => {{ const v = lookup[k].get(day);
      if (v === undefined) {{ dots[k].setAttribute("visibility", "hidden"); return; }}
      dots[k].setAttribute("cx", xi); dots[k].setAttribute("cy", y(v)); dots[k].setAttribute("visibility", "visible");
      rowsHtml += `<div><i style="background:var(--s${{s.slot + 1}})"></i>${{s.name}} <b>${{fmt(v)}}</b></div>`; }});
    tip.innerHTML = `<b>${{day}}</b>` + rowsHtml; tip.style.display = "block";
    const w = tip.offsetWidth; tip.style.left = Math.min(Math.max(xi * r.width / W + 12, 0), box.clientWidth - w) + "px";
    tip.style.top = "8px"; }});
  svg.addEventListener("mouseleave", () => {{ tip.style.display = "none"; cross.setAttribute("visibility", "hidden");
    dots.forEach(d => d.setAttribute("visibility", "hidden")); }});
}}
draw(); window.addEventListener("resize", draw);
window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", draw);
</script></body></html>
"""
