#!/usr/bin/env python3
"""Run registered HTS candidates on LumiBot's native engine, several in parallel.

Reads the registry in ``strategy_lab/experiment_registry.py``, runs each candidate
and window pair through ``strategy_lab/native_experiments.py``, and writes one
artifact directory per candidate and window under the output directory.

Candidates whose mechanisms are not implemented yet are reported as ``unsupported``
and are never run with substitute behaviour.

Examples
--------
    python scripts/run_hts_experiments.py --list-supported
    python scripts/run_hts_experiments.py --ids HTS_CONTROL_1,H001,H002 --workers 8
    python scripts/run_hts_experiments.py --priority starting --workers 8
    python scripts/run_hts_experiments.py --all --workers 8 --resume
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEFAULT_OUT_DIR = ROOT / "reports" / "hts_variations"
DEFAULT_WORKERS = 8


@dataclass(frozen=True)
class Job:
    candidate_id: str
    window_label: str


def _worker(job: Job) -> dict[str, Any]:
    """Run one (candidate, window) pair.  Top-level so it stays picklable."""
    from strategy_lab.experiment_registry import get_registry
    from strategy_lab.hts_variants import HTS_BASELINE
    from strategy_lab.native_experiments import (
        UnsupportedCandidateError,
        WINDOW_BY_LABEL,
        run_candidate,
    )

    registry = get_registry()
    candidate = registry.get(job.candidate_id)
    window = WINDOW_BY_LABEL[job.window_label]
    out_dir = Path(os.environ["HTS_EXPERIMENT_OUT_DIR"])
    try:
        run = run_candidate(candidate, window, out_dir, control_baseline=HTS_BASELINE)
    except UnsupportedCandidateError as error:
        return {"candidate_id": job.candidate_id, "window": job.window_label,
                "status": "unsupported", "detail": str(error)}
    except Exception as error:  # keep one bad candidate from killing the suite
        return {"candidate_id": job.candidate_id, "window": job.window_label,
                "status": "error", "detail": f"{type(error).__name__}: {error}"}
    metrics = run.payload["metrics"]
    return {
        "candidate_id": job.candidate_id,
        "window": job.window_label,
        "status": "ok" if run.ok else "invalid",
        "problems": list(run.problems),
        "sharpe": metrics["sharpe"],
        "total_return": metrics["total_return"],
        "max_drawdown": metrics["max_drawdown"],
        "final_equity": metrics["final_equity"],
        "runtime_seconds": run.payload["runtime_seconds"],
    }


def _completed(result_path: Path) -> bool:
    if not result_path.exists():
        return False
    try:
        payload = json.loads(result_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return False
    return not payload.get("problems")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--ids", help="comma-separated candidate IDs")
    parser.add_argument("--kind", choices=("control", "hts", "alternative"))
    parser.add_argument("--family", help="family ID filter")
    parser.add_argument("--priority", choices=("starting", "standard", "deferred"))
    parser.add_argument("--all", action="store_true", help="every registered candidate")
    parser.add_argument("--windows", default="six_year,two_year", help="comma-separated window labels")
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR))
    parser.add_argument("--resume", action="store_true", help="skip candidates that already passed")
    parser.add_argument("--limit", type=int, help="cap the number of candidates (pilot runs)")
    parser.add_argument("--list-supported", action="store_true",
                        help="report which selected candidates this engine can run and exit")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    from strategy_lab.experiment_registry import get_registry
    from strategy_lab.hts_variants import HTS_BASELINE
    from strategy_lab.native_experiments import WINDOW_BY_LABEL, check_supported

    registry = get_registry()

    if args.ids:
        candidates = [registry.get(token.strip()) for token in args.ids.split(",") if token.strip()]
    else:
        candidates = list(registry.all_candidates())
    if args.kind:
        candidates = [c for c in candidates if c.kind == args.kind]
    if args.family:
        candidates = [c for c in candidates if c.family_id == args.family]
    if args.priority:
        candidates = [c for c in candidates if c.priority == args.priority]
    if args.limit:
        candidates = candidates[: args.limit]

    window_labels = [token.strip() for token in args.windows.split(",") if token.strip()]
    for label in window_labels:
        if label not in WINDOW_BY_LABEL:
            print(f"unknown window {label!r}; known: {sorted(WINDOW_BY_LABEL)}", file=sys.stderr)
            return 2

    runnable: list[Any] = []
    unsupported: list[tuple[Any, tuple[str, ...]]] = []
    for candidate in candidates:
        missing = check_supported(dict(candidate.parameters), HTS_BASELINE)
        if missing:
            unsupported.append((candidate, missing))
        else:
            runnable.append(candidate)

    if args.list_supported:
        print(f"runnable: {len(runnable)}   unsupported: {len(unsupported)}   windows: {window_labels}")
        for candidate in runnable:
            print(f"  run {candidate.candidate_id:16} {candidate.name}")
        for candidate, missing in unsupported:
            print(f"  --  {candidate.candidate_id:16} needs: {', '.join(missing)}")
        return 0

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    os.environ["HTS_EXPERIMENT_OUT_DIR"] = str(out_dir)

    jobs: list[Job] = []
    skipped = 0
    for candidate in runnable:
        for label in window_labels:
            if args.resume and _completed(out_dir / candidate.candidate_id / label / "run_result.json"):
                skipped += 1
                continue
            jobs.append(Job(candidate.candidate_id, label))

    print(f"candidates runnable={len(runnable)} unsupported={len(unsupported)} "
          f"jobs={len(jobs)} skipped={skipped} workers={args.workers}", flush=True)
    if unsupported:
        print("unsupported candidates are listed below and are not run with substitute behaviour",
              flush=True)
        for candidate, missing in unsupported:
            print(f"  unsupported {candidate.candidate_id}: {', '.join(missing)}", flush=True)

    started = time.perf_counter()
    results: list[dict[str, Any]] = []
    if jobs:
        with ProcessPoolExecutor(max_workers=max(1, args.workers)) as pool:
            futures = {pool.submit(_worker, job): job for job in jobs}
            for index, future in enumerate(as_completed(futures), start=1):
                job = futures[future]
                try:
                    record = future.result()
                except Exception as error:
                    record = {"candidate_id": job.candidate_id, "window": job.window_label,
                              "status": "error", "detail": f"{type(error).__name__}: {error}"}
                results.append(record)
                status = record.get("status")
                extra = ""
                if status == "ok":
                    extra = (f"sharpe={record['sharpe']:.3f} totret={record['total_return']:+.3f} "
                             f"dd={record['max_drawdown']:.3f} {record['runtime_seconds']:.1f}s")
                elif record.get("detail"):
                    extra = record["detail"][:90]
                print(f"[{index:>4}/{len(jobs)}] {job.candidate_id:16} {job.window_label:9} "
                      f"{status:12} {extra}", flush=True)
    elapsed = time.perf_counter() - started

    summary = {
        "out_dir": str(out_dir),
        "windows": window_labels,
        "workers": args.workers,
        "runnable": [candidate.candidate_id for candidate in runnable],
        "unsupported": {candidate.candidate_id: list(missing) for candidate, missing in unsupported},
        "skipped_completed": skipped,
        "elapsed_seconds": elapsed,
        "results": sorted(results, key=lambda row: (row["candidate_id"], row["window"])),
    }
    (out_dir / "suite_summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")

    ok = sum(1 for row in results if row.get("status") == "ok")
    invalid = sum(1 for row in results if row.get("status") == "invalid")
    errored = sum(1 for row in results if row.get("status") == "error")
    print(f"\nfinished {len(results)} job(s) in {elapsed:.1f}s on {args.workers} workers "
          f"(ok={ok} invalid={invalid} error={errored})")
    print(f"summary written to {(out_dir / 'suite_summary.json').relative_to(ROOT)}")
    return 0 if errored == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
