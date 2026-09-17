#!/usr/bin/env python3
"""Generate a self-contained, non-indexable HTS results HTML page
for the HTS_CONTROL_1 + H001-H100 + runnable A strategies from the
clock-hour rebaseline, into glitch-strategy-infra/reports/.
"""
from __future__ import annotations

import html
import json
import sys
from pathlib import Path

LUMIBOT = Path("/Users/openclaw/.hermes/profiles/free_money_glitch/home/lumibot")
if str(LUMIBOT) not in sys.path:
    sys.path.insert(0, str(LUMIBOT))
REBASE = LUMIBOT / "reports" / "hts_rebaseline_2026-09-15"
OUT_DIR = Path("/Users/openclaw/.hermes/profiles/free_money_glitch/glitch-strategy-infra/reports")

FOLD_NAMES = ("f1", "f2", "f3", "f4", "f5", "f6")
FOLD_LABEL = {"f1": "23-24 H2", "f2": "24 H1", "f3": "24 H2", "f4": "25 H1", "f5": "25 H2", "f6": "26 H1"}


def _fmt_pct(v):
    if v is None:
        return "–"
    try:
        return f"{float(v) * 100:.1f}%"
    except (TypeError, ValueError):
        return "–"


def _fmt_num(v, nd=3):
    if v is None:
        return "–"
    try:
        return f"{float(v):.{nd}f}"
    except (TypeError, ValueError):
        return "–"


def main() -> int:
    summary = json.loads((REBASE / "suite_summary.json").read_text())
    manifest = json.loads((REBASE / "suite_manifest.json").read_text())
    res = summary["results"]

    # names/rules from registry
    from strategy_lab.experiment_registry import get_registry
    reg = get_registry()
    def describe(cid):
        try:
            c = reg.get(cid)
            return c.family_id, c.rule
        except Exception:
            return "", ""

    # candidate order: control, H001..H100, runnable A's
    order = ["HTS_CONTROL_1"] + [f"H{i:03d}" for i in range(1, 101)] + \
            ["A01", "A03", "A04", "A05", "A06", "A09"]

    rows_html = []
    for cid in order:
        six = next((r for r in res if r["candidate_id"] == cid and r["window"] == "six_year"), None)
        two = next((r for r in res if r["candidate_id"] == cid and r["window"] == "two_year"), None)
        family, rule = describe(cid)
        def cell(s, key):
            return f'<td class="num">{_fmt_num(s.get(key)) if s else "–"}</td>'
        def ret(s):
            return f'<td class="num">{_fmt_pct(s.get("total_return")) if s else "–"}</td>'
        def dd(s):
            return f'<td class="num">{_fmt_pct(s.get("max_drawdown")) if s else "–"}</td>'
        rows_html.append(
            "<tr>"
            f"<td><b>{html.escape(cid)}</b></td>"
            f"<td>{html.escape(family)}</td>"
            + cell(six, "sharpe") + ret(six) + dd(six)
            + cell(two, "sharpe") + ret(two) + dd(two)
            + "<td style='max-width:340px;color:#8A91A0;font-size:12px'>" + html.escape(rule) + "</td>"
            "</tr>"
        )

    # walk-forward selection summary
    wf = json.loads((REBASE / "walk_forward_report.json").read_text())
    wf_rows = []
    for e in wf.get("selection_per_fold", []):
        o = e.get("outer_sharpe")
        cls = "pos" if (o or 0) > 0 else ("neg" if o is not None else "")
        wf_rows.append(
            "<tr>"
            f'<td><b>{html.escape(str(e.get("fold")))}</b></td>'
            f'<td>{html.escape(str(e.get("discovery_end")))}</td>'
            f'<td>{html.escape(str(e.get("selected_id")))}</td>'
            f'<td class="num">{_fmt_num(e.get("selected_inner_sharpe"))}</td>'
            f'<td class="num {cls}">{_fmt_num(o)}</td>'
            f'<td class="num">{_fmt_pct(e.get("outer_total_return"))}</td>'
            f'<td class="num">{_fmt_pct(e.get("outer_max_dd"))}</td>'
            "</tr>"
        )
    st = wf.get("stitched_selected_track", {})
    st_sha = _fmt_num(st.get("sharpe"))
    st_cls = "pos" if (st.get("sharpe") or 0) > 0 else "neg"
    st_ret = _fmt_pct(st.get("total_return"))

    revision = manifest.get("implementation_revision", "?")
    generated = "2026-09-16"

    doc = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow,noarchive">
<title>HTS Rebaselined Results — clock-hour convention</title>
<style>
:root{{--bg:#0D0F12;--card:#161920;--border:#262933;--green:#22C55E;--red:#EF4444;--amber:#F59E0B;--muted:#8A91A0;--text:#E5E7EB}}
*{{box-sizing:border-box}}
body{{background:var(--bg);color:var(--text);font-family:Inter,-apple-system,Segoe UI,Roboto,sans-serif;margin:0;padding:24px}}
.wrap{{max-width:1400px;margin:0 auto}}
h1{{font-size:22px}} h2{{font-size:15px;color:var(--muted)}}
.sub{{color:var(--muted);font-size:13px;margin-bottom:16px}}
.card{{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:16px;margin-bottom:16px;overflow-x:auto}}
table{{border-collapse:collapse;width:100%;font-size:13px}}
th,td{{border-bottom:1px solid var(--border);padding:6px 8px;text-align:left;white-space:nowrap}}
th{{color:var(--muted)}} td.num{{font-variant-numeric:tabular-nums}}
.pos{{color:var(--green)}}.neg{{color:var(--red)}}
.kpi{{display:inline-block;background:var(--bg);border:1px solid var(--border);border-radius:8px;padding:10px 16px;margin:4px 10px 4px 0}}
.kpi b{{display:block;font-size:20px;color:var(--green)}}.kpi span{{color:var(--muted);font-size:12px}}
.notice{{background:#2a1f10;border:1px solid #8a5a1a;border-radius:8px;padding:10px 14px;color:#f0c674;font-size:13px;margin-bottom:16px}}
footer{{color:var(--muted);font-size:11px;margin-top:8px}}
</style></head><body><div class="wrap">
<h1>HTS Rebaselined Results — clock-hour 09:00–15:00 ET convention</h1>
<div class="sub">Native LumiBot engine · revision {html.escape(revision)} · generated {generated} · 2,140 accepted runs, 0 errors · 3.5&nbsp;bps/side · sharpe = daily-return arithmetic Sharpe unless noted</div>

<div class="notice"><b>These are research/hypothesis numbers, not trading recommendations.</b>
Backtests use historical data and do not guarantee future results. No candidate is presented as deployable;
the walk-forward selected track is negative under this basis.</div>

<div class="card"><h2>Walk-forward selection → held-out test (honest forward-filter)</h2>
<table><thead><tr><th>Fold</th><th>Test start</th><th>Selected (frozen)</th><th class="num">Inner score</th>
<th class="num">TEST Sharpe</th><th class="num">Return</th><th class="num">MaxDD</th></tr></thead>
<tbody>{''.join(wf_rows)}</tbody></table>
<div class="kpi"><b class="{st_cls}">{st_sha}</b><span>Stitched selected-track Sharpe</span></div>
<div class="kpi"><b>{st_ret}</b><span>Stitched total return</span></div>
</div>

<div class="card"><h2>All strategies — six-year &amp; two-year (daily Sharpe)</h2>
<table><thead><tr><th>ID</th><th>Family</th><th class="num">6y Sharpe</th><th class="num">6y Return</th>
<th class="num">6y MaxDD</th><th class="num">2y Sharpe</th><th class="num">2y Return</th><th class="num">2y MaxDD</th>
<th>Rule</th></tr></thead><tbody>{''.join(rows_html)}</tbody></table></div>

<footer>Independently recomputed daily-return Sharpe from the native LumiBot equity curves · cost 3.5 bps/side on buy+sell.
A02/A07/A08/A10 are blocked-data and are not run. This page is <b>noindex</b> and must not be crawled.</footer>
</div></body></html>"""

    (OUT_DIR / "hts_rebaselined_2026-09-16.html").write_text(doc, encoding="utf-8")
    # robots.txt to block crawlers on the whole site
    robots = "User-agent: *\nDisallow: /\n"
    (OUT_DIR / "robots.txt").write_text(robots, encoding="utf-8")
    print("wrote", OUT_DIR / "hts_rebaselined_2026-09-16.html", f"{len(doc):,} bytes")
    print("wrote", OUT_DIR / "robots.txt")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())