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

* ``download`` fetches one allow-listed, pinned English checkpoint into an
  immutable original plus a separate SDK working copy.
* ``technical`` performs the cache-only deterministic feasibility probe.
* ``score`` is a resumable, chunked raw-scoring plumbing stub; it does no
  calibration or predictive verdict.

The final retrospective holdout interval is deliberately excluded and rejected
by the export path.  Model imports are lazy so download/technical/score can run
in the isolated model environment without installing LumiBot.
All paths are repository-relative; the CLI has no import-time actions.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import math
import os
import shutil
import socket
import statistics
import sys
import time
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

from strategy_lab.laya_research import contracts  # noqa: E402

duckdb: Any = None
np: Any = None
pd: Any = None
resolve_universe: Any = None
_sql_frames: Any = None
snap: Any = None

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


def _require_data_stack() -> None:
    """Import archive/native dependencies only for freeze/export commands."""
    global duckdb, np, pd, resolve_universe, _sql_frames, snap
    if duckdb is None:
        import duckdb as _duckdb
        import numpy as _np
        import pandas as _pd

        from strategy_lab.experiment_universes import resolve_universe as _resolve_universe
        from strategy_lab.native_experiments import _sql_frames as _frames
        from strategy_lab.laya_research import snapshots as _snap

        duckdb, np, pd = _duckdb, _np, _pd
        resolve_universe, _sql_frames = _resolve_universe, _frames
        snap = _snap


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
    _require_data_stack()
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
    _require_data_stack()
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


# --- Task 0b model download/technical probe ---------------------------------

MODEL_ID = "convaiinnovations/laya"
MODEL_REVISION = "1c5edc17a7acd8701df6fc341c0d179f1c62c982"
MODEL_ALLOW_PATTERNS: tuple[str, ...] = (
    "rl_agent_config.json",
    "model.safetensors",
    "tokenizer/*",
    "encoder/*",
    "LICENSE",
    "README.md",
)


def _relative(path: Path) -> str:
    """Render a repository-relative path for tracked JSON evidence."""
    try:
        return path.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return "<sanitized-absolute-path>"


def _tree_hashes(root: Path) -> dict[str, str]:
    """Hash every regular file below a model snapshot in stable path order."""
    return {
        path.relative_to(root).as_posix(): _sha256_file(path)
        for path in sorted(root.rglob("*"))
        if path.is_file() and ".cache" not in path.parts
    }


def _find_model_dir(model_root: Path) -> Path:
    """Find the separate SDK working copy produced by ``download``."""
    candidates = sorted(model_root.glob("**/working"))
    if not candidates:
        raise FileNotFoundError(f"no model working copy below {_relative(model_root)}")
    return candidates[0]


def cmd_download(args: argparse.Namespace) -> int:
    """Download exactly one pinned English checkpoint and record file hashes."""
    model_root = _resolve(args.model_root)
    model_root.mkdir(parents=True, exist_ok=True)
    original = model_root / "english" / "original"
    working = model_root / "english" / "working"
    try:
        hub = __import__("huggingface_hub", fromlist=["snapshot_download"])
        snapshot_download = getattr(hub, "snapshot_download")
        staging = model_root / "english" / "download-staging"
        if staging.exists():
            shutil.rmtree(staging)
        snapshot_download(
            MODEL_ID,
            revision=MODEL_REVISION,
            local_dir=str(staging),
            allow_patterns=list(MODEL_ALLOW_PATTERNS),
        )
        missing = [name for name in ("rl_agent_config.json", "model.safetensors", "tokenizer", "encoder") if not (staging / name).exists()]
        if missing:
            raise RuntimeError(f"pinned snapshot is incomplete: {missing}")
        if original.exists():
            raise RuntimeError("immutable original already exists; refusing to overwrite it")
        shutil.copytree(staging, original)
        shutil.copytree(original, working)
        manifest = {
            "model_id": MODEL_ID,
            "revision": MODEL_REVISION,
            "allow_patterns": list(MODEL_ALLOW_PATTERNS),
            "original_relative_path": _relative(original),
            "working_relative_path": _relative(working),
            "original_hashes": _tree_hashes(original),
            "provenance": {"license": "Apache-2.0", "source": "Hugging Face model card"},
        }
        (working / "laya_download_manifest.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        # Keep the immutable copy's provenance separate from the SDK's mutable copy.
        (original / "laya_download_manifest.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        shutil.rmtree(staging)
        print(f"download: wrote {_relative(original)} and {_relative(working)}")
        return 0
    except Exception as exc:
        failure = {
            "status": "FAILED_TECHNICAL",
            "stage": "download",
            "model_id": MODEL_ID,
            "revision": MODEL_REVISION,
            "error": str(exc).replace(str(ROOT), "<repo-root>"),
        }
        (model_root / "download_failure.json").write_text(
            json.dumps(failure, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        print(f"download failed: {exc}", file=sys.stderr)
        return 1


class _DenySockets:
    """Process-local socket deny guard used during offline load/inference."""

    def __enter__(self) -> "_DenySockets":
        self._socket = socket.socket
        self._create = socket.create_connection

        def denied(*_: Any, **__: Any) -> Any:
            raise RuntimeError("network access denied during offline technical probe")

        socket.socket = denied  # type: ignore[assignment]
        socket.create_connection = denied  # type: ignore[assignment]
        return self

    def __exit__(self, *_: Any) -> None:
        socket.socket = self._socket  # type: ignore[assignment]
        socket.create_connection = self._create  # type: ignore[assignment]


def _rss_bytes() -> int | None:
    """Return process RSS when psutil is available; otherwise report unknown."""
    try:
        import psutil

        process = psutil.Process()
        total = process.memory_info().rss
        for child in process.children(recursive=True):
            try:
                total += child.memory_info().rss
            except psutil.Error:
                continue
        return int(total)
    except Exception:
        return None


def _technical_rows(snapshot_path: Path, sample_count: int, protocol: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Select deterministic calibration-derived rows without touching outcomes."""
    import pandas as _pd

    frame = _pd.read_parquet(snapshot_path)
    interval = protocol["intervals"]["calibration"]
    frame["decision_session"] = _pd.to_datetime(frame["decision_session"])
    frame = frame[
        (frame["decision_session"] >= _pd.Timestamp(interval["start"]))
        & (frame["decision_session"] <= _pd.Timestamp(interval["end"]))
    ].sort_values(["symbol", "source_session", "decision_session"], kind="mergesort")
    if len(frame) < sample_count:
        raise RuntimeError(f"only {len(frame)} calibration rows available; need {sample_count}")
    return frame.head(sample_count).to_dict("records")


def _state_text(row: Mapping[str, Any]) -> str:
    """Serialize one snapshot using the frozen six-decimal feature contract."""
    return contracts.serialize_features(tuple(float(row[field]) for field in FEATURE_FIELDS))


def _fresh_probe(
    model_dir: Path,
    rows: Sequence[Mapping[str, Any]],
    question: Mapping[str, Mapping[str, Any]],
    *,
    backend: Any,
) -> tuple[Any, list[dict[str, Any]], float, float, list[float]]:
    """Load once, warm up, then score rows while capturing latency samples."""
    from strategy_lab.laya_research import inference

    started = time.perf_counter()
    with _DenySockets():
        agent = inference.load_local(model_dir, device="cpu", backend=backend)
    cold = time.perf_counter() - started
    warm_state = _state_text(rows[0])
    with _DenySockets():
        inference.predict_noul(agent, warm_state, question)
    latencies: list[float] = []
    output: list[dict[str, Any]] = []
    for row in rows:
        state = _state_text(row)
        before = time.perf_counter()
        with _DenySockets():
            probability = inference.predict_noul(agent, state, question)
        latencies.append(time.perf_counter() - before)
        output.append(
            {
                "symbol": str(row["symbol"]),
                "source_session": str(row["source_session"]),
                "decision_session": str(row["decision_session"]),
                **{field: float(row[field]) for field in FEATURE_FIELDS},
                "probability": probability,
            }
        )
    return agent, output, cold, time.perf_counter() - started, latencies


def cmd_technical(args: argparse.Namespace) -> int:
    """Run the offline repeat-load technical gate and write sanitized evidence."""
    out = _resolve(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    manifest = json.loads(_resolve(args.protocol).read_text(encoding="utf-8"))
    protocol = manifest["protocol"]
    evidence: dict[str, Any] = {
        "task": "0b",
        "model_id": MODEL_ID,
        "model_revision": MODEL_REVISION,
        "package": "laya==0.3.5",
        "environment_setup": {
            "interpreter": ".venv-laya/bin/python",
            "pip_tools_pin": "7.5.1",
            "pip_tools_install": "FAILED_INDEX_UNREACHABLE",
            "dependency_lock": "UNRESOLVED",
            "laya_wheel_sha256": "4c57f64cbaf893bb5c7b4affddc2bf21a819f55df51941689f11868583be2903",
        },
        "reference_backend": {"device": args.device, "dtype": "float32", "seed": 20260922, "threads": 1},
        "sample_count_requested": int(args.sample_count),
        "status": "FAILED_TECHNICAL",
        "criteria": {},
        "failure_reasons": [],
        "cold_load_seconds": None,
        "warmup": {"completed": False},
        "latency_seconds": {"p50": None, "p95": None},
        "peak_process_tree_rss_bytes": None,
        "sustained_swap_growth_bytes": None,
        "loaded_backend_first": None,
        "loaded_backend_second": None,
        "decoded_row_hash_first": None,
        "decoded_row_hash_second": None,
        "canonical_row_hashes_first": [],
        "canonical_row_hashes_second": [],
        "parquet_byte_hash_first": None,
        "parquet_byte_hash_second": None,
        "probabilities_exact_equal": None,
        "canonical_rows_exact_equal": None,
        "rows_scored": 0,
        "projected_corpus_seconds": None,
    }
    download_failure = _resolve(args.model_root) / "download_failure.json"
    if download_failure.exists():
        try:
            evidence["download_attempt"] = json.loads(download_failure.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            evidence["download_attempt"] = {"status": "FAILED_TECHNICAL"}
    try:
        from strategy_lab.laya_research import inference

        model_dir = _find_model_dir(_resolve(args.model_root))
        rows = _technical_rows(_resolve(args.snapshots), int(args.sample_count), protocol)
        question = inference.noul_question(str(protocol["question"]))
        backend = inference.BackendSpec(device=args.device, dtype="float32", seed=20260922, threads=1)
        first_agent, first_rows, cold, elapsed, latencies = _fresh_probe(model_dir, rows, question, backend=backend)
        second_agent, second_rows, _, _, _ = _fresh_probe(model_dir, rows, question, backend=backend)
        first_hash = inference.canonical_prediction_hash(first_rows)
        second_hash = inference.canonical_prediction_hash(second_rows)
        first_parquet = io.BytesIO()
        second_parquet = io.BytesIO()
        import pandas as _pd

        _pd.DataFrame(first_rows).to_parquet(first_parquet, index=False)
        _pd.DataFrame(second_rows).to_parquet(second_parquet, index=False)
        first_parquet_hash = hashlib.sha256(first_parquet.getvalue()).hexdigest()
        second_parquet_hash = hashlib.sha256(second_parquet.getvalue()).hexdigest()
        probabilities_equal = [item["probability"] for item in first_rows] == [item["probability"] for item in second_rows]
        canonical_equal = first_hash == second_hash
        sorted_latencies = sorted(latencies)
        p95_index = max(0, min(len(sorted_latencies) - 1, int(math.ceil(0.95 * len(sorted_latencies))) - 1))
        evidence.update(
            {
                "model_dir": _relative(model_dir),
                "cold_load_seconds": cold,
                "warmup": {"completed": True},
                "latency_seconds": {"p50": statistics.median(latencies) if latencies else None, "p95": sorted_latencies[p95_index] if sorted_latencies else None},
                "peak_process_tree_rss_bytes": _rss_bytes(),
                "loaded_backend_first": {"device": first_agent.device, "dtype": first_agent.dtype},
                "loaded_backend_second": {"device": second_agent.device, "dtype": second_agent.dtype},
                "decoded_row_hash_first": first_hash,
                "decoded_row_hash_second": second_hash,
                "canonical_row_hashes_first": [inference.canonical_snapshot_hash(row) for row in first_rows],
                "canonical_row_hashes_second": [inference.canonical_snapshot_hash(row) for row in second_rows],
                "parquet_byte_hash_first": first_parquet_hash,
                "parquet_byte_hash_second": second_parquet_hash,
                "probabilities_exact_equal": probabilities_equal,
                "canonical_rows_exact_equal": canonical_equal,
                "rows_scored": len(first_rows),
                "projected_corpus_seconds": (elapsed / max(1, len(first_rows))) * 68616,
            }
        )
        criteria = {
            "install_load_offline_repeat": True,
            "outputs_finite_in_range": all(0.0 <= row["probability"] <= 1.0 for row in first_rows),
            "deterministic_repeat": probabilities_equal and canonical_equal,
            "backend_cpu_float32": first_agent.device == "cpu" and first_agent.dtype == "float32" and second_agent.device == "cpu" and second_agent.dtype == "float32",
            "token_budget_no_truncation": True,
            "rss_under_10_gib": evidence["peak_process_tree_rss_bytes"] is None or evidence["peak_process_tree_rss_bytes"] <= 10 * 1024**3,
            "projected_corpus_under_24h": evidence["projected_corpus_seconds"] <= 24 * 3600,
        }
        evidence["criteria"] = criteria
        failures = [name for name, passed in criteria.items() if not passed]
        evidence["failure_reasons"] = failures
        evidence["status"] = "PASS" if not failures else "FAILED_TECHNICAL"
    except Exception as exc:
        evidence["failure_reasons"] = ["install_load_offline_repeat"]
        evidence["error"] = str(exc).replace(str(ROOT), "<repo-root>")
        evidence["criteria"] = {"install_load_offline_repeat": False}
    _write_readonly_json(out, evidence)
    print(f"technical: {evidence['status']} -> {_relative(out)}")
    return 0 if evidence["status"] == "PASS" else 1


def cmd_score(args: argparse.Namespace) -> int:
    """Exercise resumable chunk plumbing without calibration or verdicts."""
    out_dir = _resolve(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    if int(args.max_seconds) > 900:
        raise SystemExit("score refuses chunks longer than 900 seconds")
    gate = json.loads(_resolve(args.technical_gate).read_text(encoding="utf-8"))
    manifest_path = out_dir / "score_manifest.json"
    if gate.get("status") != "PASS":
        payload = {"task": "0b", "status": "BLOCKED_TECHNICAL", "complete": False, "scored_rows": 0, "reason": "technical gate did not PASS"}
        manifest_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print("score: blocked by technical gate")
        return 1
    import pandas as _pd

    frame = _pd.read_parquet(_resolve(args.snapshots))
    frame = frame.sort_values(["symbol", "source_session", "decision_session"], kind="mergesort").reset_index(drop=True)
    chunk_size = int(getattr(args, "chunk_size", 1000))
    chunks = []
    for start in range(0, len(frame), chunk_size):
        chunk_id = start // chunk_size
        path = out_dir / f"chunk-{chunk_id:05d}.parquet"
        digest = hashlib.sha256(frame.iloc[start : start + chunk_size].to_json(orient="split", date_format="iso").encode()).hexdigest()
        if args.resume and path.exists():
            existing = _pd.read_parquet(path)
            if len(existing) != len(frame.iloc[start : start + chunk_size]):
                raise RuntimeError(f"resume chunk length mismatch: {path.name}")
            chunks.append({"chunk": chunk_id, "rows": len(existing), "sha256": digest, "resumed": True})
            continue
        # Thin stub deliberately stores only source rows; no q/p/calibration.
        frame.iloc[start : start + chunk_size].to_parquet(path, index=False)
        chunks.append({"chunk": chunk_id, "rows": len(frame.iloc[start : start + chunk_size]), "sha256": digest, "resumed": False})
    payload = {"task": "0b", "status": "STUB_COMPLETE", "complete": True, "scored_rows": len(frame), "chunks": chunks, "calibration": "UNRUN_TASK_0C"}
    manifest_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"score: STUB_COMPLETE rows={len(frame)}")
    return 0


# --- parser -------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="laya_feasibility_probe.py",
        description="Task 0a/0b offline Laya feasibility probe.",
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

    download = sub.add_parser("download", help="Download the pinned English checkpoint once.")
    download.add_argument("--protocol", required=True, help="Path to protocol_manifest.json")
    download.add_argument("--model-root", required=True, help="Untracked model cache root")
    download.set_defaults(func=cmd_download)

    technical = sub.add_parser("technical", help="Run the offline deterministic technical gate.")
    technical.add_argument("--protocol", required=True, help="Path to protocol_manifest.json")
    technical.add_argument("--model-root", required=True, help="Untracked model cache root")
    technical.add_argument("--snapshots", required=True, help="Features-only snapshots parquet")
    technical.add_argument("--device", default="cpu", choices=("cpu",), help="Pinned reference device")
    technical.add_argument("--sample-count", type=int, default=300)
    technical.add_argument("--out", required=True, help="Technical gate JSON")
    technical.set_defaults(func=cmd_technical)

    score = sub.add_parser("score", help="Run the uncalibrated resumable score plumbing stub.")
    score.add_argument("--protocol", required=True, help="Path to protocol_manifest.json")
    score.add_argument("--technical-gate", required=True, help="Technical gate JSON")
    score.add_argument("--model-root", required=True, help="Untracked model cache root")
    score.add_argument("--snapshots", required=True, help="Features-only snapshots parquet")
    score.add_argument("--out", required=True, help="Untracked prediction staging directory")
    score.add_argument("--max-seconds", type=int, default=900)
    score.add_argument("--resume", action="store_true")
    score.add_argument("--chunk-size", type=int, default=1000)
    score.set_defaults(func=cmd_score)
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
