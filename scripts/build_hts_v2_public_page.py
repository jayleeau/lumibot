#!/usr/bin/env python3
"""Build a self-contained, non-indexable v2 HTS results page from the fixed
descriptive suite + the corrected walk-forward outcome, into
glitch-strategy-infra/reports/."""
from __future__ import annotations

import html
import json
from pathlib import Path

LUMIBOT = Path("/Users/openclaw/.hermes/profiles/free_money_glitch/home/lumibot")
DESC = LUMIBOT / "reports" / "hts_v2_descriptive_fixed_2026-09-16"
WF = LUMIBOT / "reports" / "hts_v2_walkforward_2026-09-16_02"
OUT_DIR = Path("/Users/openclaw/.hermes/profiles/free_money_glitch/glitch-strategy-infra/reports")


def _pct(v):
    try:
        return f"{float(v) * 100:.1f}%"
    except (TypeError, ValueError):
        return "–"


def _num(v, nd=3):
    try:
        return f"{float(v):.{nd}f}"
    except (TypeError, ValueError):
        return "–"


def main() -> int:
    s = json.load(open(DESC / "suite_summary.json"))
    res = s["results"]
    wf = json.load(open(WF / "walk_forward_report.json"))

    six = [r for r in res if r["window"] == "six_year"]
    six.sort(key=lambda r: r.get("sharpe") if r.get("sharpe") is not None else -999, reverse=True)

    rows = []
    for r in six:
        cid = r["candidate_id"]
        rank = sum(1 for x in six if (x.get("sharpe") or -999) > (r.get("sharpe") or -999)) + 1
        rows.append(
            "<tr>"
            f"<td class='num'>{rank}</td>"
            f"<td><b>{html.escape(cid)}</b></td>"
            f"<td class='num'>{_num(r.get('sharpe'))}</td>"
            f"<td class='num'>{_pct(r.get('total_return'))}</td>"
            f"<td class='num'>{_pct(r.get('max_drawdown'))}</td>"
            "</tr>"
        )
    # per-fold selection (all cash) honesty table
    fold_rows = []
    for e in wf["selection_per_fold"]:
        fold_rows.append(
            "<tr>"
            f"<td><b>{html.escape(str(e['fold']))}</b></td>"
            f"<td>{html.escape(str(e.get('outer_block')))}</td>"
            f"<td>{html.escape(str(e.get('selected_id')) or 'CASH')}</td>"
            "</tr>"
        )

    revision = s.get("implementation_revision", "?")
    doc = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow,noarchive">
<title>HTS v2 Results — robustness suite (V001-V100)</title>
<style>
:root{{--bg:#0D0F12;--card:#161920;--border:#262933;--green:#22C55E;--red:#EF4444;--amber:#F59E0B;--muted:#8A91A0;--text:#E5E7EB}}
*{{box-sizing:border-box}}
body{{background:var(--bg);color:var(--text);font-family:Inter,-apple-system,Segoe UI,Roboto,sans-serif;margin:0;padding:24px}}
.wrap{{max-width:1200px;margin:0 auto}}
h1{{font-size:22px}} h2{{font-size:15px;color:var(--muted)}}
.sub{{color:var(--muted);font-size:13px;margin-bottom:16px}}
.card{{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:16px;margin-bottom:16px;overflow-x:auto}}
table{{border-collapse:collapse;width:100%;font-size:13px}}
th,td{{border-bottom:1px solid var(--border);padding:6px 8px;text-align:left;white-space:nowrap}}
th{{color:var(--muted)}} td.num{{font-variant-numeric:tabular-nums}}
.warn{{background:#2a1f10;border:1px solid #8a5a1a;border-radius:8px;padding:10px 14px;color:#f0c674;font-size:13px;margin-bottom:16px}}
.bad{{color:var(--red)}}
.good{{color:var(--green)}}
footer{{color:var(--muted);font-size:11px;margin-top:8px}}
th.sortable{{cursor:pointer;user-select:none}}
th.sortable:hover{{color:var(--text)}}
th.sortable .arrow{{color:var(--muted);font-size:10px;margin-left:4px}}
</style>
<script>
document.addEventListener("DOMContentLoaded", function () {{
  document.querySelectorAll("table.sortable").forEach(function (table) {{
    var headers = table.querySelectorAll("th.sortable");
    headers.forEach(function (th, idx) {{
      th.addEventListener("click", function () {{
        var tb = table.querySelector("tbody");
        var rows = Array.prototype.slice.call(tb.querySelectorAll("tr"));
        var dir = th.dataset.dir === "asc" ? "desc" : "asc";
        th.dataset.dir = dir;
        headers.forEach(function (h) {{ h.querySelector(".arrow").textContent = ""; }});
        th.querySelector(".arrow").textContent = dir === "asc" ? "▲" : "▼";
        var numFirst = th.classList.contains("num");
        rows.sort(function (a, b) {{
          var av = a.cells[idx].innerText.trim();
          var bv = b.cells[idx].innerText.trim();
          if (numFirst) {{
            av = parseFloat((av + "").replace(/[%$,]/g, "")) || -Infinity;
            bv = parseFloat((bv + "").replace(/[%$,]/g, "")) || -Infinity;
          }} else {{
            av = av.toLowerCase(); bv = bv.toLowerCase();
          }}
          var cmp = av < bv ? -1 : av > bv ? 1 : 0;
          return dir === "asc" ? cmp : -cmp;
        }});
        rows.forEach(function (r) {{ tb.appendChild(r); }});
      }});
    }});
  }});
}});
</script></head><body><div class="wrap">
<h1>HTS v2 — robustness suite (V001–V100)</h1>
<div class="sub">Native LumiBot engine · revision {html.escape(revision)} · generated 2026-09-17 · 3.5&nbsp;bps/side · six-year + two-year, daily-return Sharpe</div>

<div class="warn"><b>Honest forward verdict: no v2 candidate qualified.</b>
The walk-forward gate (6 discovery blocks → held-out test, selection locked before reveal) selects
<b>CASH on all six folds</b>. The predeclared anti-edge gates — median per-position PnL &gt; 0,
top-3 PnL concentration ≤ 50%, and transaction cost ≤ 10% of gross — each independently reject
<b>all 100</b> v2 candidates on every fold. The table below is <b>discovery/retrospective</b> evidence
only, not a deployable edge. No live or paper trading is implied.</div>

<div class="card"><h2>Walk-forward selection (honest gate)</h2>
<table class="sortable"><thead><tr><th class="sortable">Fold<span class="arrow"></span></th><th class="sortable">Outer block<span class="arrow"></span></th><th class="sortable">Selected<span class="arrow"></span></th></tr></thead>
<tbody>{''.join(fold_rows)}</tbody></table>
</div>

<div class="card"><h2>All 100 v2 candidates — six-year discovery (daily Sharpe)</h2>
<table class="sortable"><thead><tr><th class="sortable num">#<span class="arrow"></span></th><th class="sortable">ID<span class="arrow"></span></th><th class="sortable num">Sharpe<span class="arrow"></span></th>
<th class="sortable num">Return<span class="arrow"></span></th><th class="sortable num">Max DD<span class="arrow"></span></th></tr></thead>
<tbody>{''.join(rows)}</tbody></table></div>

<footer>Independently recomputed daily-return Sharpe from native LumiBot equity curves · 3.5 bps/side.
Correlated-position + cost gates reject the whole suite forward. This page is <b>noindex</b> and must not be crawled.
See <a href="hts_rebaselined_2026-09-16.html">v1 results</a>.</footer>
</div></body></html>"""

    (OUT_DIR / "hts_v2_results_2026-09-17.html").write_text(doc, encoding="utf-8")
    print("wrote", OUT_DIR / "hts_v2_results_2026-09-17.html", f"{len(doc):,} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())