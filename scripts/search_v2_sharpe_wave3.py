"""Wave-3: tight focus on the W0007 lineage (resting-stop-atr + vol-target +
min-hold) pushing tighter vol targets and larger top_n to try to break 1.8.
"""
from __future__ import annotations
import json, sys
from pathlib import Path
LUMIBOT = Path("/Users/openclaw/.hermes/profiles/free_money_glitch/home/lumibot")
sys.path.insert(0, str(LUMIBOT))
from strategy_lab.experiment_config import CandidateSpec, resolve_parameters, frozen_pairs
from strategy_lab.hts_variants import HTS_V2_BASELINE, HTS_V2_FAMILY
from strategy_lab.native_experiments import WINDOW_BY_LABEL, run_candidate

TARGET = 1.8
WINDOW = WINDOW_BY_LABEL["six_year"]
FAMILY = HTS_V2_FAMILY
OUT_DIR = LUMIBOT / "reports" / "hts_v2_search_wave3_2026-09-17"
OUT_DIR.mkdir(parents=True, exist_ok=True)
RESULTS = OUT_DIR / "search_results.jsonl"

def _slug(cid, params):
    key = ",".join(f"{k}={v}" for k, v in sorted(params.items()))
    return f"{cid.lower()}-{__import__('hashlib').sha256(key.encode()).hexdigest()[:10]}"

def build_candidates():
    work = []
    n = 1
    def add(over):
        nonlocal n; cid = f"Z{n:04d}"; n += 1; work.append((cid, over))
    # W0007 lineage = resting-stop-atr + vol-target + min_hold 8; tighten vol, widen top_n
    for vt in [0.06, 0.08, 0.10, 0.12, 0.14, 0.15, 0.18, 0.25]:
        for tn in [4, 6, 8, 10]:
            add({**{"exit_mode":"resting-stop-atr","weight_mode":"vol-target","vol_covariance_sessions":60,
                   "min_position_holding_bars":10,"vol_target":vt,"top_n":tn,
                   "reentry_cooldown_bars":8,"min_trade_edge_bps":14.0,"risk_off_gate":"spy-sma200","risk_off_cooldown_bars":5}})
    for vt in [0.08, 0.10, 0.12, 0.15]:
        for gt in [0.60, 0.80, 0.995]:
            add({**{"exit_mode":"resting-stop-atr","weight_mode":"vol-target","vol_covariance_sessions":60,
                   "vol_target":vt,"top_n":8,"gross_target":gt,"min_position_holding_bars":12,
                   "risk_off_gate":"breadth-60","risk_off_cooldown_bars":8}})
    # heavier stacks
    for _ in range(3):
        add({**{"exit_mode":"resting-stop-atr","weight_mode":"vol-target","vol_covariance_sessions":40,
               "vol_target":0.10,"top_n":12,"min_position_holding_bars":12,"min_trade_edge_bps":28.0,
               "reentry_cooldown_bars":10,"risk_off_gate":"breadth-60","risk_off_cooldown_bars":10,
               "exposure_group_limit":1}})
    cids=[]; seen=set()
    for cid, over in work:
        try: resolved = resolve_parameters(HTS_V2_BASELINE, over, FAMILY)
        except Exception: continue
        cand=CandidateSpec(candidate_id=cid,name=f"Wave3 {cid}",slug=_slug(cid,dict(resolved)),kind="hts-v2",
            family_id=FAMILY.family_id,rule="wave3",hypothesis="push Sharpe>1.8",parent_candidate_id="H100",
            parameters=resolved,overrides=frozen_pairs(over))
        if cand.fingerprint() in seen: continue
        seen.add(cand.fingerprint()); cids.append((cid,cand))
    print(f"wave3 built {len(cids)}", file=sys.stderr)
    return cids

def worker(args):
    cid, cand = args
    try:
        res = run_candidate(candidate=cand, window=WINDOW, out_dir=OUT_DIR, control_baseline=dict(HTS_V2_BASELINE))
        payload=getattr(res,"payload",{}); metrics=payload.get("metrics",{}) or {}
        return {"candidate_id":cid,"wave":3,"sharpe":metrics.get("sharpe"),"total_return":metrics.get("total_return"),
                "max_drawdown":metrics.get("max_drawdown"),"final_equity":metrics.get("final_equity"),
                "fills":payload.get("fills"),"params":dict(cand.parameters),"problems":list(getattr(res,"problems",()))}
    except Exception as e:
        return {"candidate_id":cid,"wave":3,"error":f"{type(e).__name__}: {str(e)[:120]}","sharpe":None}

def main():
    from concurrent.futures import ProcessPoolExecutor, as_completed
    from multiprocessing import cpu_count
    cands=build_candidates(); workers=max(2,min(4,cpu_count())); best={"sharpe":-1e9}
    with open(RESULTS,"w") as f:
        with ProcessPoolExecutor(max_workers=workers) as ex:
            futs={ex.submit(worker,(cid,c)):cid for cid,c in cands}
            for fut in as_completed(futs):
                cid=futs[fut]; rec=fut.result()
                f.write(json.dumps(rec,sort_keys=True,default=str)+"\n"); f.flush()
                sh=rec.get("sharpe")
                if sh is not None and not rec.get("error"):
                    if sh>best["sharpe"]: best=rec
                    if sh>=TARGET:
                        print(f"\n*** FOUND: {cid} sharpe={sh:.3f} ret={rec['total_return']*100:.1f}% dd={rec['max_drawdown']*100:.1f}% ***")
                        print(json.dumps(rec["params"],indent=2)); return 0
                elif rec.get("error"): print("ERR",cid,rec["error"])
        print(f"\nwave3 exhausted. best={best.get('candidate_id')} sharpe={best.get('sharpe')} ret={best.get('total_return')} dd={best.get('max_drawdown')}")
        print("params:", json.dumps(best.get("params",{}),default=str))
    return 0

if __name__=="__main__": raise SystemExit(main())