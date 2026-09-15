#!/usr/bin/env python3
"""Build a sortable, filterable HTML report for a native HTS walk-forward suite.

Reads the per-candidate, per-window artifacts (``run_result.json``) plus the
evaluator's ``walk_forward_report.json`` and renders a self-contained page with:

* a summary of the frozen per-fold selection procedure and its held-out test results;
* a stitched track-record card (honestly labelled as chained from-cash runs);
* a sortable/filterable table of every candidate across every walk-forward
  window, with fold-based columns.

The page is self-contained (no external requests) so it opens straight from the
filesystem.  Metrics are the independently recomputed daily-return Sharpe.

Examples
--------
    python scripts/build_walk_forward_page.py
    python scripts/build_walk_forward_page.py --out-dir reports/hts_walkforward_2026-09-14
"""
from __future__ import annotations

import argparse
import html
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

FOLDS = ("f1", "f2", "f3", "f4", "f5", "f6")
KINDS = ("innerA", "innerB", "test")
FOLD_LABELS = {"f1": "23-24 H2", "f2": "24 H1", "f3": "24 H2", "f4": "25 H1", "f5": "25 H2", "f6": "26 H1"}


def _fmt_percent(value: Any) -> str:
    if value is None:
        return "n/a"
    try:
        return f"{float(value):+.2%}"
    except (TypeError, ValueError):
        return "n/a"


def _fmt_number(value: Any, nd: int = 3) -> str:
    if value is None:
        return "n/a"
    try:
        return f"{float(value):.{nd}f}"
    except (TypeError, ValueError):
        return "n/a"


def _window_key(fold: str, kind: str) -> str:
    return f"{fold}_{kind}"


def _load(p, default=None):
    try:
        return json.loads(Path(p).read_text(encoding="utf-8"))
    except Exception:
        return default


def build(out_dir: Path) -> str:
    wf = _load(out_dir / "walk_forward_report.json", {}) or {}
    selection = wf.get("selection_per_fold", []) or []
    stitched = wf.get("stitched_selected_track", {}) or {}
    folds_detail = wf.get("folds", {}) or {}

    # ---- gather per-candidate metrics across all walk-forward windows ----
    rows: list[dict[str, Any]] = []
    for run_dir in sorted(out_dir.iterdir()):
        if not run_dir.is_dir():
            continue
        cid = run_dir.name
        windows: dict[str, dict[str, Any]] = {}
        for window_dir in sorted(run_dir.iterdir()):
            if not window_dir.is_dir():
                continue
            label = window_dir.name
            parts = label.split("_")
            if len(parts) != 2 or parts[0] not in FOLDS or parts[1] not in KINDS:
                continue
            payload = _load(window_dir / "run_result.json")
            if not payload or (payload.get("problems") != []):
                continue
            m = payload.get("metrics") or {}
            windows[label] = {
                "sharpe": m.get("sharpe"),
                "cagr": m.get("cagr"),
                "total_return": m.get("total_return"),
                "max_drawdown": m.get("max_drawdown"),
                "fills": int(payload.get("fills") or 0),
            }
        if windows:
            rows.append({"id": cid, "family": str(_family_of(cid)), "windows": windows})

    # columns: test windows first, then inner windows per fold
    columns: list[tuple[str, str]] = []
    seen: set[str] = set()
    for fold in FOLDS:
        col = _window_key(fold, "test")
        columns.append((col, f"TEST {FOLD_LABELS[fold]}"))
        seen.add(col)
    for kind in KINDS:
        for fold in FOLDS:
            col = _window_key(fold, kind)
            if col not in seen:
                columns.append((col, f"{FOLD_LABELS[fold]} {kind}"))
                seen.add(col)

    table_header = ["Candidate", "Family", *[label for _, label in columns]]
    table_rows = []
    for row in rows:
        cells = [f"<td>{html.escape(row['id'])}</td>", f"<td>{html.escape(row['family'])}</td>"]
        for col, _ in columns:
            w = row["windows"].get(col)
            if not w:
                cells.append('<td class="num muted">—</td>')
            else:
                cls = "pos" if (w.get("sharpe") or 0) > 0 else "neg"
                cells.append(
                    f'<td class="num {cls}">{_fmt_number(w.get("sharpe"))}</td>')
        table_rows.append(f"<tr>{''.join(cells)}</tr>")

    # ---- selection table ----
    sel_rows = []
    for entry in selection:
        outer = entry.get("outer_sharpe")
        cls = "pos" if (outer or 0) > 0 else ("neg" if outer is not None else "muted")
        sel_rows.append(f"""
        <tr>
          <td><strong>{html.escape(str(entry.get('fold','')))}</strong></td>
          <td>{html.escape(str(entry.get('discovery_end','')))}</td>
          <td><strong>{html.escape(str(entry.get('selected_id','')))}</strong></td>
          <td class="num">{_fmt_number(entry.get('selected_inner_sharpe'))}</td>
          <td class="num {cls}">{_fmt_number(outer)}</td>
          <td class="num">{_fmt_percent(entry.get('outer_total_return'))}</td>
          <td class="num">{_fmt_percent(entry.get('outer_max_dd'))}</td>
        </tr>""")

    # stitched card
    stitched_rows = [
        ("Total return", _fmt_percent(stitched.get("total_return"))),
        ("CAGR (annualised)", _fmt_percent(stitched.get("cagr"))),
        ("Sharpe (daily, arithmetic)", _fmt_number(stitched.get("sharpe"))),
        ("Max drawdown", _fmt_percent(stitched.get("max_drawdown"))),
        ("Sessions", f"{stitched.get('sessions','n/a')}"),
        ("Cost per side", f"{stitched.get('cost_bps_per_side','n/a')} bps"),
    ]
    stitched_cells = "".join(f"<span class='kpi'><b>{v}</b>{k}</span>"
                             for k, v in stitched_rows)

    note = html.escape(wf.get("stitch_note", "") or "")

    return _PAGE_FMT.replace("@@SUITE@@", html.escape(str(out_dir))) \
        .replace("@@PROCEDURE@@", html.escape(wf.get("procedure", "") or "")) \
        .replace("@@SEL_HEADER@@", "Fold · Discovery end · Selected (frozen) · Inner score · <span class='test'>TEST</span> Sharpe · Return · MaxDD") \
        .replace("@@SEL_ROWS@@", "".join(sel_rows) or "<tr><td colspan=7>no folds evaluated</td></tr>") \
        .replace("@@STITCH_CELLS@@", stitched_cells) \
        .replace("@@STITCH_NOTE@@", note) \
        .replace("@@TABLE_HEADER@@", "".join(f"<th>{html.escape(l)}</th>" for l in table_header)) \
        .replace("@@TABLE_ROWS@@", "".join(table_rows) or "<tr><td>no data</td></tr>")


def _family_of(cid: str) -> str:
    try:
        from strategy_lab.experiment_registry import get_registry

        reg = get_registry()
        return reg.get(cid).family_id
    except Exception:
        return ""


_PAGE_FMT = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Walk-forward selection — native HTS</title>
<style>
:root{--bg:#0D0F12;--card:#161920;--border:#262933;--green:#22C55E;--red:#EF4444;--blue:#3B82F6;--amber:#F59E0B;--muted:#8A91A0;--text:#E5E7EB}
*{box-sizing:border-box}
body{background:var(--bg);color:var(--text);font-family:Inter,-apple-system,Segoe UI,Roboto,sans-serif;margin:0;padding:24px}
h1{font-size:22px;margin:0 0 4px}h2{font-size:15px;margin:0 0 10px;color:var(--muted)}
.sub{color:var(--muted);font-size:13px;margin-bottom:20px}
.card{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:16px;margin-bottom:16px}
a{color:var(--blue);text-decoration:none}
.kpi{display:inline-block;background:var(--bg);border:1px solid var(--border);border-radius:8px;padding:10px 14px;margin:4px 8px 4px 0}
.kpi b{display:block;font-size:20px;color:var(--green)} .kpi span{color:var(--muted);font-size:12px}
table{border-collapse:collapse;width:100%;font-size:13px}
th,td{border-bottom:1px solid var(--border);padding:6px 8px;text-align:left;white-space:nowrap}
th{color:var(--muted);cursor:pointer;user-select:none;position:sticky;top:0;background:var(--card)}
th.sort-asc::after{content:" ▲"} th.sort-desc::after{content:" ▼"}
td.num{font-variant-numeric:tabular-nums} .pos{color:var(--green)}.neg{color:var(--red)}.muted{color:var(--muted)} .test{color:var(--amber)}
input[type=search]{background:var(--bg);border:1px solid var(--border);color:var(--text);border-radius:6px;padding:8px 10px;width:280px;margin-bottom:12px}
.badge{display:inline-block;background:var(--bg);border:1px solid var(--border);border-radius:20px;padding:2px 10px;font-size:12px;color:var(--muted);margin-left:8px}
#sel thead th:nth-child(5){color:var(--amber)}
.wrap{max-width:1400px;margin:0 auto;overflow-x:auto} footer{color:var(--muted);font-size:11px;margin-top:8px}
</style></head><body><div class="wrap">
<h1>Retrospective walk-forward selection — native HTS</h1>
<div class="sub">@@PROCEDURE@@
<div class="badge">suite: @@SUITE@@</div></div>

<div class="card"><h2>Frozen selection procedure → held-out test (all candidates evaluated; only pre-selected reported)</h2>
<div style="overflow-x:auto"><table id="sel"><thead><tr>@@SEL_HEADER@@</tr></thead><tbody>@@SEL_ROWS@@</tbody></table></div></div>

<div class="card"><h2>Stitched selected track record</h2>
<p class="sub">@@STITCH_NOTE@@</p>
<div>@@STITCH_CELLS@@</div></div>

<div class="card"><h2>All candidates × walk-forward windows (Sharpe)</h2>
<input type="search" id="filter" placeholder="Filter by candidate or family…">
<div style="overflow-x:auto"><table id="grid"><thead><tr>@@TABLE_HEADER@@</tr></thead><tbody>@@TABLE_ROWS@@</tbody></table></div></div>

<footer>Metrics are the independently recomputed daily-return arithmetic Sharpe (rf=0). Walk-forward is retrospective evidence;
the selected row is frozen from inner-validation only and never peeks at its own test window.</footer>
</div>
<script>
const grid=document.getElementById('grid');
const tbody=grid.tBodies[0];
const heads=[...grid.tHead.rows[0].cells].map((th,ix)=>({th,ix}));
heads.forEach(({th,ix})=>{
  th.addEventListener('click',()=>{
    const asc=(th.dataset.d||'')!=='asc';th.dataset.d=asc?'desc':'asc';
    const rows=[...tbody.rows].sort((a,b)=>{
      const av=a.cells[ix].innerText, bv=b.cells[ix].innerText,aN=parseFloat(av),bN=parseFloat(bv);
      const cmp=isFinite(aN)&&isFinite(bN)?aN-bN:av.localeCompare(bv);
      return asc?-cmp:cmp;
    });
    heads.forEach(h=>h.th.classList.remove('sort-asc','sort-desc'));
    th.classList.add(asc?'sort-desc':'sort-asc');
    rows.forEach(r=>tbody.appendChild(r));
  });
});
const filter=document.getElementById('filter');
filter.addEventListener('input',()=>{
  const q=filter.value.toLowerCase();
  [...tbody.rows].forEach(r=>r.style.display=r.innerText.toLowerCase().includes(q)?'':'none');
});
</script></body></html>
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build walk-forward HTML report")
    parser.add_argument("--out-dir", default=str(ROOT / "reports" / "hts_walkforward_2026-09-14"))
    args = parser.parse_args(argv)
    out_dir = Path(args.out_dir)
    html_str = build(out_dir)
    target = out_dir / "walk_forward.html"
    target.write_text(html_str, encoding="utf-8")
    print(f"wrote {target} ({len(html_str):,} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())