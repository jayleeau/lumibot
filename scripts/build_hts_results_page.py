#!/usr/bin/env python3
"""Build a sortable HTML results browser for an HTS native experiment suite.

Reads the artifacts under an output directory and writes:

* ``results.html``     - one sortable, filterable row per configuration
* ``detail/<ID>.html`` - per-candidate metrics, equity curve, artifact links

The page is self-contained with no external requests, so it opens straight from
the filesystem.  Metrics are the *independently recomputed* values from
:mod:`strategy_lab.experiment_validation`, not the runner's recorded ones, so the
table shows the audited view.

Examples
--------
    python scripts/build_hts_results_page.py
    python scripts/build_hts_results_page.py --out-dir reports/hts_native_2026-09-14
"""
from __future__ import annotations

import argparse
import html
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEFAULT_OUT_DIR = ROOT / "reports" / "hts_native_2026-09-14"
WINDOW_ORDER = ("six_year", "two_year")
WINDOW_LABELS = {"six_year": "6 year", "two_year": "2 year"}
WINDOW_COLUMNS: tuple[tuple[str, str], ...] = (
    ("Sharpe", "num"),
    ("CAGR", "num"),
    ("Total return", "num"),
    ("Volatility", "num"),
    ("Max drawdown", "num"),
    ("Equity", "text"),
)


@dataclass
class WindowView:
    """Audited metrics plus a downsampled equity curve for one window."""

    label: str
    ok: bool
    findings: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    fills: int = 0
    fees: float = 0.0
    runtime_seconds: float | None = None
    first_session: str | None = None
    last_session: str | None = None
    curve: list[float] = field(default_factory=list)
    terminal_positions: dict[str, Any] = field(default_factory=dict)

    def number(self, key: str) -> float | None:
        try:
            return float(self.metrics.get(key))
        except (TypeError, ValueError):
            return None


@dataclass
class Row:
    """One configuration across both windows."""

    candidate_id: str
    name: str
    kind: str
    family_id: str
    rule: str = ""
    fingerprint: str = ""
    blocked_reason: str = ""
    windows: dict[str, WindowView] = field(default_factory=dict)


def _downsample(values: Sequence[float], limit: int = 320) -> list[float]:
    if len(values) <= limit:
        return [float(value) for value in values]
    stride = len(values) / float(limit)
    picked = [float(values[min(int(index * stride), len(values) - 1)]) for index in range(limit)]
    picked[-1] = float(values[-1])
    return picked


def _load_curve(stats_path: Path) -> list[float]:
    """Daily portfolio value from the equity curve, normalised to 1.0 at start."""
    import pandas as pd

    if not stats_path.exists():
        return []
    frame = pd.read_csv(stats_path, usecols=["datetime", "portfolio_value"])
    stamps = pd.to_datetime(frame["datetime"], utc=True)
    daily = (
        frame.assign(_session=stamps.dt.strftime("%Y-%m-%d"), _stamp=stamps)
        .sort_values("_stamp")
        .groupby("_session", sort=True)["portfolio_value"]
        .last()
        .astype("float64")
    )
    if daily.empty:
        return []
    base = float(daily.iloc[0])
    if base <= 0.0:
        return []
    return _downsample((daily / base).tolist())


def collect_rows(out_dir: Path, registry: Any) -> list[Row]:
    """Read every artifact directory and note configurations that have none."""
    from strategy_lab.experiment_validation import audit_run

    rows: list[Row] = []
    for candidate in registry.all_candidates():
        row = Row(
            candidate_id=candidate.candidate_id,
            name=candidate.name,
            kind=candidate.kind,
            family_id=candidate.family_id,
            rule=candidate.rule,
            fingerprint=candidate.fingerprint(),
        )
        for label in WINDOW_ORDER:
            run_dir = out_dir / candidate.candidate_id / label
            if not (run_dir / "run_result.json").exists():
                continue
            payload = json.loads((run_dir / "run_result.json").read_text(encoding="utf-8"))
            audit = audit_run(run_dir)
            view = WindowView(
                label=label,
                ok=audit.ok,
                findings=list(audit.findings),
                warnings=list(audit.warnings),
                metrics=dict(audit.metrics),
                fills=audit.fills,
                fees=audit.fees,
                runtime_seconds=payload.get("runtime_seconds"),
                first_session=audit.metrics.get("first_session"),
                last_session=audit.metrics.get("last_session"),
                terminal_positions=dict(payload.get("terminal_positions") or {}),
            )
            view.curve = _load_curve(run_dir / "run_stats.csv")
            row.windows[label] = view
        if not row.windows:
            row.blocked_reason = "; ".join(candidate.blocked_reasons) or "no closed data gate"
        rows.append(row)
    return rows


def _fmt_number(value: float | None, digits: int = 3) -> str:
    return "n/a" if value is None else f"{value:.{digits}f}"


def _fmt_percent(value: float | None, digits: int = 1) -> str:
    return "n/a" if value is None else f"{value * 100:+.{digits}f}%"


def _fmt_money(value: float | None) -> str:
    return "n/a" if value is None else f"${value:,.0f}"


def sparkline(curve: Sequence[float], width: int = 104, height: int = 26) -> str:
    """Inline SVG growth-of-1 sparkline; an empty series renders as a blank cell."""
    if not curve:
        return f'<svg class="spark" viewBox="0 0 {width} {height}" aria-hidden="true"></svg>'
    low, high = min(curve), max(curve)
    span = (high - low) or 1.0
    step = width / max(len(curve) - 1, 1)
    points = " ".join(
        f"{index * step:.1f},{height - 2 - ((value - low) / span) * (height - 4):.1f}"
        for index, value in enumerate(curve)
    )
    stroke = "#2f7d5d" if curve[-1] >= curve[0] else "#a4463c"
    return (
        f'<svg class="spark" viewBox="0 0 {width} {height}" preserveAspectRatio="none" aria-hidden="true">'
        f'<polyline points="{points}" fill="none" stroke="{stroke}" stroke-width="1.4"/></svg>'
    )


def equity_chart(curve: Sequence[float], width: int = 520, height: int = 180) -> str:
    """Inline SVG equity chart for a detail page."""
    if not curve:
        return '<p class="muted">No equity curve available.</p>'
    low, high = min(curve), max(curve)
    span = (high - low) or 1.0
    left, top, right, bottom = 46, 12, width - 10, height - 22
    plot_w, plot_h = right - left, bottom - top
    step = plot_w / max(len(curve) - 1, 1)
    points = " ".join(
        f"{left + index * step:.1f},{bottom - ((value - low) / span) * plot_h:.1f}"
        for index, value in enumerate(curve)
    )
    grid: list[str] = []
    for fraction in (0.0, 0.5, 1.0):
        label = low + span * fraction
        y = bottom - fraction * plot_h
        grid.append(f'<line x1="{left}" y1="{y:.1f}" x2="{right}" y2="{y:.1f}" class="grid"/>')
        grid.append(f'<text x="{left - 6}" y="{y + 3:.1f}" class="axis" '
                    f'text-anchor="end">{label:.2f}</text>')
    baseline = ""
    if low <= 1.0 <= high:
        base_y = bottom - ((1.0 - low) / span) * plot_h
        baseline = f'<line x1="{left}" y1="{base_y:.1f}" x2="{right}" y2="{base_y:.1f}" class="baseline"/>'
    return (
        f'<svg class="chart" viewBox="0 0 {width} {height}" role="img" aria-label="growth of one dollar">'
        f'{"".join(grid)}{baseline}'
        f'<polyline points="{points}" fill="none" stroke="#34558b" stroke-width="1.6"/></svg>'
    )


CSS = """
:root {
  color-scheme: light;
  --ink: #1c1f24; --muted: #6b7280; --line: #dfe3e8; --line-soft: #eef1f4;
  --band: #f7f8fa; --accent: #34558b; --good: #2f7d5d; --bad: #a4463c;
}
* { box-sizing: border-box; }
body { margin: 0; background: #fff; color: var(--ink);
  font: 13px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; }
header.top { position: sticky; top: 0; z-index: 5; background: #fff;
  border-bottom: 1px solid var(--line); padding: 12px 18px 10px; }
h1 { font-size: 15px; margin: 0 0 3px; font-weight: 600; }
h2 { font-size: 13px; margin: 20px 0 8px; font-weight: 600; }
.sub { color: var(--muted); font-size: 12px; }
.sub a { color: var(--accent); }
code { font-size: 12px; }
.controls { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin-top: 10px; }
.controls input[type=search], .controls select { font: inherit; padding: 5px 8px;
  border: 1px solid var(--line); border-radius: 4px; background: #fff; color: var(--ink); min-width: 140px; }
.controls input[type=search] { min-width: 230px; }
.controls .count { color: var(--muted); margin-left: auto; }
main { padding: 14px 18px 40px; }
table { border-collapse: separate; border-spacing: 0; width: 100%; }
th, td { padding: 5px 8px; border-bottom: 1px solid var(--line-soft); text-align: left; vertical-align: middle; }
thead th { position: sticky; background: var(--band); border-bottom: 1px solid var(--line);
  font-weight: 600; font-size: 12px; white-space: nowrap; cursor: pointer; user-select: none; }
thead tr.group th { top: 0; z-index: 3; text-align: center; color: var(--muted); cursor: default; }
thead tr.cols th { top: 26px; z-index: 2; }
th.num, td.num { text-align: right; font-variant-numeric: tabular-nums; }
th .caret { display: inline-block; width: 0; height: 0; margin-left: 4px; vertical-align: middle;
  border-left: 3.5px solid transparent; border-right: 3.5px solid transparent; }
th.asc .caret { border-bottom: 5px solid var(--ink); }
th.desc .caret { border-top: 5px solid var(--ink); }
tbody tr:hover td { background: #f3f6fa; }
tbody tr.blocked td { color: var(--muted); }
td.id { font-weight: 600; white-space: nowrap; }
td.id a, .links a { color: var(--accent); text-decoration: none; }
td.id a:hover, .links a:hover { text-decoration: underline; }
td.name { max-width: 250px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
td.fam { max-width: 185px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--muted); }
.pill { display: inline-block; padding: 1px 6px; border: 1px solid var(--line); border-radius: 3px;
  font-size: 11px; color: var(--muted); background: #fff; }
.pill.ok { color: var(--good); border-color: #c9e2d5; background: #f2f9f5; }
.pill.warn { color: #8a6316; border-color: #e8dcbe; background: #fbf7ec; }
.pill.bad { color: var(--bad); border-color: #e8cdc9; background: #fdf3f1; }
td.sh.good { background: #f2f9f5; }
td.sh.bad { background: #fdf3f1; }
svg.spark { width: 104px; height: 26px; display: block; }
svg.chart { width: 100%; height: auto; max-width: 520px; }
svg.chart .grid { stroke: var(--line-soft); stroke-width: 1; }
svg.chart .baseline { stroke: #c3c9d2; stroke-width: 1; stroke-dasharray: 3 3; }
svg.chart .axis { fill: var(--muted); font-size: 10px; }
.axes { color: var(--muted); font-size: 11px; margin-top: 2px; }
.grid2 { display: grid; grid-template-columns: repeat(auto-fit, minmax(420px, 1fr)); gap: 20px; margin-top: 10px; }
.card { border: 1px solid var(--line); border-radius: 6px; padding: 12px 14px; }
dl.meta { display: grid; grid-template-columns: 160px 1fr; gap: 4px 12px; margin: 0; }
dl.meta dt { color: var(--muted); }
dl.meta dd { margin: 0; }
table.mini th { background: none; cursor: default; position: static; }
table.mini td, table.mini th { padding: 4px 8px; }
.muted { color: var(--muted); }
.links a { margin-right: 10px; white-space: nowrap; }
footer { padding: 14px 18px 30px; border-top: 1px solid var(--line); color: var(--muted); font-size: 12px; }
"""

SCRIPT = """
(function () {
  var table = document.getElementById('results');
  if (!table) { return; }
  var tbody = table.tBodies[0];
  var rows = Array.prototype.slice.call(tbody.rows);
  var search = document.getElementById('q');
  var kind = document.getElementById('kind');
  var family = document.getElementById('family');
  var status = document.getElementById('status');
  var count = document.getElementById('count');
  var headers = Array.prototype.slice.call(table.tHead.rows[1].cells);

  function valueOf(row, index, type) {
    var cell = row.cells[index];
    var raw = cell.getAttribute('data-sort');
    // Text columns carry no explicit key, so fall back to the rendered text.
    if (raw === null || raw === '') { raw = (cell.textContent || '').trim(); }
    if (raw === '') { return null; }
    if (type === 'num') {
      var parsed = parseFloat(raw);
      return isNaN(parsed) ? null : parsed;
    }
    return raw.toLowerCase();
  }

  function sortBy(index, type, direction) {
    var original = rows.slice();
    rows.sort(function (a, b) {
      var av = valueOf(a, index, type);
      var bv = valueOf(b, index, type);
      if (av === null && bv === null) { return original.indexOf(a) - original.indexOf(b); }
      if (av === null) { return 1; }
      if (bv === null) { return -1; }
      if (av < bv) { return direction === 'asc' ? -1 : 1; }
      if (av > bv) { return direction === 'asc' ? 1 : -1; }
      return original.indexOf(a) - original.indexOf(b);
    });
    rows.forEach(function (row) { tbody.appendChild(row); });
    headers.forEach(function (th) { th.classList.remove('asc', 'desc'); });
    headers[index].classList.add(direction);
  }

  headers.forEach(function (th, index) {
    th.addEventListener('click', function () {
      var type = th.getAttribute('data-type') || 'text';
      var direction = th.classList.contains('desc') ? 'asc' : 'desc';
      sortBy(index, type, direction);
    });
  });

  function applyFilters() {
    var needle = (search.value || '').trim().toLowerCase();
    var wantKind = kind.value;
    var wantFamily = family.value;
    var wantStatus = status.value;
    var shown = 0;
    rows.forEach(function (row) {
      var keep = (!needle || row.getAttribute('data-search').indexOf(needle) !== -1)
        && (wantKind === 'all' || row.getAttribute('data-kind') === wantKind)
        && (wantFamily === 'all' || row.getAttribute('data-family') === wantFamily)
        && (wantStatus === 'all' || row.getAttribute('data-status') === wantStatus);
      row.style.display = keep ? '' : 'none';
      if (keep) { shown += 1; }
    });
    count.textContent = shown + ' of ' + rows.length + ' configurations';
  }

  [search, kind, family, status].forEach(function (element) {
    element.addEventListener('input', applyFilters);
    element.addEventListener('change', applyFilters);
  });

  sortBy(5, 'num', 'desc');
  applyFilters();
})();
"""


def render_index(rows: Sequence[Row], *, suite_id: str, revision: str) -> str:
    done = sum(1 for row in rows if row.windows)
    blocked = len(rows) - done
    audit_ok = sum(1 for row in rows for view in row.windows.values() if view.ok)
    audit_total = sum(len(row.windows) for row in rows)
    families = sorted({row.family_id for row in rows})
    kinds = sorted({row.kind for row in rows})

    head = [
        '<th data-type="text">ID<span class="caret"></span></th>',
        '<th data-type="text">Name<span class="caret"></span></th>',
        '<th data-type="text">Kind<span class="caret"></span></th>',
        '<th data-type="text">Family<span class="caret"></span></th>',
        '<th data-type="text">State<span class="caret"></span></th>',
    ]
    group_cells = ['<th colspan="5"></th>']
    for label in WINDOW_ORDER:
        group_cells.append(f'<th colspan="6">{html.escape(WINDOW_LABELS[label])}</th>')
        for column, sort_type in WINDOW_COLUMNS:
            css = ' class="num"' if sort_type == "num" else ""
            head.append(f'<th{css} data-type="{sort_type}">{html.escape(column)}'
                        f'<span class="caret"></span></th>')
    group_cells.append('<th colspan="3">Artifacts</th>')
    head.extend([
        '<th data-type="text">Links<span class="caret"></span></th>',
        '<th class="num" data-type="num">Fills 6y<span class="caret"></span></th>',
        '<th class="num" data-type="num">Runtime 6y<span class="caret"></span></th>',
    ])

    body: list[str] = []
    for row in rows:
        blob = " ".join((
            row.candidate_id, row.name, row.kind, row.family_id, row.rule, " ".join(row.windows),
        )).lower()
        cells = [
            f'<td class="id"><a href="detail/{row.candidate_id}.html">{row.candidate_id}</a></td>',
            f'<td class="name" title="{html.escape(row.name)}">{html.escape(row.name)}</td>',
            f'<td>{html.escape(row.kind)}</td>',
            f'<td class="fam" title="{html.escape(row.family_id)}">{html.escape(row.family_id)}</td>',
        ]
        state = "done" if row.windows else "blocked"
        if row.windows:
            failed = any(not view.ok for view in row.windows.values())
            cells.append('<td data-sort="{}"><span class="pill {}">{}</span></td>'.format(
                state, "bad" if failed else "ok", "audit failed" if failed else "accepted"))
        else:
            cells.append('<td data-sort="{}"><span class="pill warn" title="{}">'
                         'blocked-data</span></td>'.format(state, html.escape(row.blocked_reason)))

        fills_cell = runtime_cell = ""
        for label in WINDOW_ORDER:
            view = row.windows.get(label)
            if view is None:
                cells.extend(['<td class="num">n/a</td>'] * 5)
                cells.append('<td><span class="muted">n/a</span></td>')
                continue
            sharpe = view.number("sharpe")
            tone = ""
            if sharpe is not None:
                tone = " good" if sharpe >= 1.0 else (" bad" if sharpe < 0.0 else "")
            sharpe_text = _fmt_number(sharpe)
            cells.append(f'<td class="num sh{tone}" data-sort="{sharpe_text}">{sharpe_text}</td>')
            for key in ("cagr", "total_return", "volatility", "max_drawdown"):
                text = _fmt_percent(view.number(key))
                cells.append(f'<td class="num" data-sort="{text}">{text}</td>')
            cells.append(f'<td>{sparkline(view.curve)}</td>')
            if label == "six_year":
                fills_cell = f'<td class="num" data-sort="{view.fills}">{view.fills:,}</td>'
                runtime = view.runtime_seconds
                runtime_cell = (
                    f'<td class="num" data-sort="{runtime:.1f}">{runtime:.1f}s</td>'
                    if runtime else '<td class="num">n/a</td>'
                )

        links = [f'<a href="detail/{row.candidate_id}.html">detail</a>']
        for label in WINDOW_ORDER:
            if label in row.windows:
                links.append(f'<a href="{row.candidate_id}/{label}/run_result.json">'
                             f'{WINDOW_LABELS[label]} json</a>')
        cells.append('<td class="links">{}</td>'.format("".join(links)))
        cells.append(fills_cell or '<td class="num">n/a</td>')
        cells.append(runtime_cell or '<td class="num">n/a</td>')

        row_class = "" if row.windows else ' class="blocked"'
        body.append('<tr{} data-kind="{}" data-family="{}" data-status="{}" data-search="{}">{}</tr>'.format(
            row_class, html.escape(row.kind), html.escape(row.family_id), state,
            html.escape(blob), "".join(cells)))

    options_kind = "".join(f'<option value="{html.escape(k)}">{html.escape(k)}</option>' for k in kinds)
    options_family = "".join(f'<option value="{html.escape(f)}">{html.escape(f)}</option>' for f in families)
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>HTS native results - {html.escape(suite_id)}</title>
<style>{CSS}</style>
</head>
<body>
<header class="top">
  <h1>HTS native backtest results</h1>
  <div class="sub">
    {len(rows)} configurations ({done} implemented, {blocked} blocked on data) &middot;
    {audit_total} audited runs, {audit_ok} accepted &middot;
    suite <code>{html.escape(suite_id)}</code> &middot; revision <code>{html.escape(revision)}</code> &middot;
    <a href="suite_audit.json">audit</a> &middot;
    <a href="suite_summary.json">summary</a> &middot;
    <a href="suite_manifest.json">manifest</a> &middot;
    <a href="../../docs/HTS_NATIVE_RESULTS.md">notes</a>
  </div>
  <div class="controls">
    <input type="search" id="q" placeholder="Filter by id, name or rule" aria-label="Filter rows">
    <select id="kind" aria-label="Filter by kind">
      <option value="all">all kinds</option>{options_kind}
    </select>
    <select id="family" aria-label="Filter by family">
      <option value="all">all families</option>{options_family}
    </select>
    <select id="status" aria-label="Filter by state">
      <option value="all">all states</option>
      <option value="done">implemented only</option>
      <option value="blocked">blocked only</option>
    </select>
    <span class="count" id="count"></span>
  </div>
</header>
<main>
<table id="results">
  <thead>
    <tr class="group">{"".join(group_cells)}</tr>
    <tr class="cols">{"".join(head)}</tr>
  </thead>
  <tbody>
{"".join(body)}
  </tbody>
</table>
</main>
<footer>
  Metrics recomputed independently from saved artifacts by
  <code>strategy_lab/experiment_validation.py</code> and required to match the runner's
  recorded values at rtol=1e-10. Costs 3.5 bps per side. Sharpe is the daily-return
  arithmetic Sharpe with a zero risk-free convention.
</footer>
<script>{SCRIPT}</script>
</body>
</html>
"""


def render_detail(row: Row, *, suite_id: str, revision: str) -> str:
    metric_rows = [
        ("Sharpe (daily)", lambda view: _fmt_number(view.number("sharpe"))),
        ("CAGR", lambda view: _fmt_percent(view.number("cagr"))),
        ("Total return", lambda view: _fmt_percent(view.number("total_return"))),
        ("Volatility", lambda view: _fmt_percent(view.number("volatility"))),
        ("Max drawdown", lambda view: _fmt_percent(view.number("max_drawdown"))),
        ("Final equity", lambda view: _fmt_money(view.number("final_equity"))),
        ("Sessions", lambda view: f"{int(view.number('sessions') or 0):,}"),
        ("Fills", lambda view: f"{view.fills:,}"),
        ("Fees paid", lambda view: _fmt_money(view.fees)),
        ("Runtime", lambda view: f"{view.runtime_seconds:.1f}s" if view.runtime_seconds else "n/a"),
    ]
    header_cells = "".join(f"<th>{html.escape(WINDOW_LABELS[label])}</th>" for label in WINDOW_ORDER)
    body_rows = []
    for label_text, formatter in metric_rows:
        cells = []
        for label in WINDOW_ORDER:
            view = row.windows.get(label)
            cells.append('<td class="num">{}</td>'.format(
                html.escape(formatter(view)) if view else "n/a"))
        body_rows.append(f"<tr><th>{html.escape(label_text)}</th>{''.join(cells)}</tr>")

    chart_blocks = []
    for label in WINDOW_ORDER:
        view = row.windows.get(label)
        if view is None:
            continue
        artifacts = f"../{row.candidate_id}/{label}"
        chart_blocks.append(f"""
    <div class="card">
      <h2>{html.escape(WINDOW_LABELS[label])} &middot; {html.escape(str(view.first_session))} to {html.escape(str(view.last_session))}</h2>
      {equity_chart(view.curve)}
      <div class="axes">Growth of $1, daily closes</div>
      <div class="links">
        <a href="{artifacts}/run_result.json">run_result.json</a>
        <a href="{artifacts}/run_stats.csv">run_stats.csv</a>
        <a href="{artifacts}/run_trades.csv">run_trades.csv</a>
        <a href="{artifacts}/run_settings.json">run_settings.json</a>
      </div>
    </div>""")

    positions = []
    for label in WINDOW_ORDER:
        view = row.windows.get(label)
        if view is None:
            continue
        for symbol, state in list(view.terminal_positions.items())[:12]:
            quantity = state.get("quantity") if isinstance(state, dict) else state
            entry = state.get("entry_price") if isinstance(state, dict) else None
            try:
                quantity_text = f"{float(quantity):,.0f}"
            except (TypeError, ValueError):
                quantity_text = "n/a"
            try:
                entry_text = _fmt_money(float(entry))
            except (TypeError, ValueError):
                entry_text = "n/a"
            positions.append(
                f"<tr><td>{html.escape(str(symbol))}</td>"
                f"<td>{html.escape(WINDOW_LABELS[label])}</td>"
                f'<td class="num">{quantity_text}</td>'
                f'<td class="num">{entry_text}</td></tr>'
            )
    positions_block = ""
    if positions:
        positions_block = (
            "<h2>Open positions at end of window</h2>"
            '<table class="mini"><thead><tr><th>Symbol</th><th>Window</th>'
            '<th class="num">Quantity</th><th class="num">Entry</th></tr></thead>'
            f'<tbody>{"".join(positions)}</tbody></table>'
        )

    notes: list[str] = []
    for label in WINDOW_ORDER:
        view = row.windows.get(label)
        if view is None:
            continue
        notes.extend(f"{WINDOW_LABELS[label]}: {finding}" for finding in view.findings)
        notes.extend(f"{WINDOW_LABELS[label]}: warning {item}" for item in view.warnings)
    if row.blocked_reason:
        notes.append(f"Data prerequisite not met: {row.blocked_reason}")
    note_block = ""
    if notes:
        note_block = ("<h2>Notes</h2><ul>"
                      + "".join(f"<li>{html.escape(note)}</li>" for note in notes) + "</ul>")

    status = ("blocked on data" if not row.windows
              else ("accepted" if all(view.ok for view in row.windows.values()) else "audit failed"))
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(row.candidate_id)} - HTS native results</title>
<style>{CSS}</style>
</head>
<body>
<header class="top">
  <h1>{html.escape(row.candidate_id)} &middot; {html.escape(row.name)}</h1>
  <div class="sub">
    <a href="../results.html">all results</a> &middot;
    {html.escape(row.kind)} &middot; family <code>{html.escape(row.family_id)}</code> &middot;
    state <span class="pill {"ok" if row.windows else "warn"}">{html.escape(status)}</span>
  </div>
</header>
<main>
  <h2>Rule</h2>
  <p>{html.escape(row.rule)}</p>
  <h2>Metrics</h2>
  <table class="mini">
    <thead><tr><th></th>{header_cells}</tr></thead>
    <tbody>{"".join(body_rows)}</tbody>
  </table>
  <div class="grid2">{"".join(chart_blocks)}</div>
  {positions_block}
  {note_block}
  <h2>Provenance</h2>
  <dl class="meta">
    <dt>Suite</dt><dd><code>{html.escape(suite_id)}</code></dd>
    <dt>Implementation revision</dt><dd><code>{html.escape(revision)}</code></dd>
    <dt>Fingerprint</dt><dd><code>{html.escape(row.fingerprint)}</code></dd>
    <dt>Engine</dt><dd>lumibot.strategies.Strategy.run_backtest + BacktestingBroker</dd>
  </dl>
</main>
<footer>
  Metrics recomputed independently from saved artifacts and required to match the
  runner's recorded values at rtol=1e-10.
</footer>
</body>
</html>
"""


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR))
    args = parser.parse_args(argv)

    from strategy_lab.experiment_registry import get_registry

    out_dir = Path(args.out_dir)
    if not out_dir.is_dir():
        print(f"no such suite directory: {out_dir}", file=sys.stderr)
        return 2
    suite_id = revision = "unknown"
    summary_path = out_dir / "suite_summary.json"
    if summary_path.exists():
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        suite_id = str(summary.get("suite_id", "unknown"))
        revision = str(summary.get("implementation_revision", "unknown"))

    rows = collect_rows(out_dir, get_registry())
    (out_dir / "results.html").write_text(
        render_index(rows, suite_id=suite_id, revision=revision), encoding="utf-8")
    detail_dir = out_dir / "detail"
    detail_dir.mkdir(exist_ok=True)
    for row in rows:
        (detail_dir / f"{row.candidate_id}.html").write_text(
            render_detail(row, suite_id=suite_id, revision=revision), encoding="utf-8")

    done = sum(1 for row in rows if row.windows)
    print(f"wrote {out_dir / 'results.html'} ({len(rows)} rows, {done} with results)")
    print(f"wrote {len(rows)} detail pages under {detail_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
