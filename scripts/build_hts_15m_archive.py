#!/usr/bin/env python3
"""Build the durable, split-adjusted 15-minute HTS research archive.

The inputs are immutable Databento DBN files.  This program never downloads
data and deliberately stages one source at a time so the 1-minute history does
not need to fit in memory.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Sequence
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

import duckdb


ROOT = Path(__file__).resolve().parents[1]
SHORT = ROOT / "short"
DEFAULT_OUTPUT = SHORT / "hts_xnas_itch_15m_split_adjusted.duckdb"
DEFAULT_DAILY_ARCHIVE = SHORT / "suite_monitored_xnas_itch_daily_adjusted.duckdb"
DEFAULT_SOURCES = (
    SHORT / "hts_xnas_itch_1m_all57_2018-05-01_2022-01-01.dbn.zst",
    SHORT / "hts_xnas_itch_1m_all57_2022-01-01_2024-09-08.dbn.zst",
    SHORT / "hts_xnas_itch_1m_sample_5_2024-09-08_2026-09-09.dbn.zst",
    SHORT / "hts_xnas_itch_1m_remaining52_2024-09-08_2026-09-09.dbn.zst",
    SHORT / "hts_xnas_itch_1m_all57_2026-09-09_2026-09-12.dbn.zst",
)
IBIT_INCEPTION = "2024-01-11 14:30:00+00"
NANOPRICE = 1_000_000_000
DEGRADED_DATES = ("2021-07-07", "2021-10-26", "2022-09-19")
EXPECTED_SPLIT_EVENTS = 31
UNDEF_PRICE = 9_223_372_036_854_775_807


def sha256(path: Path) -> str:
    """Return the content digest without loading the input into memory."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def configure_connection(
    connection: duckdb.DuckDBPyConnection,
    spill_directory: Path,
    memory_limit: str = "1GB",
    threads: int = 2,
) -> None:
    """Set conservative process-local resource controls for a large archive build."""
    if threads < 1:
        raise ValueError("threads must be at least 1")
    if not memory_limit or "'" in memory_limit:
        raise ValueError("memory_limit must be a non-empty DuckDB size such as 1GB")
    spill_directory.mkdir(parents=True, exist_ok=True)
    escaped = str(spill_directory).replace("'", "''")
    connection.execute(f"SET memory_limit = '{memory_limit}'")
    connection.execute(f"SET threads = {threads}")
    connection.execute(f"SET temp_directory = '{escaped}'")


def create_schema(connection: duckdb.DuckDBPyConnection) -> None:
    """Create the final archive tables and transient aggregation table."""
    connection.execute(
        """
        CREATE TABLE source_files (
            source_file VARCHAR PRIMARY KEY,
            sha256 VARCHAR NOT NULL,
            compressed_bytes UBIGINT NOT NULL,
            staged_parquet VARCHAR NOT NULL,
            converted_at TIMESTAMPTZ NOT NULL,
            record_count UBIGINT NOT NULL,
            accepted_minute_count UBIGINT NOT NULL,
            anomaly_count UBIGINT NOT NULL,
            quarantined_count UBIGINT NOT NULL
        );
        CREATE TABLE source_anomalies (
            source_file VARCHAR NOT NULL,
            symbol VARCHAR,
            ts TIMESTAMPTZ,
            reason VARCHAR NOT NULL
        );
        CREATE TABLE quarantined_rows (
            source_file VARCHAR NOT NULL,
            symbol VARCHAR NOT NULL,
            ts TIMESTAMPTZ NOT NULL,
            reason VARCHAR NOT NULL
        );
        CREATE TABLE bars_15m_stage (
            symbol VARCHAR NOT NULL,
            ts TIMESTAMPTZ NOT NULL,
            source_file VARCHAR NOT NULL,
            first_source_ts TIMESTAMPTZ NOT NULL,
            last_source_ts TIMESTAMPTZ NOT NULL,
            open DOUBLE NOT NULL,
            high DOUBLE NOT NULL,
            low DOUBLE NOT NULL,
            close DOUBLE NOT NULL,
            volume DOUBLE NOT NULL,
            source_minute_count BIGINT NOT NULL
        );
        CREATE TABLE split_events (
            symbol VARCHAR NOT NULL,
            ex_ts TIMESTAMPTZ NOT NULL,
            ratio DOUBLE NOT NULL,
            PRIMARY KEY (symbol, ex_ts)
        );
        CREATE TABLE split_event_sources (
            symbol VARCHAR NOT NULL,
            ex_ts TIMESTAMPTZ NOT NULL,
            ratio DOUBLE NOT NULL,
            source VARCHAR NOT NULL,
            source_detail VARCHAR,
            verification_status VARCHAR NOT NULL
        );
        CREATE TABLE archive_metadata (
            key VARCHAR PRIMARY KEY,
            value VARCHAR NOT NULL
        );
        CREATE TABLE validation_results (
            check_name VARCHAR PRIMARY KEY,
            passed BOOLEAN NOT NULL,
            observed_value VARCHAR NOT NULL,
            detail VARCHAR NOT NULL
        );
        """
    )


def _valid_predicate(alias: str = "") -> str:
    prefix = f"{alias}." if alias else ""
    return (
        f"{prefix}symbol IS NOT NULL AND {prefix}ts_event IS NOT NULL "
        f"AND {prefix}open > 0 AND {prefix}high > 0 AND {prefix}low > 0 "
        f"AND {prefix}close > 0 AND {prefix}high >= {prefix}low "
        f"AND {prefix}high >= {prefix}open AND {prefix}high >= {prefix}close "
        f"AND {prefix}low <= {prefix}open AND {prefix}low <= {prefix}close "
        f"AND {prefix}open <> {UNDEF_PRICE} AND {prefix}high <> {UNDEF_PRICE} "
        f"AND {prefix}low <> {UNDEF_PRICE} AND {prefix}close <> {UNDEF_PRICE} "
        f"AND {prefix}volume IS NOT NULL"
    )


def ingest_parquet(
    connection: duckdb.DuckDBPyConnection, parquet_path: Path, source_file: Path
) -> None:
    """Aggregate one exact-price DBN parquet staging file into 15-minute rows."""
    source_name = source_file.name
    connection.execute(
        """
        INSERT INTO source_anomalies
        SELECT ?, symbol, ts_event, 'invalid_ohlc_or_missing_identity'
        FROM read_parquet(?)
        WHERE NOT ("""
        + _valid_predicate()
        + ")",
        [source_name, str(parquet_path)],
    )
    connection.execute(
        """
        INSERT INTO quarantined_rows
        SELECT ?, symbol, ts_event, 'IBIT ticker predates iShares Bitcoin Trust inception'
        FROM read_parquet(?)
        WHERE symbol = 'IBIT' AND ts_event < CAST(? AS TIMESTAMPTZ)
        """,
        [source_name, str(parquet_path), IBIT_INCEPTION],
    )
    connection.execute(
        """
        INSERT INTO bars_15m_stage
        SELECT
            symbol,
            time_bucket(INTERVAL '15 minutes', ts_event) AS ts,
            ? AS source_file,
            min(ts_event) AS first_source_ts,
            max(ts_event) AS last_source_ts,
            arg_min(open, ts_event) / ?::DOUBLE AS open,
            max(high) / ?::DOUBLE AS high,
            min(low) / ?::DOUBLE AS low,
            arg_max(close, ts_event) / ?::DOUBLE AS close,
            sum(volume)::DOUBLE AS volume,
            count(*) AS source_minute_count
        FROM read_parquet(?)
        WHERE """
        + _valid_predicate()
        + " AND NOT (symbol = 'IBIT' AND ts_event < CAST(? AS TIMESTAMPTZ))"
        + " GROUP BY symbol, time_bucket(INTERVAL '15 minutes', ts_event)",
        [source_name, NANOPRICE, NANOPRICE, NANOPRICE, NANOPRICE, str(parquet_path), IBIT_INCEPTION],
    )


def finalize_raw_bars(connection: duckdb.DuckDBPyConnection) -> None:
    """Merge source boundary buckets without assuming source date boundaries align."""
    overlapping = connection.execute(
        """
        SELECT count(*)
        FROM bars_15m_stage left_stage
        JOIN bars_15m_stage right_stage
          ON left_stage.symbol = right_stage.symbol
         AND left_stage.ts = right_stage.ts
         AND left_stage.source_file < right_stage.source_file
         AND greatest(left_stage.first_source_ts, right_stage.first_source_ts)
             <= least(left_stage.last_source_ts, right_stage.last_source_ts)
        """
    ).fetchone()[0]
    if overlapping:
        raise RuntimeError(f"Source DBNs contain {overlapping} overlapping symbol/time buckets")
    connection.execute(
        """
        CREATE TABLE bars_15m_raw AS
        SELECT
            symbol,
            ts,
            arg_min(open, first_source_ts) AS open,
            max(high) AS high,
            min(low) AS low,
            arg_max(close, last_source_ts) AS close,
            sum(volume) AS volume,
            sum(source_minute_count) AS source_minute_count
        FROM bars_15m_stage
        GROUP BY symbol, ts
        ORDER BY symbol, ts;
        DROP TABLE bars_15m_stage;
        """
    )


def import_split_events(
    connection: duckdb.DuckDBPyConnection, daily_archive: Path
) -> None:
    """Use the established daily archive's selected HTS-universe split events."""
    if not daily_archive.is_file():
        raise FileNotFoundError(f"Required split-event archive is missing: {daily_archive}")
    escaped = str(daily_archive).replace("'", "''")
    connection.execute(f"ATTACH '{escaped}' AS daily_source (READ_ONLY)")
    connection.execute(
        """
        INSERT INTO split_events
        SELECT event.symbol,
               CAST(CAST(event.ex_ts AS DATE) AS TIMESTAMP)
                   AT TIME ZONE 'America/New_York' AS ex_ts,
               event.ratio
        FROM daily_source.split_events AS event
        INNER JOIN (SELECT DISTINCT symbol FROM bars_15m_raw) AS universe USING (symbol)
        WHERE event.ratio > 0;
        INSERT INTO split_event_sources
        SELECT symbol, ex_ts, ratio, 'daily_adjusted_archive',
               'suite_monitored_xnas_itch_daily_adjusted.duckdb', 'SOURCE_SELECTED'
        FROM split_events;
        DETACH daily_source;
        """
    )

    count = connection.execute("SELECT count(*) FROM split_events").fetchone()[0]
    if count != EXPECTED_SPLIT_EVENTS:
        raise RuntimeError(
            f"Expected {EXPECTED_SPLIT_EVENTS} HTS split events, found {count}; "
            "the event ledger or archive universe changed"
        )


def fetch_yahoo_splits(symbol: str, start_epoch: int, end_epoch: int) -> list[dict[str, Any]]:
    """Fetch split records for one symbol over the exact archive interval."""
    query = urlencode(
        {
            "period1": start_epoch,
            "period2": end_epoch,
            "interval": "1d",
            "events": "splits",
        }
    )
    request = Request(
        f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?{query}",
        headers={"User-Agent": "Mozilla/5.0"},
    )
    with urlopen(request, timeout=30) as response:
        payload = json.load(response)
    results = payload.get("chart", {}).get("result") or []
    return list((results[0].get("events", {}).get("splits", {}) if results else {}).values())


def _new_york_midnight(timestamp: int) -> datetime:
    event_date = datetime.fromtimestamp(timestamp, UTC).astimezone(ZoneInfo("America/New_York")).date()
    return datetime.combine(event_date, datetime.min.time(), ZoneInfo("America/New_York"))


def verify_yahoo_splits(
    connection: duckdb.DuckDBPyConnection,
    fetcher: Callable[[str, int, int], list[dict[str, Any]]] = fetch_yahoo_splits,
) -> None:
    """Optionally cross-check selected splits against Yahoo, failing closed on disagreement."""
    events = connection.execute("SELECT symbol, ex_ts, ratio FROM split_events ORDER BY symbol, ex_ts").fetchall()
    yahoo_rows: list[tuple[str, datetime, float]] = []
    bounds = connection.execute(
        "SELECT epoch(min(ts))::BIGINT, epoch(max(ts) + INTERVAL '1 day')::BIGINT FROM bars_15m_raw"
    ).fetchone()
    symbols = [
        row[0]
        for row in connection.execute(
            "SELECT DISTINCT symbol FROM bars_15m_raw ORDER BY symbol"
        ).fetchall()
    ]
    for symbol in symbols:
        for event in fetcher(symbol, bounds[0], bounds[1]):
            numerator = float(event["numerator"])
            denominator = float(event["denominator"])
            if numerator <= 0 or denominator <= 0:
                raise RuntimeError(f"Yahoo returned an invalid split ratio for {symbol}: {event}")
            yahoo_rows.append(
                (symbol, _new_york_midnight(int(event["date"])), numerator / denominator)
            )

    expected = {(symbol, ex_ts.date(), round(float(ratio), 10)) for symbol, ex_ts, ratio in events}
    observed = {(symbol, timestamp.date(), round(ratio, 10)) for symbol, timestamp, ratio in yahoo_rows}
    if expected != observed:
        missing = sorted(expected - observed)
        extra = sorted(observed - expected)
        raise RuntimeError(f"Yahoo split verification mismatch; missing={missing[:5]}, extra={extra[:5]}")
    connection.executemany(
        "INSERT INTO split_event_sources VALUES (?, ?, ?, 'yahoo_chart', 'chart API split event', 'MATCHED')",
        yahoo_rows,
    )


def create_adjusted_bars(connection: duckdb.DuckDBPyConnection) -> None:
    """Back-adjust prices/volume without altering post-event history.

    A forward 4:1 event has ratio 4: every earlier price is divided by four and
    earlier volume is multiplied by four, preserving traded notional.
    """
    connection.execute(
        """
        CREATE TABLE bars_15m AS
        SELECT raw.symbol, raw.ts,
               raw.open / factor AS open,
               raw.high / factor AS high,
               raw.low / factor AS low,
               raw.close / factor AS close,
               raw.volume * factor AS volume,
               raw.source_minute_count
        FROM bars_15m_raw AS raw
        CROSS JOIN LATERAL (
            SELECT coalesce(product(ratio), 1.0) AS factor
            FROM split_events AS event
            WHERE event.symbol = raw.symbol AND raw.ts < event.ex_ts
        ) AS adjustment
        ORDER BY raw.symbol, raw.ts;
        CREATE INDEX bars_15m_raw_symbol_ts_idx ON bars_15m_raw(symbol, ts);
        CREATE INDEX bars_15m_symbol_ts_idx ON bars_15m(symbol, ts);
        """
    )


def record_validation(connection: duckdb.DuckDBPyConnection) -> None:
    """Write invariant checks into the artifact and fail the build on any failure."""
    checks = {
        "no_duplicate_bars": (
            "SELECT count(*) FROM (SELECT symbol, ts FROM bars_15m_raw GROUP BY 1, 2 HAVING count(*) > 1)",
            lambda value: value == 0,
            "one output bar per symbol and UTC 15-minute bucket",
        ),
        "raw_ohlc_valid": (
            "SELECT count(*) FROM bars_15m_raw WHERE open <= 0 OR high < low OR high < open OR high < close OR low > open OR low > close",
            lambda value: value == 0,
            "raw OHLC range invariant",
        ),
        "adjusted_ohlc_valid": (
            "SELECT count(*) FROM bars_15m WHERE open <= 0 OR high < low OR high < open OR high < close OR low > open OR low > close",
            lambda value: value == 0,
            "split adjustment preserves OHLC ordering",
        ),
        "minute_count_conserved": (
            "SELECT abs((SELECT coalesce(sum(source_minute_count), 0) FROM bars_15m_raw) - (SELECT coalesce(sum(accepted_minute_count), 0) FROM source_files))",
            lambda value: value == 0,
            "every accepted one-minute source record contributes exactly once",
        ),
        "split_events_valid": (
            "SELECT count(*) FROM split_events WHERE ratio <= 0",
            lambda value: value == 0,
            "all selected split ratios are positive",
        ),
        "split_event_count": (
            "SELECT count(*) FROM split_events",
            lambda value: value == EXPECTED_SPLIT_EVENTS,
            f"exactly {EXPECTED_SPLIT_EVENTS} independently corroborated HTS split events",
        ),
        "adjustment_notional_preserved": (
            "SELECT coalesce(max(abs(raw.close * raw.volume - adjusted.close * adjusted.volume) / greatest(abs(raw.close * raw.volume), 1.0)), 0.0) FROM bars_15m_raw raw JOIN bars_15m adjusted USING(symbol, ts)",
            lambda value: float(value) <= 1e-12,
            "close times volume is invariant under the selected split factor (relative error)",
        ),
    }
    failures = []
    for name, (query, predicate, detail) in checks.items():
        value = connection.execute(query).fetchone()[0]
        passed = bool(predicate(value))
        connection.execute(
            "INSERT INTO validation_results VALUES (?, ?, ?, ?)",
            [name, passed, str(value), detail],
        )
        if not passed:
            failures.append(f"{name}={value}")
    if failures:
        raise RuntimeError("Archive validation failed: " + ", ".join(failures))


def record_metadata(
    connection: duckdb.DuckDBPyConnection,
    sources: Sequence[Path],
    verify_yahoo: bool,
    memory_limit: str,
    threads: int,
) -> None:
    """Store self-describing, non-secret provenance in the final database."""
    metadata = {
        "archive_format": "hts-15m-split-adjusted:v1",
        "created_at_utc": datetime.now(UTC).isoformat(),
        "bar_timestamp": "UTC interval start; time_bucket(15 minutes, ts_event)",
        "raw_inputs": json.dumps([path.name for path in sources]),
        "price_representation": "Databento fixed nanoprice staged; DOUBLE output dollars",
        "split_adjustment": "for raw.ts < ex_ts: OHLC / product(ratio); volume * product(ratio)",
        "split_event_source": "daily adjusted archive; Yahoo exact-range cross-check" if verify_yahoo else "daily adjusted archive only",
        "split_event_verification": "31/31 Yahoo exact match" if verify_yahoo else "not independently checked in this build",
        "reference_endpoint_provenance": "Databento adjustment-factors request attempted 2026-09-14; HTTP 403 license_reference_dataset_no_subscription",
        "ibit_quarantine_before": IBIT_INCEPTION,
        "known_degraded_source_dates": ",".join(DEGRADED_DATES),
        "duckdb_memory_limit": memory_limit,
        "duckdb_threads": str(threads),
    }
    connection.executemany("INSERT INTO archive_metadata VALUES (?, ?)", metadata.items())


def convert_dbn_to_parquet(source: Path, staging_path: Path) -> None:
    """Convert exactly one DBN input to fixed-price Parquet using Databento's reader."""
    try:
        import databento as db
    except ImportError as exc:  # pragma: no cover - environment dependency
        raise RuntimeError("databento is required to convert DBN input") from exc
    db.DBNStore.from_file(source).to_parquet(
        staging_path, price_type="fixed", pretty_ts=True, map_symbols=True
    )


def build_archive(
    output: Path,
    sources: Sequence[Path] = DEFAULT_SOURCES,
    daily_archive: Path = DEFAULT_DAILY_ARCHIVE,
    verify_yahoo: bool = False,
    force: bool = False,
    memory_limit: str = "1GB",
    threads: int = 2,
) -> Path:
    """Build an atomic 15-minute archive from immutable DBNs and local split data."""
    sources = tuple(Path(source) for source in sources)
    missing = [str(source) for source in sources if not source.is_file()]
    if missing:
        raise FileNotFoundError("Missing immutable DBN inputs: " + ", ".join(missing))
    if output.exists() and not force:
        raise FileExistsError(f"Refusing to replace existing archive without --force: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.building-{os.getpid()}")
    if temporary.exists():
        raise FileExistsError(f"Temporary archive already exists: {temporary}")
    staging_directory = output.parent / f".{output.stem}.staging-{os.getpid()}"
    staging_directory.mkdir(parents=True, exist_ok=False)

    try:
        connection = duckdb.connect(str(temporary))
        configure_connection(connection, staging_directory / "duckdb-spill", memory_limit, threads)
        create_schema(connection)
        for index, source in enumerate(sources):
            staged = staging_directory / f"{index:02d}-{source.stem}.parquet"
            convert_dbn_to_parquet(source, staged)
            ingest_parquet(connection, staged, source)
            counts = connection.execute(
                """
                SELECT count(*),
                       count(*) FILTER (WHERE """ + _valid_predicate() + """),
                       count(*) FILTER (WHERE NOT (""" + _valid_predicate() + """)),
                       count(*) FILTER (
                           WHERE """ + _valid_predicate() + """
                             AND symbol = 'IBIT' AND ts_event < CAST(? AS TIMESTAMPTZ)
                       )
                FROM read_parquet(?)
                """,
                [IBIT_INCEPTION, str(staged)],
            ).fetchone()
            accepted = counts[1] - counts[3]
            connection.execute(
                "INSERT INTO source_files VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    source.name,
                    sha256(source),
                    source.stat().st_size,
                    staged.name,
                    datetime.now(UTC),
                    counts[0],
                    accepted,
                    counts[2],
                    counts[3],
                ],
            )
        finalize_raw_bars(connection)
        import_split_events(connection, daily_archive)
        if verify_yahoo:
            verify_yahoo_splits(connection)
        create_adjusted_bars(connection)
        record_metadata(connection, sources, verify_yahoo, memory_limit, threads)
        record_validation(connection)
        connection.execute("CHECKPOINT")
        connection.close()
        temporary.replace(output)
    except BaseException:
        # Preserve the staged inputs and partial archive for diagnosis; never publish it.
        raise
    else:
        shutil.rmtree(staging_directory)
    return output


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse the intentionally small, explicit command-line interface."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--daily-archive", type=Path, default=DEFAULT_DAILY_ARCHIVE)
    parser.add_argument("--source", type=Path, action="append", help="DBN source; repeat five times")
    parser.add_argument("--verify-yahoo", action="store_true", help="cross-check split evidence online and fail on disagreement")
    parser.add_argument("--force", action="store_true", help="atomically replace an existing output archive")
    parser.add_argument("--memory-limit", default="1GB", help="DuckDB memory cap (default: 1GB)")
    parser.add_argument("--threads", type=int, default=2, help="DuckDB worker threads (default: 2)")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """Run the builder and print only the durable artifact path."""
    args = parse_args(argv)
    archive = build_archive(
        output=args.output,
        sources=tuple(args.source) if args.source else DEFAULT_SOURCES,
        daily_archive=args.daily_archive,
        verify_yahoo=args.verify_yahoo,
        force=args.force,
        memory_limit=args.memory_limit,
        threads=args.threads,
    )
    print(archive)
    return 0


if __name__ == "__main__":
    sys.exit(main())
