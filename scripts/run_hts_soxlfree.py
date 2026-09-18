"""Build & run "-b" (SOXL-free) variants of the top-20 performers.

For each candidate in the merged top-20 (six-year + two-year), clone its exact
resolved parameters but set universe=U0_EX_SOXL (U0 minus SOXL), then run both
the six-year and two-year windows on the native LumiBot engine.

Result rows are appended to out_dir/search_results_soxlfree.jsonl with:
  candidate_id  (base id), variant_id  (base + "-b"), window, sharpe, total_return,
  max_drawdown, fills, universe, params(shared)
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

LUMIBOT = Path("/Users/openclaw/.hermes/profiles/free_money_glitch/home/lumibot")
sys.path.insert(0, str(LUMIBOT))

from strategy_lab.experiment_config import CandidateSpec, frozen_pairs
from strategy_lab.hts_variants import HTS_V2_BASELINE
from strategy_lab.native_experiments import WINDOW_BY_LABEL, run_candidate

MERGED = LUMIBOT / "reports" / "hts_v2_sharpe_top20_merged_2026-09-17.json"
OUT_DIR = LUMIBOT / "reports" / "hts_v2_soxlfree_2026-09-17"
OUT_DIR.mkdir(parents=True, exist_ok=True)
RESULTS = OUT_DIR / "search_results_soxlfree.jsonl"
SOXL_FREE_UNIVERSE = "U0_EX_SOXL"

WINDOWS = [WINDOW_BY_LABEL["six_year"], WINDOW_BY_LABEL["two_year"]]


def _jobs() -> list[tuple[dict, object]]:
    merged = json.load(open(MERGED))["merged"]
    jobs = []
    for entry in merged:
        base_params = dict(entry["params"])
        # swap universe to the SOXL-free keyword; keep every other knob identical
        variant_params = {**base_params, "universe": SOXL_FREE_UNIVERSE}
        resolved = tuple(sorted(variant_params.items(), key=lambda kv: kv[0]))
        cand = CandidateSpec(
            candidate_id=f"{entry['candidate_id']}-b",
            name=f"{entry['candidate_id']} -b (no SOXL)",
            slug=f"soxlfree-{entry['candidate_id'].lower()}",
            kind="hts-v2",
            family_id="family-v2-robust-overlay",
            rule="same recipe, universe U0_EX_SOXL",
            hypothesis="measure top-20 edge without SOXL in the universe",
            parent_candidate_id=entry["candidate_id"],
            parameters=frozen_pairs(variant_params),
            overrides=tuple(sorted(
                {"universe": SOXL_FREE_UNIVERSE}.items(), key=lambda kv: kv[0])),
        )
        for window in WINDOWS:
            jobs.append((entry, cand, window))
    return jobs


def worker(args) -> dict:
    entry, cand, window = args
    try:
        res = run_candidate(candidate=cand, window=window, out_dir=OUT_DIR,
                            control_baseline=dict(HTS_V2_BASELINE))
        payload = getattr(res, "payload", {})
        metrics = payload.get("metrics", {}) or {}
        return {
            "candidate_id": entry["candidate_id"],
            "variant_id": cand.candidate_id,
            "window": window.label,
            "universe": SOXL_FREE_UNIVERSE,
            "sharpe": metrics.get("sharpe"),
            "total_return": metrics.get("total_return"),
            "max_drawdown": metrics.get("max_drawdown"),
            "final_equity": metrics.get("final_equity"),
            "fills": payload.get("fills"),
            "errors": list(getattr(res, "problems", ())),
            "orig_6y_sharpe": entry.get("sharpe"),
        }
    except Exception as e:  # noqa: BLE001
        return {"candidate_id": entry["candidate_id"], "variant_id": cand.candidate_id,
                "window": window.label, "error": f"{type(e).__name__}: {str(e)[:120]}",
                "sharpe": None}


def main() -> int:
    from concurrent.futures import ProcessPoolExecutor, as_completed
    from multiprocessing import cpu_count
    jobs = _jobs()
    print(f"soxl-free jobs: {len(jobs)} ({len({j[0]['candidate_id'] for j in jobs})} variants x 2 windows)", file=sys.stderr)
    workers = max(2, min(6, cpu_count()))
    with open(RESULTS, "w") as f:
        with ProcessPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(worker, j): j[1].candidate_id for j in jobs}
            done = 0
            for fut in as_completed(futs):
                rec = fut.result()
                f.write(json.dumps(rec, sort_keys=True, default=str) + "\n")
                f.flush()
                done += 1
                if rec.get("error"):
                    print("ERR", rec.get("variant_id"), rec["window"], rec["error"])
    print(f"done {done} jobs -> {RESULTS}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())