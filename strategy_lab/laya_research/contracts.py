"""Frozen, typed contracts for the Laya offline entry-eligibility study.

Task 0a of ``plans/codex_astra_laya_plan.md``.  This module is pure Python.  It
defines the interval roles, the per-parameter frozen protocol, the snapshot and
outcome records, deterministic ASCII serialization, and the fail-closed helpers
shared by the causal snapshot builder and the feasibility CLI.  It imports no
inference framework, model, broker, or archive code.

Design rules:

* A snapshot is a fixed, ordered vector of twelve daily features.  Symbol and
  session dates are metadata carried *outside* the serialized model text.
* Invalid input fails closed to an invalid observation, never to prose such as
  ``"unknown"``.
* The final retrospective holdout role can never be exported by Task 0a.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
from hashlib import sha256
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

# The twelve snapshot fields, in the exact frozen order required by the plan.
FEATURE_FIELDS: tuple[str, ...] = (
    "close_over_sma_minus_1",
    "ret",
    "log10_mdv",
    "ret1",
    "r10",
    "r20",
    "r60",
    "vol20",
    "ddvol20",
    "eff20",
    "reg_slope",
    "reg_r2",
)
FEATURE_FIELD_COUNT: int = len(FEATURE_FIELDS)
SCHEMA_VERSION: str = "laya-snapshot-v1"
SNAPSHOT_DECIMALS: int = 6
DEFAULT_SEED: int = 20260922


class ProtocolError(ValueError):
    """Raised when the frozen protocol is malformed or an export is forbidden."""


class SnapshotInvalid(ValueError):
    """Raised when a snapshot carries a nonfinite or mis-shaped feature vector."""


class IntervalRole(str, Enum):
    """The four frozen decision-session intervals of the research protocol."""

    TRAIN = "train"
    CALIBRATION = "calibration"
    GATE = "gate"
    FINAL = "final"


# Task 0a may export only the first three roles.  The final retrospective
# holdout is deliberately excluded until a later, explicitly authorized task.
EXPORTABLE_ROLES: tuple[IntervalRole, ...] = (
    IntervalRole.TRAIN,
    IntervalRole.CALIBRATION,
    IntervalRole.GATE,
)
FORBIDDEN_EXPORT_ROLES: tuple[IntervalRole, ...] = (IntervalRole.FINAL,)


def _as_date(value: Any, *, field: str) -> date:
    """Coerce an ISO string, ``date`` or ``datetime`` to a ``date``."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        return date.fromisoformat(value)
    raise ProtocolError(f"{field} must be a date or ISO date string, got {value!r}")


def finite(value: object) -> bool:
    """True when ``value`` is a finite real number (``None``/NaN are not)."""
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False
    return math.isfinite(number)


def serialize_features(
    features: Sequence[float], *, decimals: int = SNAPSHOT_DECIMALS
) -> str:
    """Serialize one feature vector as deterministic ASCII model text.

    The output is the twelve values in frozen order, six decimal places by
    default, comma separated, with negative zero normalized to ``0.000000``.  It
    contains no symbol, date, outcome, or narrative text.
    """
    if len(features) != FEATURE_FIELD_COUNT:
        raise SnapshotInvalid(
            f"expected {FEATURE_FIELD_COUNT} features, got {len(features)}"
        )
    parts: list[str] = []
    for value in features:
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise SnapshotInvalid(f"non-numeric feature: {value!r}") from exc
        if not math.isfinite(number):
            raise SnapshotInvalid(f"nonfinite feature: {value!r}")
        if number == 0.0:
            number = 0.0  # normalize -0.0 so text bytes are stable
        parts.append(f"{number:.{decimals}f}")
    return ",".join(parts)


def text_digest(text: str) -> str:
    """Return the SHA-256 of the exact ASCII text bytes."""
    return sha256(text.encode("ascii")).hexdigest()


@dataclass(frozen=True)
class Interval:
    """One inclusive decision-session interval."""

    role: IntervalRole
    start: date
    end: date

    def __post_init__(self) -> None:
        if self.start > self.end:
            raise ProtocolError(
                f"interval {self.role.value!r} start {self.start} after end {self.end}"
            )

    def contains(self, day: date) -> bool:
        """True when ``day`` is inside this inclusive interval."""
        return self.start <= day <= self.end

    def to_mapping(self) -> dict[str, str]:
        return {"start": self.start.isoformat(), "end": self.end.isoformat()}


@dataclass(frozen=True)
class Snapshot:
    """A validated, features-only causal snapshot for one source session.

    ``features`` is the frozen twelve-value vector.  ``source_available_at`` is
    the calendar close of ``source_session`` in UTC, which establishes that the
    input existed before the decision session.
    """

    symbol: str
    source_session: date
    decision_session: date
    source_available_at: datetime
    features: tuple[float, ...]

    def __post_init__(self) -> None:
        if len(self.features) != FEATURE_FIELD_COUNT:
            raise SnapshotInvalid(
                f"snapshot {self.symbol} has {len(self.features)} features"
            )
        if not all(finite(value) for value in self.features):
            raise SnapshotInvalid(f"snapshot {self.symbol} has a nonfinite feature")

    @property
    def text(self) -> str:
        """Deterministic ASCII model text for this snapshot's features."""
        return serialize_features(self.features)

    @property
    def text_digest(self) -> str:
        """SHA-256 of the exact serialized text bytes."""
        return text_digest(self.text)


@dataclass(frozen=True)
class Outcome:
    """The forward-return target for one symbol and decision session.

    ``r_net`` uses entry at ``decision_session`` 10:00 New York and exit at the
    following exchange session 10:00 New York, both from the back-adjusted
    hourly archive.  ``valid`` is false whenever a price, session, interval, or
    feature requirement is unmet; ``reason`` then names the failure.
    """

    symbol: str
    source_session: date | None
    decision_session: date
    entry_session: date | None
    exit_session: date | None
    entry_price: float | None
    exit_price: float | None
    r_net: float | None
    y: int | None
    features_complete: bool
    valid: bool
    reason: str | None


@dataclass(frozen=True)
class EligibilityRow:
    """One row of the all-eligible daily observation mask.

    The mask is computed without any Laya inference: a row is eligible when its
    features are complete and its forward-return target is valid.
    """

    symbol: str
    decision_session: date
    role: IntervalRole
    features_complete: bool
    target_valid: bool

    @property
    def eligible(self) -> bool:
        """True when both the features and the target are present."""
        return self.features_complete and self.target_valid


def all_eligible_mask(rows: Iterable[EligibilityRow]) -> tuple[EligibilityRow, ...]:
    """Return only the eligible rows of an observation mask, order preserved."""
    return tuple(row for row in rows if row.eligible)


def summarize_eligibility(
    rows: Sequence[EligibilityRow],
) -> dict[str, dict[str, int]]:
    """Compact per-role counts of attempted, feature-complete, target-valid rows."""
    summary: dict[str, dict[str, int]] = {}
    for row in rows:
        bucket = summary.setdefault(
            row.role.value,
            {"attempted": 0, "features_complete": 0, "target_valid": 0, "eligible": 0},
        )
        bucket["attempted"] += 1
        bucket["features_complete"] += int(row.features_complete)
        bucket["target_valid"] += int(row.target_valid)
        bucket["eligible"] += int(row.eligible)
    return summary


@dataclass(frozen=True)
class Protocol:
    """The immutable research protocol frozen for this study."""

    protocol_version: str
    schema_version: str
    universe_keyword: str
    feature_fields: tuple[str, ...]
    trend_sma: int
    return_period: int
    liquidity_period: int
    fee_per_side: float
    cost_bps_per_side: float
    entry_hour: int
    exit_hour: int
    calendar_requested: str
    calendar_resolved: str
    calendar_version: str
    intervals: Mapping[IntervalRole, Interval]
    export_roles: tuple[IntervalRole, ...]
    question: str
    seed: int
    serialization_decimals: int
    data_query_start: date
    data_query_end: date
    raw: Mapping[str, Any]

    def interval(self, role: IntervalRole) -> Interval:
        """Return the frozen inclusive interval for ``role``."""
        try:
            return self.intervals[role]
        except KeyError as exc:  # pragma: no cover - guarded by from_mapping
            raise ProtocolError(f"protocol has no interval for {role.value!r}") from exc

    def to_mapping(self) -> dict[str, Any]:
        """Return a deep, JSON-serializable copy of the frozen protocol."""
        return json.loads(json.dumps(self.raw))

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> "Protocol":
        """Parse and validate a protocol mapping, failing closed on any defect."""
        if not isinstance(payload, Mapping):
            raise ProtocolError("protocol must be a JSON object")
        feature_fields = tuple(payload.get("feature_fields", ()))
        if feature_fields != FEATURE_FIELDS:
            raise ProtocolError(
                "protocol feature_fields must equal the frozen twelve-field order"
            )
        params = payload.get("feature_parameters")
        if not isinstance(params, Mapping):
            raise ProtocolError("protocol feature_parameters must be an object")
        try:
            trend_sma = int(params["trend_sma"])
            return_period = int(params["return_period"])
            liquidity_period = int(params["liquidity_period"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ProtocolError("feature_parameters must define integer lookbacks") from exc

        fee_per_side = float(payload.get("fee_per_side", float("nan")))
        if not finite(fee_per_side) or not 0.0 <= fee_per_side < 1.0:
            raise ProtocolError("fee_per_side must be finite in [0, 1)")

        calendar = payload.get("calendar")
        if not isinstance(calendar, Mapping):
            raise ProtocolError("protocol calendar must be an object")

        raw_intervals = payload.get("intervals")
        if not isinstance(raw_intervals, Mapping):
            raise ProtocolError("protocol intervals must be an object")
        intervals: dict[IntervalRole, Interval] = {}
        for role in IntervalRole:
            entry = raw_intervals.get(role.value)
            if not isinstance(entry, Mapping):
                raise ProtocolError(f"protocol is missing the {role.value!r} interval")
            intervals[role] = Interval(
                role=role,
                start=_as_date(entry.get("start"), field=f"intervals.{role.value}.start"),
                end=_as_date(entry.get("end"), field=f"intervals.{role.value}.end"),
            )

        export_roles = tuple(IntervalRole(name) for name in payload.get("export_roles", ()))
        if export_roles != EXPORTABLE_ROLES:
            raise ProtocolError(
                "protocol export_roles must be exactly train/calibration/gate"
            )
        for forbidden in FORBIDDEN_EXPORT_ROLES:
            if forbidden in export_roles:
                raise ProtocolError(
                    f"final holdout role {forbidden.value!r} may not be exportable"
                )

        bounds = payload.get("data_query_bounds")
        if not isinstance(bounds, Mapping):
            raise ProtocolError("protocol data_query_bounds must be an object")

        decimals = int(payload.get("serialization", {}).get("decimals", SNAPSHOT_DECIMALS))
        if not 0 <= decimals <= 12:
            raise ProtocolError("serialization.decimals must be in [0, 12]")

        question = str(payload.get("question", "")).strip()
        if not question:
            raise ProtocolError("protocol question must be a non-empty string")

        for hour_field, hour in (("entry_hour", payload.get("entry_hour")), ("exit_hour", payload.get("exit_hour"))):
            if not isinstance(hour, int) or isinstance(hour, bool) or not 0 <= hour <= 23:
                raise ProtocolError(f"protocol {hour_field} must be an hour in [0, 23]")

        return cls(
            protocol_version=str(payload.get("protocol_version", "")),
            schema_version=str(payload.get("schema_version", SCHEMA_VERSION)),
            universe_keyword=str(payload.get("universe_keyword", "")),
            feature_fields=feature_fields,
            trend_sma=trend_sma,
            return_period=return_period,
            liquidity_period=liquidity_period,
            fee_per_side=fee_per_side,
            cost_bps_per_side=float(payload.get("cost_bps_per_side", fee_per_side * 10_000.0)),
            entry_hour=int(payload["entry_hour"]),
            exit_hour=int(payload["exit_hour"]),
            calendar_requested=str(calendar.get("requested", "")),
            calendar_resolved=str(calendar.get("resolved", "")),
            calendar_version=str(calendar.get("version", "")),
            intervals=intervals,
            export_roles=export_roles,
            question=question,
            seed=int(payload.get("seed", DEFAULT_SEED)),
            serialization_decimals=decimals,
            data_query_start=_as_date(bounds.get("start"), field="data_query_bounds.start"),
            data_query_end=_as_date(bounds.get("end_inclusive"), field="data_query_bounds.end_inclusive"),
            raw=payload,
        )


def load_protocol(path: str | Path) -> Protocol:
    """Load and validate a protocol JSON file from disk."""
    text = Path(path).read_text(encoding="utf-8")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ProtocolError(f"protocol JSON is invalid: {exc}") from exc
    return Protocol.from_mapping(payload)


def assert_export_roles(requested: Sequence[str]) -> tuple[IntervalRole, ...]:
    """Validate requested export role names.

    The final retrospective holdout and any unknown role are rejected, so Task
    0a cannot leak a later interval even if a caller asks for it.
    """
    if not requested:
        raise ProtocolError("at least one export role is required")
    roles: list[IntervalRole] = []
    for name in requested:
        try:
            role = IntervalRole(str(name))
        except ValueError as exc:
            raise ProtocolError(f"unknown export role: {name!r}") from exc
        if role in FORBIDDEN_EXPORT_ROLES:
            raise ProtocolError(
                f"role {role.value!r} is forbidden in Task 0a; the final holdout "
                "interval is deliberately excluded"
            )
        roles.append(role)
    return tuple(roles)
