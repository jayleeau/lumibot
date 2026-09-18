"""Run the 26 merged top-20 candidates on the `early` (2018-05->2021-12) and
`y2022_2024` (2022->2024) windows so the graph pages can show those regimes.

Reads each candidate's full resolved params from the merged top-20 JSON and
runs both windows on the native LumiBot engine. Results append to
out_dir/search_results_extrawindows.jsonl.
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
OUT_DIR = LUMIBOT / "reports" / "hts_v2_extrawindows_2026-09-18"
OUT_DIR.mkdir(parents=True, exist_ok=True)
RESULTS = OUT_DIR / "search_results_extrawindows.jsonl"
WINDOWS = [WINDOW_BY_LABEL["early"], WINDOW_BY_LABEL["y2022_2024"]]


def _jobs() -> list[tuple[dict, CandidateSpec, object]]:
    merged = json.load(open(MERGED))["merged"]
    jobs = []
    for entry in merged:
        params = dict(entry["params"])
        cand = CandidateSpec(
            candidate_id=f"{entry['candidate_id']}-ew",
            name=f"{entry['candidate_id']} extra windows",
            slug=f"ew-{entry['candidate_id'].lower()}",
            kind="hts-v2",
            family_id="family-v2-robust-overlay",
            rule="same recipe, early + 2022-2024 windows",
            hypothesis="extra asymmetric windows for graph pages",
            parent_candidate_id=entry["candidate_id"],
            parameters=frozen_pairs(params),
            overrides=tuple(),
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
            "window": window.label,
            "sharpe": metrics.get("sharpe"),
            "total_return": metrics.get("total_return"),
            "max_drawdown": metrics.get("max_drawdown"),
            "fills": payload.get("fills"),
            "errors": list(getattr(res, "problems", ())),
        }
    except Exception as e:  # noqa: BLE001
        return {"candidate_id": entry["candidate_id"], "window": window.label,
                "error": f"{type(e).__name__}: {str(e)[:120]}", "sharpe": None}


def main() -> int:
    from concurrent.futures import ProcessPoolExecutor, as_completed
    from multiprocessing import cpu_count
    jobs = _jobs()
    workers = max(2, min(4, cpu_count()))
    done = 0
    with open(RESULTS, "w") as f:
        with ProcessPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(worker, j): j[2].label for j in jobs}
            for fut in as_completed(futs):
                rec = fut.result()
                f.write(json.dumps(rec, sort_keys=True, default=str) + "\n")
                f.flush()
                done += 1
                if rec.get("error"):
                    print("ERR", rec.get("candidate_id"), rec["window"], rec["error"])
    print(f"done {done} jobs -> {RESULTS}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())