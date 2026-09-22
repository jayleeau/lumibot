"""Unit tests for the Task 0a causal snapshot/target contract.

All fixtures are synthetic numbers.  No vendor data, archive, model, or network
is used.  The existing backtest/dotenv controls are set before importing the
native-adjacent modules so importing this file can never start a live stream.
"""
from __future__ import annotations

import dataclasses
import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

os.environ.setdefault("IS_BACKTESTING", "true")
os.environ.setdefault("LUMIBOT_DISABLE_DOTENV", "true")

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import pytest  # noqa: E402

from strategy_lab.laya_research import contracts  # noqa: E402
from strategy_lab.laya_research import snapshots as snap  # noqa: E402

PROTOCOL_PATH = Path(__file__).resolve().parents[1] / "protocol_v1.json"
TRAIN = contracts.IntervalRole.TRAIN


@pytest.fixture()
def protocol() -> contracts.Protocol:
    return contracts.load_protocol(PROTOCOL_PATH)


def _daily_raw(sessions: tuple[date, ...], *, scale: float = 1.0, seed: float = 0.0) -> pd.DataFrame:
    index = pd.DatetimeIndex([pd.Timestamp(day) for day in sessions])
    steps = np.arange(len(sessions), dtype="float64")
    close = scale * np.exp(0.0009 * steps + 0.0007 * np.sin(steps + seed))
    return pd.DataFrame(
        {
            "open": close * 0.999,
            "high": close * 1.01,
            "low": close * 0.99,
            "close": close,
            "volume": 1_000.0 + seed,
        },
        index=index,
    )


def _hourly_frame(sessions: tuple[date, ...], opens: np.ndarray) -> pd.DataFrame:
    stamps = pd.DatetimeIndex(
        [pd.Timestamp(day) + pd.Timedelta(hours=10) for day in sessions]
    )
    return pd.DataFrame(
        {
            "open": opens,
            "high": opens * 1.01,
            "low": opens * 0.99,
            "close": opens,
            "volume": 1_000.0,
        },
        index=stamps,
    )


def _close_at_16_utc(day: date) -> datetime:
    return pd.Timestamp(day, tz="UTC") + pd.Timedelta(hours=16)


# --- protocol -----------------------------------------------------------------


def test_protocol_roundtrip_and_frozen_fields(protocol: contracts.Protocol) -> None:
    assert protocol.feature_fields == contracts.FEATURE_FIELDS
    assert len(protocol.feature_fields) == 12
    assert protocol.fee_per_side == pytest.approx(0.00035)
    assert protocol.trend_sma == 20 and protocol.return_period == 20
    assert protocol.liquidity_period == 63
    assert protocol.entry_hour == 10 and protocol.exit_hour == 10
    assert protocol.interval(TRAIN).start == date(2019, 1, 1)
    assert protocol.interval(TRAIN).end == date(2021, 12, 31)
    assert protocol.interval(contracts.IntervalRole.CALIBRATION).start == date(2022, 1, 1)
    assert protocol.interval(contracts.IntervalRole.GATE).end == date(2023, 12, 31)
    assert protocol.interval(contracts.IntervalRole.FINAL).start == date(2024, 1, 1)
    assert protocol.interval(contracts.IntervalRole.FINAL).end == date(2026, 9, 8)
    assert contracts.Protocol.from_mapping(protocol.to_mapping()).to_mapping() == protocol.to_mapping()


def test_assert_export_roles_rejects_final_and_unknown() -> None:
    assert contracts.assert_export_roles(["train", "calibration", "gate"]) == contracts.EXPORTABLE_ROLES
    with pytest.raises(contracts.ProtocolError):
        contracts.assert_export_roles(["final"])
    with pytest.raises(contracts.ProtocolError):
        contracts.assert_export_roles(["train", "final"])
    with pytest.raises(contracts.ProtocolError):
        contracts.assert_export_roles(["bogus"])
    with pytest.raises(contracts.ProtocolError):
        contracts.assert_export_roles([])


# --- calendar -----------------------------------------------------------------


def test_previous_next_session_skip_holiday() -> None:
    sessions = (date(2019, 7, 3), date(2019, 7, 5), date(2019, 7, 8))
    assert snap.next_session(sessions, date(2019, 7, 3)) == date(2019, 7, 5)
    assert snap.previous_session(sessions, date(2019, 7, 5)) == date(2019, 7, 3)
    assert snap.next_session(sessions, date(2019, 7, 8)) is None
    assert snap.previous_session(sessions, date(2019, 7, 3)) is None


def test_exchange_calendar_skips_holiday_and_covers_range() -> None:
    sessions = snap.calendar_sessions("2019-07-01", "2019-07-08")
    assert date(2019, 7, 4) not in sessions
    assert date(2019, 7, 5) in sessions
    assert sessions == tuple(sorted(sessions))


def test_session_close_utc_handles_dst_and_early_closes() -> None:
    # Normal summer close (EDT) and normal winter close (EST) differ by one hour.
    assert snap.session_close_utc(date(2023, 6, 15)) == datetime(2023, 6, 15, 20, 0, tzinfo=timezone.utc)
    assert snap.session_close_utc(date(2023, 1, 17)) == datetime(2023, 1, 17, 21, 0, tzinfo=timezone.utc)
    # Early closes: 13:00 New York.
    assert snap.session_close_utc(date(2019, 7, 3)) == datetime(2019, 7, 3, 17, 0, tzinfo=timezone.utc)
    assert snap.session_close_utc(date(2019, 12, 24)) == datetime(2019, 12, 24, 18, 0, tzinfo=timezone.utc)


# --- features -----------------------------------------------------------------


def test_future_rows_and_truncation_do_not_change_earlier_snapshot(
    protocol: contracts.Protocol,
) -> None:
    long_sessions = tuple(pd.bdate_range("2019-01-02", periods=170).date)
    long_raw = _daily_raw(long_sessions)
    short_raw = long_raw.iloc[:140]
    long_features = snap.daily_features_for(long_raw, protocol=protocol)
    short_features = snap.daily_features_for(short_raw, protocol=protocol)
    source = long_sessions[125]
    full = snap.feature_snapshot_values(long_features, source)
    truncated = snap.feature_snapshot_values(short_features, source)
    assert full is not None
    assert full == truncated


def test_missing_prior_symbol_bar_excluded(protocol: contracts.Protocol) -> None:
    sessions = tuple(pd.bdate_range("2019-03-01", periods=80).date)
    source = sessions[70]
    decision = sessions[71]
    raw_a = _daily_raw(sessions, scale=1.0)
    raw_b = _daily_raw(sessions, scale=1.0).drop(index=[pd.Timestamp(source)])
    records = snap.build_decision_records(
        symbols=("AAA", "BBB"),
        sessions=sessions,
        daily_features={
            "AAA": snap.daily_features_for(raw_a, protocol=protocol),
            "BBB": snap.daily_features_for(raw_b, protocol=protocol),
        },
        hourly_frames={},
        protocol=protocol,
        role=TRAIN,
        session_close=_close_at_16_utc,
    )
    present = {snapshot.symbol for snapshot in records.snapshots if snapshot.decision_session == decision}
    assert present == {"AAA"}


def test_nonfinite_or_missing_features_are_invalid_not_prose() -> None:
    row = pd.Series(
        {
            "close": 100.0,
            "sma": 99.0,
            "mdv": 1_000_000.0,
            "ret": 0.1,
            "ret1": 0.01,
            "r10": 0.1,
            "r20": 0.1,
            "r60": 0.1,
            "vol20": 0.1,
            "ddvol20": 0.05,
            "eff20": 0.5,
            "reg_slope": 0.01,
            "reg_r2": 0.9,
        }
    )
    assert snap.derive_feature_values(row) is not None
    for field in ("close", "sma", "mdv", "ret", "r60", "reg_r2"):
        mutated = row.copy()
        mutated[field] = float("nan")
        assert snap.derive_feature_values(mutated) is None
    infinite = row.copy()
    infinite["close"] = float("inf")
    assert snap.derive_feature_values(infinite) is None
    nonpositive = row.copy()
    nonpositive["mdv"] = 0.0
    assert snap.derive_feature_values(nonpositive) is None
    missing = row.drop(labels=["sma"])
    assert snap.derive_feature_values(missing) is None

    with pytest.raises(contracts.SnapshotInvalid):
        contracts.Snapshot(
            "AAA",
            date(2019, 1, 2),
            date(2019, 1, 3),
            datetime(2019, 1, 2, 21, 0, tzinfo=timezone.utc),
            tuple([1.0] * 11 + [float("nan")]),
        )


# --- serialization ------------------------------------------------------------


def test_serialized_snapshot_bytes_are_deterministic() -> None:
    features = tuple(float(i) + 0.1234567 for i in range(12))
    text = contracts.serialize_features(features)
    assert text == contracts.serialize_features(features)
    assert text == ",".join(f"{value:.6f}" for value in features)
    snapshot = contracts.Snapshot(
        "AAA",
        date(2019, 1, 2),
        date(2019, 1, 3),
        datetime(2019, 1, 2, 21, 0, tzinfo=timezone.utc),
        features,
    )
    assert snapshot.text == text
    assert snapshot.text_digest == contracts.text_digest(text)
    # Negative zero must normalize so text bytes are stable.
    normalized = contracts.serialize_features((0.0, -0.0, *([1.0] * 10)))
    assert normalized.startswith("0.000000,0.000000,")


def test_serialized_text_contains_no_identity_outcome_or_date() -> None:
    text = contracts.serialize_features(tuple(1.5 for _ in range(12)))
    assert "AAA" not in text and "TQQQ" not in text
    assert "2019" not in text
    assert "unknown" not in text.lower()
    assert text.count(",") == 11
    assert all(part.replace(".", "").replace("-", "").isdigit() for part in text.split(","))


# --- target -------------------------------------------------------------------


def test_hand_worked_net_return_arithmetic_and_sign() -> None:
    fee = 0.00035
    r_net = snap.net_target(100.0, 101.0, fee)
    expected = 101.0 * (1.0 - fee) / (100.0 * (1.0 + fee)) - 1.0
    assert r_net == pytest.approx(expected, rel=0.0, abs=1e-15)
    assert r_net > 0.0
    assert snap.target_label(r_net) == 1
    loser = snap.net_target(100.0, 99.9, fee)
    assert loser < 0.0
    assert snap.target_label(loser) == 0
    # A flat price loses after costs.
    assert snap.net_target(100.0, 100.0, fee) < 0.0


def test_target_crossing_a_split_preserves_economic_return() -> None:
    fee = 0.00035
    adjusted = snap.net_target(100.0, 104.0, fee)
    split_scaled = snap.net_target(200.0, 208.0, fee)  # 2:1 split on both legs
    assert adjusted == pytest.approx(split_scaled, rel=0.0, abs=1e-15)
    assert adjusted > 0.0
    # The unadjusted post-split price would flip the sign, so adjustment must
    # be resolved from the back-adjusted archive series.
    assert snap.net_target(100.0, 52.0, fee) < 0.0


def test_build_records_uses_next_session_open_for_entry_and_exit(
    protocol: contracts.Protocol,
) -> None:
    sessions = tuple(pd.bdate_range("2019-03-01", periods=80).date)
    opens = 100.0 + np.arange(len(sessions), dtype="float64")
    hourly = {"AAA": _hourly_frame(sessions, opens)}
    raw = _daily_raw(sessions)
    decision = sessions[66]
    exit_session = sessions[67]
    records = snap.build_decision_records(
        symbols=("AAA",),
        sessions=sessions,
        daily_features={"AAA": snap.daily_features_for(raw, protocol=protocol)},
        hourly_frames=hourly,
        protocol=protocol,
        role=TRAIN,
        session_close=_close_at_16_utc,
    )
    row = next(
        outcome
        for outcome in records.outcomes
        if outcome.symbol == "AAA" and outcome.decision_session == decision
    )
    assert row.entry_session == decision
    assert row.exit_session == exit_session
    assert row.entry_price == pytest.approx(opens[66])
    assert row.exit_price == pytest.approx(opens[67])
    assert row.valid is True
    assert row.y == (1 if row.r_net > 0 else 0)
    assert row.y == 1  # open rises one step, well above the fee


def test_snapshot_source_is_the_previous_session(protocol: contracts.Protocol) -> None:
    sessions = tuple(pd.bdate_range("2019-03-01", periods=80).date)
    raw = _daily_raw(sessions)
    records = snap.build_decision_records(
        symbols=("AAA",),
        sessions=sessions,
        daily_features={"AAA": snap.daily_features_for(raw, protocol=protocol)},
        hourly_frames={},
        protocol=protocol,
        role=TRAIN,
        session_close=_close_at_16_utc,
    )
    snapshot = next(s for s in records.snapshots if s.decision_session == sessions[70])
    assert snapshot.source_session == sessions[69]
    assert snapshot.source_available_at == _close_at_16_utc(sessions[69])


def test_outcome_dropped_when_exit_falls_outside_interval(
    protocol: contracts.Protocol,
) -> None:
    train = contracts.Interval(TRAIN, date(2019, 3, 1), date(2019, 6, 30))
    variant = dataclasses.replace(protocol, intervals={**protocol.intervals, TRAIN: train})
    sessions = tuple(pd.bdate_range("2019-03-01", "2019-07-15").date)
    opens = 100.0 + np.arange(len(sessions), dtype="float64")
    raw = _daily_raw(sessions)
    records = snap.build_decision_records(
        symbols=("AAA",),
        sessions=sessions,
        daily_features={"AAA": snap.daily_features_for(raw, protocol=variant)},
        hourly_frames={"AAA": _hourly_frame(sessions, opens)},
        protocol=variant,
        role=TRAIN,
        session_close=_close_at_16_utc,
    )
    last_in_interval = max(day for day in sessions if train.contains(day))
    row = next(
        outcome
        for outcome in records.outcomes
        if outcome.decision_session == last_in_interval
    )
    assert row.valid is False
    assert row.reason == "exit_outside_interval"
    assert row.y is None
    # Snapshots remain features-only even when the target is dropped.
    assert any(s.decision_session == last_in_interval for s in records.snapshots)
    # Every valid target's exit stays inside the interval.
    assert all(
        outcome.exit_session is not None and train.contains(outcome.exit_session)
        for outcome in records.outcomes
        if outcome.valid
    )


def test_symbol_start_excludes_pre_inception_history(protocol: contracts.Protocol) -> None:
    sessions = tuple(pd.bdate_range("2019-03-01", periods=80).date)
    raw = _daily_raw(sessions)
    features = snap.daily_features_for(raw, protocol=protocol)
    start = sessions[60]
    records = snap.build_decision_records(
        symbols=("AAA",),
        sessions=sessions,
        daily_features={"AAA": features},
        hourly_frames={},
        protocol=protocol,
        role=TRAIN,
        session_close=_close_at_16_utc,
        symbol_start={"AAA": start},
    )
    assert records.snapshots
    assert all(snapshot.source_session >= start for snapshot in records.snapshots)
    assert all(outcome.decision_session >= start for outcome in records.outcomes)

    later = sessions[-1] + timedelta(days=30)
    excluded = snap.build_decision_records(
        symbols=("AAA",),
        sessions=sessions,
        daily_features={"AAA": features},
        hourly_frames={},
        protocol=protocol,
        role=TRAIN,
        session_close=_close_at_16_utc,
        symbol_start={"AAA": later},
    )
    assert excluded.snapshots == ()
    assert excluded.outcomes == ()
    assert excluded.eligibility == ()


def test_all_eligible_mask_filters_and_summarizes() -> None:
    rows = (
        contracts.EligibilityRow("AAA", date(2019, 1, 2), TRAIN, True, True),
        contracts.EligibilityRow("BBB", date(2019, 1, 2), TRAIN, True, False),
        contracts.EligibilityRow("AAA", date(2019, 1, 3), TRAIN, False, True),
    )
    eligible = contracts.all_eligible_mask(rows)
    assert [(row.symbol, row.decision_session) for row in eligible] == [("AAA", date(2019, 1, 2))]
    summary = contracts.summarize_eligibility(rows)
    assert summary["train"] == {
        "attempted": 3,
        "features_complete": 2,
        "target_valid": 2,
        "eligible": 1,
    }
