#!/usr/bin/env python
"""Read-only feasibility probe for the Laya offline entry-eligibility study.

Task 0a of ``plans/codex_astra_laya_plan.md``.  Two subcommands exist at this
stage:

* ``freeze`` resolves the read-only archive/model metadata, builds the frozen
  protocol manifest, captures a small synthetic OFF golden fixture, and writes
  ``reports/laya_research_v1/protocol_manifest.json`` (read-only).
* ``export`` enforces the protocol roles (train/calibration/gate only), loads
  the frozen local DuckDB archives read-only, and writes features-only
  snapshots plus a separate outcomes file under ``short/laya_work/gate/``.

No model is downloaded, installed, imported, or run.  The final retrospective
holdout interval is deliberately excluded and rejected by both code paths.
All paths are repository-relative; the CLI has no import-time actions.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import date
from pathlib import Path
from typing import Any, Mapping, Sequence

# Mark the process as a backtest and disable dotenv *before* importing the
# native-adjacent modules so no live stream or network client is created.
os.environ.setdefault("IS_BACKTESTING", "true")
os.environ.setdefault("LUMIBOT_DISABLE_DOTENV", "true")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import duckdb  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from strategy_lab.experiment_universes import resolve_universe  # noqa: E402
from strategy_lab.laya_research import contracts  # noqa: E402
from strategy_lab.laya_research import snapshots as snap  # noqa: E402
from strategy_lab.native_experiments import _sql_frames  # noqa: E402

FEATURE_FIELDS = contracts.FEATURE_FIELDS
MANIFEST_VERSION = "laya-research-manifest-v1"
DIGEST_BATCH = 50_000

CODE_HASH_FILES: tuple[str, ...] = (
    "strategy_lab/laya_research/__init__.py",
    "strategy_lab/laya_research/contracts.py",
    "strategy_lab/laya_research/snapshots.py",
    "strategy_lab/laya_research/protocol_v1.json",
    "strategy_lab/laya_research/tests/test_snapshots.py",
    "scripts/laya_feasibility_probe.py",
    "strategy_lab/LAYA_RESEARCH.md",
)


# --- small IO helpers ---------------------------------------------------------


def _resolve(path: str | Path) -> Path:
    """Resolve a repository-relative path (absolute paths are accepted as-is)."""
    candidate = Path(path)
    return candidate if candidate.is_absolute() else (ROOT / candidate)


def _file_bytes(path: Path) -> int:
    return path.stat().st_size


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_readonly_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        os.chmod(path, 0o644)  # a previously frozen manifest is read-only
    text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    path.write_text(text, encoding="utf-8")
    os.chmod(path, 0o444)


def _sanitize_metadata_value(value: str) -> str:
    """Redact absolute filesystem paths before a value enters a tracked file."""
    if value.startswith("/") or "/Users/" in value or "/home/" in value:
        return "<sanitized-absolute-path>"
    return value


def _archive_metadata(db: Path) -> dict[str, str]:
    with duckdb.connect(str(db), read_only=True) as con:
        rows = con.execute("SELECT key, value FROM archive_metadata").fetchall()
    return {str(key): _sanitize_metadata_value(str(value)) for key, value in rows}


def _placeholders(symbols: Sequence[str]) -> str:
    return ", ".join("?" for _ in symbols)


def _bounds(db: Path, symbols: Sequence[str], start: str, end: str) -> list[Any]:
    del db
    return [
        *symbols,
        pd.Timestamp(start, tz="UTC"),
        pd.Timestamp(end, tz="UTC") + pd.Timedelta(days=1) - pd.Timedelta(microseconds=1),
    ]


def _preflight(
    db: Path, table: str, symbols: Sequence[str], start: str, end: str, *, daily: bool
) -> dict[str, Any]:
    """Raw duplicate/session/OHLC checks performed before any deduplication.

    The exporter must never let the canonical keep-last dedup conceal a raw
    duplicate, so these counts fail closed when nonzero.
    """
    ph = _placeholders(symbols)
    if daily:
        key = "CAST(ts AT TIME ZONE 'UTC' AS DATE)"
        key_name = "duplicate_session_keys"
    else:
        key = "ts"
        key_name = "duplicate_stamp_keys"
    where = f"WHERE symbol IN ({ph}) AND ts >= ? AND ts <= ?"
    params = _bounds(db, symbols, start, end)
    with duckdb.connect(str(db), read_only=True) as con:
        duplicates = con.execute(
            f"SELECT count(*) FROM (SELECT symbol, {key} AS k, count(*) c FROM {table} "
            f"{where} GROUP BY 1, 2 HAVING c > 1)",
            params,
        ).fetchone()[0]
        bad_ohlc = con.execute(
            f"SELECT count(*) FROM {table} {where} AND (open IS NULL OR high IS NULL "
            f"OR low IS NULL OR close IS NULL OR volume IS NULL OR open <= 0 OR high <= 0 "
            f"OR low <= 0 OR close <= 0 OR volume < 0 OR high < low OR NOT isfinite(close))",
            params,
        ).fetchone()[0]
    return {key_name: int(duplicates), "bad_ohlc_rows": int(bad_ohlc)}


def _stream_ohlcv_digest(
    db: Path, table: str, symbols: Sequence[str], start: str, end: str
) -> tuple[str, int, str | None, str | None]:
    """SHA-256 over a canonical text rendering of the exact queried OHLCV rows."""
    ph = _placeholders(symbols)
    query = (
        f"SELECT symbol, ts, open, high, low, close, volume FROM {table} "
        f"WHERE symbol IN ({ph}) AND ts >= ? AND ts <= ? ORDER BY symbol, ts"
    )
    params = _bounds(db, symbols, start, end)
    digest = hashlib.sha256()
    count = 0
    first: str | None = None
    last: str | None = None
    with duckdb.connect(str(db), read_only=True) as con:
        cursor = con.execute(query, params)
        while True:
            batch = cursor.fetchmany(DIGEST_BATCH)
            if not batch:
                break
            for symbol, ts, open_, high, low, close, volume in batch:
                stamp = pd.Timestamp(ts).tz_convert("UTC").isoformat()
                digest.update(
                    f"{symbol}|{stamp}|{open_:.6f}|{high:.6f}|{low:.6f}|"
                    f"{close:.6f}|{volume:.3f}\n".encode("ascii")
                )
                count += 1
                if first is None:
                    first = stamp
                last = stamp
    return digest.hexdigest(), count, first, last


def _first_bars(db: Path, symbols: Sequence[str]) -> dict[str, str]:
    ph = _placeholders(symbols)
    with duckdb.connect(str(db), read_only=True) as con:
        rows = con.execute(
            f"SELECT symbol, min(CAST(ts AT TIME ZONE 'UTC' AS DATE)) AS first_day "
            f"FROM bars_daily WHERE symbol IN ({ph}) GROUP BY symbol",
            list(symbols),
        ).fetchall()
    return {str(symbol): str(first_day) for symbol, first_day in rows}


def _identity_guards(hourly_metadata: Mapping[str, str]) -> dict[str, Any]:
    raw = hourly_metadata.get("identity_guards_json", "")
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _effective_starts(
    first_bars: Mapping[str, str],
    guards: Mapping[str, Any],
    symbols: Sequence[str],
) -> dict[str, str]:
    """First valid session per symbol, honoring identity guards where present."""
    effective: dict[str, str] = {}
    for symbol in symbols:
        candidates: list[date] = []
        first = first_bars.get(symbol)
        if first:
            candidates.append(date.fromisoformat(first))
        guard = guards.get(symbol)
        if isinstance(guard, Mapping):
            guarded = str(guard.get("first_regular_session_utc", ""))[:10]
            if guarded:
                candidates.append(date.fromisoformat(guarded))
        if candidates:
            effective[symbol] = max(candidates).isoformat()
    return effective


def _code_hashes() -> dict[str, str]:
    hashes: dict[str, str] = {}
    for relative in CODE_HASH_FILES:
        path = ROOT / relative
        if path.exists():
            hashes[relative] = _sha256_file(path)
    return hashes


# --- synthetic OFF golden fixture --------------------------------------------


def _synthetic_off_golden(protocol: contracts.Protocol) -> dict[str, Any]:
    """Build a tiny deterministic features/decisions fixture (no Laya, no model).

    This is the pre-change canonical OFF artifact referenced by the later native
    parity task.  It is derived only from fixed synthetic numbers so it cannot
    leak vendor data and is reproducible across processes.
    """
    sessions = tuple(pd.bdate_range("2019-01-02", periods=100).date)
    symbols = ("AAA", "BBB")
    daily_raw: dict[str, pd.DataFrame] = {}
    for offset, symbol in enumerate(symbols):
        index = pd.DatetimeIndex([pd.Timestamp(day) for day in sessions])
        steps = np.arange(len(sessions), dtype="float64")
        close = (100.0 + offset) * np.exp(
            0.0009 * steps + 0.001 * np.sin(steps + offset)
        )
        daily_raw[symbol] = pd.DataFrame(
            {
                "open": close * 0.999,
                "high": close * 1.01,
                "low": close * 0.99,
                "close": close,
                "volume": 1000.0 + offset,
            },
            index=index,
        )
    features = {
        symbol: snap.daily_features_for(frame, protocol=protocol)
        for symbol, frame in daily_raw.items()
    }
    stamps = pd.DatetimeIndex(
        [pd.Timestamp(day) + pd.Timedelta(hours=protocol.entry_hour) for day in sessions]
    )
    hourly: dict[str, pd.DataFrame] = {}
    for symbol, frame in daily_raw.items():
        open_ = frame["close"].to_numpy()
        hourly[symbol] = pd.DataFrame(
            {
                "open": open_,
                "high": open_ * 1.005,
                "low": open_ * 0.995,
                "close": open_,
                "volume": 1000.0,
            },
            index=stamps,
        )

    def close_of(day: date) -> pd.Timestamp:
        return pd.Timestamp(day, tz="UTC") + pd.Timedelta(hours=16)

    records = snap.build_decision_records(
        symbols=symbols,
        sessions=sessions,
        daily_features=features,
        hourly_frames=hourly,
        protocol=protocol,
        role=contracts.IntervalRole.TRAIN,
        session_close=close_of,
    )
    return {
        "kind": "synthetic-off-causal-fixture",
        "note": (
            "Deterministic synthetic features/decisions captured in Task 0a. "
            "No model, no Laya, no vendor data."
        ),
        "seed": protocol.seed,
        "n_sessions": len(sessions),
        "n_snapshots": len(records.snapshots),
        "n_outcomes": len(records.outcomes),
        "n_eligible": sum(1 for row in records.eligibility if row.eligible),
        "digest": snap.canonical_snapshots_digest([records]),
        "sample_serialized": [snapshot.text for snapshot in records.snapshots[:2]],
    }


# --- freeze -------------------------------------------------------------------


def cmd_freeze(args: argparse.Namespace) -> int:
    protocol = contracts.load_protocol(args.protocol)
    sources = protocol.raw["source_archives"]
    daily_path = _resolve(sources["daily"])
    hourly_path = _resolve(sources["hourly"])
    for path in (daily_path, hourly_path):
        if not path.exists():
            print(f"data failure: missing archive {path}", file=sys.stderr)
            return 3

    symbols = list(resolve_universe(protocol.universe_keyword))
    start = protocol.data_query_start.isoformat()
    end = protocol.data_query_end.isoformat()

    daily_preflight = _preflight(daily_path, "bars_daily", symbols, start, end, daily=True)
    hourly_preflight = _preflight(hourly_path, "bars_hourly", symbols, start, end, daily=False)
    if daily_preflight["duplicate_session_keys"] or hourly_preflight["duplicate_stamp_keys"]:
        print("data failure: raw duplicate keys detected; refusing to freeze", file=sys.stderr)
        return 3
    if daily_preflight["bad_ohlc_rows"] or hourly_preflight["bad_ohlc_rows"]:
        print("data failure: raw OHLC violations detected; refusing to freeze", file=sys.stderr)
        return 3

    daily_digest, daily_rows, daily_min, daily_max = _stream_ohlcv_digest(
        daily_path, "bars_daily", symbols, start, end
    )
    hourly_digest, hourly_rows, hourly_min, hourly_max = _stream_ohlcv_digest(
        hourly_path, "bars_hourly", symbols, start, end
    )

    sessions_all = snap.calendar_sessions(start, end, calendar=protocol.calendar_requested)
    expected = {
        role.value: sum(1 for day in sessions_all if protocol.interval(role).contains(day))
        for role in contracts.IntervalRole
    }

    first_bars = _first_bars(daily_path, symbols)
    guards = _identity_guards(hourly_metadata=_archive_metadata(hourly_path))
    effective_starts = _effective_starts(first_bars, guards, symbols)
    train_start = protocol.interval(contracts.IntervalRole.TRAIN).start
    inception_exclusions = {
        symbol: start
        for symbol, start in effective_starts.items()
        if date.fromisoformat(start) > train_start
    }

    golden = _synthetic_off_golden(protocol)
    golden_path = ROOT / "short" / "laya_work" / "gate" / "golden_off_synthetic.json"
    golden_path.parent.mkdir(parents=True, exist_ok=True)
    golden_path.write_text(json.dumps(golden, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    protocol_mapping = protocol.to_mapping()
    manifest = {
        "manifest_version": MANIFEST_VERSION,
        "task": "0a",
        "created_by": "scripts/laya_feasibility_probe.py freeze",
        "protocol": protocol_mapping,
        "protocol_sha256": hashlib.sha256(
            json.dumps(protocol_mapping, sort_keys=True).encode("utf-8")
        ).hexdigest(),
        "source_archives": {
            "daily": {
                "path": sources["daily"],
                "bytes": _file_bytes(daily_path),
                "sha256": _sha256_file(daily_path),
                "metadata": _archive_metadata(daily_path),
            },
            "hourly": {
                "path": sources["hourly"],
                "bytes": _file_bytes(hourly_path),
                "sha256": _sha256_file(hourly_path),
                "metadata": _archive_metadata(hourly_path),
            },
        },
        "query_bounds": {
            "start": start,
            "end_inclusive": end,
            "final_holdout_start": protocol.interval(contracts.IntervalRole.FINAL).start.isoformat(),
            "symbols": len(symbols),
        },
        "digests": {
            "daily_ohlcv_sha256": daily_digest,
            "daily_rows": daily_rows,
            "daily_first": daily_min,
            "daily_last": daily_max,
            "hourly_ohlcv_sha256": hourly_digest,
            "hourly_rows": hourly_rows,
            "hourly_first": hourly_min,
            "hourly_last": hourly_max,
        },
        "preflight": {"daily": daily_preflight, "hourly": hourly_preflight},
        "expected_sessions": {
            "source": (
                f"exchange_calendars {protocol.calendar_resolved} {protocol.calendar_version} "
                f"(requested {protocol.calendar_requested})"
            ),
            "query_total": len(sessions_all),
            "train": expected["train"],
            "calibration": expected["calibration"],
            "gate": expected["gate"],
        },
        "inception_exclusions": inception_exclusions,
        "symbol_effective_start": effective_starts,
        "identity_guards": guards,
        "code_hashes": _code_hashes(),
        "golden_fixture": {
            "path": "short/laya_work/gate/golden_off_synthetic.json",
            "sha256": _sha256_file(golden_path),
            "digest": golden["digest"],
            "n_snapshots": golden["n_snapshots"],
            "n_outcomes": golden["n_outcomes"],
            "n_eligible": golden["n_eligible"],
        },
        "read_only": True,
        "note": (
            "Read-only Task 0a manifest. The final holdout interval is excluded; no model, "
            "inference, calibration, or fit is performed."
        ),
    }

    out_path = _resolve(args.out)
    _write_readonly_json(out_path, manifest)
    print(f"freeze: wrote {out_path.relative_to(ROOT)} (read-only)")
    print(
        f"  daily rows={daily_rows} hourly rows={hourly_rows} "
        f"sessions={len(sessions_all)} train={expected['train']} "
        f"calibration={expected['calibration']} gate={expected['gate']}"
    )
    print(f"  golden digest={golden['digest']} snapshots={golden['n_snapshots']}")
    if inception_exclusions:
        print(f"  inception exclusions: {', '.join(sorted(inception_exclusions))}")
    return 0


# --- export -------------------------------------------------------------------


def _snapshots_frame(records: Sequence[snap.GateRecords]) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for record in records:
        for snapshot in record.snapshots:
            row: dict[str, Any] = {
                "symbol": snapshot.symbol,
                "source_session": pd.Timestamp(snapshot.source_session),
                "decision_session": pd.Timestamp(snapshot.decision_session),
                "source_available_at": pd.Timestamp(snapshot.source_available_at).tz_convert("UTC"),
            }
            for name, value in zip(FEATURE_FIELDS, snapshot.features):
                row[name] = float(value)
            rows.append(row)
    columns = ["symbol", "source_session", "decision_session", "source_available_at", *FEATURE_FIELDS]
    return pd.DataFrame(rows, columns=columns)


def _outcomes_frame(records: Sequence[snap.GateRecords]) -> pd.DataFrame:
    columns = [
        "symbol",
        "source_session",
        "decision_session",
        "entry_session",
        "exit_session",
        "entry_price",
        "exit_price",
        "r_net",
        "y",
        "features_complete",
        "valid",
        "reason",
    ]
    rows: list[dict[str, Any]] = []
    for record in records:
        for outcome in record.outcomes:
            rows.append(
                {
                    "symbol": outcome.symbol,
                    "source_session": pd.Timestamp(outcome.source_session) if outcome.source_session else pd.NaT,
                    "decision_session": pd.Timestamp(outcome.decision_session),
                    "entry_session": pd.Timestamp(outcome.entry_session) if outcome.entry_session else pd.NaT,
                    "exit_session": pd.Timestamp(outcome.exit_session) if outcome.exit_session else pd.NaT,
                    "entry_price": outcome.entry_price,
                    "exit_price": outcome.exit_price,
                    "r_net": outcome.r_net,
                    "y": outcome.y,
                    "features_complete": outcome.features_complete,
                    "valid": outcome.valid,
                    "reason": outcome.reason,
                }
            )
    return pd.DataFrame(rows, columns=columns)


def _mask_frame(records: Sequence[snap.GateRecords]) -> pd.DataFrame:
    columns = ["symbol", "decision_session", "role", "features_complete", "target_valid", "eligible"]
    rows: list[dict[str, Any]] = []
    for record in records:
        for row in record.eligibility:
            rows.append(
                {
                    "symbol": row.symbol,
                    "decision_session": pd.Timestamp(row.decision_session),
                    "role": row.role.value,
                    "features_complete": row.features_complete,
                    "target_valid": row.target_valid,
                    "eligible": row.eligible,
                }
            )
    return pd.DataFrame(rows, columns=columns)


def cmd_export(args: argparse.Namespace) -> int:
    manifest_path = _resolve(args.protocol)
    if not manifest_path.exists():
        print(f"protocol manifest not found: {manifest_path}", file=sys.stderr)
        return 3
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    protocol = contracts.Protocol.from_mapping(manifest["protocol"])
    try:
        roles = contracts.assert_export_roles(args.roles)
    except contracts.ProtocolError as exc:
        print(f"role rejection: {exc}", file=sys.stderr)
        return 2

    sources = protocol.raw["source_archives"]
    for key in ("daily", "hourly"):
        path = _resolve(sources[key])
        record = manifest["source_archives"][key]
        if not path.exists():
            print(f"data failure: missing archive {path}", file=sys.stderr)
            return 3
        if _file_bytes(path) != int(record["bytes"]) or _sha256_file(path) != record["sha256"]:
            print(f"data failure: archive {sources[key]} does not match the frozen manifest", file=sys.stderr)
            return 3

    symbols = list(resolve_universe(protocol.universe_keyword))
    start = protocol.data_query_start.isoformat()
    end = protocol.data_query_end.isoformat()
    daily_raw = _sql_frames(_resolve(sources["daily"]), "bars_daily", symbols, start, end, hourly=False)
    hourly_raw = _sql_frames(_resolve(sources["hourly"]), "bars_hourly", symbols, start, end, hourly=True)
    features = {
        symbol: snap.daily_features_for(frame, protocol=protocol)
        for symbol, frame in daily_raw.items()
    }
    ordered = [symbol for symbol in symbols if symbol in features]
    sessions = snap.calendar_sessions(start, end, calendar=protocol.calendar_requested)
    symbol_start = {
        symbol: date.fromisoformat(value)
        for symbol, value in manifest.get("symbol_effective_start", {}).items()
    }

    def close_of(day: date) -> Any:
        return snap.session_close_utc(day, calendar=protocol.calendar_requested)

    records = [
        snap.build_decision_records(
            symbols=ordered,
            sessions=sessions,
            daily_features=features,
            hourly_frames=hourly_raw,
            protocol=protocol,
            role=role,
            session_close=close_of,
            symbol_start=symbol_start,
        )
        for role in roles
    ]

    final_start = protocol.interval(contracts.IntervalRole.FINAL).start
    decisions = [snapshot.decision_session for record in records for snapshot in record.snapshots]
    if decisions and max(decisions) >= final_start:
        print("internal error: final holdout decision leaked into the export", file=sys.stderr)
        return 4

    out_dir = _resolve(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    snapshots_frame = _snapshots_frame(records)
    outcomes_frame = _outcomes_frame(records)
    mask_frame = _mask_frame(records)
    snapshots_frame.to_parquet(out_dir / "snapshots.parquet", index=False)
    outcomes_frame.to_parquet(out_dir / "outcomes.parquet", index=False)
    mask_frame.to_parquet(out_dir / "eligibility_mask.parquet", index=False)

    summary = contracts.summarize_eligibility([row for record in records for row in record.eligibility])
    summary_path = out_dir / "eligibility_summary.json"
    summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    print(f"export: wrote {out_dir.relative_to(ROOT)}")
    print(f"  snapshots.parquet rows={len(snapshots_frame)} cols={len(snapshots_frame.columns)}")
    print(f"  outcomes.parquet rows={len(outcomes_frame)} cols={len(outcomes_frame.columns)}")
    for role in roles:
        counts = summary.get(role.value, {})
        print(
            f"  {role.value}: eligible={counts.get('eligible', 0)} "
            f"features_complete={counts.get('features_complete', 0)} attempted={counts.get('attempted', 0)}"
        )
    max_decision = max(decisions).isoformat() if decisions else "none"
    print(f"  max decision_session={max_decision} (final holdout excluded)")
    return 0


# --- parser -------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="laya_feasibility_probe.py",
        description="Task 0a freeze/export for the Laya offline entry-eligibility study.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    freeze = sub.add_parser("freeze", help="Freeze the read-only protocol manifest.")
    freeze.add_argument("--protocol", required=True, help="Path to protocol_v1.json")
    freeze.add_argument("--out", required=True, help="Path to the manifest JSON to write")
    freeze.set_defaults(func=cmd_freeze)

    export = sub.add_parser("export", help="Export snapshots/outcomes for allowed roles only.")
    export.add_argument("--protocol", required=True, help="Path to protocol_manifest.json")
    export.add_argument(
        "--roles",
        nargs="+",
        required=True,
        help="Decision-session roles to export: train calibration gate (final is forbidden).",
    )
    export.add_argument("--out", required=True, help="Output directory under short/laya_work/gate")
    export.set_defaults(func=cmd_export)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if "--include-final" in arguments:
        print("role rejection: --include-final is not available in Task 0a", file=sys.stderr)
        return 2
    parser = build_parser()
    args = parser.parse_args(arguments)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
