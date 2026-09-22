"""Deterministic causal snapshot builder and forward-return target.

Task 0a of ``plans/codex_astra_laya_plan.md``.  This module is pure Python over
pandas frames: it imports :mod:`strategy_lab.feature_store` for the daily feature
library but no archive, broker, model, or inference module.  Callers (the CLI)
load DuckDB frames and pass them in, which keeps every rule here unit-testable
with synthetic values only.

Causality contract
------------------
For decision session ``D`` the source session ``S`` is the previous expected
exchange session.  The snapshot uses the daily feature row at ``S`` only, so it
can never see bars completed after ``S``'s close.  The forward-return target
enters at ``D`` 10:00 New York and exits at the following exchange session's
10:00 New York open, both from the back-adjusted hourly archive.  This is not an
``S``-close-to-``D``-close return.
"""
from __future__ import annotations

import bisect
import math
from dataclasses import dataclass
from datetime import date, datetime
from hashlib import sha256
from typing import Any, Callable, Mapping, Sequence

import pandas as pd

from strategy_lab.feature_store import daily_feature_frame
from strategy_lab.laya_research.contracts import (
    FEATURE_FIELDS,
    EligibilityRow,
    Interval,
    IntervalRole,
    Outcome,
    Protocol,
    Snapshot,
)

# Re-export for callers that only import this module.
__all__ = [
    "FEATURE_FIELDS",
    "GateRecords",
    "build_decision_records",
    "calendar_sessions",
    "canonical_snapshots_digest",
    "daily_features_for",
    "derive_feature_values",
    "feature_snapshot_values",
    "hourly_open",
    "net_target",
    "next_session",
    "previous_session",
    "session_close_utc",
    "target_label",
]

DEFAULT_CALENDAR = "XNAS"
_CALENDAR_CACHE: dict[str, Any] = {}


# --- exchange calendar --------------------------------------------------------


def _calendar(name: str = DEFAULT_CALENDAR) -> Any:
    """Return a cached ``exchange_calendars`` calendar instance."""
    if name not in _CALENDAR_CACHE:
        import exchange_calendars as xcals  # imported lazily; pure Python

        _CALENDAR_CACHE[name] = xcals.get_calendar(name)
    return _CALENDAR_CACHE[name]


def calendar_sessions(start: date | str, end: date | str, *, calendar: str = DEFAULT_CALENDAR) -> tuple[date, ...]:
    """Return the ordered inclusive exchange sessions in ``[start, end]``."""
    cal = _calendar(calendar)
    index = cal.sessions_in_range(pd.Timestamp(start), pd.Timestamp(end))
    return tuple(pd.Timestamp(stamp).date() for stamp in index)


def session_close_utc(day: date | str, *, calendar: str = DEFAULT_CALENDAR) -> datetime:
    """Return the actual UTC close of an exchange session.

    Handles holidays, DST, and early closes because it reads the installed
    exchange calendar rather than assuming a fixed 16:00 New York clock.
    """
    cal = _calendar(calendar)
    stamp = cal.session_close(pd.Timestamp(day))
    return stamp.tz_convert("UTC").to_pydatetime()


# --- session ordering ---------------------------------------------------------


def previous_session(sessions: Sequence[date], day: date) -> date | None:
    """The session strictly before ``day``, or ``None`` when none exists."""
    index = bisect.bisect_left(sessions, day) - 1
    return sessions[index] if index >= 0 else None


def next_session(sessions: Sequence[date], day: date) -> date | None:
    """The session strictly after ``day``, or ``None`` when none exists."""
    index = bisect.bisect_right(sessions, day)
    return sessions[index] if index < len(sessions) else None


# --- features -----------------------------------------------------------------


def derive_feature_values(row: pd.Series) -> tuple[float, ...] | None:
    """Derive the twelve frozen features from one daily feature row.

    Returns ``None`` when any required source value is missing, nonfinite, or
    non-positive where a ratio/logarithm requires positivity.  No prose value is
    ever produced for an invalid row.
    """
    try:
        close = float(row["close"])
        sma = float(row["sma"])
        mdv = float(row["mdv"])
    except (KeyError, TypeError, ValueError):
        return None
    if not (math.isfinite(close) and math.isfinite(sma) and math.isfinite(mdv)):
        return None
    if sma <= 0.0 or mdv <= 0.0:
        return None
    try:
        values = (
            close / sma - 1.0,
            float(row["ret"]),
            math.log10(mdv),
            float(row["ret1"]),
            float(row["r10"]),
            float(row["r20"]),
            float(row["r60"]),
            float(row["vol20"]),
            float(row["ddvol20"]),
            float(row["eff20"]),
            float(row["reg_slope"]),
            float(row["reg_r2"]),
        )
    except (KeyError, TypeError, ValueError):
        return None
    if not all(math.isfinite(value) for value in values):
        return None
    return values


def feature_snapshot_values(
    feature_frame: pd.DataFrame | None, source_session: date
) -> tuple[float, ...] | None:
    """Exact-session feature vector, or ``None`` when the bar is missing/invalid.

    The lookup is an exact date key, never nearest-row or forward-fill, so a
    missing expected symbol session cannot silently reuse an older session.
    """
    if feature_frame is None or feature_frame.empty:
        return None
    try:
        row = feature_frame.loc[pd.Timestamp(source_session)]
    except KeyError:
        return None
    if isinstance(row, pd.DataFrame):
        return None  # duplicate index defeated the exact lookup
    return derive_feature_values(row)


def daily_features_for(
    raw_frame: pd.DataFrame, *, protocol: Protocol, benchmark_close: pd.Series | None = None
) -> pd.DataFrame:
    """Call the shared causal feature library with the protocol's lookbacks."""
    return daily_feature_frame(
        raw_frame,
        trend_sma=protocol.trend_sma,
        return_period=protocol.return_period,
        liquidity_period=protocol.liquidity_period,
        benchmark_close=benchmark_close,
    )


# --- target -------------------------------------------------------------------


def net_target(entry_price: float, exit_price: float, fee_per_side: float) -> float | None:
    """Net round-trip return after per-side cost, or ``None`` for bad prices.

    ``R_net = P_exit * (1 - fee) / (P_entry * (1 + fee)) - 1``.  The quantity
    depends only on the price ratio, so a consistently back-adjusted split
    cancels and the economic return is preserved.  The caller guarantees that
    entry and exit came from the same adjusted series.
    """
    try:
        entry = float(entry_price)
        exit_ = float(exit_price)
        fee = float(fee_per_side)
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(entry) and math.isfinite(exit_) and math.isfinite(fee)):
        return None
    if entry <= 0.0 or exit_ <= 0.0:
        return None
    return exit_ * (1.0 - fee) / (entry * (1.0 + fee)) - 1.0


def target_label(r_net: float) -> int:
    """Binary target ``1[R_net > 0]``."""
    return 1 if r_net > 0.0 else 0


def hourly_open(frame: pd.DataFrame | None, stamp: pd.Timestamp) -> float | None:
    """The open of the exact hourly bar at ``stamp``, or ``None``."""
    if frame is None or frame.empty:
        return None
    try:
        row = frame.loc[stamp]
    except KeyError:
        return None
    if isinstance(row, pd.DataFrame):
        return None
    try:
        value = float(row["open"])
    except (KeyError, TypeError, ValueError):
        return None
    return value if math.isfinite(value) and value > 0.0 else None


# --- record assembly ----------------------------------------------------------


@dataclass(frozen=True)
class GateRecords:
    """The features-only snapshots, targets, and eligibility mask for one role."""

    role: IntervalRole
    snapshots: tuple[Snapshot, ...]
    outcomes: tuple[Outcome, ...]
    eligibility: tuple[EligibilityRow, ...]


def build_decision_records(
    *,
    symbols: Sequence[str],
    sessions: Sequence[date],
    daily_features: Mapping[str, pd.DataFrame],
    hourly_frames: Mapping[str, pd.DataFrame],
    protocol: Protocol,
    role: IntervalRole,
    session_close: Callable[[date], datetime],
    symbol_start: Mapping[str, date] | None = None,
) -> GateRecords:
    """Build causal snapshots, forward-return targets, and the eligibility mask.

    ``sessions`` must be the complete ordered session list (including history
    before the role interval) so the previous source session of the first
    in-interval decision is available.  Target observations whose entry or exit
    session falls outside the assigned interval are dropped consistently.

    ``symbol_start`` gives the first valid session of each symbol.  A symbol is
    excluded for a decision when its source or decision session precedes that
    date, which fails closed on pre-inception or reused-ticker history (for
    example IBIT before its 2024 listing).
    """
    interval: Interval = protocol.interval(role)
    snapshots: list[Snapshot] = []
    outcomes: list[Outcome] = []
    eligibility: list[EligibilityRow] = []
    entry_offset = pd.Timedelta(hours=protocol.entry_hour)
    exit_offset = pd.Timedelta(hours=protocol.exit_hour)

    for decision in sessions:
        if not interval.contains(decision):
            continue
        source = previous_session(sessions, decision)
        exit_decision = next_session(sessions, decision)
        exit_in_interval = exit_decision is not None and interval.contains(exit_decision)
        source_available = session_close(source) if source is not None else None
        entry_stamp = pd.Timestamp(decision) + entry_offset
        exit_stamp = (
            pd.Timestamp(exit_decision) + exit_offset if exit_in_interval else None
        )

        for symbol in symbols:
            effective_start = symbol_start.get(symbol) if symbol_start else None
            if effective_start is not None and (
                source is None or source < effective_start or decision < effective_start
            ):
                continue
            values = (
                feature_snapshot_values(daily_features.get(symbol), source)
                if source is not None
                else None
            )
            if values is not None and source_available is not None:
                snapshots.append(
                    Snapshot(
                        symbol=symbol,
                        source_session=source,
                        decision_session=decision,
                        source_available_at=source_available,
                        features=values,
                    )
                )

            entry_price = hourly_open(hourly_frames.get(symbol), entry_stamp)
            exit_price = (
                hourly_open(hourly_frames.get(symbol), exit_stamp)
                if exit_stamp is not None
                else None
            )

            r_net: float | None = None
            y: int | None = None
            valid = False
            reason: str | None
            if values is None:
                reason = "features_incomplete"
            elif not exit_in_interval:
                reason = "exit_outside_interval"
            elif entry_price is None:
                reason = "missing_entry_price"
            elif exit_price is None:
                reason = "missing_exit_price"
            else:
                r_net = net_target(entry_price, exit_price, protocol.fee_per_side)
                if r_net is None:
                    reason = "invalid_prices"
                else:
                    y = target_label(r_net)
                    valid = True
                    reason = None

            outcomes.append(
                Outcome(
                    symbol=symbol,
                    source_session=source,
                    decision_session=decision,
                    entry_session=decision,
                    exit_session=exit_decision if exit_in_interval else None,
                    entry_price=entry_price if exit_in_interval else None,
                    exit_price=exit_price,
                    r_net=r_net,
                    y=y,
                    features_complete=values is not None,
                    valid=valid,
                    reason=reason,
                )
            )
            eligibility.append(
                EligibilityRow(
                    symbol=symbol,
                    decision_session=decision,
                    role=role,
                    features_complete=values is not None,
                    target_valid=valid,
                )
            )

    return GateRecords(
        role=role,
        snapshots=tuple(snapshots),
        outcomes=tuple(outcomes),
        eligibility=tuple(eligibility),
    )


def canonical_snapshots_digest(records: Sequence[GateRecords]) -> str:
    """SHA-256 over sorted canonical snapshot records.

    Symbol and dates are metadata outside the model text; they are included in
    this canonical digest only so changed coverage is detectable.
    """
    lines: list[str] = []
    for record in records:
        for snapshot in record.snapshots:
            lines.append(
                f"{snapshot.symbol}|{snapshot.source_session.isoformat()}|"
                f"{snapshot.decision_session.isoformat()}|{snapshot.text}"
            )
    lines.sort()
    return sha256("\n".join(lines).encode("ascii")).hexdigest()
