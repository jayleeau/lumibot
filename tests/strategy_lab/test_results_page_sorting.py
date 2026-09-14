"""Regression tests for the sortable results page's client-side behaviour.

These execute the page's *actual* JavaScript under Node against a minimal DOM
stub, because simulating the sort in Python is what missed a real bug: text
columns (ID, name, kind, family, state) carried no ``data-sort`` key and so were
never reordered at all.

The suite is skipped when Node is not available.
"""
from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")

HARNESS = r"""
import { readFileSync } from "node:fs";

const html = readFileSync(process.argv[2], "utf8");

function attr(text, name) {
  const found = new RegExp(name + '="([^"]*)"').exec(text);
  return found ? found[1] : null;
}

function classes() {
  const set = new Set();
  return { add: (c) => set.add(c), remove: (...cs) => cs.forEach((c) => set.delete(c)),
           contains: (c) => set.has(c) };
}

function cell(html) {
  const sort = attr(html, "data-sort");
  return {
    textContent: html.replace(/<[^>]*>/g, "").trim(),
    getAttribute: (name) => (name === "data-sort" ? sort : null),
  };
}

const script = /<script>([\s\S]*?)<\/script>/.exec(html)[1];
const thead = html.split("</thead>")[0];
const headerTypes = [...thead.matchAll(/data-type="([^"]+)"/g)].map((m) => m[1]);
const colsRow = /<tr class="cols">([\s\S]*?)<\/tr>/.exec(thead)[1];
const headerCells = [...colsRow.matchAll(/<th\b[^>]*>/g)].map(() => ({ attrs: {} }));
headerTypes.forEach((type, index) => { headerCells[index].attrs["data-type"] = type; });

const body = html.split("<tbody>")[1].split("</tbody>")[0];
const rows = [...body.matchAll(/<tr\b[^>]*>[\s\S]*?<\/tr>/g)].map((m) => {
  const tag = m[0].slice(0, m[0].indexOf(">") + 1);
  const cells = [...m[0].matchAll(/<td\b[^>]*>[\s\S]*?<\/td>/g)].map((c) => cell(c[0]));
  return { cells, style: {}, getAttribute: (name) => attr(tag, name) };
});

function element(extra) {
  const listeners = {};
  return Object.assign({
    attrs: {}, style: {}, children: [], textContent: "", value: "",
    classList: classes(), listeners,
    getAttribute(name) { return Object.prototype.hasOwnProperty.call(this.attrs, name) ? this.attrs[name] : null; },
    addEventListener(type, fn) { (listeners[type] = listeners[type] || []).push(fn); },
    appendChild(child) {
      const at = this.children.indexOf(child);
      if (at >= 0) { this.children.splice(at, 1); }
      this.children.push(child);
    },
    get rows() { return this.children; },
  }, extra || {});
}

const headers = headerCells.map((h) => element({ attrs: h.attrs }));
const tbody = element();
tbody.children = rows;
const table = element({ tBodies: [tbody], tHead: { rows: [element(), { cells: headers }] } });

const nodes = { results: table, q: element(), kind: element(), family: element(), status: element(), count: element() };
nodes.kind.value = "all";
nodes.family.value = "all";
nodes.status.value = "all";
nodes.q.value = "";
globalThis.document = { getElementById: (id) => nodes[id] || null };

new Function(script)();

// The ID column is the first cell; the page does not carry a data-id attribute.
const order = () => tbody.children.map((row) => row.cells[0].textContent);
const click = (index) => { headers[index].listeners.click.forEach((fn) => fn()); };
const type = (id, value) => {
  nodes[id].value = value;
  (nodes[id].listeners.input || []).forEach((fn) => fn());
  (nodes[id].listeners.change || []).forEach((fn) => fn());
};

const out = {};
click(0); click(0);
out.byId = order();
click(1); click(1);
out.byName = order();
click(5);
out.sharpeClick1 = order();
click(5);
out.sharpeClick2 = order();
type("q", "momentum");
out.filtered = tbody.children
  .filter((row) => row.style.display !== "none")
  .map((row) => row.cells[0].textContent);
out.countText = nodes.count.textContent;
console.log(JSON.stringify(out));
"""


def _load_page_module():
    path = ROOT / "scripts" / "build_hts_results_page.py"
    spec = importlib.util.spec_from_file_location("build_hts_results_page", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    # dataclasses resolve their annotations through sys.modules, so the module
    # must be registered before it is executed.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _row(page, candidate_id: str, name: str, sharpe: float | None, *, kind: str = "hts"):
    windows = {}
    if sharpe is not None:
        metrics = {
            "sharpe": sharpe, "cagr": 0.1, "total_return": 0.5, "volatility": 0.2,
            "max_drawdown": 0.15, "final_equity": 150_000.0, "sessions": 500,
            "first_session": "2020-09-08", "last_session": "2026-09-08",
        }
        for label in page.WINDOW_ORDER:
            windows[label] = page.WindowView(label=label, ok=True, metrics=dict(metrics), fills=10)
    return page.Row(
        candidate_id=candidate_id, name=name, kind=kind, family_id="family-1",
        rule="rule text", fingerprint="f" * 8,
        blocked_reason="" if windows else "needs data", windows=windows,
    )


def _run_page(tmp_path: Path) -> dict:
    page = _load_page_module()
    rows = [
        _row(page, "H001", "Trend filter SMA 10", 0.5),
        _row(page, "H010", "Trend filter SMA 200", -0.1),
        _row(page, "H100", "Resting protective stops with a 20% volatility target", 1.5),
        _row(page, "A01", "Multi-horizon time-series momentum", 1.1, kind="alternative"),
        _row(page, "HTS_CONTROL_1", "Audited HTS control", 0.9, kind="control"),
        _row(page, "A02", "Dual-momentum rotation", None, kind="alternative"),
    ]
    page_html = page.render_index(rows, suite_id="test-suite", revision="test-rev")
    html_path = tmp_path / "results.html"
    html_path.write_text(page_html, encoding="utf-8")
    harness = tmp_path / "harness.mjs"
    harness.write_text(HARNESS, encoding="utf-8")
    completed = subprocess.run(
        ["node", str(harness), str(html_path)],
        capture_output=True, text=True, check=True, timeout=120,
    )
    return json.loads(completed.stdout.strip().splitlines()[-1])


def test_text_columns_actually_reorder(tmp_path: Path) -> None:
    out = _run_page(tmp_path)
    # ID sorts as text: A01 and A02 first, then H###, then the control.
    assert out["byId"] == ["A01", "A02", "H001", "H010", "H100", "HTS_CONTROL_1"]
    # Name sorts alphabetically, case-insensitively.
    assert out["byName"] == ["HTS_CONTROL_1", "A02", "A01", "H100", "H001", "H010"]


def test_numeric_columns_sort_and_missing_keys_sink(tmp_path: Path) -> None:
    out = _run_page(tmp_path)
    first, second = out["sharpeClick1"], out["sharpeClick2"]
    # Which click lands ascending depends on the class left by earlier clicks, so
    # identify the two directions rather than assuming one.
    ascending, descending = (first, second) if first[0] == "H010" else (second, first)
    assert ascending[:5] == ["H010", "H001", "HTS_CONTROL_1", "A01", "H100"]
    assert descending[:5] == ["H100", "A01", "HTS_CONTROL_1", "H001", "H010"]
    assert set(ascending) == set(descending)
    # The blocked row carries no numeric key, so it sinks in both directions.
    assert ascending[-1] == "A02"
    assert descending[-1] == "A02"


def test_search_filter_hides_rows_and_reports_a_count(tmp_path: Path) -> None:
    out = _run_page(tmp_path)
    assert out["filtered"] == ["A01", "A02"]
    assert out["countText"] == "2 of 6 configurations"
