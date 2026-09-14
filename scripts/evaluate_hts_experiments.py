#!/usr/bin/env python3
"""Independently audit a native HTS experiment suite.

Rebuilds daily metrics from the saved artifacts without importing the strategy's
own metric code, reconciles positions and fees from the trade ledger, verifies the
runner's recorded metrics agree, and writes a result table plus a failure ledger.

Examples
--------
    python scripts/evaluate_hts_experiments.py --out-dir reports/hts_native_2026-09-14
    python scripts/evaluate_hts_experiments.py --out-dir reports/hts_native_2026-09-14 --write
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEFAULT_OUT_DIR = ROOT / "reports" / "hts_native_2026-09-14"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR))
    parser.add_argument("--write", action="store_true",
                        help="write suite_audit.json into the output directory")
    parser.add_argument("--top", type=int, default=15, help="rows to print per window")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    from strategy_lab.experiment_validation import audit_suite

    out_dir = Path(args.out_dir)
    audit = audit_suite(out_dir)
    print(f"suite {out_dir}")
    print(f"runs={audit['runs']} accepted={audit['accepted']} rejected={audit['rejected']}")

    table = audit["table"]
    for window in ("six_year", "two_year"):
        rows = [row for row in table if row["window"] == window and row["ok"]]
        rows.sort(key=lambda row: -(row["sharpe"] or float("-inf")))
        if not rows:
            continue
        print(f"\n{window}: top {min(args.top, len(rows))} by daily Sharpe")
        print(f"  {'ID':16} {'sharpe':>7} {'totret':>8} {'cagr':>8} {'vol':>7} {'maxdd':>7} {'sessions':>8}")
        for row in rows[: args.top]:
            print(f"  {row['candidate_id']:16} {row['sharpe']:>7.3f} {row['total_return']:>+8.3f} "
                  f"{row['cagr']:>+8.3f} {row['volatility']:>7.3f} {row['max_drawdown']:>7.3f} "
                  f"{row['sessions']:>8}")

    if audit["failed"]:
        print(f"\nfailure ledger ({len(audit['failed'])}):")
        for item in audit["failed"]:
            print(f"  {item['candidate_id']} {item['window']}: {item['findings']}")
    warnings = [(row["candidate_id"], row["window"], row["warnings"]) for row in table if row["warnings"]]
    if warnings:
        print(f"\nwarnings ({len(warnings)}):")
        for candidate_id, window, items in warnings[:20]:
            print(f"  {candidate_id} {window}: {items}")

    if args.write:
        target = out_dir / "suite_audit.json"
        target.write_text(json.dumps(audit, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
        print(f"\naudit written to {target}")
    return 0 if audit["rejected"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
