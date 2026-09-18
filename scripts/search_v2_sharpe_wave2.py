"""Wave-2 grid search: push harder toward six-year daily Sharpe > 1.8 by
stressing volatility reduction (tighter vol-target / gross_target), deeper
diversification (larger top_n), tighter risk-contribution caps, larger edge
floors, and stronger risk-off. Runs on the native six-year window.
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

TARGET = 1.8
WINDOW = WINDOW_BY_LABEL["six_year"]
FAMILY = HTS_V2_FAMILY
OUT_DIR = LUMIBOT / "reports" / "hts_v2_search_wave2_2026-09-17"
OUT_DIR.mkdir(parents=True, exist_ok=True)
RESULTS = OUT_DIR / "search_results.jsonl"


def _slug(cid: str, params: dict) -> str:
    key = ",".join(f"{k}={v}" for k, v in sorted(params.items()))
    digest = __import__("hashlib").sha256(key.encode()).hexdigest()[:10]
    return f"{cid.lower()}-{digest}"


def build_candidates() -> list[tuple[str, CandidateSpec]]:
    work: list[tuple[str, dict]] = []

    # Best-known lineage from wave 1: vol-target 0.20 + min_hold 8 + resting stop (S079 1.213)
    vol_base = {
        "exit_mode": "resting-stop-atr",
        "weight_mode": "vol-target",
        "vol_covariance_sessions": 60,
        "vol_target": 0.20,
        "min_position_holding_bars": 8,
    }
    fast = {"atr_period": 7, "atr_k": 1.5, "min_position_holding_bars": 8}

    vol_targets = [0.05, 0.08, 0.10, 0.12, 0.15, 0.20, 0.25]
    gross_targets = [0.50, 0.70, 0.90, 0.995]
    top_ns = [4, 5, 6, 8, 10, 12]
    caps = [None, 0.0025, 0.005, 0.0075, 0.01]
    edges = [0.0, 14.0, 21.0, 28.0, 42.0, 56.0]
    cooldowns = [0, 3, 5, 8, 12]
    gates = ["none", "spy-sma100", "spy-sma200", "qqq-sma100", "breadth-50", "breadth-60", "spy-and-qqq-sma200"]

    n = 1

    def add(over):
        nonlocal n
        cid = f"W{n:04d}"
        n += 1
        work.append((cid, over))

    # 1) vol-target sweep on the vol_base lineage
    for vt in vol_targets:
        add({**vol_base, "vol_target": vt})
    # 2) top_n diversification on vol_base (low vol target keeps risk bounded)
    for tn in top_ns:
        add({**vol_base, "top_n": tn, "vol_target": 0.15})
    # 3) tighter gross_target on vol_base
    for gt in gross_targets:
        add({**vol_base, "gross_target": gt, "vol_target": 0.15})
    # 4) risk-contribution caps on the fast lineage
    for cp in caps[1:]:
        add({**fast, "risk_contribution_cap": cp})
    # 5) edge floors on fast lineage
    for e in edges[1:]:
        add({**fast, "min_trade_edge_bps": e, "min_position_holding_bars": 10})
    # 6) risk-off gates on vol_base
    for g in gates:
        if g != "none":
            add({**vol_base, "risk_off_gate": g, "risk_off_cooldown_bars": 5})
    # 7) strongest stacks: tight vol + diversification + edge + gate
    stack_sets = [
        {**vol_base, "vol_target": 0.10, "top_n": 8, "min_trade_edge_bps": 21.0, "risk_off_gate": "breadth-50", "risk_off_cooldown_bars": 8, "min_position_holding_bars": 10, "reentry_cooldown_bars": 8},
        {**vol_base, "vol_target": 0.08, "top_n": 10, "min_trade_edge_bps": 28.0, "risk_off_gate": "spy-sma200", "risk_off_cooldown_bars": 8, "min_position_holding_bars": 12, "reentry_cooldown_bars": 8},
        {**vol_base, "vol_target": 0.12, "top_n": 12, "min_trade_edge_bps": 42.0, "risk_off_gate": "breadth-60", "risk_off_cooldown_bars": 10, "min_position_holding_bars": 10, "risk_contribution_cap": 0.0075},
        {**vol_base, "vol_target": 0.15, "top_n": 6, "min_trade_edge_bps": 28.0, "risk_off_cooldown_bars": 5, "min_position_holding_bars": 12},
        {**vol_base, "vol_target": 0.10, "top_n": 8, "exposure_group_limit": 1, "min_position_holding_bars": 12, "risk_off_gate": "breadth-50", "reentry_cooldown_bars": 8},
        {**vol_base, "vol_target": 0.20, "top_n": 10, "min_trade_edge_bps": 21.0, "min_position_holding_bars": 12, "risk_off_gate": "qqq-sma100"},
        {"atr_period": 14, "atr_k": 4.0, "weight_mode": "vol-target", "vol_target": 0.12, "vol_covariance_sessions": 40, "min_position_holding_bars": 12, "reentry_cooldown_bars": 8, "top_n": 8, "min_trade_edge_bps": 28.0, "risk_off_gate": "breadth-60"},
        {"exit_mode": "resting-stop-atr", "weight_mode": "vol-target", "vol_target": 0.10, "vol_covariance_sessions": 40, "min_position_holding_bars": 12, "reentry_cooldown_bars": 8, "top_n": 10, "risk_off_gate": "breadth-50", "min_trade_edge_bps": 14.0},
    ]
    for stk in stack_sets:
        add(stk)

    # build+dedup
    seen = set()
    candidates = []
    for cid, over in work:
        try:
            resolved = resolve_parameters(HTS_V2_BASELINE, over, FAMILY)
        except Exception:
            continue
        cand = CandidateSpec(
            candidate_id=cid, name=f"Wave2 {cid}", slug=_slug(cid, dict(resolved)),
            kind="hts-v2", family_id=FAMILY.family_id, rule="wave2 grid",
            hypothesis="wave2: push toward Sharpe>1.8 via vol reduction + diversification",
            parent_candidate_id="H100", parameters=resolved, overrides=frozen_pairs(over),
        )
        fp = cand.fingerprint()
        if fp in seen:
            continue
        seen.add(fp)
        candidates.append((cid, cand))
    print(f"wave2 built {len(candidates)} unique", file=sys.stderr)
    return candidates


def worker(args) -> dict:
    cid, cand = args
    try:
        res = run_candidate(candidate=cand, window=WINDOW, out_dir=OUT_DIR, control_baseline=dict(HTS_V2_BASELINE))
        payload = getattr(res, "payload", {})
        metrics = payload.get("metrics", {}) or {}
        return {
            "candidate_id": cid, "wave": 2,
            "sharpe": metrics.get("sharpe"), "total_return": metrics.get("total_return"),
            "max_drawdown": metrics.get("max_drawdown"), "final_equity": metrics.get("final_equity"),
            "fills": payload.get("fills"), "params": dict(cand.parameters),
            "problems": list(getattr(res, "problems", ())),
        }
    except Exception as e:
        return {"candidate_id": cid, "wave": 2, "error": f"{type(e).__name__}: {str(e)[:120]}", "sharpe": None}


def main() -> int:
    from concurrent.futures import ProcessPoolExecutor, as_completed
    from multiprocessing import cpu_count
    candidates = build_candidates()
    workers = max(2, min(4, cpu_count()))
    best = {"sharpe": -1e9}
    with open(RESULTS, "w") as f:
        with ProcessPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(worker, (cid, cand)): cid for cid, cand in candidates}
            for fut in as_completed(futs):
                cid = futs[fut]
                rec = fut.result()
                f.write(json.dumps(rec, sort_keys=True, default=str) + "\n"); f.flush()
                sh = rec.get("sharpe")
                if sh is not None and rec.get("error") is None:
                    if sh > best["sharpe"]:
                        best = rec
                    if sh >= TARGET:
                        print(f"\n*** FOUND wave2 target: {cid} sharpe={sh:.3f} ret={rec['total_return']*100:.1f}% dd={rec['max_drawdown']*100:.1f}% ***")
                        print(json.dumps(rec["params"], indent=2))
                        return 0
                elif rec.get("error"):
                    print("ERR", cid, rec["error"])
        print(f"\nwave2 exhausted. best={best.get('candidate_id')} sharpe={best.get('sharpe')} ret={best.get('total_return')} dd={best.get('max_drawdown')}")
        print("params:", json.dumps(best.get("params", {}), default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())