"""Retrospective walk-forward selection evaluator for the native HTS suite.

Reads the per-candidate, per-window artifacts the native runner wrote and
implements the plan's Phase 4/5 selection procedure:

* Selection uses ONLY the inner-validation windows (``{fold}_innerA`` and
  ``{fold}_innerB``) — never the outer test window.  Default ordering:
  ``score = median(inner Sharpe) - 0.5 * IQR(inner Sharpe)``, tie-break by
  lower turnover, then simpler rule count, then ID.
* The winner is FROZEN before the outer test; the reported number for a fold is
  that pre-selected candidate's held-out ``{fold}_test`` result.
* Every candidate is still evaluated on every outer test for transparent
  comparison, but only the pre-selected row is the *reported* selection result
  — picking the best outer-test row afterwards would re-introduce the bias.

Also builds a stitched selection-procedure track record: chain each fold's
pre-selected candidate's outer-test daily returns with charged turnover at the
boundaries (from a cash start), and a per-fold isolated-diagnostics table.

Usage
-----
    python scripts/evaluate_walk_forward.py --out-dir reports/hts_walkforward_2026-09-14
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

FOLD_NAMES = ("f1", "f2", "f3", "f4", "f5", "f6")


@dataclass
class FoldRun:
    candidate_id: str
    family_id: str
    window_label: str
    metrics: dict[str, Any]
    fills: int = 0
    fees: float = 0.0
    turnover: float = 0.0
    runtime_seconds: float | None = None
    ok: bool = False
    problems: list[str] = field(default_factory=list)
    daily_returns: list[float] = field(default_factory=list)


@dataclass
class FoldResult:
    fold: str
    disc_end: str
    inner_leaderboard: list[dict[str, Any]]
    selected_id: str
    selected_inner_sharpe: float
    outer: dict[str, Any] | None
    all_outer: list[dict[str, Any]]


def _load_result(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _load_daily_returns(stats_path: Path) -> list[float]:
    """Daily arithmetic returns from the native runner's equity curve."""
    import pandas as pd

    if not stats_path.exists():
        return []
    try:
        frame = pd.read_csv(stats_path, usecols=["datetime", "portfolio_value"])
        stamps = pd.to_datetime(frame["datetime"], utc=True)
        frame = frame.assign(_stamp=stamps, _session=stamps.dt.date)
        daily = frame.sort_values("_stamp").groupby("_session", sort=True)["portfolio_value"].last()
        series = daily.astype(float)
        returns = series.pct_change().dropna()
        return [float(x) for x in returns.tolist()]
    except Exception:
        return []


def _fold_schedule(out_dir: Path) -> dict[str, str]:
    """Map fold -> outer-test start from the manifest's windows list."""
    manifest_path = out_dir / "suite_manifest.json"
    if not manifest_path.exists():
        return {f: "" for f in FOLD_NAMES}
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        windows = manifest.get("windows") or []
        out: dict[str, str] = {}
        for entry in windows:
            label = str(entry.get("label", ""))
            for fold in FOLD_NAMES:
                if label == f"{fold}_test":
                    out[fold] = str(entry.get("start", ""))
        return out
    except Exception:
        return {f: "" for f in FOLD_NAMES}


def _leader_turnover(candidate_id: str, runs: Sequence[FoldRun]) -> float:
    """Approximate turnover proxy from fill count scaled by window length."""
    fills = sum(r.fills for r in runs)
    sessions = max((r.metrics.get("sessions") for r in runs if r.metrics), default=0)
    return (fills / sessions) if sessions else float("inf")


def _rule_count(candidate_id: str) -> int:
    """Proxy for rule simplicity: length of candidate ID payload name is unavailable here;
    return 1 for all (tie-break falls through to ID, which is deterministic)."""
    return 1


def _select_leader(candidate_id: str, runs: Sequence[FoldRun]) -> tuple[float, dict[str, Any]]:
    """Plan default inner ordering: median - 0.5*IQR of inner Sharpe."""
    sharpes = [r.metrics.get("sharpe") for r in runs if r.metrics and r.metrics.get("sharpe") is not None]
    if len(sharpes) < 2:
        median = float(sharpes[0]) if sharpes else -math.inf
        iqr = 0.0
    else:
        sorted_s = sorted(sharpes)
        n = len(sorted_s)
        q1 = sorted_s[n // 4] if n >= 4 else sorted_s[0]
        q3 = sorted_s[(3 * n) // 4] if n >= 4 else sorted_s[-1]
        median = statistics.median(sorted_s)
        iqr = q3 - q1
    return (median - 0.5 * iqr), {"median": median, "iqr": iqr, "sharpes": sharpes}


def _load_registry_family() -> dict[str, str]:
    try:
        from strategy_lab.experiment_registry import get_registry

        reg = get_registry()
        return {c.candidate_id: c.family_id for c in reg.all_candidates()}
    except Exception:
        return {}


def evaluate_fold(
    fold: str,
    out_dir: Path,
    runs_by_cand: Mapping[str, Mapping[str, Mapping[str, Any]]],
    registry_families: Mapping[str, str],
) -> FoldResult:
    """Run the selection procedure for a single fold.  Public + deterministic."""
    inner_labels = (f"{fold}_innerA", f"{fold}_innerB")
    test_label = f"{fold}_test"
    # ---- gather inner-validation rows (selection evidence only) ----
    inner_leaderboard: list[dict[str, Any]] = []
    for cid in sorted(runs_by_cand):
        runs = []
        for wl in inner_labels:
            payload = runs_by_cand[cid].get(wl)
            if not payload or not payload.get("ok"):
                continue
            runs.append(_to_foldrun(cid, wl, payload))
        if len(runs) < 2:
            continue  # need both inner windows to have a score
        score, detail = _select_leader(cid, runs)
        turnover = _leader_turnover(cid, runs)
        inner_leaderboard.append({
            "candidate_id": cid,
            "family_id": registry_families.get(cid, runs_by_cand[cid].get("_family", "")),
            "score": score,
            "median": detail["median"],
            "iqr": detail["iqr"],
            "inner_sharpes": detail["sharpes"],
            "inner_fills": sum(r.fills for r in runs),
            "turnover": turnover,
        })
    inner_leaderboard.sort(key=lambda row: (-row["score"], row["turnover"], row["candidate_id"]))
    if not inner_leaderboard:
        return FoldResult(fold, "", [], "", -math.inf, None, [])

    selected_id = inner_leaderboard[0]["candidate_id"]
    selected_inner_sharpe = float(inner_leaderboard[0]["score"])

    # ---- frozen outer test (held out; never used for selection) ----
    outer_payload = runs_by_cand.get(selected_id, {}).get(test_label) or {}
    outer = None
    if outer_payload and outer_payload.get("ok"):
        runs = _to_foldrun(selected_id, test_label, outer_payload)
        outer = {
            "candidate_id": selected_id,
            "sharpe": runs.metrics.get("sharpe"),
            "cagr": runs.metrics.get("cagr"),
            "total_return": runs.metrics.get("total_return"),
            "max_drawdown": runs.metrics.get("max_drawdown"),
            "volatility": runs.metrics.get("volatility"),
            "fills": runs.fills,
            "fees": runs.fees,
            "runtime_seconds": runs.runtime_seconds,
            "problems": runs.problems,
        }
    # ---- all candidates on the outer test, transparent comparison ----
    all_outer: list[dict[str, Any]] = []
    for cid in sorted(runs_by_cand):
        p = runs_by_cand[cid].get(test_label)
        if not p or not p.get("ok"):
            continue
        fr = _to_foldrun(cid, test_label, p)
        all_outer.append({
            "candidate_id": cid,
            "family_id": registry_families.get(cid, ""),
            "sharpe": fr.metrics.get("sharpe"),
            "cagr": fr.metrics.get("cagr"),
            "total_return": fr.metrics.get("total_return"),
            "max_drawdown": fr.metrics.get("max_drawdown"),
            "fills": fr.fills,
        })
    all_outer.sort(key=lambda row: -float(row["sharpe"] or -math.inf))
    return FoldResult(fold, "", inner_leaderboard, selected_id, selected_inner_sharpe, outer, all_outer)


def _to_foldrun(cid: str, window_label: str, payload: Mapping[str, Any]) -> FoldRun:
    metrics = dict(payload.get("metrics") or {})
    # trades file for fills/cost
    run_dir = Path(payload.get("_dir", ""))
    return FoldRun(
        candidate_id=cid,
        family_id=str(payload.get("family_id", "")),
        window_label=window_label,
        metrics=metrics,
        fills=int(payload.get("fills") or 0),
        fees=float(payload.get("fees") or 0.0),
        runtime_seconds=payload.get("runtime_seconds"),
        ok=bool(payload.get("ok")),
        problems=list(payload.get("problems") or []),
        daily_returns=_load_daily_returns(run_dir / "run_stats.csv") if run_dir.exists() else [],
    )


def load_suite(out_dir: Path) -> dict[str, dict[str, dict[str, Any]]]:
    """Load every candidate's run_result.json keyed by (candidate_id, window_label)."""
    out: dict[str, dict[str, dict[str, Any]]] = {}
    for run_dir in sorted(out_dir.iterdir()):
        if not run_dir.is_dir():
            continue
        cid = run_dir.name
        for window_dir in sorted(run_dir.iterdir()):
            if not window_dir.is_dir():
                continue
            result_path = window_dir / "run_result.json"
            if not result_path.exists():
                continue
            payload = _load_result(result_path)
            if payload is None:
                continue
            payload["_dir"] = str(window_dir)
            payload["ok"] = (payload.get("problems") == [])
            out.setdefault(cid, {})[window_dir.name] = payload
    return out


def stitch_selected(curves: Mapping[str, list[float]], order: list[str], cost_bps_per_side: float) -> dict[str, Any]:
    """Chain each fold's pre-selected daily returns through fold boundaries.

    Charges a full liquidation+re-entry turnover (two sides at cost) at each
    boundary as a standing position in equities is unwound and redeployed.
    Each fold is an independent from-cash run, so this is a conservative
    approximation of continuous carry — labelled as such in the report, not a
    claim of gapless live continuity.
    """
    nav = 1.0
    daily_nav: list[float] = []
    boundary_cost_each = cost_bps_per_side / 10_000.0
    for idx, key in enumerate(order):
        returns = curves.get(key, [])
        for r in returns:
            nav *= (1.0 + r)
            daily_nav.append(nav)
        if idx < len(order) - 1 and returns:
            nav *= (1.0 - 2.0 * boundary_cost_each)
            daily_nav[-1] = nav
    if len(daily_nav) < 2:
        return {"sessions": len(daily_nav), "total_return": float("nan"), "sharpe": float("nan"),
                "max_drawdown": float("nan")}
    arr = np.asarray(daily_nav, dtype=float)
    rets = np.diff(arr) / arr[:-1]
    sharpe = float(rets.mean() / rets.std(ddof=1) * math.sqrt(252.0)) if rets.std(ddof=1) else 0.0
    cagr_years = max((len(arr)) / 252.0, 1e-9)
    cagr = float((arr[-1] / arr[0]) ** (1.0 / cagr_years) - 1.0) if arr[0] > 0 else float("nan")
    dd = float(-(arr / np.maximum.accumulate(arr) - 1.0).min())
    return {
        "sessions": int(len(arr)),
        "total_return": float(arr[-1] - 1.0),
        "cagr": cagr,
        "sharpe": sharpe,
        "max_drawdown": dd,
        "cost_bps_per_side": cost_bps_per_side,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Walk-forward selection evaluator (native HTS)")
    parser.add_argument("--out-dir", default=str(ROOT / "reports" / "hts_walkforward_2026-09-14"))
    parser.add_argument("--cost-bps-per-side", type=float, default=3.5)
    args = parser.parse_args(argv)

    out_dir = Path(args.out_dir)
    if not out_dir.exists():
        print(f"no suite at {out_dir}", file=sys.stderr)
        return 2

    suite = load_suite(out_dir)
    families = _load_registry_family()
    folds_results: list[FoldResult] = []
    for fold in FOLD_NAMES:
        folds_results.append(evaluate_fold(fold, out_dir, suite, families))
    schedule = _fold_schedule(out_dir)

    # ---- stitched selected track record ----
    order: list[str] = []
    curves: dict[str, list[float]] = {}
    sel_rows: list[dict[str, Any]] = []
    for fr in folds_results:
        order.append(fr.fold)
        if fr.selected_id:
            runs = suite.get(fr.selected_id, {})
            stats_dir = runs.get(fr.fold + "_test", {}).get("_dir", "")
            if stats_dir:
                from pathlib import Path as P
                curves[fr.fold] = _load_daily_returns(P(stats_dir) / "run_stats.csv")
        sel_rows.append({
            "fold": fr.fold,
            "discovery_end": schedule.get(fr.fold, ""),
            "selected_id": fr.selected_id,
            "selected_inner_sharpe": round(fr.selected_inner_sharpe, 4) if fr.selected_inner_sharpe != -math.inf else None,
            "outer_sharpe": round(fr.outer["sharpe"], 4) if fr.outer and fr.outer.get("sharpe") is not None else None,
            "outer_total_return": round(fr.outer["total_return"], 4) if fr.outer and fr.outer.get("total_return") is not None else None,
            "outer_max_dd": round(fr.outer["max_drawdown"], 4) if fr.outer and fr.outer.get("max_drawdown") is not None else None,
        })
    stitched = stitch_selected(curves, order, args.cost_bps_per_side)

    report = {
        "procedure": "retrospective walk-forward, per fold select from inner-validation only (median-0.5*IQR), freeze, score held-out outer test",
        "selection_per_fold": sel_rows,
        "stitched_selected_track": {k: round(v, 6) if isinstance(v, float) else v for k, v in stitched.items()},
        "stitch_note": "independent from-cash fold runs chained with charged boundary turnover; NOT gapless continuous carry",
        "folds": {fr.fold: {
            "inner_leaderboard": fr.inner_leaderboard[:10],
            "selected": fr.selected_id,
            "outer": fr.outer,
            "all_outer_top": fr.all_outer[:10],
        } for fr in folds_results},
    }
    (out_dir / "walk_forward_report.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report["selection_per_fold"], indent=2, sort_keys=True))
    print("\nSTITCHED SELECTED TRACK:", json.dumps(report["stitched_selected_track"], sort_keys=True))
    print(f"\nwrote {out_dir / 'walk_forward_report.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())