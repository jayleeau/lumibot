"""Offline regression coverage for the HTS 15-minute archive builder."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import duckdb
import pytest


MODULE_PATH = Path(__file__).parents[2] / "scripts" / "build_hts_15m_archive.py"
SPEC = importlib.util.spec_from_file_location("build_hts_15m_archive", MODULE_PATH)
assert SPEC and SPEC.loader
archive_builder = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(archive_builder)


def connection_with_stage() -> duckdb.DuckDBPyConnection:
    connection = duckdb.connect(":memory:")
    archive_builder.create_schema(connection)
    return connection


def test_15_minute_aggregation_uses_first_open_last_close_and_sum_volume() -> None:
    connection = connection_with_stage()
    connection.execute(
        """
        INSERT INTO bars_15m_stage VALUES
        ('SPY', '2024-01-02 14:30:00+00', 'part-a', '2024-01-02 14:30:00+00', '2024-01-02 14:35:00+00', 100, 103, 99, 101, 10, 1),
        ('SPY', '2024-01-02 14:30:00+00', 'part-a', '2024-01-02 14:36:00+00', '2024-01-02 14:44:00+00', 101, 105, 100, 104, 20, 1),
        ('SPY', '2024-01-02 14:45:00+00', 'part-a', '2024-01-02 14:45:00+00', '2024-01-02 14:45:00+00', 104, 106, 103, 105, 30, 1)
        """
    )
    archive_builder.finalize_raw_bars(connection)
    assert connection.execute("SELECT * FROM bars_15m_raw ORDER BY ts").fetchall() == [
        ("SPY", connection.execute("SELECT CAST('2024-01-02 14:30:00+00' AS TIMESTAMPTZ)").fetchone()[0], 100.0, 105.0, 99.0, 104.0, 30.0, 2),
        ("SPY", connection.execute("SELECT CAST('2024-01-02 14:45:00+00' AS TIMESTAMPTZ)").fetchone()[0], 104.0, 106.0, 103.0, 105.0, 30.0, 1),
    ]


def test_split_adjustment_applies_only_before_event_and_preserves_notional() -> None:
    connection = connection_with_stage()
    connection.execute(
        """
        INSERT INTO bars_15m_stage VALUES
        ('AAPL', '2020-08-28 14:30:00+00', 'part-a', '2020-08-28 14:30:00+00', '2020-08-28 14:30:00+00', 500, 504, 496, 500, 10, 1),
        ('AAPL', '2020-08-31 14:30:00+00', 'part-a', '2020-08-31 14:30:00+00', '2020-08-31 14:30:00+00', 125, 126, 124, 125, 40, 1)
        """
    )
    archive_builder.finalize_raw_bars(connection)
    connection.execute("INSERT INTO split_events VALUES ('AAPL', '2020-08-31 00:00:00+00', 4.0)")
    archive_builder.create_adjusted_bars(connection)
    assert connection.execute("SELECT close, volume FROM bars_15m ORDER BY ts").fetchall() == [(125.0, 40.0), (125.0, 40.0)]
    connection.execute(
        "INSERT INTO source_files VALUES ('part-a', 'sha', 1, 'stage', now(), 2, 2, 0, 0)"
    )
    original_expected = archive_builder.EXPECTED_SPLIT_EVENTS
    archive_builder.EXPECTED_SPLIT_EVENTS = 1
    archive_builder.record_validation(connection)
    archive_builder.EXPECTED_SPLIT_EVENTS = original_expected
    assert connection.execute("SELECT bool_and(passed) FROM validation_results").fetchone()[0] is True


def test_yahoo_verification_checks_symbols_without_selected_events() -> None:
    connection = connection_with_stage()
    connection.execute(
        """
        CREATE TABLE bars_15m_raw AS
        SELECT * FROM (VALUES
          ('SPY', TIMESTAMPTZ '2024-01-02 14:30:00+00', 100.0, 101.0, 99.0, 100.0, 10.0, 1),
          ('TQQQ', TIMESTAMPTZ '2024-01-02 14:30:00+00', 50.0, 51.0, 49.0, 50.0, 20.0, 1)
        ) bars(symbol, ts, open, high, low, close, volume, source_minute_count)
        """
    )
    connection.execute(
        "INSERT INTO split_events VALUES ('TQQQ', '2024-01-03 05:00:00+00', 2.0)"
    )
    requested = []

    def fetcher(symbol: str, start_epoch: int, end_epoch: int) -> list[dict[str, object]]:
        requested.append(symbol)
        if symbol == "TQQQ":
            return [{"date": 1704292200, "numerator": 2.0, "denominator": 1.0}]
        return []

    archive_builder.verify_yahoo_splits(connection, fetcher=fetcher)
    assert requested == ["SPY", "TQQQ"]
    assert connection.execute(
        "SELECT count(*) FROM split_event_sources WHERE source = 'yahoo_chart'"
    ).fetchone()[0] == 1


def test_ibit_quarantine_predicate_is_boundary_safe() -> None:
    connection = duckdb.connect(":memory:")
    connection.execute("CREATE TABLE rows(symbol VARCHAR, ts_event TIMESTAMPTZ)")
    connection.execute(
        """
        INSERT INTO rows VALUES
        ('IBIT', '2024-01-11 14:29:00+00'),
        ('IBIT', '2024-01-11 14:30:00+00'),
        ('SPY', '2018-01-01 14:30:00+00')
        """
    )
    actual = connection.execute(
        "SELECT symbol, symbol = 'IBIT' AND ts_event < CAST(? AS TIMESTAMPTZ) FROM rows ORDER BY ts_event",
        [archive_builder.IBIT_INCEPTION],
    ).fetchall()
    assert actual == [("SPY", False), ("IBIT", True), ("IBIT", False)]


def test_imported_split_date_becomes_new_york_midnight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    daily_path = tmp_path / "daily.duckdb"
    daily = duckdb.connect(str(daily_path))
    daily.execute(
        """
        CREATE TABLE split_events(symbol VARCHAR, ex_ts TIMESTAMPTZ, ratio DOUBLE);
        INSERT INTO split_events VALUES ('SPY', '2024-07-01 00:00:00+00', 2.0);
        """
    )
    daily.close()

    connection = connection_with_stage()
    connection.execute(
        """
        CREATE TABLE bars_15m_raw AS
        SELECT 'SPY'::VARCHAR AS symbol, TIMESTAMPTZ '2024-06-28 14:30:00+00' AS ts,
               1.0 AS open, 1.0 AS high, 1.0 AS low, 1.0 AS close, 1.0 AS volume,
               1::BIGINT AS source_minute_count
        """
    )
    monkeypatch.setattr(archive_builder, "EXPECTED_SPLIT_EVENTS", 1)
    archive_builder.import_split_events(connection, daily_path)
    assert connection.execute(
        "SELECT timezone('America/New_York', ex_ts) FROM split_events"
    ).fetchone()[0].isoformat() == "2024-07-01T00:00:00"


def test_validation_fails_closed_on_source_minute_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = connection_with_stage()
    connection.execute(
        """
        INSERT INTO bars_15m_stage VALUES
        ('SPY', '2024-01-02 14:30:00+00', 'part-a', '2024-01-02 14:30:00+00',
         '2024-01-02 14:30:00+00', 100, 101, 99, 100, 10, 1)
        """
    )
    archive_builder.finalize_raw_bars(connection)
    archive_builder.create_adjusted_bars(connection)
    connection.execute(
        "INSERT INTO source_files VALUES ('part-a', 'sha', 1, 'stage', now(), 2, 2, 0, 0)"
    )
    monkeypatch.setattr(archive_builder, "EXPECTED_SPLIT_EVENTS", 0)
    with pytest.raises(RuntimeError, match="minute_count_conserved"):
        archive_builder.record_validation(connection)


def test_build_fails_closed_when_an_input_is_missing(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="Missing immutable DBN inputs"):
        archive_builder.build_archive(
            tmp_path / "archive.duckdb",
            sources=(tmp_path / "not-present.dbn.zst",),
            daily_archive=tmp_path / "daily.duckdb",
        )
