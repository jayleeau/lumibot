#!/usr/bin/env python3
"""Cost-stress the walk-forward selected track on the native engine.

The walk-forward report picked one frozen candidate per fold.  This re-runs
exactly those selected (candidate, test-window) paths through the native engine
at 7 bps and 15 bps per side (plan Phase 6) and re-stitches the selected track,
so we can see whether the forward edge survives realistic friction.

Usage
-----
    python scripts/stress_walk_forward_costs.py \
      --suite-out reports/hts_walkforward_2026-09-14 \
      --out reports/hts_wf_cost_stress \
      --fees 7,15
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from strategy_lab.experiment_registry import get_registry  # noqa: E402
from strategy_lab.hts_variants import HTS_BASELINE  # noqa: E402
from strategy_lab.native_experiments import (  # noqa: E402
    WINDOW_BY_LABEL,
    run_candidate,
)
from strategy_lab.native_alternatives import BLOCKED_ALTERNATIVES  # noqa: E402


def _rate_key(fee: float) -> str:
    return f"{fee:g}".replace(".", "p")


def _run(paths: list[tuple[str, str, str, float, str]], out: Path) -> list[dict[str, Any]]:
    """Run selected (candidate, test window, fee) triples in-process, sequential."""
    registry = get_registry()
    results: list[dict[str, Any]] = []
    for cid, fold, window_label, fee, base_window in paths:
        candidate = registry.get(cid)
        parameters = dict(candidate.parameters)
        parameters["cost_bps_per_side"] = fee
        stressed = replace(candidate, parameters=tuple(parameters.items()))
        window = WINDOW_BY_LABEL[window_label]
        try:
            candidate_run = run_candidate(
                stressed, window, out / _rate_key(fee),
                control_baseline=HTS_BASELINE,
            )
            m = candidate_run.payload.get("metrics") or {}
            results.append({
                "candidate_id": cid,
                "fold": fold,
                "window": window_label,
                "fee_bps_per_side": fee,
                "base_window": base_window,
                "sharpe": m.get("sharpe"),
                "total_return": m.get("total_return"),
                "max_drawdown": m.get("max_drawdown"),
                "cagr": m.get("cagr"),
                "problems": candidate_run.problems,
            })
        except Exception as error:  # noqa: BLE001
            results.append({
                "candidate_id": cid, "fold": fold, "window": window_label,
                "fee_bps_per_side": fee, "base_window": base_window,
                "sharpe": None, "total_return": None, "max_drawdown": None,
                "cagr": None, "error": f"{type(error).__name__}: {error}",
            })
    return results


def _stitch(fold_metrics: Mapping[str, Mapping[str, float | None]]) -> dict[str, Any]:
    """Chain per-fold test Sharpe into one number is not valid arithmetic across
    different candidates; instead report the mean/median of fold test Sharpe and
    the worst, as the honest summary of the selection procedure's cost sensitivity."""
    sharpes: list[float] = [
        float(v["sharpe"]) for v in fold_metrics.values()
        if v.get("sharpe") is not None
    ]
    if not sharpes:
        return {"mean_test_sharpe": None, "median_test_sharpe": None, "worst_test_sharpe": None, "n": 0}
    return {
        "mean_test_sharpe": float(np.mean(sharpes)),
        "median_test_sharpe": float(np.median(sharpes)),
        "worst_test_sharpe": float(min(sharpes)),
        "n": len(sharpes),
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Cost-stress walk-forward selected track")
    parser.add_argument("--suite-out", default=str(ROOT / "reports" / "hts_walkforward_2026-09-14"))
    parser.add_argument("--out", default=str(ROOT / "reports" / "hts_wf_cost_stress"))
    parser.add_argument("--fees", default="7,15", help="comma-separated bps per side to stress")
    args = parser.parse_args(argv)

    suite_out = Path(args.suite_out)
    report = json.loads((suite_out / "walk_forward_report.json").read_text(encoding="utf-8"))
    fees = [float(t) for t in args.fees.split(",")]

    # The v2 evaluator freezes one candidate before each named outer discovery
    # block is revealed.  Stress exactly that frozen path; never reselect at a
    # higher cost.
    selected: list[tuple[str, str, str]] = []
    for entry in report["selection_per_fold"]:
        fold = entry["fold"]
        cid = entry["selected_id"]
        block = entry.get("outer_block")
        if cid is not None and block:
            selected.append((cid, fold, str(block)))
    if not selected:
        print("no selected paths", file=sys.stderr)
        return 2

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    all_results: list[dict[str, Any]] = []
    for fee in fees:
        paths = [(cid, fold, block, fee, block) for cid, fold, block in selected]
        print(f"running {len(paths)} selected paths at {fee:g} bps/side ...", flush=True)
        all_results.extend(_run(paths, out))

    # fold per candidate per fee
    folded: dict[float, dict[str, dict[str, float | None]]] = {}
    for row in all_results:
        fee = float(row["fee_bps_per_side"])
        folded.setdefault(fee, {})[row["fold"]] = row
    summary = {f"{_rate_key(fee)}bps": _stitch(folded[fee]) for fee in fees}

    (out / "cost_stress.json").write_text(
        json.dumps({"fees": fees, "results": all_results, "summary": summary},
                   indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")

    print("\n=== COST STRESS: walk-forward selected test-track (mean/median/worst fold Sharpe) ===")
    for fee in fees:
        s = summary[f"{_rate_key(fee)}bps"]
        print(f"  {fee:g} bps/side: mean={s['mean_test_sharpe']} median={s['median_test_sharpe']} "
              f"worst={s['worst_test_sharpe']} n={s['n']}")
    print("\nper-(fold,candidate) rows:")
    for row in all_results:
        print(f"  {row['fee_bps_per_side']:g}bps {row['fold']} {row['candidate_id']:14} "
              f"sharpe={row['sharpe']} tr={row['total_return']} dd={row['max_drawdown']} "
              f"{row.get('error','')}")
    print(f"\nwrote {out / 'cost_stress.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
