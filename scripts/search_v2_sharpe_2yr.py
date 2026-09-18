"""Retest every candidate from search waves 1-3 on the TWO-YEAR window
(2024-09-08 -> 2026-09-08), native LumiBot engine, same conventions.
Runs all candidates each wave's build_candidates() produces, so it also
covers the 20 wave-1 configs that never completed.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

LUMIBOT = Path("/Users/openclaw/.hermes/profiles/free_money_glitch/home/lumibot")
sys.path.insert(0, str(LUMIBOT))
sys.path.insert(0, str(LUMIBOT / "scripts"))

from strategy_lab.native_experiments import WINDOW_BY_LABEL, run_candidate
from strategy_lab.hts_variants import HTS_V2_BASELINE

import search_v2_sharpe as W1
import search_v2_sharpe_wave2 as W2
import search_v2_sharpe_wave3 as W3

WINDOW = WINDOW_BY_LABEL["two_year"]
OUT_DIR = LUMIBOT / "reports" / "hts_v2_search_2yr_2026-09-17"
OUT_DIR.mkdir(parents=True, exist_ok=True)
RESULTS = OUT_DIR / "search_results_2yr.jsonl"


def gather() -> list[tuple[str, str, object]]:
    """Return (wave, candidate_id, CandidateSpec) across all 3 waves, deduped by id."""
    seen = set()
    out = []
    for wave, mod in (("1", W1), ("2", W2), ("3", W3)):
        for cid, cand in mod.build_candidates():
            key = cand.fingerprint()  # dedupe by config so identical configs across waves collapse
            if key in seen:
                continue
            seen.add(key)
            out.append((wave, cid, cand))
    print(f"gathered {len(out)} unique candidates across waves", file=sys.stderr)
    return out


def worker(args) -> dict:
    wave, cid, cand = args
    try:
        res = run_candidate(candidate=cand, window=WINDOW, out_dir=OUT_DIR, control_baseline=dict(HTS_V2_BASELINE))
        payload = getattr(res, "payload", {})
        metrics = payload.get("metrics", {}) or {}
        return {
            "candidate_id": cid, "wave": wave, "window": "two_year",
            "sharpe": metrics.get("sharpe"), "total_return": metrics.get("total_return"),
            "max_drawdown": metrics.get("max_drawdown"), "final_equity": metrics.get("final_equity"),
            "fills": payload.get("fills"), "params": dict(cand.parameters),
            "problems": list(getattr(res, "problems", ())),
        }
    except Exception as e:
        return {"candidate_id": cid, "wave": wave, "window": "two_year",
                "error": f"{type(e).__name__}: {str(e)[:120]}", "sharpe": None}


def main() -> int:
    from concurrent.futures import ProcessPoolExecutor, as_completed
    from multiprocessing import cpu_count
    cands = gather()
    workers = max(2, min(4, cpu_count()))
    best = {"sharpe": -1e9}
    with open(RESULTS, "w") as f:
        with ProcessPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(worker, (w, cid, c)): cid for w, cid, c in cands}
            for fut in as_completed(futs):
                cid = futs[fut]
                rec = fut.result()
                f.write(json.dumps(rec, sort_keys=True, default=str) + "\n"); f.flush()
                sh = rec.get("sharpe")
                if sh is not None and not rec.get("error"):
                    if sh > best["sharpe"]:
                        best = rec
                elif rec.get("error"):
                    print("ERR", cid, rec["error"])
        print(f"\n2yr retest exhausted. best={best.get('candidate_id')} sharpe={best.get('sharpe')} ret={best.get('total_return')} dd={best.get('max_drawdown')}")
        print("params:", json.dumps(best.get("params", {}), default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())