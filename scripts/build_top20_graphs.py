"""Build interactive per-strategy graph pages (holdings + 2y return + 6y return +
max drawdown) for each of the 26 merged top-20 strategies, plus a top-20 list page.

Reads real run_stats.csv (equity) + run_trades.csv (holdings/PnL) for BOTH windows
from the wave+retest dirs. Publishes noindex HTML into
glitch-strategy-infra/reports/top20/.
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

LUMIBOT = Path("/Users/openclaw/.hermes/profiles/free_money_glitch/home/lumibot")
REPORTS = LUMIBOT / "reports"
OUT = Path("/Users/openclaw/.hermes/profiles/free_money_glitch/glitch-strategy-infra/reports/top20")
ECHART_CDN = '<script src="https://cdn.jsdelivr.net/npm/echarts@5/dist/echarts.min.js"></script>'

WAVE_DIRS = {"1": "hts_v2_search_2026-09-17", "2": "hts_v2_search_wave2_2026-09-17", "3": "hts_v2_search_wave3_2026-09-17"}
TWO_DIR = "hts_v2_search_2yr_2026-09-17"


def _fmt_pct(v):
    try:
        return f"{float(v) * 100:.1f}%"
    except (TypeError, ValueError):
        return "–"


def _num(v, nd=3):
    try:
        return f"{float(v):.{nd}f}"
    except (TypeError, ValueError):
        return "–"


def _load_equity(path: Path) -> tuple[list, list, list]:
    """Return (dates, equity_norm, drawdown_pct) from run_stats.csv."""
    if not path.exists():
        return [], [], []
    st = pd.read_csv(path, parse_dates=["datetime"])
    st["ts"] = pd.to_datetime(st["datetime"], utc=True)
    st["day"] = st["ts"].dt.strftime("%Y-%m-%d")
    daily = st.sort_values("ts").groupby("day")["portfolio_value"].last()
    if len(daily) < 2:
        return [], [], []
    first = float(daily.iloc[0])
    norm = daily / first
    peak = norm.cummax()
    dd = (norm / peak - 1.0) * 100.0
    dates = list(daily.index)
    values = list(norm)
    ddvals = list(dd)
    # sample to ~300 (consistent positions across dates/equity/drawdown)
    if len(values) > 300:
        stride = len(values) // 300
        dates = dates[::stride]; values = values[::stride]; ddvals = ddvals[::stride]
    return dates, [round(v, 4) for v in values], [round(x, 3) for x in ddvals]


def _load_holdings_over_time(trades_path: Path, max_pts: int = 300) -> tuple[list, list]:
    """Return (dates, list_of_{symbol: value_t}) of mark-to-market gross exposure.

    Reconstructs per-symbol net position held over time from run_trades.csv fills
    and marks each holding carry-forward at its last fill price. Returns the
    cumulative per-symbol notional at sampled points (a holdings-over-time path).
    """
    if not trades_path.exists():
        return [], []
    tr = pd.read_csv(trades_path)
    fills = tr[(tr["status"].astype(str).str.lower() == "fill")] if "status" in tr else tr
    if fills.empty or "time" not in fills:
        return [], []
    fills = fills.sort_values("time")
    ts = pd.to_datetime(fills["time"], utc=True)
    times = list(ts)
    # build running position/book
    pos: dict[str, float] = {}
    book: dict[str, float] = {}
    points: list[tuple[pd.Timestamp, dict[str, float]]] = []
    for i, (_, row) in enumerate(fills.iterrows()):
        s = str(row["symbol"]); side = str(row["side"]).lower()
        qty = float(pd.to_numeric(row.get("filled_quantity"), errors="coerce") or 0.0)
        px = float(pd.to_numeric(row.get("price"), errors="coerce") or 0.0)
        if qty <= 0 or px <= 0:
            continue
        if side == "buy":
            pos[s] = pos.get(s, 0.0) + qty
            book[s] = px
        else:
            pos[s] = pos.get(s, 0.0) - qty
            book[s] = px
        if pos.get(s, 0.0) <= 1e-12:
            pos.pop(s, None)
        # snapshot notional of current holdings
        snapshot = {s2: round(pos[s2] * book[s2], 2) for s2 in pos}
        points.append((times[i], snapshot))
    if not points:
        return [], []
    # sample to max_pts
    if len(points) > max_pts:
        stride = len(points) // max_pts
        points = points[::stride]
    dates = [pts.strftime("%Y-%m-%d") for pts, _ in points]
    series = [{s: v for s, v in snapshot.items()} for _, snapshot in points]
    return dates, series


def _load_holdings(trades_path: Path) -> dict[str, float]:
    """Return {symbol: net_pnl} from run_trades.csv fills."""
    if not trades_path.exists():
        return {}
    tr = pd.read_csv(trades_path)
    fills = tr[tr["status"].astype(str).str.lower() == "fill"] if "status" in tr else tr
    bysym = {}
    for _, row in fills.iterrows():
        s = str(row["symbol"]); side = str(row["side"])
        qty = float(row.get("filled_quantity") or 0)
        px = float(row.get("price") or 0)
        cst = float(row.get("trade_cost") or 0) + float(row.get("trade_slippage") or 0)
        d = bysym.setdefault(s, {"qty": 0.0, "cb": 0.0, "real": 0.0, "cst": 0.0})
        if side == "buy":
            d["qty"] += qty; d["cb"] += qty * px; d["cst"] += cst
        else:
            if d["qty"]:
                d["real"] += qty * px - d["cb"] * (qty / d["qty"]); d["cb"] -= d["cb"] * (qty / d["qty"]); d["qty"] -= qty
            d["cst"] += cst
    return {s: round(d["real"] - d["cst"], 2) for s, d in bysym.items()}


def _build_one(entry: dict, variant: str = "base", b_lut: dict | None = None) -> None:
    cid = entry["candidate_id"]
    if variant == "b":
        suffix = "-b"
        # soxl-free variants live under <cid>-b dirs
        six_dir = REPORTS / "hts_v2_soxlfree_2026-09-17" / f"{cid}-b" / "six_year"
        two_dir = REPORTS / "hts_v2_soxlfree_2026-09-17" / f"{cid}-b" / "two_year"
        EW = REPORTS / "hts_v2_soxlfree_extrawindows_2026-09-18" / f"{cid}-b-ew"
        title_cid = f"{cid}-b"
    else:
        variant = "base"; suffix = ""
        six_dir = None
        for wd in WAVE_DIRS.values():
            cand = REPORTS / wd / cid / "six_year"
            if (cand / "run_stats.csv").exists():
                six_dir = cand
                break
        if six_dir is None:
            print("WARN no six-year artifacts for", cid)
            return None
        two_dir = REPORTS / TWO_DIR / cid / "two_year"
        EW = REPORTS / "hts_v2_extrawindows_2026-09-18" / f"{cid}-ew"
        title_cid = cid

    d6, e6, dd6 = _load_equity(six_dir / "run_stats.csv")
    d2, e2, dd2 = _load_equity(two_dir / "run_stats.csv")
    h6 = _load_holdings(six_dir / "run_trades.csv")
    h2 = _load_holdings(two_dir / "run_trades.csv")

    # 2018->2022 (early) and 2022->2024 windows
    d_early, e_early, dd_early = _load_equity(EW / "early" / "run_stats.csv")
    d_2224, e_2224, dd_2224 = _load_equity(EW / "y2022_2024" / "run_stats.csv")
    # holdings over time (6y path, carry-forward notional per symbol)
    hot_dates, hot_series = _load_holdings_over_time(six_dir / "run_trades.csv")

    # metrics table
    six_rank = ""  # filled by caller optionally
    params = entry.get("params", {})
    soxl_badge = "<span class='badge'>SOXL-free</span>" if variant == "b" else ""
    # For the SOXL-free (-b) variant, prefer its OWN results (b_lut keyed by base
    # candidate_id) over the base entry's metrics, so the table is not mislabelled.
    if variant == "b":
        m6 = _gw(b_lut, cid, "six_year") or {}
        m2 = _gw(b_lut, cid, "two_year") or {}
        sh6 = m6.get("sharpe"); rt6 = m6.get("total_return"); dd6v = m6.get("max_drawdown"); fl6 = m6.get("fills")
        sh2 = m2.get("sharpe"); rt2 = m2.get("total_return"); dd2v = m2.get("max_drawdown")
    else:
        sh6 = entry.get("sharpe"); rt6 = entry.get("total_return"); dd6v = entry.get("max_drawdown"); fl6 = entry.get("fills")
        sh2 = entry.get("sharpe_2y"); rt2 = entry.get("total_return_2y"); dd2v = entry.get("max_drawdown_2y")
    rows_html = "\n".join(
        f"<tr><td class='k'>{k}</td><td>{html_esc(str(v))}</td></tr>" for k, v in [
            ("Candidate", title_cid), ("Family", params.get("family_id", "")),
            ("6y Sharpe", _num(sh6)), ("6y Return", _fmt_pct(rt6)),
            ("6y Max DD", _fmt_pct(dd6v)), ("6y Fills", fl6),
        ]
    )
    # holdings bars
    def _holdings_html(h, label):
        if not h:
            return f"<p class='muted'>No trades captured ({label}).</p><div id='h_{label}' style='height:200px'></div>"
        items = sorted(h.items(), key=lambda kv: -kv[1])
        top = items[:20]
        names = json.dumps([s for s, _ in top])
        vals = json.dumps([round(v) for _, v in top])
        pos = sum(1 for _, v in top if v > 0)
        return (
            f"<div id='h_{label}' style='width:100%;height:320px'></div>"
            f"<p class='muted'>{len(items)} positions, {pos} winners ({label})</p>"
            f"<script>window['PL_{label}']={{names:{names},vals:{vals}}};</script>"
        )

    doc = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow,noarchive">
<title>{title_cid} — top-20 graph</title>
{ECHART_CDN}
<style>
:root{{--bg:#0D0F12;--card:#161920;--border:#262933;--green:#22C55E;--red:#EF4444;--muted:#8A91A0;--text:#E5E7EB}}
*{{box-sizing:border-box}} body{{background:var(--bg);color:var(--text);font-family:Inter,sans-serif;margin:0;padding:24px}}
.wrap{{max-width:1150px;margin:0 auto}} h1{{font-size:22px;margin-bottom:4px}}
.sub{{color:var(--muted);font-size:13px;margin-bottom:16px}}
.card{{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:16px;margin-bottom:16px}}
table{{border-collapse:collapse;font-size:13px}} td{{padding:5px 10px;border-bottom:1px solid var(--border)}}
td.k{{color:var(--muted);width:150px}}
.muted{{color:var(--muted);font-size:12px}} .warn{{background:#2a1f10;border:1px solid #8a5a1a;border-radius:8px;padding:10px 14px;color:#f0c674;font-size:13px;margin-bottom:16px}}
footer{{color:var(--muted);font-size:11px;margin-top:8px}}
</style></head><body><div class="wrap">
<h1>{title_cid} — top-20 strategy graphs {soxl_badge}</h1>
<div class="sub">Discovery/retrospective only · native LumiBot engine · <b>noindex</b></div>
<div class="warn"><b>Not a deployable edge.</b> Discovery rankings; our walk-forward gate selected cash. LumiBot's built-in tearsheet can't render hourly data, so this audited-native-data page is used instead.</div>
<div class="card"><table>{rows_html}
<tr><td class='k'>6y ATR period</td><td>{params.get('atr_period')}</td></tr>
<tr><td class='k'>6y ATR k</td><td>{params.get('atr_k')}</td></tr>
<tr><td class='k'>6y min hold</td><td>{params.get('min_position_holding_bars')}</td></tr>
<tr><td class='k'>6y vol target</td><td>{params.get('vol_target')}</td></tr>
<tr><td class='k'>6y weight</td><td>{params.get('weight_mode')}</td></tr>
<tr><td class='k'>6y exit</td><td>{params.get('exit_mode')}</td></tr>
<tr><td class='k'>6y top_n</td><td>{params.get('top_n')}</td></tr>
<tr><td class='k'>2y Sharpe</td><td>{_num(sh2)}</td></tr>
<tr><td class='k'>2y Return</td><td>{_fmt_pct(rt2)}</td></tr>
<tr><td class='k'>2y Max DD</td><td>{_fmt_pct(dd2v)}</td></tr></table></div>

<div class="card"><h2>6-year equity &amp; drawdown</h2><div id="c6" style="width:100%;height:360px"></div></div>
<div class="card"><h2>2-year equity &amp; drawdown</h2><div id="c2" style="width:100%;height:360px"></div></div>
<div class="card"><h2>2018&ndash;2022 return &amp; drawdown</h2><div id="ce" style="width:100%;height:360px"></div><p class="muted">2018-05-01 &#8594; 2021-12-31 (data begins 2018-05; no pre-window warmup)</p></div>
<div class="card"><h2>2022&ndash;2024 return &amp; drawdown</h2><div id="c2224" style="width:100%;height:360px"></div><p class="muted">2022-01-01 &#8594; 2024-12-31 (2022 bear, 2023 grind, 2024 recovery)</p></div>
<div class="card"><h2>Holdings over time — 6-year (gross notional per symbol)</h2><div id="ht" style="width:100%;height:420px"></div><p class="muted">Carry-forward position notional marked at last fill price; top symbols only.</p></div>
<div class="card"><h2>Holdings — 6-year (per-symbol net PnL)</h2>{_holdings_html(h6, "6y")}</div>
<div class="card"><h2>Holdings — 2-year (per-symbol net PnL)</h2>{_holdings_html(h2, "2y")}</div>
<footer>Generated from run_stats.csv + run_trades.csv (native LumiBot backtest artifacts). Crawlers blocked.</footer>
<script>
var D6 = {json.dumps(d6 or [])}, E6 = {json.dumps(e6 or [])}, DD6 = {json.dumps(dd6 or [])};
var D2 = {json.dumps(d2 or [])}, E2 = {json.dumps(e2 or [])}, DD2 = {json.dumps(dd2 or [])};
var DE = {json.dumps(d_early or [])}, EE = {json.dumps(e_early or [])}, DDE = {json.dumps(dd_early or [])};
var D4 = {json.dumps(d_2224 or [])}, E4 = {json.dumps(e_2224 or [])}, DD4 = {json.dumps(dd_2224 or [])};
var HT_D = {json.dumps(hot_dates or [])}, HT_X = {json.dumps(hot_series or [])};
function mkChart(el, dates, eq, dd) {{
  var c = echarts.init(document.getElementById(el));
  c.setOption({{
    tooltip: {{trigger:'axis', backgroundColor:'#1E232B', borderColor:'#262933', textStyle:{{color:'#FFF',fontSize:12}}}},
    legend: {{data:['Equity','Drawdown'], textStyle:{{color:'#8A91A0'}}}},
    grid:[{{left:60,right:20,top:40,height:'62%'}},{{left:60,right:20,top:'80%',height:'15%'}}],
    xAxis:[{{type:'category',data:dates,gridIndex:0,axisLabel:{{color:'#8A91A0'}}}},{{type:'category',data:dates,gridIndex:1,axisLabel:{{show:false}}}}],
    yAxis:[{{type:'value',gridIndex:0,scale:true,axisLabel:{{color:'#8A91A0'}},splitLine:{{lineStyle:{{color:'#262933'}}}}}},{{type:'value',gridIndex:1,scale:true,axisLabel:{{color:'#8A91A0',formatter:'{{value}}%'}},splitLine:{{lineStyle:{{color:'#262933'}}}}}}],
    series:[
      {{name:'Equity',type:'line',data:eq,showSymbol:false,lineWidth:1.6,areaStyle:{{opacity:0.12,color:'#22C55E'}},lineStyle:{{color:'#22C55E'}}}},
      {{name:'Drawdown',type:'line',data:dd,showSymbol:false,lineWidth:1.2,lineStyle:{{color:'#EF4444'}},areaStyle:{{opacity:0.2,color:'#EF4444'}},xAxisIndex:1,yAxisIndex:1}}
    ]
  }});
  window.addEventListener('resize', function(){{c.resize();}});
}}
function mkHold(el, names, vals) {{
  var c = echarts.init(document.getElementById(el));
  c.setOption({{
    tooltip: {{trigger:'axis', backgroundColor:'#1E232B', borderColor:'#262933', textStyle:{{color:'#FFF',fontSize:12}}}},
    grid: {{left:60, right:20, top:20, bottom:70}},
    xAxis: {{type:'category', data:names, axisLabel:{{color:'#8A91A0', rotate:45}}}},
    yAxis: {{type:'value', axisLabel:{{color:'#8A91A0'}}, splitLine:{{lineStyle:{{color:'#262933'}}}}}},
    series: [{{type:'bar', data:vals.map(function(v,i){{return {{value:v,itemStyle:{{color: v>=0?'#22C55E':'#EF4444'}}}};}}), barMaxWidth: 30}}]
  }});
  window.addEventListener('resize', function(){{c.resize();}});
}}
function mkHoldTime(el, dates, series) {{
  var c = echarts.init(document.getElementById(el));
  var syms = {{}};
  series.forEach(function (s) {{ Object.keys(s).forEach(function (k) {{ syms[k] = 1; }}); }});
  var all = Object.keys(syms);
  var top = all.sort(function (a, b) {{
    var sa = 0, sb = 0;
    series.forEach(function (s) {{ sa += s[a] || 0; }});
    series.forEach(function (s) {{ sb += s[b] || 0; }});
    return sb - sa;
  }});
  var keep = top.slice(0, 12);
  var seriesJS = keep.map(function (sym, i) {{
    var data = series.map(function (s) {{ return s[sym] || 0; }});
    var palette = ['#22C55E','#3B82F6','#F59E0B','#EF4444','#8B5CF6','#14B8A6','#EC4899','#84CC16','#F97316','#0EA5E9','#A3E635','#E11D48'];
    return {{ name: sym, type: 'line', stack: 'h', areaStyle: {{opacity:0.7}}, lineStyle: {{width:0}}, data: data.map(function(v){{return Math.round(v);}}), itemStyle:{{color:palette[i%palette.length]}} }};
  }});
  c.setOption({{
    tooltip: {{trigger:'axis', backgroundColor:'#1E232B', borderColor:'#262933', textStyle:{{color:'#FFF',fontSize:12}}}},
    legend: {{type:'scroll', bottom:0, textStyle:{{color:'#8A91A0',fontSize:10}}}},
    grid: {{left:70, right:20, top:40, bottom:70}},
    xAxis: {{type:'category', data:dates, axisLabel:{{color:'#8A91A0', fontSize:10}}}},
    yAxis: {{type:'value', axisLabel:{{color:'#8A91A0'}}, splitLine:{{lineStyle:{{color:'#262933'}}}}}},
    series: seriesJS
  }});
  window.addEventListener('resize', function(){{c.resize();}});
}}
if (D6.length) mkChart('c6', D6, E6, DD6);
if (D2.length) mkChart('c2', D2, E2, DD2);
if (DE.length) mkChart('ce', DE, EE, DDE);
if (D4.length) mkChart('c2224', D4, E4, DD4);
if (HT_D.length && HT_X.length) mkHoldTime('ht', HT_D, HT_X);
if (window.PL_6y) mkHold('h_6y', window.PL_6y.names, window.PL_6y.vals);
if (window.PL_2y) mkHold('h_2y', window.PL_2y.names, window.PL_2y.vals);
</script>
</div></body></html>"""
    (OUT / f"{title_cid}_graphs.html").write_text(doc, encoding="utf-8")
    return len(doc)


def _wind_lookup(jsonl_loader_windows):
    """Build {(cid, window): record} from an already-split source list."""
    return {(r["candidate_id"], r["window"]): r for r in jsonl_loader_windows}


def _gw(lut, cid, window):
    rec = lut.get((cid, window))
    return rec


def _list_row(i, cid, rel, base_or_b, lookup_cid=None):
    """Build one <tr>. `rel` is the (cid, window) lookup for the current variant.

    `cid` is the display id (e.g. "W0007-b"); `lookup_cid` is the key actually
    present in `rel` (the result JSONLs are keyed by the BASE id). Defaults to
    `cid`. This fixes the -b rows reading metrics by the "-b" key, which never
    equals the base-id key the soxlfree JSONLs use.
    """
    lk = lookup_cid if lookup_cid is not None else cid
    def g(window):
        return _gw(rel, lk, window) or {}
    s6 = g("six_year").get("sharpe"); r6 = g("six_year").get("total_return"); d6 = g("six_year").get("max_drawdown")
    s2 = g("two_year").get("sharpe"); r2 = g("two_year").get("total_return"); d2 = g("two_year").get("max_drawdown")
    se = g("early").get("sharpe"); re_ = g("early").get("total_return")
    sy = g("y2022_2024").get("sharpe"); ry = g("y2022_2024").get("total_return")
    badge = "<span class='badge'>(no SOXL)</span>" if base_or_b == "b" else ""
    return (
        "<tr><td class='num'>{}</td><td><a href='{}_graphs.html'><b>{}{}</b></a></td>"
        "<td class='num'>{}</td><td class='num'>{}</td><td class='num'>{}</td>"
        "<td class='num'>{}</td><td class='num'>{}</td><td class='num'>{}</td>"
        "<td class='num'>{}</td><td class='num'>{}</td>"
        "<td class='num'>{}</td><td class='num'>{}</td><td class='num'></td></tr>".format(
            i, cid, cid, badge,
            _num(s6), _fmt_pct(r6), _fmt_pct(d6),
            _num(s2), _fmt_pct(r2), _fmt_pct(d2),
            _num(se), _fmt_pct(re_),
            _num(sy), _fmt_pct(ry)))


def html_esc(s):
    import html
    return html.escape(s)


def _load_jsonl(rel: str) -> list[dict]:
    p = REPORTS / rel
    return [json.loads(l) for l in open(p)] if p.exists() else []


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    merged = json.load(open(REPORTS / "hts_v2_sharpe_top20_merged_2026-09-17.json"))
    # the merged list holds six-year entries; attach 2-year metrics from two_top
    two = {r["candidate_id"]: r for r in merged["two_top"]}
    entries = list(merged["merged"])

    # Per-variant (cid, window) metric lookups
    base_lut = _wind_lookup(
        _load_jsonl("hts_v2_extrawindows_2026-09-18/search_results_extrawindows.jsonl"))
    b_lut = _wind_lookup(
        _load_jsonl("hts_v2_soxlfree_extrawindows_2026-09-18/search_results_soxlfree_extrawindows.jsonl"))
    b6_lut = _wind_lookup(
        _load_jsonl("hts_v2_soxlfree_2026-09-17/search_results_soxlfree.jsonl"))

    built = 0
    for e in entries:
        t = two.get(e["candidate_id"], {})
        e["sharpe_2y"] = t.get("sharpe"); e["total_return_2y"] = t.get("total_return"); e["max_drawdown_2y"] = t.get("max_drawdown")
        # base + soxl-free variant graph pages
        for variant in ("base", "b"):
            size = _build_one(e, variant=variant, b_lut=b_lut)
            if size is None:
                print("skipped", e["candidate_id"], variant)
                continue
            built += 1
            print(f"built {e['candidate_id']}{'-b' if variant=='b' else ''} ({size} bytes)")

    # list page: base rows then -b rows
    rows = []
    for i, e in enumerate(entries, 1):
        cid = e["candidate_id"]
        base_lut[(cid, "six_year")] = e  # 6y metrics live on the merged entry
        base_lut[(cid, "two_year")] = two.get(cid, {})
        rows.append(_list_row(i, cid, base_lut, "base"))
    bi = len(entries) + 1
    for e in entries:
        cid = e["candidate_id"]
        # -b variant rows: their own 6y/2y (b6_lut) + their early/2022-24 (b_lut).
        # The soxlfree JSONLs are keyed by the BASE id, so pass lookup_cid=cid.
        b_lut[(cid, "six_year")] = b6_lut.get((cid, "six_year"), {})
        b_lut[(cid, "two_year")] = b6_lut.get((cid, "two_year"), {})
        rows.append(_list_row(bi, f"{cid}-b", b_lut, "b", lookup_cid=cid))
        bi += 1
    idx = f"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="robots" content="noindex,nofollow,noarchive"><title>HTS v2 — Top-20 (6y &times; 2y)</title>
<style>body{{background:#0D0F12;color:#E5E7EB;font-family:Inter,sans-serif;padding:24px}}
h1{{font-size:22px}} a{{color:#22C55E;text-decoration:none}} a:hover{{text-decoration:underline}}
table{{border-collapse:collapse;margin-top:12px}} th,td{{border-bottom:1px solid #262933;padding:7px 12px;font-size:13px;text-align:left}}
th{{color:#8A91A0}} td.num{{font-variant-numeric:tabular-nums}} .muted{{color:#8A91A0;font-size:12px}}
.badge{{color:#F59E0B;font-size:11px}}
th.sortable{{cursor:pointer;user-select:none}} th.sortable:hover{{color:#E5E7EB}}
th.sortable .arrow{{color:#8A91A0;font-size:10px;margin-left:4px}}
</style>
<script>
document.addEventListener("DOMContentLoaded", function () {{
  var table = document.querySelector("table.sortable");
  var headers = table.querySelectorAll("th.sortable");
  headers.forEach(function (th, idx) {{
    th.addEventListener("click", function () {{
      var rows = Array.prototype.slice.call(table.querySelectorAll("tbody tr"));
      var dir = th.dataset.dir === "asc" ? "desc" : "asc";
      th.dataset.dir = dir;
      headers.forEach(function (h) {{ h.querySelector(".arrow").textContent = ""; }});
      th.querySelector(".arrow").textContent = dir === "asc" ? "\u25b2" : "\u25bc";
      var numFirst = th.classList.contains("num");
      rows.sort(function (a, b) {{
        var av = a.cells[idx].innerText.trim().replace(/[%$#,]/g, "");
        var bv = b.cells[idx].innerText.trim().replace(/[%$#,]/g, "");
        if (numFirst) {{ av = parseFloat(av) || -Infinity; bv = parseFloat(bv) || -Infinity; }}
        else {{ av = av.toLowerCase(); bv = bv.toLowerCase(); }}
        var cmp = av < bv ? -1 : av > bv ? 1 : 0;
        return dir === "asc" ? cmp : -cmp;
      }});
      rows.forEach(function (r) {{ table.querySelector("tbody").appendChild(r); }});
    }});
  }});
}});
</script></head><body>
<h1>HTS v2 — top-20 providers &amp; SOXL-free (-b) variants</h1>
<div class="muted">Discovery/retrospective only · native LumiBot engine · 3.5 bps/side · noindex · click a strategy for its graphs · click a column header to sort · <b>(no SOXL)</b> rows are the same recipe with SOXL removed from the universe</div>
<table class="sortable"><thead><tr>
<th class="sortable num">#<span class="arrow"></span></th><th class="sortable">ID<span class="arrow"></span></th><th class="sortable num">6y Sharpe<span class="arrow"></span></th><th class="sortable num">6y Return<span class="arrow"></span></th><th class="sortable num">6y MaxDD<span class="arrow"></span></th>
<th class="sortable num">2y Sharpe<span class="arrow"></span></th><th class="sortable num">2y Return<span class="arrow"></span></th><th class="sortable num">2y MaxDD<span class="arrow"></span></th>
<th class="sortable num">2018-22 Sharpe<span class="arrow"></span></th><th class="sortable num">2018-22 Return<span class="arrow"></span></th>
<th class="sortable num">2022-24 Sharpe<span class="arrow"></span></th><th class="sortable num">2022-24 Return<span class="arrow"></span></th>
</tr></thead><tbody>{''.join(rows)}</tbody></table>
</body></html>"""
    (OUT / "index.html").write_text(idx, encoding="utf-8")
    print("wrote", OUT / "index.html", "| built", built, "graph pages")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())