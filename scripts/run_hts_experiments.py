#!/usr/bin/env python3
"""Run registered HTS research candidates on LumiBot's native engine in parallel.

Reads the registry in ``strategy_lab/experiment_registry.py``, runs each candidate
and window pair through ``strategy_lab/native_experiments.py`` (HTS/control) or
``strategy_lab/native_alternatives.py`` (daily alternatives), and writes one
artifact directory per candidate and window under the output directory.

Candidates whose mechanisms are not implemented, or whose required data gate is
not closed, are reported as ``unsupported`` or ``blocked_data`` and are never run
with substitute behaviour.

Resume is revisioned.  ``--resume`` skips a job only when an existing artifact
carries the same candidate fingerprint, the same
``IMPLEMENTATION_REVISION``, the same window, and no validation problems, so
results from two implementations cannot be mixed.

Examples
--------
    python scripts/run_hts_experiments.py --list-supported
    python scripts/run_hts_experiments.py --ids HTS_CONTROL_1,H001,H002 --workers 8
    python scripts/run_hts_experiments.py --all --workers 8 --resume
    python scripts/run_hts_experiments.py --all --workers 8 --suite-id hts-native-2026-09-14-1
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEFAULT_OUT_DIR = ROOT / "reports" / "hts_variations"
# Each native job peaks near 1 GB resident.  The suite default is deliberately
# conservative so a batch cannot exhaust a 16 GB machine; raise it only after
# measuring peak RSS with ``--report-memory``.
DEFAULT_WORKERS = 3

STATUS_RUNNABLE = "runnable"
STATUS_UNSUPPORTED = "unsupported"
STATUS_BLOCKED_DATA = "blocked-data"


@dataclass(frozen=True)
class Job:
    candidate_id: str
    window_label: str


def _file_checksum(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _git_revision() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, timeout=20, check=False,
        )
        return result.stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def classify_candidate(candidate: Any, *, baseline: Mapping[str, Any]) -> tuple[str, tuple[str, ...]]:
    """Return ``(status, reasons)`` for one registry candidate."""
    from strategy_lab.experiment_config import KIND_ALTERNATIVE
    from strategy_lab.native_alternatives import BLOCKED_ALTERNATIVES, DAILY_ALTERNATIVES
    from strategy_lab.native_experiments import check_supported

    if candidate.kind == KIND_ALTERNATIVE:
        if candidate.candidate_id in DAILY_ALTERNATIVES:
            return STATUS_RUNNABLE, ()
        reason = BLOCKED_ALTERNATIVES.get(candidate.candidate_id, "no closed data gate")
        return STATUS_BLOCKED_DATA, (reason,)
    known_baseline = dict(baseline)
    if candidate.kind == "hts-v2":
        from strategy_lab.hts_variants import HTS_V2_BASELINE

        known_baseline.update(HTS_V2_BASELINE)
    missing = check_supported(dict(candidate.parameters), known_baseline)
    return (STATUS_RUNNABLE, ()) if not missing else (STATUS_UNSUPPORTED, tuple(missing))


def build_manifest(
    *,
    suite_id: str,
    registry: Any,
    baseline: Mapping[str, Any],
    window_labels: Sequence[str],
) -> dict[str, Any]:
    """Freeze the contract a suite's results are tied to (plan batch 0)."""
    from strategy_lab.native_experiments import (
        DAILY_DB,
        ENGINE_LABEL,
        HTS_HOURLY_CONVENTION,
        HOURLY_DB,
        IMPLEMENTATION_REVISION,
        WINDOW_BY_LABEL,
    )

    classification: dict[str, dict[str, Any]] = {}
    for candidate in registry.all_candidates():
        status, reasons = classify_candidate(candidate, baseline=baseline)
        classification[candidate.candidate_id] = {
            "kind": candidate.kind,
            "family_id": candidate.family_id,
            "parent_candidate_id": candidate.parent_candidate_id,
            "fingerprint": candidate.fingerprint(),
            "resolved_parameters_hash": sha256(
                json.dumps(dict(candidate.parameters), sort_keys=True, separators=(",", ":"), default=str).encode()
            ).hexdigest(),
            "implementation_status": "implemented" if status == STATUS_RUNNABLE else "not-implemented",
            "data_status": STATUS_BLOCKED_DATA if status == STATUS_BLOCKED_DATA else "available",
            "status": status,
            "reasons": list(reasons),
        }
    return {
        "suite_id": suite_id,
        "implementation_revision": IMPLEMENTATION_REVISION,
        "engine": ENGINE_LABEL,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "python": platform.python_version(),
        "git_commit": _git_revision(),
        "windows": [
            {
                "label": label,
                "start": WINDOW_BY_LABEL[label].start,
                "end": WINDOW_BY_LABEL[label].end,
                "warmup_start": WINDOW_BY_LABEL[label].warmup_start,
            }
            for label in window_labels
        ],
        "inputs": {
            "daily": {"path": str(DAILY_DB.relative_to(ROOT)), "sha256": _file_checksum(DAILY_DB)},
            "hourly": {"path": str(HOURLY_DB.relative_to(ROOT)), "sha256": _file_checksum(HOURLY_DB)},
        },
        "bar_conventions": {
            "hourly": HTS_HOURLY_CONVENTION,
            "daily_alternatives": "exchange-session daily bars; decisions use prior completed sessions",
        },
        "cost_convention": "3.5 bps per side, submitted on buy and sell fills",
        "registry_hash": sha256(
            json.dumps(
                {c.candidate_id: c.fingerprint() for c in registry.all_candidates()},
                sort_keys=True,
            ).encode()
        ).hexdigest(),
        "counts": registry.statistics(),
        "candidates": classification,
    }


def _worker(job: Job) -> dict[str, Any]:
    """Run one (candidate, window) pair.  Top-level so it stays picklable."""
    import resource

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
                "status": STATUS_BLOCKED_DATA, "detail": str(error)}
    except Exception as error:  # keep one bad candidate from killing the suite
        return {"candidate_id": job.candidate_id, "window": job.window_label,
                "status": "error", "detail": f"{type(error).__name__}: {error}"}
    metrics = run.payload["metrics"]
    return {
        "candidate_id": job.candidate_id,
        "parent_candidate_id": candidate.parent_candidate_id,
        "kind": candidate.kind,
        "window": job.window_label,
        "status": "ok" if run.ok else "invalid",
        "problems": list(run.problems),
        "warnings": list(run.payload.get("warnings", ())),
        "implementation_revision": run.payload.get("implementation_revision"),
        "fingerprint": run.payload.get("fingerprint"),
        "sharpe": metrics["sharpe"],
        "total_return": metrics["total_return"],
        "max_drawdown": metrics["max_drawdown"],
        "final_equity": metrics["final_equity"],
        "runtime_seconds": run.payload["runtime_seconds"],
        "peak_rss_mb": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1e6, 1),
    }


def _resumable(result_path: Path, *, fingerprint: str, revision: str) -> bool:
    """True only when an existing artifact matches this revision and fingerprint."""
    if not result_path.exists():
        return False
    try:
        payload = json.loads(result_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return False
    return (
        not payload.get("problems")
        and payload.get("fingerprint") == fingerprint
        and payload.get("implementation_revision") == revision
    )


def _summary_record(result_path: Path, candidate: Any, window_label: str) -> dict[str, Any]:
    """Build a summary row from an artifact that ``--resume`` reused."""
    payload = json.loads(result_path.read_text(encoding="utf-8"))
    metrics = payload.get("metrics") or {}
    return {
        "candidate_id": candidate.candidate_id,
        "parent_candidate_id": candidate.parent_candidate_id,
        "kind": candidate.kind,
        "window": window_label,
        "status": "ok" if not payload.get("problems") else "invalid",
        "problems": list(payload.get("problems") or []),
        "warnings": list(payload.get("warnings") or []),
        "implementation_revision": payload.get("implementation_revision"),
        "fingerprint": payload.get("fingerprint"),
        "sharpe": metrics.get("sharpe"),
        "total_return": metrics.get("total_return"),
        "max_drawdown": metrics.get("max_drawdown"),
        "final_equity": metrics.get("final_equity"),
        "runtime_seconds": payload.get("runtime_seconds"),
        "reused": True,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--ids", help="comma-separated candidate IDs")
    parser.add_argument("--kind", choices=("control", "hts", "hts-v2", "alternative"))
    parser.add_argument("--family", help="family ID filter")
    parser.add_argument("--priority", choices=("starting", "standard", "deferred"))
    parser.add_argument("--all", action="store_true", help="every registered candidate")
    parser.add_argument("--windows", default="six_year,two_year", help="comma-separated window labels")
    parser.add_argument(
        "--v2-walk-forward",
        action="store_true",
        help="use the prescribed b01-b12 six-month v2 block windows (requires --kind hts-v2 or explicit v2 IDs)",
    )
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    parser.add_argument("--recycle-workers", action="store_true", default=True,
                        help="restart a worker process after each job (default; caps leaked memory)")
    parser.add_argument("--no-recycle-workers", dest="recycle_workers", action="store_false",
                        help="keep worker processes alive across jobs (faster, more memory)")
    parser.add_argument("--report-memory", action="store_true",
                        help="print peak RSS per job and the batch maximum")
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR))
    parser.add_argument("--resume", action="store_true",
                        help="skip jobs that already passed under this revision and fingerprint")
    parser.add_argument("--suite-id", default=None, help="label written into the suite manifest")
    parser.add_argument("--limit", type=int, help="cap the number of candidates (pilot runs)")
    parser.add_argument("--list-supported", action="store_true",
                        help="report which selected candidates this engine can run and exit")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    from strategy_lab.experiment_registry import get_registry
    from strategy_lab.hts_variants import HTS_BASELINE
    from strategy_lab.native_experiments import IMPLEMENTATION_REVISION, WINDOW_BY_LABEL

    registry = get_registry()
    suite_id = args.suite_id or IMPLEMENTATION_REVISION

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

    window_labels = (
        [f"b{number:02d}" for number in range(1, 13)]
        if args.v2_walk_forward
        else [token.strip() for token in args.windows.split(",") if token.strip()]
    )
    if args.v2_walk_forward and not any(candidate.kind == "hts-v2" for candidate in candidates):
        print("--v2-walk-forward requires at least one hts-v2 candidate", file=sys.stderr)
        return 2
    for label in window_labels:
        if label not in WINDOW_BY_LABEL:
            print(f"unknown window {label!r}; known: {sorted(WINDOW_BY_LABEL)}", file=sys.stderr)
            return 2

    runnable: list[Any] = []
    blocked: list[tuple[Any, str, tuple[str, ...]]] = []
    for candidate in candidates:
        status, reasons = classify_candidate(candidate, baseline=HTS_BASELINE)
        if status == STATUS_RUNNABLE:
            runnable.append(candidate)
        else:
            blocked.append((candidate, status, reasons))

    if args.list_supported:
        implemented = sum(1 for c in registry.all_candidates()
                          if classify_candidate(c, baseline=HTS_BASELINE)[0] == STATUS_RUNNABLE)
        print(f"runnable: {len(runnable)}   blocked: {len(blocked)}   "
              f"windows: {window_labels}   revision: {IMPLEMENTATION_REVISION}")
        print(f"registry: {len(registry.all_candidates())} configurations, "
              f"{implemented} implemented")
        for candidate in runnable:
            print(f"  run     {candidate.candidate_id:16} {candidate.name}")
        for candidate, status, reasons in blocked:
            print(f"  {status:7} {candidate.candidate_id:16} {', '.join(reasons)}")
        return 0

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    os.environ["HTS_EXPERIMENT_OUT_DIR"] = str(out_dir)

    manifest = build_manifest(suite_id=suite_id, registry=registry,
                              baseline=HTS_BASELINE, window_labels=window_labels)
    (out_dir / "suite_manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    jobs: list[Job] = []
    skipped = 0
    reused: list[dict[str, Any]] = []
    for candidate in runnable:
        for label in window_labels:
            result_path = out_dir / candidate.candidate_id / label / "run_result.json"
            if args.resume and _resumable(result_path, fingerprint=candidate.fingerprint(),
                                          revision=IMPLEMENTATION_REVISION):
                skipped += 1
                reused.append(_summary_record(result_path, candidate, label))
                continue
            jobs.append(Job(candidate.candidate_id, label))

    print(f"suite={suite_id} revision={IMPLEMENTATION_REVISION} "
          f"runnable={len(runnable)} blocked={len(blocked)} "
          f"jobs={len(jobs)} skipped={skipped} workers={args.workers}", flush=True)
    for candidate, status, reasons in blocked:
        print(f"  {status} {candidate.candidate_id}: {', '.join(reasons)}", flush=True)

    started = time.perf_counter()
    # Reused artifacts count toward the suite summary, so the summary always
    # describes the whole suite rather than only this invocation's job list.
    results: list[dict[str, Any]] = list(reused)
    if jobs:
        # ``max_tasks_per_child=1`` means every job runs in a fresh process, so
        # all of a job's ~1 GB is returned to the OS before the next one starts.
        executor_kwargs: dict[str, Any] = {"max_workers": max(1, args.workers)}
        if args.recycle_workers:
            executor_kwargs["max_tasks_per_child"] = 1
        with ProcessPoolExecutor(**executor_kwargs) as pool:
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
                if args.report_memory and record.get("peak_rss_mb") is not None:
                    extra = f"{extra}   rss={record['peak_rss_mb']:.0f}MB"
                print(f"[{index:>4}/{len(jobs)}] {job.candidate_id:16} {job.window_label:9} "
                      f"{status:12} {extra}", flush=True)
    elapsed = time.perf_counter() - started

    summary = {
        "suite_id": suite_id,
        "implementation_revision": IMPLEMENTATION_REVISION,
        "out_dir": str(out_dir),
        "windows": window_labels,
        "workers": args.workers,
        "runnable": [candidate.candidate_id for candidate in runnable],
        "blocked": {
            candidate.candidate_id: {"status": status, "reasons": list(reasons)}
            for candidate, status, reasons in blocked
        },
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
          f"(ok={ok} invalid={invalid} error={errored} skipped={skipped})")
    peaks = [row["peak_rss_mb"] for row in results if row.get("peak_rss_mb") is not None]
    if peaks:
        print(f"peak RSS: max {max(peaks):.0f} MB  mean {sum(peaks) / len(peaks):.0f} MB "
              f"at {args.workers} concurrent worker(s)")
    print(f"summary written to {out_dir / 'suite_summary.json'}")
    return 0 if errored == 0 and invalid == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
