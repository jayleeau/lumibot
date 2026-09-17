#!/usr/bin/env python3
"""Evaluate the locked HTS v2 six-block walk-forward selection programme.

This script deliberately separates ``--prepare-fold`` from ``--reveal-fold``.
Preparation reads only the six discovery blocks, independently audits them, and
writes an immutable selection lock. Reveal refuses to inspect the next outer
block unless that matching lock predates its result artifact.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from hashlib import sha256
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from strategy_lab.experiment_registry import get_registry  # noqa: E402
from strategy_lab.experiment_validation import audit_run, reconcile_trades  # noqa: E402
from strategy_lab.native_experiments import INITIAL_CASH, IMPLEMENTATION_REVISION, V2_FOLD_BLOCKS  # noqa: E402

PRIMARY_COST_BPS = 3.5


def _artifact(out_dir: Path, candidate_id: str, block: str) -> Path:
    return out_dir / candidate_id / block / "run_result.json"


def _load_payload(out_dir: Path, candidate_id: str, block: str) -> dict[str, Any]:
    path = _artifact(out_dir, candidate_id, block)
    if not path.exists():
        raise FileNotFoundError(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("kind") != "hts-v2":
        raise ValueError(f"{path}: expected kind hts-v2")
    if payload.get("implementation_revision") != IMPLEMENTATION_REVISION:
        raise ValueError(f"{path}: implementation revision mismatch")
    audit = audit_run(path.parent)
    if not audit.ok:
        raise ValueError(f"{path}: independent audit failed: {audit.findings}")
    return payload


def _position_pnls(payloads: Sequence[Mapping[str, Any]]) -> list[float]:
    values: list[float] = []
    for payload in payloads:
        for record in payload.get("position_pnl_records") or []:
            value = record.get("net_pnl")
            if value is not None and math.isfinite(float(value)):
                values.append(float(value))
    return values


def _pnl_breadth(payloads: Sequence[Mapping[str, Any]]) -> tuple[float, float]:
    values = _position_pnls(payloads)
    if not values:
        return float("-inf"), float("inf")
    series = pd.Series(values, dtype="float64")
    total = float(series.sum())
    top_three_share = float(series.nlargest(3).sum() / total) if total > 0.0 else float("inf")
    return float(series.median()), top_three_share


def _daily_returns(run_dir: Path) -> pd.Series:
    stats = pd.read_csv(run_dir / "run_stats.csv", usecols=["datetime", "portfolio_value"])
    stamps = pd.to_datetime(stats["datetime"], utc=True)
    daily = (
        stats.assign(_session=stamps.dt.strftime("%Y-%m-%d"), _stamp=stamps)
        .sort_values("_stamp")
        .groupby("_session", sort=True)["portfolio_value"]
        .last()
        .astype("float64")
    )
    returns = daily.pct_change()
    if not returns.empty:
        # Every walk-forward block starts independently from the native engine's
        # fixed cash budget, so its first saved session is also a return.
        returns.iloc[0] = daily.iloc[0] / INITIAL_CASH - 1.0
    return returns.dropna()


def _chained_metrics(out_dir: Path, candidate_id: str, blocks: Sequence[str]) -> dict[str, float]:
    nav = 1.0
    path: list[float] = [nav]
    boundary_cost = 2.0 * PRIMARY_COST_BPS / 10_000.0
    for index, block in enumerate(blocks):
        for value in _daily_returns(_artifact(out_dir, candidate_id, block).parent):
            nav *= 1.0 + float(value)
            path.append(nav)
        if index < len(blocks) - 1:
            nav *= 1.0 - boundary_cost
            path.append(nav)
    values = np.asarray(path, dtype="float64")
    returns = np.diff(values) / values[:-1]
    sharpe = float(returns.mean() / returns.std(ddof=1) * math.sqrt(252.0)) if len(returns) > 1 and returns.std(ddof=1) else 0.0
    max_drawdown = float(-(values / np.maximum.accumulate(values) - 1.0).min())
    return {"total_return": float(values[-1] - 1.0), "sharpe": sharpe, "max_drawdown": max_drawdown}


def _discovery_row(out_dir: Path, candidate: Any, blocks: Sequence[str]) -> dict[str, Any]:
    payloads = [_load_payload(out_dir, candidate.candidate_id, block) for block in blocks]
    metrics = [payload["metrics"] for payload in payloads]
    sharpes = [float(metric["sharpe"]) for metric in metrics]
    total_returns = [float(metric["total_return"]) for metric in metrics]
    costs: list[float] = []
    fills = 0
    sessions = 0
    for block, payload in zip(blocks, payloads):
        _net, fees, block_fills, _problems = reconcile_trades(_artifact(out_dir, candidate.candidate_id, block).parent / "run_trades.csv")
        costs.append(fees)
        fills += block_fills
        sessions += int(payload["metrics"]["sessions"])
    median_pnl, top_three_share = _pnl_breadth(payloads)
    chained = _chained_metrics(out_dir, candidate.candidate_id, blocks)
    net_pnl = sum(float(payload["metrics"]["final_equity"]) - 100_000.0 for payload in payloads)
    charged_cost = float(sum(costs))
    gross_profit = net_pnl + charged_cost
    positive_blocks = sum(1 for sharpe, total_return in zip(sharpes, total_returns) if sharpe > 0.0 and total_return > 0.0)
    eligible = (
        positive_blocks >= 4 and median_pnl > 0.0 and top_three_share <= 0.50
        and chained["max_drawdown"] <= 0.35 and gross_profit > 0.0
        and charged_cost <= 0.10 * gross_profit
    )
    return {
        "candidate_id": candidate.candidate_id,
        "parent_candidate_id": candidate.parent_candidate_id,
        "blocks": list(blocks),
        "block_sharpes": sharpes,
        "block_total_returns": total_returns,
        "positive_blocks": positive_blocks,
        "worst_block_sharpe": min(sharpes),
        "robust_sharpe": float(np.median(sharpes) - 0.5 * (np.percentile(sharpes, 75) - np.percentile(sharpes, 25))),
        "median_position_pnl": median_pnl,
        "top_three_pnl_share": top_three_share,
        "chained_max_drawdown": chained["max_drawdown"],
        "chained_total_return": chained["total_return"],
        "charged_transaction_cost": charged_cost,
        "gross_profit_before_cost": gross_profit,
        "fills_per_session": float(fills / sessions) if sessions else float("inf"),
        "eligible": eligible,
    }


def _selection_key(row: Mapping[str, Any]) -> tuple[float, float, float, float, float, float, str]:
    """Fixed lexicographic selection rule from the v2 plan."""
    return (
        -float(row["positive_blocks"]), -float(row["worst_block_sharpe"]), -float(row["robust_sharpe"]),
        float(row["top_three_pnl_share"]), float(row["chained_max_drawdown"]),
        float(row["fills_per_session"]), str(row["candidate_id"]),
    )


def _registry_hash() -> str:
    registry = get_registry()
    return sha256(json.dumps({c.candidate_id: c.fingerprint() for c in registry.all_candidates()}, sort_keys=True).encode()).hexdigest()


def prepare_fold(out_dir: Path, fold: str) -> Path:
    """Audit discovery blocks and write the immutable lock before outer reveal."""
    if fold not in V2_FOLD_BLOCKS:
        raise KeyError(fold)
    blocks, outer = V2_FOLD_BLOCKS[fold]
    registry = get_registry()
    rows = [_discovery_row(out_dir, candidate, blocks) for candidate in registry.hts_v2_variations]
    eligible = [row for row in rows if row["eligible"]]
    parent_champions = [
        min((row for row in eligible if row["parent_candidate_id"] == parent), key=_selection_key)
        for parent in sorted({row["parent_candidate_id"] for row in eligible})
    ]
    selected = min(parent_champions, key=_selection_key) if parent_champions else None
    manifest = json.loads((out_dir / "suite_manifest.json").read_text(encoding="utf-8"))
    lock = {
        "fold": fold, "discovery_blocks": list(blocks), "outer_block": outer,
        "registry_hash": _registry_hash(), "implementation_revision": IMPLEMENTATION_REVISION,
        "input_hashes": manifest.get("inputs"), "eligible_set": [row["candidate_id"] for row in eligible],
        "ranking_values": rows, "parent_champions": parent_champions,
        "selected_id": selected["candidate_id"] if selected else None,
        "selection_rule": "parent champion then fixed lexicographic robust rule",
    }
    lock["lock_hash"] = sha256(json.dumps(lock, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()
    path = out_dir / fold / "selection_lock.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        existing = json.loads(path.read_text(encoding="utf-8"))
        if existing.get("lock_hash") != lock["lock_hash"]:
            raise ValueError(f"{path}: existing lock disagrees with current audited discovery data")
        return path
    path.write_text(json.dumps(lock, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    return path


def reveal_fold(out_dir: Path, fold: str) -> dict[str, Any]:
    """Verify one lock and reveal only its frozen held-out candidate result."""
    if fold not in V2_FOLD_BLOCKS:
        raise KeyError(fold)
    _blocks, outer = V2_FOLD_BLOCKS[fold]
    lock_path = out_dir / fold / "selection_lock.json"
    if not lock_path.exists():
        raise FileNotFoundError(f"{lock_path}: prepare the fold before revealing its outer block")
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    if lock.get("registry_hash") != _registry_hash() or lock.get("implementation_revision") != IMPLEMENTATION_REVISION:
        raise ValueError(f"{lock_path}: registry or implementation revision mismatch")
    selected_id = lock.get("selected_id")
    if selected_id is None:
        return {"fold": fold, "selected_id": None, "outer_block": outer, "cash": True}
    outer_path = _artifact(out_dir, selected_id, outer)
    if not outer_path.exists():
        raise FileNotFoundError(outer_path)
    if lock_path.stat().st_mtime >= outer_path.stat().st_mtime:
        raise ValueError(f"{outer_path}: outer artifact predates selection lock; do not reveal retrospectively")
    payload = _load_payload(out_dir, selected_id, outer)
    return {
        "fold": fold, "selected_id": selected_id, "parent_candidate_id": payload.get("parent_candidate_id"),
        "outer_block": outer, "metrics": payload["metrics"],
        "position_pnl_records": payload.get("position_pnl_records") or [],
    }


def _write_report(out_dir: Path, revealed: Sequence[Mapping[str, Any]]) -> Path:
    selected = [row for row in revealed if row.get("selected_id")]
    outer_payloads = [_load_payload(out_dir, str(row["selected_id"]), str(row["outer_block"])) for row in selected]
    pnls = _position_pnls(outer_payloads)
    median, top_share = _pnl_breadth(outer_payloads)
    report = {
        "procedure": "v2 six-block discovery, parent champions, locked sequential outer reveal",
        "selection_per_fold": list(revealed),
        "outer_positive_fold_count": sum(1 for row in selected if float(row["metrics"]["total_return"]) > 0.0),
        "outer_position_breadth": {"median_position_pnl": median, "top_three_pnl_share": top_share, "positions": len(pnls)},
        "qualification_note": "Retrospective discovery evidence only; no qualification or live-edge claim.",
    }
    path = out_dir / "walk_forward_report.json"
    path.write_text(json.dumps(report, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    return path


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", default=str(ROOT / "reports" / "hts_v2_walkforward"))
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--prepare-fold", choices=tuple(V2_FOLD_BLOCKS))
    group.add_argument("--reveal-fold", choices=tuple(V2_FOLD_BLOCKS))
    group.add_argument("--all-folds", action="store_true")
    args = parser.parse_args(argv)
    out_dir = Path(args.out_dir)
    try:
        if args.prepare_fold:
            print(prepare_fold(out_dir, args.prepare_fold))
            return 0
        if args.reveal_fold:
            print(json.dumps(reveal_fold(out_dir, args.reveal_fold), indent=2, sort_keys=True, default=str))
            return 0
        revealed: list[dict[str, Any]] = []
        for fold in V2_FOLD_BLOCKS:
            prepare_fold(out_dir, fold)
            revealed.append(reveal_fold(out_dir, fold))
        print(_write_report(out_dir, revealed))
        return 0
    except (FileNotFoundError, KeyError, ValueError) as error:
        print(f"walk-forward evaluation refused: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
