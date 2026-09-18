"""Grid search for v2 HTS configs with six-year daily Sharpe > 1.8, on the
native LumiBot engine. Generates candidates directly from HTS_V2_BASELINE
(family-v2-robust-overlay), runs each on the native six-year window via
run_candidate, and tries to stop early once a config clears the bar.

Parallel via ProcessPoolExecutor (process-per-config). Results streamed to a
JSONL result file. This is a DISCOVERY search, not qualification.
"""
from __future__ import annotations

import itertools
import json
import sys
from pathlib import Path

LUMIBOT = Path("/Users/openclaw/.hermes/profiles/free_money_glitch/home/lumibot")
sys.path.insert(0, str(LUMIBOT))

from strategy_lab.experiment_config import CandidateSpec, resolve_parameters, frozen_pairs
from strategy_lab.hts_variants import HTS_V2_BASELINE, HTS_V2_FAMILY
from strategy_lab.native_experiments import WINDOW_BY_LABEL, run_candidate
from strategy_lab.experiment_registry import get_registry

TARGET = 1.8
WINDOW = WINDOW_BY_LABEL["six_year"]
FAMILY = HTS_V2_FAMILY
OUT_DIR = LUMIBOT / "reports" / "hts_v2_search_2026-09-17"
OUT_DIR.mkdir(parents=True, exist_ok=True)
RESULTS = OUT_DIR / "search_results.jsonl"


def _slug(cid: str, params: dict) -> str:
    key = ",".join(f"{k}={v}" for k, v in sorted(params.items()))
    digest = __import__("hashlib").sha256(key.encode()).hexdigest()[:10]
    return f"{cid.lower()}-{digest}"


def build_candidates() -> list[tuple[str, CandidateSpec]]:
    # seed overrides from top performers + a focused grid on the winning lineage
    # (H022 fast-ATR / H027 slow-ATR) with v2 risk overlays and top_n expansion.
    work: list[tuple[str, dict]] = []

    base_lineages = [
        # H022 lineage (fast ATR 7, 1.5k)
        {"atr_period": 7, "atr_k": 1.5},
        # H027 lineage (slow ATR 28, 1.5k)
        {"atr_period": 28, "atr_k": 1.5},
        # H100 lineage (resting stop + vol target)
        {"exit_mode": "resting-stop-atr", "vol_target": 0.20, "vol_covariance_sessions": 60, "weight_mode": "vol-target"},
        # control-ish baseline
        {},
    ]

    atr_k_vals = [1.5, 2.0, 2.5, 3.0, 3.5, 4.0]
    cooldown_vals = [0, 2, 3, 5, 8]
    hold_vals = [3, 5, 8, 10]
    edge_vals = [0.0, 7.0, 14.0, 21.0, 28.0]
    cap_vals = [None, 0.01, 0.02, 0.03, 0.05]
    gates = ["none", "spy-sma100", "spy-sma200", "qqq-sma100", "breadth-50", "breadth-60"]
    top_n_vals = [2, 3, 4, 5, 8]

    # 1) One-factor sweeps near the top lineage (fast ATR + min-hold worked)
    for base in base_lineages:
        base_id = len(work)
        work.append((f"S{base_id+1:03d}", dict(base)))
        for k in atr_k_vals:
            work.append((f"S{len(work)+1:03d}", {**base, "atr_k": k}))
        for c in cooldown_vals:
            work.append((f"S{len(work)+1:03d}", {**base, "reentry_cooldown_bars": c}))
        for h in hold_vals:
            work.append((f"S{len(work)+1:03d}", {**base, "min_position_holding_bars": h}))
        for e in edge_vals:
            work.append((f"S{len(work)+1:03d}", {**base, "min_trade_edge_bps": e}))
        for cp in cap_vals:
            work.append((f"S{len(work)+1:03d}", {**base, "risk_contribution_cap": cp}))
        for g in gates:
            work.append((f"S{len(work)+1:03d}", {**base, "risk_off_gate": g}))

    # 2) Two-factor combos on the fast-ATR lineage (which produced V048/V047)
    fast = {"atr_period": 7, "atr_k": 1.5}
    for h, c in itertools.product(hold_vals, cooldown_vals):
        work.append((f"S{len(work)+1:03d}", {**fast, "min_position_holding_bars": h, "reentry_cooldown_bars": c}))
    for h, cp in itertools.product(hold_vals[:3], cap_vals[1:]):
        work.append((f"S{len(work)+1:03d}", {**fast, "min_position_holding_bars": h, "risk_contribution_cap": cp}))
    for h, g in itertools.product(hold_vals[:3], gates[:4]):
        work.append((f"S{len(work)+1:03d}", {**fast, "min_position_holding_bars": h, "risk_off_gate": g}))
    for h, e in itertools.product(hold_vals[:3], edge_vals[2:]):
        work.append((f"S{len(work)+1:03d}", {**fast, "min_position_holding_bars": h, "min_trade_edge_bps": e}))
    for tn in top_n_vals:
        work.append((f"S{len(work)+1:03d}", {**fast, "top_n": tn, "min_position_holding_bars": 5}))

    # 3) stack the best candidates from the search history (strong combos)
    stack_sets = [
        {**fast, "min_position_holding_bars": 5, "reentry_cooldown_bars": 3, "atr_k": 3.0},
        {**fast, "min_position_holding_bars": 8, "reentry_cooldown_bars": 5, "atr_k": 4.0},
        {**fast, "min_position_holding_bars": 10, "min_trade_edge_bps": 14, "atr_k": 2.5},
        {**fast, "min_position_holding_bars": 6, "risk_contribution_cap": 0.02, "atr_k": 3.0},
        {"atr_period": 7, "atr_k": 4.0, "min_position_holding_bars": 8, "reentry_cooldown_bars": 5, "risk_off_gate": "breadth-50", "top_n": 4},
        {"atr_period": 7, "atr_k": 3.0, "min_position_holding_bars": 8, "min_trade_edge_bps": 21, "top_n": 3},
        {"atr_period": 28, "atr_k": 4.0, "min_position_holding_bars": 8, "reentry_cooldown_bars": 5},
        {"atr_period": 14, "atr_k": 3.0, "min_position_holding_bars": 8, "reentry_cooldown_bars": 5, "top_n": 4},
        {**fast, "exposure_group_limit": 1, "reentry_cooldown_bars": 5, "top_n": 4, "atr_k": 3.0, "min_position_holding_bars": 8},
    ]
    for i, stk in enumerate(stack_sets):
        work.append((f"STACK{i+1:02d}", stk))

    # build CandidateSpecs, de-dup by resolved fingerprint
    seen: set = set()
    candidates: list[tuple[str, CandidateSpec]] = []
    dedup = 0
    for cid, over in work:
        try:
            resolved = resolve_parameters(HTS_V2_BASELINE, over, FAMILY)
        except Exception as e:
            print("skip", cid, type(e).__name__)
            continue
        cand = CandidateSpec(
            candidate_id=cid,
            name=f"Search {cid}",
            slug=_slug(cid, dict(resolved)),
            kind="hts-v2",
            family_id=FAMILY.family_id,
            rule="grid search config",
            hypothesis="discovery search for Sharpe>1.8",
            parent_candidate_id="H022",
            parameters=resolved,
            overrides=frozen_pairs(over),
        )
        fp = cand.fingerprint()
        if fp in seen:
            dedup += 1
            continue
        seen.add(fp)
        candidates.append((cid, cand))
    print(f"built {len(candidates)} unique candidates (deduped {dedup})", file=sys.stderr)
    return candidates


def worker(args) -> dict:
    cid, cand = args
    try:
        res = run_candidate(candidate=cand, window=WINDOW, out_dir=OUT_DIR, control_baseline=dict(HTS_V2_BASELINE))
        payload = getattr(res, "payload", {})
        metrics = payload.get("metrics", {}) or {}
        rec = {
            "candidate_id": cid,
            "sharpe": metrics.get("sharpe"),
            "total_return": metrics.get("total_return"),
            "max_drawdown": metrics.get("max_drawdown"),
            "final_equity": metrics.get("final_equity"),
            "fills": payload.get("fills"),
            "params": dict(cand.parameters),
            "problems": list(getattr(res, "problems", ())),
        }
        return rec
    except Exception as e:
        return {"candidate_id": cid, "error": f"{type(e).__name__}: {str(e)[:120]}", "sharpe": None}


def main() -> int:
    from multiprocessing import cpu_count
    from concurrent.futures import ProcessPoolExecutor, as_completed

    candidates = build_candidates()
    workers = max(2, min(6, cpu_count()))
    best = {"sharpe": -1e9}
    counts = {"done": 0, "error": 0}
    with open(RESULTS, "w") as f:
        with ProcessPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(worker, (cid, c)): cid for cid, c in candidates}
            for fut in as_completed(futs):
                cid = futs[fut]
                rec = fut.result()
                line = json.dumps(rec, sort_keys=True, default=str)
                f.write(line + "\n"); f.flush()
                sh = rec.get("sharpe")
                if sh is not None and rec.get("error") is None:
                    counts["done"] += 1
                    if sh > best["sharpe"]:
                        best = rec
                    if sh >= TARGET:
                        print(f"\n*** FOUND target: {cid} sharpe={sh:.3f} ret={rec['total_return']*100:.1f}% dd={rec['max_drawdown']*100:.1f}% ***")
                        print(json.dumps(rec["params"], indent=2))
                        return 0
                elif rec.get("error"):
                    counts["error"] += 1
                    print("ERR", cid, rec["error"])
        print(f"\nSearch exhausted: {counts['done']} ok, {counts['error']} err. Best: {best.get('candidate_id')} sharpe={best.get('sharpe')}")
        print("params:", json.dumps(best.get("params", {}), indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())