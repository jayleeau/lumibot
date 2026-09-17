"""Build interactive per-candidate tearsheets (equity + drawdown + metrics + trades)
for the top-10 v2 six-year performers, from the audited descriptive-fixed artifacts.

Each tearsheet is a self-contained noindex HTML with an ECharts equity/drawdown panel
(browser loads ECharts from CDN) + a metrics table + trade stats. Published into
glitch-strategy-infra/reports/tearsheets/.
"""
from __future__ import annotations

import html
import json
from pathlib import Path

import pandas as pd

LUMIBOT = Path("/Users/openclaw/.hermes/profiles/free_money_glitch/home/lumibot")
DESC = LUMIBOT / "reports" / "hts_v2_descriptive_fixed_2026-09-16"
OUT = Path("/Users/openclaw/.hermes/profiles/free_money_glitch/glitch-strategy-infra/reports/tearsheets")

TOP10 = ["V048", "V047", "V042", "V059", "V022", "V041", "V021", "V058", "V061", "V054"]
ECHART_CDN = '<script src="https://cdn.jsdelivr.net/npm/echarts@5/dist/echarts.min.js"></script>'


def _fmt_pct(v):
    try:
        return f"{float(v) * 100:.2f}%"
    except (TypeError, ValueError):
        return "–"


def _num(v, nd=3):
    try:
        return f"{float(v):.{nd}f}"
    except (TypeError, ValueError):
        return "–"


def _build_one(summ, cid) -> None:
    six = next(r for r in summ["results"] if r["candidate_id"] == cid and r["window"] == "six_year")
    run_dir = DESC / cid / "six_year"
    stats = pd.read_csv(run_dir / "run_stats.csv", parse_dates=["datetime"])
    stats["ts"] = pd.to_datetime(stats["datetime"], utc=True)
    stats["day"] = stats["ts"].dt.strftime("%Y-%m-%d")
    daily = stats.sort_values("ts").groupby("day")["portfolio_value"].last()
    rets = daily.pct_change().dropna()
    equity = (1 + rets).cumprod()
    peak = equity.cummax()
    dd = equity / peak - 1.0
    # sample to ~400 points for chart
    pts = daily
    if len(pts) > 400:
        stride = len(pts) // 400
        pts = pts.iloc[::stride]
    dates = [str(pts.index[i])[:10] for i in range(len(pts))]
    eq_vals = [round(v, 4) for v in pts.values]

    # drawdown aligned to sample dates (resample picks nearest)
    dd_key = pd.to_datetime(dd.index).strftime("%Y-%m-%d")
    dd_map = dict(zip(dd_key, dd.values))
    dd_vals = [round(dd_map.get(d, 0.0), 5) for d in dates]

    metrics = six.get("metrics")
    cand = None
    try:
        import sys
        sys.path.insert(0, str(LUMIBOT))
        from strategy_lab.experiment_registry import get_registry
        cand = get_registry().get(cid)
    except Exception:
        pass
    desc = getattr(cand, "rule", "") or ""
    fam = getattr(cand, "family_id", "") or ""

    eq_json = json.dumps(eq_vals)
    dd_json = json.dumps(dd_vals)
    dates_json = json.dumps(dates)

    stats_rows = [
        ("Candidate", cid),
        ("Family", fam),
        ("Rule", desc),
        ("Sharpe (daily)", _num(six.get("sharpe"))),
        ("Total return", _fmt_pct(six.get("total_return"))),
        ("Max drawdown", _fmt_pct(six.get("max_drawdown"))),
        ("Final equity", f"${six.get('final_equity', 0):,.0f}"),
        ("Fills", six.get("fills")),
        ("Sessions", six.get("metrics", {}).get("sessions")),
        ("Cost", f"{six.get('metrics', {}).get('cost_bps_per_side', '3.5') if 'cost_bps_per_side' in (six.get('metrics',{}) or {}) else '3.5'} bps/side"),
    ]
    stats_html = "\n".join(
        f"<tr><td class='k'>{html.escape(k)}</td><td>{html.escape(str(v)) if not str(v).startswith('<') else v}</td></tr>"
        for k, v in stats_rows
    )

    doc = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow,noarchive">
<title>{cid} — six-year tear sheet</title>
{ECHART_CDN}
<style>
:root{{--bg:#0D0F12;--card:#161920;--border:#262933;--green:#22C55E;--red:#EF4444;--muted:#8A91A0;--text:#E5E7EB}}
*{{box-sizing:border-box}} body{{background:var(--bg);color:var(--text);font-family:Inter,-apple-system,Segoe UI,Roboto,sans-serif;margin:0;padding:24px}}
.wrap{{max-width:1100px;margin:0 auto}} h1{{font-size:22px;margin-bottom:4px}}
.sub{{color:var(--muted);font-size:13px;margin-bottom:16px}}
.card{{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:16px;margin-bottom:16px}}
table{{border-collapse:collapse;font-size:13px}} td{{padding:5px 10px;border-bottom:1px solid var(--border)}}
td.k{{color:var(--muted);width:170px}}
.warn{{background:#2a1f10;border:1px solid #8a5a1a;border-radius:8px;padding:10px 14px;color:#f0c674;font-size:13px;margin-bottom:16px}}
footer{{color:var(--muted);font-size:11px;margin-top:8px}}
</style></head><body><div class="wrap">
<h1>{cid} — six-year tear sheet (2020-09-08 → 2026-09-08)</h1>
<div class="sub">RegistryHtsStrategy · native LumiBot engine · 3.5 bps/side · daily-return Sharpe recomputed independently · <b>noindex</b></div>
<div class="warn">Discovery/retrospective evidence only. Not a deployable edge — the v2 walk-forward selected <b>cash on all folds</b>. LumiBot's built-in QuantStats tearsheet cannot render for hourly data (benchmark alignment degenerates), so this audited native-data tearsheet is used instead.</div>
<div class="card"><table>{stats_html}</table></div>
<div class="card"><h2>Equity curve (normalized) &amp; drawdown</h2>
<div id="chart" style="width:100%;height:400px"></div></div>
<footer>Generated 2026-09-17 from <code>reports/hts_v2_descriptive_fixed_2026-09-16/{cid}/six_year/run_stats.csv</code>. Crawlers blocked.</footer>
<script>
(function(){{
  var el = document.getElementById('chart');
  var chart = echarts.init(el);
  var dates = {dates_json};
  var eq = {eq_json};
  var ddd = {dd_json};
  var opt = {{
    tooltip: {{ trigger: 'axis', backgroundColor:'#1E232B', borderColor:'#262933',
               textStyle:{{color:'#FFF',fontSize:12}},
               axisPointer:{{ type:'cross' }},
               valueFormatter: function(v){{ return v==null ? '—' : v.toFixed(2); }} }},
    legend: {{ data:['Equity','Drawdown'], textStyle:{{color:'#8A91A0'}} }},
    grid: [{{ left:60, right:20, top:40, height:'60%' }}, {{ left:60, right:20, top:'75%', height:'18%' }}],
    xAxis: [
      {{ type:'category', data:dates, gridIndex:0, axisLabel:{{color:'#8A91A0'}} }},
      {{ type:'category', data:dates, gridIndex:1, axisLabel:{{show:false}} }}
    ],
    yAxis: [
      {{ type:'value', gridIndex:0, scale:true, axisLabel:{{color:'#8A91A0'}},
         splitLine:{{lineStyle:{{color:'#262933'}}}} }},
      {{ type:'value', gridIndex:1, scale:true, axisLabel:{{color:'#8A91A0',formatter:'{{value}}%'}},
         splitLine:{{lineStyle:{{color:'#262933'}}}} }}
    ],
    series: [
      {{ name:'Equity', type:'line', data:eq, showSymbol:false, lineWidth:1.6,
         emphasis:{{focus:'series'}},
         areaStyle:{{opacity:0.12, color:'#22C55E'}},
         lineStyle:{{color:'#22C55E'}} }},
      {{ name:'Drawdown', type:'line', data:ddd, showSymbol:false, lineWidth:1.2,
         emphasis:{{focus:'series'}}, lineStyle:{{color:'#EF4444'}},
         areaStyle:{{opacity:0.2, color:'#EF4444'}}, xAxisIndex:1, yAxisIndex:1 }}
    ]
  }};
  chart.setOption(opt);
  window.addEventListener('resize', function(){{ chart.resize(); }});
}})();
</script>
</div></body></html>"""
    (OUT / f"{cid}_six_year_tearsheet.html").write_text(doc, encoding="utf-8")
    print("wrote", OUT / f"{cid}_six_year_tearsheet.html", len(doc), "bytes")


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    summ = json.load(open(DESC / "suite_summary.json"))
    for cid in TOP10:
        _build_one(summ, cid)
    # index
    links = "\n".join(
        f'<li><a href="{cid}_six_year_tearsheet.html">{cid}</a> — six-year</li>' for cid in TOP10
    )
    idx = f"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="robots" content="noindex,nofollow,noarchive"><title>Top-10 v2 tearsheets</title>
<style>body{{background:#0D0F12;color:#E5E7EB;font-family:Inter,sans-serif;padding:24px}} a{{color:#22C55E}}
li{{margin:6px 0}}</style></head><body><h1>Top-10 v2 six-year tearsheets</h1>
<div style="color:#8A91A0;font-size:13px">Discovery/retrospective only. noindex.</div><ul>{links}</ul></body></html>"""
    (OUT / "index.html").write_text(idx, encoding="utf-8")
    print("wrote", OUT / "index.html")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())