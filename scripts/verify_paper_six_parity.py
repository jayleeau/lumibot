#!/usr/bin/env python
"""Offline verifier for paper6 (HTS) persisted decision sessions.

Default mode reads one or more persisted session artifacts and performs
pure-policy and lifecycle verification only.  It is strictly offline: it never
contacts a broker, loads credentials, or downloads data.

Exit codes:
    0  complete PASS
    1  a decision or lifecycle mismatch was found
    2  invalid or unverifiable input

Usage::

    python scripts/verify_paper_six_parity.py --live-session <session.json>
    python scripts/verify_paper_six_parity.py --live-session <session.json> \
        --run-native --native-out <temporary-output-directory> --compare-pnl
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Mapping

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from strategy_lab.hts_audit import (  # noqa: E402
    AtomicJsonStore,
    AuditError,
    validate_decision_snapshot,
)
from strategy_lab.hts_parity import (  # noqa: E402
    ParityMismatch,
    ParityTolerances,
    UnverifiableError,
    assert_decision_parity,
    compare_pnl as compare_pnl_curves,
    validate_lifecycle,
)

EXIT_PASS = 0
EXIT_MISMATCH = 1
EXIT_UNVERIFIABLE = 2

# These are the declared parents in the discovery builders, not parents inferred
# from parameter similarity (W0018, for example, still belongs to H100).
_PAPER6_PARENT_BY_CATALOG_ID = {
    "W0006": "H100",
    "W0007": "H100",
    "W0018": "H100",
    "S158": "H022",
    "S159": "H022",
}


def _normalize_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Accept a flat session payload or its native ``strategy_events`` nesting."""
    if not isinstance(payload, Mapping):
        return {}
    if "decisions" in payload or "lifecycle_trace" in payload:
        return dict(payload)
    nested = payload.get("strategy_events")
    if isinstance(nested, Mapping):
        merged = dict(nested)
        for key in ("session", "window", "resolved_parameters"):
            if payload.get(key) is not None and merged.get(key) is None:
                merged[key] = payload[key]
        return merged
    return dict(payload)


def _load_session(path: Path) -> dict[str, Any]:
    return _normalize_payload(AtomicJsonStore(path).read())


def _decision_snapshots(payload: Mapping[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    """Return (valid full snapshots, per-decision validation failures).

    A stub or malformed decision is always reported; it is never skipped.
    """
    valid: list[dict[str, Any]] = []
    invalid: list[str] = []
    for index, decision in enumerate(payload.get("decisions") or []):
        try:
            validate_decision_snapshot(decision)
        except AuditError as error:
            decision_id = decision.get("decision_id") if isinstance(decision, Mapping) else None
            invalid.append(f"decisions.{index} ({decision_id}): {error}")
            continue
        if str(decision.get("outcome") or "executed") == "executed":
            valid.append(dict(decision))
    return valid, invalid


def _verify_snapshots(snapshots: list[dict[str, Any]],
                      tolerances: ParityTolerances) -> tuple[list[str], list[str]]:
    mismatches: list[str] = []
    unverifiable: list[str] = []
    for snapshot in snapshots:
        decision_id = (snapshot.get("identity") or {}).get("decision_id") or snapshot.get("decision_id") or "?"
        try:
            assert_decision_parity(snapshot, tolerances)
        except ParityMismatch as error:
            mismatches.append(f"{decision_id}: {error.field}: {error.message}")
        except UnverifiableError as error:
            unverifiable.append(f"{decision_id}: {error}")
    return mismatches, unverifiable


def _classify_lifecycle(issues: list[dict[str, Any]]) -> tuple[list[str], list[str]]:
    """Split lifecycle defects into semantic mismatches and structural gaps."""
    mismatches: list[str] = []
    unverifiable: list[str] = []
    for issue in issues:
        message = str(issue.get("message", ""))
        text = f"{issue.get('field')}: {message}"
        if "missing" in message or "strictly increase" in message or "nondecreasing" in message:
            unverifiable.append(text)
        else:
            mismatches.append(text)
    return mismatches, unverifiable


def _resolve_native_parent_candidate_id(payload: Mapping[str, Any]) -> str:
    """Resolve the persisted paper candidate to its declared v2 parent."""
    identity = payload.get("identity")
    if not isinstance(identity, Mapping):
        raise ValueError("session artifact has no strategy identity for native lineage")
    raw_catalog_id = identity.get("catalog_id")
    if not isinstance(raw_catalog_id, str) or not raw_catalog_id.strip():
        raise ValueError("session identity has no catalog_id for native lineage")
    catalog_id = raw_catalog_id.strip().upper()
    parent_candidate_id = _PAPER6_PARENT_BY_CATALOG_ID.get(catalog_id)
    if parent_candidate_id is None:
        raise ValueError(
            f"session catalog_id {raw_catalog_id!r} has no declared paper6 native parent"
        )
    return parent_candidate_id


def _run_native(payload: Mapping[str, Any], native_out: Path) -> tuple[dict[str, float] | None, str | None]:
    """Run the same-window native backtest and return its real session curve."""
    resolved = payload.get("resolved_parameters")
    window = payload.get("window")
    if not isinstance(resolved, Mapping) or not resolved or not isinstance(window, Mapping):
        return None, "session artifact has no resolved_parameters/window; cannot run native"
    if not window.get("start") or not window.get("end"):
        return None, "session artifact has no complete native window bounds"
    try:
        from strategy_lab.experiment_config import CandidateSpec, KIND_HTS_V2
        from strategy_lab.experiment_registry import get_registry
        from strategy_lab.native_experiments import (
            ExperimentWindow,
            load_session_end_equity,
            run_candidate,
        )
        from strategy_lab.hts_variants import HTS_BASELINE
    except Exception as error:  # pragma: no cover - import guard
        return None, f"native modules unavailable: {type(error).__name__}"

    try:
        parent_candidate_id = _resolve_native_parent_candidate_id(payload)
        # The persisted catalog ID selects the lineage declared by the real
        # search builders: W candidates use H100 and S candidates use H022.
        get_registry().get(parent_candidate_id)
        candidate = CandidateSpec(
            candidate_id="paper6_parity_native",
            name="paper6 parity native",
            slug="paper6-parity-native",
            kind=KIND_HTS_V2,
            family_id="paper6",
            rule="same-window native replay of a persisted paper6 decision session",
            hypothesis="native and live pure-policy decisions agree within tolerance",
            parameters=tuple(sorted(resolved.items())),
            overrides=(),
            tags=("parity",),
            parent_candidate_id=parent_candidate_id,
        )
        exp_window = ExperimentWindow(
            label="paper6_parity",
            start=str(window["start"]),
            end=str(window["end"]),
        )
        run = run_candidate(candidate, exp_window, native_out, control_baseline=HTS_BASELINE)
        stats_path = Path(run.out_dir) / "run_stats.csv"
        curve = load_session_end_equity(stats_path)
    except Exception as error:
        return None, f"native replay failed closed: {type(error).__name__}: {error}"
    if not curve:
        return None, "native replay produced no session-end equity observations"
    return curve, None


def _live_equity(payload: Mapping[str, Any]) -> dict[str, float]:
    curve: dict[str, float] = {}
    for event in payload.get("session_end_events") or []:
        if isinstance(event, Mapping) and event.get("session") is not None:
            curve[str(event["session"])] = float(event.get("equity", 0.0))
    return curve


def verify_sessions(
    paths: list[str],
    tolerances: ParityTolerances | None = None,
    *,
    run_native: bool = False,
    native_out: Path | None = None,
    compare_pnl: bool = False,
) -> dict[str, Any]:
    """Verify a set of persisted session artifacts.  Never touches the network."""
    tolerances = tolerances or ParityTolerances()

    all_mismatches: list[str] = []
    all_unverifiable: list[str] = []
    lifecycle_gaps: list[str] = []
    any_snapshot = False
    loaded_payloads: list[tuple[Path, dict[str, Any]]] = []

    for raw_path in paths:
        path = Path(raw_path)
        try:
            payload = _load_session(path)
        except Exception as error:
            all_unverifiable.append(f"{path.name}: {type(error).__name__}: {error}")
            continue
        loaded_payloads.append((path, payload))

        snapshots, invalid = _decision_snapshots(payload)
        any_snapshot = any_snapshot or bool(snapshots)
        all_unverifiable.extend(f"{path.name}: {item}" for item in invalid)

        mismatches, unverifiable = _verify_snapshots(snapshots, tolerances)
        all_mismatches.extend(f"{path.name}: {item}" for item in mismatches)
        all_unverifiable.extend(f"{path.name}: {item}" for item in unverifiable)

        lifecycle_mismatches, lifecycle_unverifiable = _classify_lifecycle(
            validate_lifecycle(payload)
        )
        all_mismatches.extend(f"{path.name}: {item}" for item in lifecycle_mismatches)
        lifecycle_gaps.extend(f"{path.name}: {item}" for item in lifecycle_unverifiable)

    if run_native:
        if native_out is None:
            all_unverifiable.append("--run-native requires --native-out")
        else:
            grouped: dict[str, list[tuple[Path, dict[str, Any]]]] = {}
            for path, payload in loaded_payloads:
                identity = payload.get("identity")
                if not isinstance(identity, Mapping) or not identity.get("strategy_name"):
                    all_unverifiable.append(f"{path.name}: missing strategy identity for native grouping")
                    continue
                key = json.dumps(dict(identity), sort_keys=True, default=str)
                grouped.setdefault(key, []).append((path, payload))

            for entries in grouped.values():
                first_path, first_payload = entries[0]
                strategy_name = str((first_payload.get("identity") or {}).get("strategy_name"))
                sessions: set[str] = set()
                live_curve: dict[str, float] = {}
                conflict = False
                for path, payload in entries:
                    if payload.get("session") is not None:
                        sessions.add(str(payload["session"]))
                    try:
                        payload_curve = _live_equity(payload)
                    except Exception as curve_error:
                        all_unverifiable.append(
                            f"{path.name}: live equity is invalid: "
                            f"{type(curve_error).__name__}: {curve_error}"
                        )
                        conflict = True
                        continue
                    for session, equity in payload_curve.items():
                        sessions.add(session)
                        if session in live_curve and live_curve[session] != equity:
                            all_unverifiable.append(
                                f"{strategy_name}: conflicting live equity for session {session}"
                            )
                            conflict = True
                        live_curve[session] = equity
                if conflict or not sessions:
                    if not sessions:
                        all_unverifiable.append(
                            f"{strategy_name}: no sessions available to derive native window"
                        )
                    continue

                aggregate = dict(first_payload)
                aggregate["window"] = {"start": min(sessions), "end": max(sessions)}
                aggregate["session_end_events"] = [
                    {"event": "session_end", "session": session, "equity": live_curve[session]}
                    for session in sorted(live_curve)
                ]
                strategy_out = native_out / strategy_name if len(grouped) > 1 else native_out
                try:
                    native_curve, error = _run_native(aggregate, strategy_out)
                except Exception as unexpected:
                    all_unverifiable.append(
                        f"{strategy_name}: native replay failed closed: "
                        f"{type(unexpected).__name__}: {unexpected}"
                    )
                    continue
                if error:
                    all_unverifiable.append(f"{strategy_name}: {error}")
                    continue
                if compare_pnl:
                    if not live_curve:
                        all_unverifiable.append(
                            f"{strategy_name}: no session_end equity for PnL comparison"
                        )
                        continue
                    try:
                        result = compare_pnl_curves(
                            live_curve, native_curve or {}, tolerances=tolerances
                        )
                    except Exception as compare_error:
                        all_unverifiable.append(
                            f"{strategy_name}: pnl comparison failed closed: "
                            f"{type(compare_error).__name__}: {compare_error}"
                        )
                        continue
                    if result["status"] == "mismatch":
                        all_mismatches.append(
                            f"{strategy_name}: pnl first divergence "
                            f"{result['first_divergent_session']}"
                        )
                    elif result["status"] == "unverifiable":
                        all_unverifiable.append(
                            f"{strategy_name}: pnl comparison unverifiable"
                        )

    report: dict[str, Any] = {
        "status": "pass",
        "mismatches": all_mismatches,
        "unverifiable": all_unverifiable,
        "lifecycle_gaps": lifecycle_gaps,
        "decision_snapshots_found": any_snapshot,
    }
    # Structural evidence gaps take precedence over semantic mismatches: a
    # malformed payload cannot be promoted to a meaningful mismatch result.
    if all_unverifiable or lifecycle_gaps or not any_snapshot:
        report["status"] = "unverifiable"
    elif all_mismatches:
        report["status"] = "mismatch"
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live-session", action="append", default=[], required=True,
                        help="persisted paper6 session artifact (repeatable)")
    parser.add_argument("--run-native", action="store_true")
    parser.add_argument("--native-out", type=Path, default=None)
    parser.add_argument("--compare-pnl", action="store_true")
    parser.add_argument("--pnl-abs-return", type=float, default=1e-4)
    parser.add_argument("--pnl-rel", type=float, default=1e-3)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    tolerances = ParityTolerances(abs_return_tol=args.pnl_abs_return, rel_tol=args.pnl_rel)

    # Validate the option contract before doing any work.
    if args.compare_pnl and not args.run_native:
        reason = "--compare-pnl requires --run-native"
        if args.json:
            print(json.dumps({"status": "unverifiable", "reason": reason}, indent=2))
        else:
            print(f"status: unverifiable\n  reason: {reason}")
        return EXIT_UNVERIFIABLE
    if args.run_native and args.native_out is None:
        reason = "--run-native requires --native-out"
        if args.json:
            print(json.dumps({"status": "unverifiable", "reason": reason}, indent=2))
        else:
            print(f"status: unverifiable\n  reason: {reason}")
        return EXIT_UNVERIFIABLE

    report = verify_sessions(
        list(args.live_session),
        tolerances,
        run_native=args.run_native,
        native_out=args.native_out,
        compare_pnl=args.compare_pnl,
    )

    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        print(f"status: {report['status']}")
        for label in ("mismatches", "unverifiable", "lifecycle_gaps"):
            for item in report[label]:
                print(f"  {label}: {item}")

    if report["status"] == "mismatch":
        return EXIT_MISMATCH
    if report["status"] == "unverifiable":
        return EXIT_UNVERIFIABLE
    return EXIT_PASS


if __name__ == "__main__":
    raise SystemExit(main())
