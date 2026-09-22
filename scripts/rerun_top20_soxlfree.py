"""Regenerate missing SOXL-free (-b) top-20 results and republish the page.

Why this exists
---------------
The top-20 index page (glitch.defenders.co.nz/top20/ and the local
glitch-strategy-infra/reports/top20/) listed all 26 base + 26 SOXL-free (-b)
rows, but every -b row rendered as "–" for all metric columns. Two independent
causes:

1. The SOXL-free backtest result files (search_results_soxlfree.jsonl and
   search_results_soxlfree_extrawindows.jsonl) were deleted during disk
   reclamation, so the index builder found no metrics for the -b rows.
2. Even had those files existed, build_top20_graphs.py had a lookup-key bug:
   it builds the -b index row keyed by "<cid>-b" but the result JSONL records
   are keyed by the BASE candidate id, so the lookup never matched.

This script:
  A. Reconstructs the deleted merged top-20 config JSON (hts_v2_sharpe_top20_
     merged_2026-09-17.json) faithfully from the wave search candidate builders
     (search_v2_sharpe.py / _wave2 / _wave3) -- verified all 26 base candidate
     params are recoverable and identical to the values embedded in the graph
     pages.
  B. Re-runs the 26 SOXL-free variants on all 4 windows (six_year, two_year,
     early, y2022_2024) on the NATIVE LumiBot engine, writing the two result
     JSONLs (6y/2y and early/2022-24) plus per-window run_stats.csv /
     run_trades.csv.
  C. Rebuilds the index + 52 graph pages with the FIXED builder (correct -b
     lookup key, and -b graph pages show SOXL-free metrics, not base).
  D. Republishes into glitch-strategy-infra/reports/top20.

Run from the lumibot repo root with the repo venv:
  .venv/bin/python scripts/rerun_top20_soxlfree.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

LUMIBOT = Path("/Users/openclaw/.hermes/profiles/free_money_glitch/home/lumibot")
sys.path.insert(0, str(LUMIBOT))
REPORTS = LUMIBOT / "reports"
VENV_PY = LUMIBOT / ".venv" / "bin" / "python"
INFRA_OUT = Path("/Users/openclaw/.hermes/profiles/free_money_glitch/glitch-strategy-infra/reports/top20")

MERGED_OUT = REPORTS / "hts_v2_sharpe_top20_merged_2026-09-17.json"
SOXL_SIX_TWO_JSONL = REPORTS / "hts_v2_soxlfree_2026-09-17" / "search_results_soxlfree.jsonl"
SOXL_EW_JSONL = REPORTS / "hts_v2_soxlfree_extrawindows_2026-09-18" / "search_results_soxlfree_extrawindows.jsonl"

# The 26 top-20 base ids (from the surviving graph pages in glitch-strategy-infra).
TOP20_IDS = [
    "W0007", "S079", "W0006", "S159", "W0005", "S134", "S160", "W0004",
    "S156", "S154", "S158", "S169", "S155", "S165", "W0021", "S157",
    "W0028", "S153", "S014", "S144", "W0019", "S139", "S015", "S016",
    "W0018", "W0020",
]
# Base ids are keyed by their run dir name (candidate_id as built by the waves).
assert len(set(TOP20_IDS)) == 26, "expected exactly 26 distinct top-20 ids"


def log(msg: str) -> None:
    print(msg, flush=True)


def step_a_reconstruct_merged() -> dict:
    """Rebuild the deleted merged top-20 JSON from the wave candidate builders."""
    log("A. Reconstructing merged top-20 config from wave search builders ...")
    from scripts import search_v2_sharpe as W1
    from scripts import search_v2_sharpe_wave2 as W2
    from scripts import search_v2_sharpe_wave3 as W3

    pool: dict[str, dict] = {}
    for name, mod in (("W1", W1), ("W2", W2), ("W3", W3)):
        try:
            for cid, cand in mod.build_candidates():
                pool[cid] = dict(cand.parameters)
        except Exception as e:  # noqa: BLE001
            log(f"  WARN {name}.build_candidates failed: {type(e).__name__}: {e}")

    missing = [c for c in TOP20_IDS if c not in pool]
    if missing:
        raise RuntimeError(f"cannot reconstruct params for: {missing}")

    merged_entries = [
        {"candidate_id": cid, "params": pool[cid],
         "sharpe": None, "total_return": None, "max_drawdown": None, "fills": None}
        for cid in TOP20_IDS
    ]
    merged = {"merged": merged_entries, "two_top": []}
    MERGED_OUT.write_text(
        json.dumps(merged, indent=1, sort_keys=True, default=str), encoding="utf-8"
    )
    log(f"  wrote {MERGED_OUT.name}: {len(merged_entries)} candidates (params only;"
        f" metrics refilled by the runs)")
    return merged


def _run(py_cmd: list[str], label: str) -> None:
    log(f"  running {label} ...")
    proc = subprocess.run([str(VENV_PY), *py_cmd], cwd=LUMIBOT)
    if proc.returncode != 0:
        raise RuntimeError(f"{label} failed with exit {proc.returncode}")
    log(f"  {label} OK")


def step_b_run_soxlfree_variants() -> None:
    """Re-run the 26 SOXL-free variants on 6y/2y + early/2022-24 windows."""
    if SOXL_SIX_TWO_JSONL.exists():
        log("  six/two-year soxlfree results already present, skipping (delete to force).")
    else:
        log("B1. Running 26 SOXL-free variants on six_year + two_year ...")
        _run(["scripts/run_hts_soxlfree.py"], "run_hts_soxlfree.py")

    if SOXL_EW_JSONL.exists():
        log("  early/2022-24 soxlfree results already present, skipping (delete to force).")
    else:
        log("B2. Running 26 SOXL-free variants on early + y2022_2024 ...")
        _run(["scripts/run_soxlfree_extrawindows.py"], "run_soxlfree_extrawindows.py")


def step_c_rebuild_pages() -> None:
    """Regenerate the 26 SOXL-free (-b) graph pages + -b index rows ONLY.

    The base graph pages, base index rows, and the base raw artifacts were all
    deleted/are still fine, so we must NOT run the full build_top20_graphs.py
    (it would blank the base rows/pages whose extrawindows JSONL is also gone).
    We reuse its _build_one for the -b variant and patch the published index by
    preserving the existing base rows and regenerating ONLY the -b rows.
    """
    import re

    log("C. Regenerating -b graph pages + -b index rows (base untouched) ...")

    # --- load the fixed builder for helpers + -b _build_one ---
    sys.path.insert(0, str(LUMIBOT / "scripts"))
    import build_top20_graphs as btg

    merged = json.load(open(REPORTS / "hts_v2_sharpe_top20_merged_2026-09-17.json"))
    entries = list(merged["merged"])

    # metric lookups (identical structure to the full builder)
    b_lut = btg._wind_lookup(
        btg._load_jsonl("hts_v2_soxlfree_extrawindows_2026-09-18/search_results_soxlfree_extrawindows.jsonl"))
    b6_lut = btg._wind_lookup(
        btg._load_jsonl("hts_v2_soxlfree_2026-09-17/search_results_soxlfree.jsonl"))
    for e in entries:
        cid = e["candidate_id"]
        b_lut[(cid, "six_year")] = b6_lut.get((cid, "six_year"), {})
        b_lut[(cid, "two_year")] = b6_lut.get((cid, "two_year"), {})

    # regenerate every -b graph page from fresh run artifacts
    n_pages = 0
    for e in entries:
        size = btg._build_one(e, variant="b", b_lut=b_lut)
        if size is None:
            log(f"  WARN could not build {e['candidate_id']}-b graph page")
            continue
        n_pages += 1
    log(f"  regenerated {n_pages} -b graph pages")

    # --- patch the published index.html: keep base <tr>s, replace -b <tr>s ---
    idx_path = INFRA_OUT / "index.html"
    existing = idx_path.read_text(encoding="utf-8")
    m = re.search(r"<tbody>(.*?)</tbody>", existing, re.S)
    if not m:
        raise RuntimeError("could not locate <tbody> in existing index.html")
    old_rows = re.findall(r"<tr>.*?</tr>", m.group(1), re.S)
    if len(old_rows) != 52:
        raise RuntimeError(f"expected 52 existing rows, found {len(old_rows)}")
    base_rows = old_rows[:26]                      # keep base rows verbatim
    new_b_rows = []
    bi = 27
    for e in entries:
        cid = e["candidate_id"]
        new_b_rows.append(btg._list_row(bi, f"{cid}-b", b_lut, "b", lookup_cid=cid))
        bi += 1
    row_html = "".join(base_rows) + "".join(new_b_rows)
    new_idx = existing[:m.start()] + row_html + existing[m.end():]
    idx_path.write_text(new_idx, encoding="utf-8")
    log(f"  rewrote {idx_path.name} ({len(base_rows)} base + {len(new_b_rows)} -b rows)")


def step_d_verify() -> dict[str, int]:
    log("D. Verifying output ...")
    # Count rows and metric fills in published index.
    idx = INFRA_OUT / "index.html"
    idx_src = idx.read_text(encoding="utf-8") if idx.exists() else ""
    n_rows = idx_src.count("<td class='num'>") // 11 if idx_src else 0
    n_links = idx_src.count("_graphs.html'>") if idx_src else 0
    n_b_dash = sum(
        1 for _ in _b_row_dashs(idx_src)
    ) if idx_src else 0
    # jsonl row counts
    def jl_rows(p: Path) -> int:
        if not p.exists():
            return 0
        return sum(1 for _ in open(p))
    return {
        "index_rows": n_rows,
        "index_links": n_links,
        "b_rows_blank": n_b_dash,
        "six_two_soxlfree": jl_rows(SOXL_SIX_TWO_JSONL),
        "ew_soxlfree": jl_rows(SOXL_EW_JSONL),
    }


def _b_row_dashs(html: str) -> list[bool]:
    """Return True per -b row whose metric cells are all blank/dash."""
    import re
    rows = re.findall(r"<tr>(.*?)</tr>", html, re.S)
    out = []
    for r in rows:
        if "no SOXL" not in r:
            continue
        cells = re.findall(r"<td[^>]*>(.*?)</td>", r, re.S)
        metric_cells = cells[2:-1]  # skip # and ID + trailing empty
        blank = all(re.sub(r"<[^>]+>", "", c).strip() in ("", "–") for c in metric_cells)
        out.append(blank)
    return out


def main() -> int:
    step_a_reconstruct_merged()
    step_b_run_soxlfree_variants()
    step_c_rebuild_pages()
    v = step_d_verify()
    log(f"VERIFY {json.dumps(v, sort_keys=True)}")
    if v["index_rows"] != 52:
        raise RuntimeError(f"expected 52 index rows, got {v['index_rows']}")
    if v["index_links"] != 52:
        raise RuntimeError(f"expected 52 index links, got {v['index_links']}")
    if v["b_rows_blank"] != 0:
        raise RuntimeError(f"{v['b_rows_blank']} SOXL-free rows still blank")
    log("SUCCESS: top-20 (base + SOXL-free) fully repopulated and republished.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())