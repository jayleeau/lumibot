"""Versioned audit schemas, atomic storage, and event-stream serialization.

This module is the strategy-owned persistence contract described by
``plans/fix-3-persist-parity-state.md``.  It has no dependency on LumiBot's
broker layer, the network, or credentials:

* decision snapshots are a versioned, validated JSON schema;
* runtime state is a compact, operational checkpoint (never a copy of the
  whole journal and never raw ``Order`` objects);
* every existing in-memory strategy stream is serialized through one common
  helper so native backtest artifacts and live artifacts cannot drift;
* state/session writes are atomic (temp file + ``fsync`` + ``os.replace``).

Missing evidence is preserved as missing, never backfilled with a later value.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo

DECISION_SCHEMA_VERSION = 1
EVENT_SCHEMA_VERSION = 2
STATE_SCHEMA_VERSION = 2
# Backwards-compatible alias: the historical name referred to decision snapshots.
SCHEMA_VERSION = DECISION_SCHEMA_VERSION

# Streams that make up a serialized strategy event payload.  The attribute name
# on the strategy maps to the key in the emitted payload.
_EVENT_STREAMS: tuple[tuple[str, str], ...] = (
    ("_journal", "journal"),
    ("_risk_cap_events", "risk_cap_events"),
    ("_lifecycle_trace", "lifecycle_trace"),
    ("_stop_gap_events", "stop_gap_events"),
    ("_entry_edge_events", "entry_edge_events"),
    ("_rejections", "rejections"),
    ("_diag", "diag"),
    ("_risk_off_transitions", "risk_off_transitions"),
    ("_deferred_rebalance_events", "deferred_rebalance_events"),
    ("_session_end_events", "session_end_events"),
)

_IDENTITY_KEYS = (
    "strategy_name",
    "catalog_id",
    "implementation_revision",
    "resolved_parameters_hash",
    "feature_hash",
)

_ORDER_LIFECYCLE_EVENTS = frozenset({
    "intent_created",
    "order_submitted",
    "order_canceled",
    "order_rejected",
    "partial_fill",
    "final_fill",
    "exit_fill",
    "protective_order_submitted",
    "virtual_stop_submitted",
    "virtual_stop_filled",
})

_TERMINAL_LIFECYCLE_EVENTS = frozenset({
    "final_fill",
    "exit_fill",
    "order_canceled",
    "order_rejected",
})


class AuditError(Exception):
    """Raised for malformed, unsupported, or corrupt audit artifacts."""


class AuditValidationError(AuditError):
    """A schema violation with the exact offending field path."""

    def __init__(self, field: str, message: str) -> None:
        super().__init__(f"{field}: {message}")
        self.field = field
        self.message = message


class StateIdentityError(AuditError):
    """Raised when a persisted checkpoint does not match the running strategy."""


# --- JSON helpers -------------------------------------------------------------

def _json_default(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, set):
        return sorted(value, key=str)
    if isinstance(value, tuple):
        return list(value)
    # numpy scalars / pandas timestamps without importing numpy here.
    for attribute in ("item", "isoformat"):
        method = getattr(value, attribute, None)
        if callable(method):
            try:
                return method()
            except Exception:
                continue
    return str(value)


def _plain(value: Any) -> Any:
    """Return a JSON-safe copy of ``value`` without mutating the original."""
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted((_plain(item) for item in value), key=str)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if hasattr(value, "isoformat") and not isinstance(value, (dict, list)):
        try:
            return value.isoformat()
        except Exception:
            pass
    if hasattr(value, "item"):
        try:
            return _plain(value.item())
        except Exception:
            pass
    return str(value)


def _finite(value: Any) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _nonempty_str(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


# --- decision snapshot --------------------------------------------------------

_REQUIRED_SECTIONS = (
    "identity",
    "time",
    "selection",
    "allocation",
    "account",
    "book",
    "planned_orders",
    "parameters",
)


def build_decision_snapshot(
    *,
    identity: Mapping[str, Any],
    time: Mapping[str, Any],
    selection: Mapping[str, Any],
    allocation: Mapping[str, Any],
    account: Mapping[str, Any],
    book: Mapping[str, Any],
    planned_orders: Iterable[Mapping[str, Any]],
    parameters: Mapping[str, Any],
    fills: Iterable[Mapping[str, Any]] | None = None,
    protective_stop: Mapping[str, Any] | None = None,
    outcome: str = "executed",
    outcome_reason: str | None = None,
) -> dict[str, Any]:
    """Assemble a versioned decision snapshot and validate it strictly."""
    plain_identity = _plain(dict(identity))
    snapshot: dict[str, Any] = {
        "schema_version": DECISION_SCHEMA_VERSION,
        "decision_id": plain_identity.get("decision_id"),
        "outcome": str(outcome),
        "outcome_reason": outcome_reason,
        "identity": plain_identity,
        "time": _plain(dict(time)),
        "selection": _plain(dict(selection)),
        "allocation": _plain(dict(allocation)),
        "account": _plain(dict(account)),
        "book": _plain(dict(book)),
        "planned_orders": _plain(list(planned_orders)),
        "parameters": _plain(dict(parameters)),
        "fills": _plain(list(fills or [])),
        "protective_stop": _plain(dict(protective_stop)) if protective_stop is not None else None,
    }
    validate_decision_snapshot(snapshot)
    return snapshot


def encode_decision_snapshot(snapshot: Mapping[str, Any]) -> str:
    """Serialize a decision snapshot to a canonical JSON string."""
    validate_decision_snapshot(snapshot)
    return json.dumps(snapshot, sort_keys=False, default=_json_default)


def decode_decision_snapshot(encoded: str) -> dict[str, Any]:
    """Deserialize and validate a decision snapshot JSON string."""
    try:
        snapshot = json.loads(encoded)
    except (TypeError, ValueError) as error:
        raise AuditError(f"invalid decision snapshot JSON: {error}") from error
    if not isinstance(snapshot, dict):
        raise AuditError("decision snapshot must decode to an object")
    validate_decision_snapshot(snapshot)
    return snapshot


def _require_mapping(snapshot: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = snapshot.get(key)
    if not isinstance(value, Mapping):
        raise AuditValidationError(key, "missing or not an object")
    return value


def validate_decision_snapshot(snapshot: Mapping[str, Any]) -> None:
    """Validate a decision snapshot, raising with the first bad field path."""
    if not isinstance(snapshot, Mapping):
        raise AuditValidationError("schema_version", "snapshot must be an object")
    version = snapshot.get("schema_version")
    if not _is_int(version):
        raise AuditValidationError("schema_version", "version must be an integer")
    if version != DECISION_SCHEMA_VERSION:
        raise AuditValidationError(
            "schema_version", f"unsupported version {version!r}; expected {DECISION_SCHEMA_VERSION}"
        )

    for section in _REQUIRED_SECTIONS:
        if section not in snapshot:
            raise AuditValidationError(section, "required section is missing")

    identity = _require_mapping(snapshot, "identity")
    for key in _IDENTITY_KEYS:
        if not identity.get(key):
            raise AuditValidationError(f"identity.{key}", "required identity value is missing")
    decision_id = snapshot.get("decision_id") or identity.get("decision_id")
    if not _nonempty_str(decision_id):
        raise AuditValidationError("decision_id", "required decision identity is missing")

    outcome = str(snapshot.get("outcome") or "executed")
    if outcome != "executed":
        # Non-trading outcomes deliberately carry no fabricated allocation inputs.
        # They still require an explicit reason and identity.
        if not _nonempty_str(snapshot.get("outcome_reason")):
            raise AuditValidationError(
                "outcome_reason", "required for a non-executed decision outcome"
            )
        return

    time = _require_mapping(snapshot, "time")
    timezone_name = time.get("timezone")
    if not _nonempty_str(timezone_name):
        raise AuditValidationError("time.timezone", "required IANA timezone is missing")
    try:
        ZoneInfo(str(timezone_name))
    except Exception as error:
        raise AuditValidationError("time.timezone", f"unknown timezone {timezone_name!r}") from error
    if not _nonempty_str(time.get("logical_rebalance_time")):
        raise AuditValidationError("time.logical_rebalance_time", "required logical time is missing")

    parameters = _require_mapping(snapshot, "parameters")

    selection = _require_mapping(snapshot, "selection")
    for key in (
        "rank_input_rows",
        "rank_scores",
        "ordinal_ranks",
        "ranked_symbols",
        "held_symbols",
        "mandatory_held_symbols",
        "selected_symbols",
    ):
        if key not in selection:
            raise AuditValidationError(f"selection.{key}", "required selection field is missing")
    for key in ("rank_input_rows", "rank_scores", "ordinal_ranks"):
        if not isinstance(selection.get(key), Mapping) or not selection.get(key):
            raise AuditValidationError(f"selection.{key}", "must be a non-empty object")
    for key in ("held_symbols", "mandatory_held_symbols", "ranked_symbols", "selected_symbols"):
        if not isinstance(selection.get(key), list):
            raise AuditValidationError(f"selection.{key}", "must be a list")
    if parameters.get("exposure_group_limit") is not None:
        groups = selection.get("exposure_groups")
        if not isinstance(groups, Mapping) or not groups:
            raise AuditValidationError(
                "selection.exposure_groups", "required when the exposure screen is enabled"
            )
    if parameters.get("correlation_screen") is not None:
        pairs = selection.get("pairwise_correlations")
        if not isinstance(pairs, Mapping) or not pairs:
            raise AuditValidationError(
                "selection.pairwise_correlations",
                "required when the correlation screen is enabled",
            )

    allocation = _require_mapping(snapshot, "allocation")

    atr = allocation.get("event_time_atr")
    if not isinstance(atr, Mapping) or not atr:
        raise AuditValidationError("allocation.event_time_atr", "missing ATR map")
    for symbol, value in atr.items():
        if not _finite(value) or float(value) <= 0.0:
            raise AuditValidationError(
                f"allocation.event_time_atr.{symbol}", "ATR must be a positive finite number"
            )

    prices = allocation.get("executable_prices")
    if not isinstance(prices, Mapping) or not prices:
        raise AuditValidationError("allocation.executable_prices", "missing price map")
    for symbol, value in prices.items():
        if not _finite(value) or float(value) <= 0.0:
            raise AuditValidationError(
                f"allocation.executable_prices.{symbol}", "price must be a positive finite number"
            )

    full_snapshot = allocation.get("full_live_price_snapshot")
    if not isinstance(full_snapshot, Mapping) or not full_snapshot:
        raise AuditValidationError(
            "allocation.full_live_price_snapshot", "missing full quote snapshot"
        )
    volatility = allocation.get("volatility_inputs")
    if not isinstance(volatility, Mapping):
        raise AuditValidationError("allocation.volatility_inputs", "missing volatility inputs")

    for stage in (
        "pre_risk_contribution_cap_weights",
        "post_risk_contribution_cap_weights",
        "final_weights_after_leveraged_cap",
    ):
        if not isinstance(allocation.get(stage), Mapping):
            raise AuditValidationError(f"allocation.{stage}", "required weight stage is missing")

    matrix = allocation.get("covariance_matrix")
    mode = str(parameters.get("weight_mode"))
    if mode == "vol-target":
        if not isinstance(matrix, list) or not matrix:
            raise AuditValidationError("allocation.covariance_matrix", "missing covariance matrix")
        _validate_square_matrix(allocation, matrix)
        for field in (
            "base_forecast_volatility",
            "pre_cap_forecast_volatility",
            "post_cap_forecast_volatility",
        ):
            if not _finite(allocation.get(field)) or float(allocation[field]) <= 0.0:
                raise AuditValidationError(
                    f"allocation.{field}",
                    "vol-target requires a positive finite forecast stage",
                )
    elif matrix is not None:
        if not isinstance(matrix, list) or not matrix:
            raise AuditValidationError("allocation.covariance_matrix", "missing covariance matrix")
        _validate_square_matrix(allocation, matrix)
    elif not _nonempty_str(allocation.get("covariance_not_required")):
        raise AuditValidationError(
            "allocation.covariance_not_required",
            "explicit reason required when covariance is not recorded",
        )

    for symbol, score in (allocation.get("final_weights_after_leveraged_cap") or {}).items():
        if not _finite(score):
            raise AuditValidationError(
                f"allocation.final_weights_after_leveraged_cap.{symbol}",
                "weight must be a finite number",
            )

    account = _require_mapping(snapshot, "account")
    if "portfolio_value" not in account or not _finite(account.get("portfolio_value")):
        raise AuditValidationError("account.portfolio_value", "equity must be a finite number")

    planned = snapshot.get("planned_orders")
    if not isinstance(planned, list):
        raise AuditValidationError("planned_orders", "must be a list")
    for index, order in enumerate(planned):
        if not isinstance(order, Mapping):
            raise AuditValidationError(f"planned_orders.{index}", "must be an object")
        for key in ("symbol", "side", "requested_quantity", "intended_notional",
                    "budget_before", "budget_after"):
            if key not in order:
                raise AuditValidationError(f"planned_orders.{index}.{key}", "required order field")
        if not _finite(order.get("requested_quantity")) or float(order["requested_quantity"]) < 0.0:
            raise AuditValidationError(
                f"planned_orders.{index}.requested_quantity", "must be a nonnegative number"
            )

    protective = snapshot.get("protective_stop")
    if protective is not None:
        if not isinstance(protective, Mapping):
            raise AuditValidationError("protective_stop", "must be an object or null")
        if protective.get("expected"):
            if not _finite(protective.get("quantity")) or float(protective["quantity"]) <= 0.0:
                raise AuditValidationError("protective_stop.quantity", "must be a positive number")
            if not _finite(protective.get("level")):
                raise AuditValidationError("protective_stop.level", "must be finite")


def _validate_square_matrix(allocation: Mapping[str, Any], matrix: list) -> None:
    columns = allocation.get("covariance_symbols")
    size = len(columns) if isinstance(columns, list) else len(matrix)
    if len(matrix) != size or any(
        not isinstance(row, list) or len(row) != size for row in matrix
    ):
        raise AuditValidationError(
            "allocation.covariance_matrix", f"matrix must be square ({size}x{size})"
        )
    for row in matrix:
        for value in row:
            if not _finite(value):
                raise AuditValidationError(
                    "allocation.covariance_matrix", "matrix values must be finite numbers"
                )


# --- event-stream serialization ----------------------------------------------

def _event_identity(event: Mapping[str, Any]) -> str:
    keys = (
        "event", "intent_id", "decision_id", "symbol", "side", "order_id",
        "broker_order_id", "fill_event_quantity", "cumulative_filled_quantity",
        "quantity", "level", "reference", "reason", "day", "hour",
        "event_time", "event_sequence",
    )
    canonical = [str(event.get(key)) for key in keys]
    digest = hashlib.sha1("\u0000".join(canonical).encode("utf-8")).hexdigest()
    return digest[:20]


def _with_event_id(event: Any) -> dict[str, Any]:
    if not isinstance(event, Mapping):
        event = {"event": "value", "value": event}
    copy = _plain(dict(event))
    copy.setdefault("event_id", _event_identity(copy))
    return copy


def build_strategy_event_payload(
    strategy: Any, *, session: str | None = None
) -> dict[str, Any]:
    """Serialize every existing in-memory strategy stream through one schema."""
    streams: dict[str, list[dict[str, Any]]] = {}
    for attribute, key in _EVENT_STREAMS:
        raw = getattr(strategy, attribute, None) or []
        streams[key] = [_with_event_id(event) for event in raw]

    decisions: list[dict[str, Any]] = []
    for snapshot in getattr(strategy, "_decision_snapshots", None) or []:
        decisions.append(_plain(dict(snapshot)))

    terminal_intents: set[str] = set()
    for event in streams.get("lifecycle_trace", ()):
        if event.get("event") in _TERMINAL_LIFECYCLE_EVENTS and event.get("intent_id"):
            terminal_intents.add(str(event["intent_id"]))
    open_orders: list[dict[str, Any]] = []
    for broker_id, meta in (getattr(strategy, "_order_index", {}) or {}).items():
        if not isinstance(meta, Mapping):
            continue
        intent_id = str(meta.get("intent_id") or "")
        if intent_id and intent_id not in terminal_intents:
            entry = _plain(dict(meta))
            entry.setdefault("local_order_id", str(broker_id))
            entry.setdefault("broker_order_id", str(broker_id))
            open_orders.append(entry)

    payload: dict[str, Any] = {
        "schema_version": EVENT_SCHEMA_VERSION,
        "kind": "strategy_events",
        "session": session,
        "strategy_name": getattr(strategy, "_strategy_name", None),
        "identity": _plain(_strategy_identity(strategy)),
        "event_sequence": int(getattr(strategy, "_event_sequence", 0) or 0),
        "resolved_parameters": _plain(dict(getattr(strategy, "_params", {}) or {})),
        "decisions": decisions,
        "open_orders": open_orders,
        "last_decision_id": getattr(strategy, "_last_decision_id", None),
        **streams,
    }
    return payload


def _strategy_identity(strategy: Any) -> dict[str, Any]:
    base = getattr(strategy, "_audit_identity", None)
    if isinstance(base, Mapping) and base:
        return dict(base)
    return {
        key: getattr(strategy, f"_{key}", None) for key in _IDENTITY_KEYS
    }


def validate_strategy_event_payload(payload: Mapping[str, Any]) -> None:
    """Validate a full session/event payload, raising the first bad field path."""
    if not isinstance(payload, Mapping):
        raise AuditValidationError("payload", "must be an object")
    version = payload.get("schema_version")
    if not _is_int(version):
        raise AuditValidationError("schema_version", "version must be an integer")
    if version != EVENT_SCHEMA_VERSION:
        raise AuditValidationError(
            "schema_version", f"unsupported version {version!r}; expected {EVENT_SCHEMA_VERSION}"
        )
    if payload.get("kind") != "strategy_events":
        raise AuditValidationError("kind", "must be 'strategy_events'")
    identity = payload.get("identity")
    if not isinstance(identity, Mapping) or not identity.get("strategy_name"):
        raise AuditValidationError("identity.strategy_name", "required identity value is missing")
    if not _nonempty_str(payload.get("session")) and not isinstance(payload.get("window"), Mapping):
        raise AuditValidationError("session", "session or window metadata is required")

    decisions = payload.get("decisions")
    if not isinstance(decisions, list):
        raise AuditValidationError("decisions", "must be a list")
    decision_ids: set[str] = set()
    for index, decision in enumerate(decisions):
        try:
            validate_decision_snapshot(decision)
        except AuditValidationError as error:
            raise AuditValidationError(f"decisions.{index}.{error.field}", error.message) from error
        decision_ids.add(str(decision.get("decision_id") or decision["identity"].get("decision_id")))

    lifecycle = payload.get("lifecycle_trace") or []
    if not isinstance(lifecycle, list):
        raise AuditValidationError("lifecycle_trace", "must be a list")
    for index, event in enumerate(lifecycle):
        if not isinstance(event, Mapping):
            raise AuditValidationError(f"lifecycle_trace.{index}", "must be an object")
        if not _nonempty_str(event.get("event_time")):
            raise AuditValidationError(f"lifecycle_trace.{index}.event_time", "required event time")
        if not _is_int(event.get("event_sequence")):
            raise AuditValidationError(
                f"lifecycle_trace.{index}.event_sequence", "required event sequence"
            )
        if event.get("event") in _ORDER_LIFECYCLE_EVENTS:
            for key in ("decision_id", "intent_id", "symbol", "side"):
                if not _nonempty_str(event.get(key)):
                    raise AuditValidationError(
                        f"lifecycle_trace.{index}.{key}", "required order-lifecycle field"
                    )
            if not (
                event.get("order_id") or event.get("broker_order_id") or event.get("local_order_id")
            ):
                raise AuditValidationError(
                    f"lifecycle_trace.{index}.order_id", "required order identifier"
                )
            decision_id = str(event.get("decision_id"))
            if decision_id not in decision_ids:
                raise AuditValidationError(
                    f"lifecycle_trace.{index}.decision_id",
                    f"no decision snapshot resolves {decision_id}",
                )


def merge_session_events(
    existing: Mapping[str, Any] | None, new: Mapping[str, Any] | None
) -> dict[str, Any]:
    """Merge two session payloads, keeping one copy of each stable event id."""
    existing = dict(existing or {})
    new = dict(new or {})
    merged: dict[str, Any] = dict(existing)
    merged["schema_version"] = EVENT_SCHEMA_VERSION
    merged["kind"] = new.get("kind", existing.get("kind", "strategy_events"))
    for key in ("strategy_name", "session", "identity", "event_sequence", "last_decision_id",
                "resolved_parameters"):
        if new.get(key) is not None:
            merged[key] = new[key]

    for _attribute, key in _EVENT_STREAMS:
        seen: set[str] = set()
        combined: list[dict[str, Any]] = []
        for source in (existing.get(key, []), new.get(key, [])):
            for event in source or []:
                event = _with_event_id(event)
                event_id = str(event.get("event_id"))
                if event_id in seen:
                    continue
                seen.add(event_id)
                combined.append(event)
        merged[key] = combined

    merged_decisions: dict[str, dict[str, Any]] = {}
    for source in (existing.get("decisions", []), new.get("decisions", [])):
        for decision in source:
            if not isinstance(decision, Mapping):
                continue
            decision_id = decision.get("decision_id")
            if not decision_id and isinstance(decision.get("identity"), Mapping):
                decision_id = decision["identity"].get("decision_id")
            if not decision_id:
                continue
            decision_id = str(decision_id)
            if decision_id in merged_decisions:
                if merged_decisions[decision_id] != _plain(dict(decision)):
                    raise AuditError(
                        f"conflicting decision snapshot for decision_id {decision_id}"
                    )
                continue
            merged_decisions[decision_id] = _plain(dict(decision))
    merged["decisions"] = list(merged_decisions.values())

    open_orders: dict[str, dict[str, Any]] = {}
    for source in (existing.get("open_orders", []), new.get("open_orders", [])):
        for entry in source:
            if not isinstance(entry, Mapping):
                continue
            key = str(entry.get("broker_order_id") or entry.get("local_order_id") or "")
            if key:
                open_orders[key] = _plain(dict(entry))
    merged["open_orders"] = list(open_orders.values())
    return merged


# --- runtime state ------------------------------------------------------------

_DATE_KEYS = ("signal_day", "entry_session")
_BUY_LINEAGE_KEYS = (
    "intent_id", "decision_id", "symbol", "side", "local_order_id",
    "broker_order_id", "quantity", "requested_quantity", "atr",
    "event_time_atr", "entry_price", "cumulative_filled_quantity",
    "last_fill_event_quantity", "reason",
)
_SELL_LINEAGE_KEYS = (
    "intent_id", "decision_id", "symbol", "side", "local_order_id",
    "broker_order_id", "requested_quantity", "reason",
    "cumulative_filled_quantity", "last_fill_event_quantity",
)


def serialize_runtime_state(strategy: Any, *, reason: str = "checkpoint") -> dict[str, Any]:
    """Build the compact, broker-independent restart checkpoint for a strategy."""
    pending_sell_meta = dict(getattr(strategy, "_pending_sell_meta", {}) or {})
    pending_sells: dict[str, Any] = {}
    for symbol in sorted(getattr(strategy, "_pending_sells", None) or ()):
        meta = pending_sell_meta.get(symbol, {})
        entry = {key: _plain(meta.get(key)) for key in _SELL_LINEAGE_KEYS}
        entry.setdefault("symbol", str(symbol))
        entry.setdefault("side", "sell")
        if not entry.get("requested_quantity"):
            entry["requested_quantity"] = _plain(meta.get("requested_quantity"))
        pending_sells[str(symbol)] = entry

    risk_off_state = {
        "active": bool(getattr(strategy, "_risk_off_active", False)),
        "deadline_index": getattr(strategy, "_re_risk_deadline_index", None),
        "last_gate_session": _plain(getattr(strategy, "_last_risk_gate_session", None)),
    }

    state: dict[str, Any] = {
        "schema_version": STATE_SCHEMA_VERSION,
        **_strategy_identity(strategy),
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "checkpoint_reason": str(reason),
        "event_sequence": int(getattr(strategy, "_event_sequence", 0) or 0),
        "decision_sequence": int(getattr(strategy, "_decision_sequence", 0) or 0),
        "intent_sequence": int(getattr(strategy, "_intent_sequence", 0) or 0),
        "active_decision_id": getattr(strategy, "_active_decision_id", None),
        "last_decision_id": getattr(strategy, "_last_decision_id", None),
        "processed_decision_ids": sorted(
            str(item) for item in (getattr(strategy, "_processed_decision_ids", None) or set())
        ),
        "signal_day": _plain(getattr(strategy, "_signal_day", None)),
        "selected": [_plain(item) for item in (getattr(strategy, "_selected", ()) or ())],
        "ranks": _plain(dict(getattr(strategy, "_ranks", {}) or {})),
        "positions": {
            str(symbol): _plain(dict(position))
            for symbol, position in (getattr(strategy, "_positions", {}) or {}).items()
        },
        "pending_buys": {
            str(symbol): _serialize_pending(str(symbol), pending)
            for symbol, pending in (getattr(strategy, "_pending_buys", {}) or {}).items()
        },
        "pending_sells": pending_sells,
        "pending_sell_reason": _plain(dict(getattr(strategy, "_pending_sell_reason", {}) or {})),
        "stop_exit_context": _plain(dict(getattr(strategy, "_stop_exit_context", {}) or {})),
        "protective_orders": _serialize_protective(strategy),
        "cooldowns": {str(symbol): int(value) for symbol, value in (getattr(strategy, "_cooldowns", {}) or {}).items()},
        "risk_off_active": bool(getattr(strategy, "_risk_off_active", False)),
        "risk_off_state": risk_off_state,
        "risk_off_race_symbols": sorted(
            str(symbol) for symbol in (getattr(strategy, "_risk_off_race_symbols", None) or set())
        ),
        "deferred_rebalance": _plain(getattr(strategy, "_deferred_rebalance", None)),
        "last_quote_snapshot": _plain(
            _structured_quote_snapshot(getattr(strategy, "_last_quote_snapshot", None))
        ),
    }
    return state


def _structured_quote_snapshot(value: Any) -> Any:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        return value
    return _plain(dict(value))


def _serialize_pending(symbol: str, pending: Mapping[str, Any]) -> dict[str, Any]:
    serialized = {key: _plain(pending[key]) for key in _BUY_LINEAGE_KEYS if key in pending}
    serialized.setdefault("symbol", symbol)
    serialized.setdefault("side", "buy")
    # The key is required even before a broker acknowledgement assigns a
    # provider ID.  ``None`` means known-not-yet-assigned, not missing lineage.
    serialized.setdefault("broker_order_id", None)
    order = pending.get("order")
    if order is not None and not serialized.get("broker_order_id"):
        serialized["broker_order_id"] = str(getattr(order, "identifier", "") or "") or None
    if order is not None and not serialized.get("local_order_id"):
        serialized["local_order_id"] = str(getattr(order, "identifier", "") or "") or None
    return serialized


def _serialize_protective(strategy: Any) -> dict[str, Any]:
    protective = getattr(strategy, "_protective", {}) or {}
    meta_store = getattr(strategy, "_protective_meta", {}) or {}
    positions = getattr(strategy, "_positions", {}) or {}
    serialized: dict[str, Any] = {}
    for symbol, order in protective.items():
        state = positions.get(symbol, {})
        meta = meta_store.get(symbol, {}) if isinstance(meta_store, Mapping) else {}
        serialized[str(symbol)] = {
            "intent_id": meta.get("intent_id"),
            "decision_id": meta.get("decision_id"),
            "symbol": str(symbol),
            "side": "sell",
            "local_order_id": str(getattr(order, "identifier", "") or "") or meta.get("local_order_id"),
            "broker_order_id": str(getattr(order, "identifier", "") or "") or meta.get("broker_order_id"),
            "quantity": _plain(getattr(order, "quantity", None)),
            "level": _plain(state.get("protective_level")),
            "parent_entry_intent_id": meta.get("parent_entry_intent_id") or state.get("entry_intent_id"),
            "parent_decision_id": meta.get("decision_id") or state.get("entry_decision_id"),
        }
    return serialized


def _parse_day(value: Any) -> Any:
    if value is None or isinstance(value, date):
        return value
    try:
        return datetime.fromisoformat(str(value)).date()
    except ValueError:
        return value


def validate_runtime_state(state: Mapping[str, Any]) -> None:
    """Strictly validate a restart checkpoint, raising the first bad field path."""
    if not isinstance(state, Mapping):
        raise AuditValidationError("state", "must be an object")
    version = state.get("schema_version")
    if not _is_int(version):
        raise AuditValidationError("schema_version", "version must be an integer")
    if version != STATE_SCHEMA_VERSION:
        raise AuditValidationError(
            "schema_version", f"unsupported version {version!r}; expected {STATE_SCHEMA_VERSION}"
        )
    for key in _IDENTITY_KEYS:
        if not state.get(key):
            raise AuditValidationError(key, "required identity value is missing")

    saved_at = state.get("saved_at")
    if not _nonempty_str(saved_at):
        raise AuditValidationError("saved_at", "required saved_at is missing")
    try:
        parsed = datetime.fromisoformat(str(saved_at))
    except ValueError as error:
        raise AuditValidationError("saved_at", "must be an ISO timestamp") from error
    if parsed.tzinfo is None:
        raise AuditValidationError("saved_at", "must be timezone-aware")

    for key in ("event_sequence", "decision_sequence", "intent_sequence"):
        if not _is_int(state.get(key)) or int(state[key]) < 0:
            raise AuditValidationError(key, "must be a nonnegative integer")

    for key in ("active_decision_id", "last_decision_id"):
        if key not in state:
            raise AuditValidationError(key, "required field is missing")
        value = state.get(key)
        if value is not None and not _nonempty_str(value):
            raise AuditValidationError(key, "must be a string or null")

    processed = state.get("processed_decision_ids")
    if not isinstance(processed, list):
        raise AuditValidationError("processed_decision_ids", "must be a list")
    seen: set[str] = set()
    for item in processed:
        if not _nonempty_str(item):
            raise AuditValidationError("processed_decision_ids", "entries must be nonempty strings")
        if item in seen:
            raise AuditValidationError("processed_decision_ids", "entries must be unique")
        seen.add(item)

    if state.get("signal_day") is not None and not isinstance(state.get("signal_day"), (str, date)):
        raise AuditValidationError("signal_day", "must be a date or null")
    selected = state.get("selected")
    if not isinstance(selected, list):
        raise AuditValidationError("selected", "must be a list")
    ranks = state.get("ranks")
    if not isinstance(ranks, Mapping):
        raise AuditValidationError("ranks", "must be an object")
    for symbol, rank in ranks.items():
        if not _is_int(rank) or int(rank) <= 0:
            raise AuditValidationError(f"ranks.{symbol}", "rank must be a positive integer")

    positions = state.get("positions")
    if not isinstance(positions, Mapping):
        raise AuditValidationError("positions", "must be an object")
    for symbol, position in positions.items():
        if not isinstance(position, Mapping):
            raise AuditValidationError(f"positions.{symbol}", "must be an object")
        if not _finite(position.get("quantity")) or float(position["quantity"]) <= 0.0:
            raise AuditValidationError(f"positions.{symbol}.quantity", "must be a positive number")
        if position.get("legacy_unverifiable") is True:
            # Broker-only positions intentionally contain only broker-visible
            # facts.  Never invent an entry price, ATR, stop, or decision ID.
            continue
        if not _finite(position.get("entry_price")) or float(position["entry_price"]) <= 0.0:
            raise AuditValidationError(f"positions.{symbol}.entry_price", "must be a positive number")
        if not _finite(position.get("entry_atr")) or float(position["entry_atr"]) <= 0.0:
            raise AuditValidationError(f"positions.{symbol}.entry_atr", "must be a positive number")
        if not _finite(position.get("stop")):
            raise AuditValidationError(f"positions.{symbol}.stop", "must be finite")

    pending_buys = state.get("pending_buys")
    if not isinstance(pending_buys, Mapping):
        raise AuditValidationError("pending_buys", "must be an object")
    for symbol, pending in pending_buys.items():
        if not isinstance(pending, Mapping):
            raise AuditValidationError(f"pending_buys.{symbol}", "must be an object")
        for key in ("intent_id", "decision_id", "symbol", "side", "local_order_id", "reason"):
            if not _nonempty_str(pending.get(key)):
                raise AuditValidationError(f"pending_buys.{symbol}.{key}", "required lineage field")
        if pending.get("symbol") != symbol:
            raise AuditValidationError(f"pending_buys.{symbol}.symbol", "must match the map key")
        if pending.get("side") != "buy":
            raise AuditValidationError(f"pending_buys.{symbol}.side", "must be 'buy'")
        if "broker_order_id" not in pending or (
            pending["broker_order_id"] is not None
            and not _nonempty_str(pending["broker_order_id"])
        ):
            raise AuditValidationError(
                f"pending_buys.{symbol}.broker_order_id",
                "must be a nonempty string or null",
            )
        for key in ("quantity", "requested_quantity"):
            if not _finite(pending.get(key)) or float(pending[key]) <= 0.0:
                raise AuditValidationError(
                    f"pending_buys.{symbol}.{key}", "must be a positive number"
                )
        for key in ("atr", "event_time_atr"):
            if not _finite(pending.get(key)) or float(pending[key]) <= 0.0:
                raise AuditValidationError(
                    f"pending_buys.{symbol}.{key}", "must be a positive number"
                )
        for key in ("cumulative_filled_quantity", "last_fill_event_quantity"):
            if not _finite(pending.get(key)) or float(pending[key]) < 0.0:
                raise AuditValidationError(
                    f"pending_buys.{symbol}.{key}", "must be a nonnegative number"
                )
        if float(pending["cumulative_filled_quantity"]) > float(pending["requested_quantity"]):
            raise AuditValidationError(
                f"pending_buys.{symbol}.cumulative_filled_quantity",
                "cannot exceed requested_quantity",
            )

    pending_sells = state.get("pending_sells")
    if not isinstance(pending_sells, Mapping):
        raise AuditValidationError("pending_sells", "must be an object")
    for symbol, lineage in pending_sells.items():
        if not isinstance(lineage, Mapping):
            raise AuditValidationError(f"pending_sells.{symbol}", "must be an object")
        for key in ("intent_id", "decision_id", "symbol", "side", "local_order_id", "reason"):
            if not _nonempty_str(lineage.get(key)):
                raise AuditValidationError(f"pending_sells.{symbol}.{key}", "required lineage field")
        if lineage.get("symbol") != symbol:
            raise AuditValidationError(f"pending_sells.{symbol}.symbol", "must match the map key")
        if lineage.get("side") != "sell":
            raise AuditValidationError(f"pending_sells.{symbol}.side", "must be 'sell'")
        if "broker_order_id" not in lineage or (
            lineage["broker_order_id"] is not None
            and not _nonempty_str(lineage["broker_order_id"])
        ):
            raise AuditValidationError(
                f"pending_sells.{symbol}.broker_order_id",
                "must be a nonempty string or null",
            )
        if not _finite(lineage.get("requested_quantity")) or float(lineage["requested_quantity"]) <= 0.0:
            raise AuditValidationError(
                f"pending_sells.{symbol}.requested_quantity", "must be a positive number"
            )
        for key in ("cumulative_filled_quantity", "last_fill_event_quantity"):
            if not _finite(lineage.get(key)) or float(lineage[key]) < 0.0:
                raise AuditValidationError(
                    f"pending_sells.{symbol}.{key}", "must be a nonnegative number"
                )
        if float(lineage["cumulative_filled_quantity"]) > float(lineage["requested_quantity"]):
            raise AuditValidationError(
                f"pending_sells.{symbol}.cumulative_filled_quantity",
                "cannot exceed requested_quantity",
            )

    pending_sell_reason = state.get("pending_sell_reason")
    if not isinstance(pending_sell_reason, Mapping):
        raise AuditValidationError("pending_sell_reason", "must be an object")
    if set(pending_sell_reason) != set(pending_sells):
        raise AuditValidationError(
            "pending_sell_reason", "must contain exactly the pending-sell symbols"
        )
    for symbol, reason in pending_sell_reason.items():
        if not _nonempty_str(reason) or reason != pending_sells[symbol]["reason"]:
            raise AuditValidationError(
                f"pending_sell_reason.{symbol}", "must match pending-sell lineage"
            )

    if not isinstance(state.get("stop_exit_context"), Mapping):
        raise AuditValidationError("stop_exit_context", "must be an object")

    protective = state.get("protective_orders")
    if not isinstance(protective, Mapping):
        raise AuditValidationError("protective_orders", "must be an object")
    for symbol, meta in protective.items():
        if not isinstance(meta, Mapping):
            raise AuditValidationError(f"protective_orders.{symbol}", "must be an object")
        for key in (
            "intent_id", "decision_id", "symbol", "side", "local_order_id",
            "broker_order_id", "parent_entry_intent_id", "parent_decision_id",
        ):
            if not _nonempty_str(meta.get(key)):
                raise AuditValidationError(
                    f"protective_orders.{symbol}.{key}", "required lineage field"
                )
        if meta.get("symbol") != symbol:
            raise AuditValidationError(f"protective_orders.{symbol}.symbol", "must match the map key")
        if meta.get("side") != "sell":
            raise AuditValidationError(f"protective_orders.{symbol}.side", "must be 'sell'")
        if not _nonempty_str(meta.get("parent_entry_intent_id")):
            raise AuditValidationError(
                f"protective_orders.{symbol}.parent_entry_intent_id", "required lineage field"
            )
        if not _finite(meta.get("quantity")) or float(meta["quantity"]) <= 0.0:
            raise AuditValidationError(f"protective_orders.{symbol}.quantity", "must be positive")
        if not _finite(meta.get("level")):
            raise AuditValidationError(f"protective_orders.{symbol}.level", "must be finite")

    cooldowns = state.get("cooldowns")
    if not isinstance(cooldowns, Mapping):
        raise AuditValidationError("cooldowns", "must be an object")
    for symbol, value in cooldowns.items():
        if not _is_int(value):
            raise AuditValidationError(f"cooldowns.{symbol}", "must be an integer")

    risk_state = state.get("risk_off_state")
    if not isinstance(risk_state, Mapping):
        raise AuditValidationError("risk_off_state", "must be an object")
    if not isinstance(risk_state.get("active"), bool):
        raise AuditValidationError("risk_off_state.active", "must be a boolean")
    if not isinstance(state.get("risk_off_active"), bool):
        raise AuditValidationError("risk_off_active", "must be a boolean")
    if state["risk_off_active"] != risk_state["active"]:
        raise AuditValidationError(
            "risk_off_active", "must agree with risk_off_state.active"
        )
    deadline = risk_state.get("deadline_index")
    if deadline is not None and not _is_int(deadline):
        raise AuditValidationError("risk_off_state.deadline_index", "must be an integer or null")
    last_gate = risk_state.get("last_gate_session")
    if last_gate is not None and not isinstance(last_gate, (str, date)):
        raise AuditValidationError("risk_off_state.last_gate_session", "must be a date or null")
    race_symbols = state.get("risk_off_race_symbols")
    if not isinstance(race_symbols, list) or any(not _nonempty_str(item) for item in race_symbols):
        raise AuditValidationError(
            "risk_off_race_symbols", "must be a list of nonempty symbols"
        )

    deferred = state.get("deferred_rebalance")
    if "deferred_rebalance" not in state:
        raise AuditValidationError("deferred_rebalance", "required field is missing")
    if deferred is not None:
        if not isinstance(deferred, Mapping):
            raise AuditValidationError("deferred_rebalance", "must be an object or null")
        if deferred.get("kind") not in ("rebalance", "risk_off_flatten"):
            raise AuditValidationError("deferred_rebalance.kind", "must be a known deferred kind")
        if not _nonempty_str(deferred.get("decision_id")):
            raise AuditValidationError("deferred_rebalance.decision_id", "required lineage field")
        if deferred["decision_id"] not in seen:
            raise AuditValidationError(
                "deferred_rebalance.decision_id",
                "must identify a committed processed decision",
            )
        if not isinstance(deferred.get("submission_started"), bool):
            raise AuditValidationError(
                "deferred_rebalance.submission_started", "must be a boolean"
            )
        if deferred.get("kind") == "rebalance":
            for list_key in ("buys", "sells"):
                if not isinstance(deferred.get(list_key), list):
                    raise AuditValidationError(
                        f"deferred_rebalance.{list_key}", "required deferred plan list"
                    )

    quote = state.get("last_quote_snapshot")
    if "last_quote_snapshot" not in state:
        raise AuditValidationError("last_quote_snapshot", "required field is missing")
    if quote is not None:
        if not isinstance(quote, Mapping):
            raise AuditValidationError("last_quote_snapshot", "must be an object or null")
        if not _nonempty_str(quote.get("captured_at")):
            raise AuditValidationError("last_quote_snapshot.captured_at", "required capture time")
        prices = quote.get("prices")
        if not isinstance(prices, Mapping) or not prices:
            raise AuditValidationError("last_quote_snapshot.prices", "required price map")
        for symbol, value in prices.items():
            if not _finite(value):
                raise AuditValidationError(
                    f"last_quote_snapshot.prices.{symbol}", "must be a finite number"
                )


def deserialize_runtime_state(state: Mapping[str, Any]) -> dict[str, Any]:
    """Convert a validated checkpoint into in-memory strategy fields."""
    validate_runtime_state(state)

    positions = {
        str(symbol): dict(position)
        for symbol, position in state["positions"].items()
    }
    for position in positions.values():
        entry_session = position.get("entry_session")
        if isinstance(entry_session, str):
            position["entry_session"] = _parse_day(entry_session)

    pending_buys: dict[str, dict[str, Any]] = {}
    for symbol, pending in state["pending_buys"].items():
        restored = {key: pending[key] for key in _BUY_LINEAGE_KEYS if key in pending}
        restored["order"] = None
        pending_buys[str(symbol)] = restored

    pending_sell_meta: dict[str, dict[str, Any]] = {}
    for symbol, lineage in state["pending_sells"].items():
        restored = {key: lineage[key] for key in _SELL_LINEAGE_KEYS}
        restored["order"] = None
        pending_sell_meta[str(symbol)] = restored

    return {
        "event_sequence": int(state["event_sequence"]),
        "decision_sequence": int(state["decision_sequence"]),
        "intent_sequence": int(state["intent_sequence"]),
        "active_decision_id": state["active_decision_id"],
        "last_decision_id": state["last_decision_id"],
        "processed_decision_ids": {str(item) for item in state["processed_decision_ids"]},
        "signal_day": _parse_day(state["signal_day"]),
        "selected": tuple(state["selected"]),
        "ranks": {str(symbol): int(rank) for symbol, rank in state["ranks"].items()},
        "positions": positions,
        "pending_buys": pending_buys,
        "pending_sell_meta": pending_sell_meta,
        "pending_sells": {str(symbol) for symbol in state["pending_sells"]},
        "pending_sell_reason": dict(state["pending_sell_reason"]),
        "stop_exit_context": dict(state["stop_exit_context"]),
        "protective_orders": dict(state["protective_orders"]),
        "cooldowns": {str(symbol): int(value) for symbol, value in state["cooldowns"].items()},
        "risk_off_active": bool(state["risk_off_active"]),
        "risk_off_state": dict(state["risk_off_state"]),
        "risk_off_race_symbols": {str(symbol) for symbol in state["risk_off_race_symbols"]},
        "deferred_rebalance": state["deferred_rebalance"],
        "last_quote_snapshot": state["last_quote_snapshot"],
    }


# --- atomic storage -----------------------------------------------------------

class AtomicJsonStore:
    """Atomic, version-checked JSON storage for state and session artifacts."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    def read(self) -> dict[str, Any]:
        if not self.path.exists():
            raise AuditError(f"artifact not found: {self.path.name}")
        try:
            text = self.path.read_text(encoding="utf-8")
        except OSError as error:
            raise AuditError(f"cannot read artifact {self.path.name}: {error}") from error
        try:
            payload = json.loads(text)
        except (TypeError, ValueError) as error:
            raise AuditError(f"corrupt artifact {self.path.name}: {error}") from error
        if not isinstance(payload, dict):
            raise AuditError(f"artifact {self.path.name} must decode to an object")
        return payload

    def write(self, payload: Mapping[str, Any]) -> None:
        """Write a raw JSON payload atomically (JSON-syntax validated only)."""
        if not isinstance(payload, Mapping):
            raise AuditError("cannot persist a non-object payload")
        text = json.dumps(payload, indent=2, sort_keys=True, default=_json_default)
        # Round-trip validate before touching the destination.
        json.loads(text)
        self._atomic_replace(text)

    def write_state(self, state: Mapping[str, Any]) -> None:
        """Validate a restart checkpoint, then atomically write it."""
        validate_runtime_state(state)
        self._atomic_replace(json.dumps(state, indent=2, sort_keys=True, default=_json_default))

    def write_session(self, payload: Mapping[str, Any]) -> None:
        """Validate a session payload, then atomically write it."""
        validate_strategy_event_payload(payload)
        self._atomic_replace(json.dumps(payload, indent=2, sort_keys=True, default=_json_default))

    def _atomic_replace(self, text: str) -> None:
        parent = self.path.parent
        parent.mkdir(parents=True, exist_ok=True)
        descriptor, tmp_name = tempfile.mkstemp(
            prefix=f".{self.path.name}.", suffix=".tmp", dir=str(parent)
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(tmp_name, 0o600)
            os.replace(tmp_name, self.path)
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
        self._fsync_directory(parent)

    @staticmethod
    def _fsync_directory(directory: Path) -> None:
        try:
            descriptor = os.open(str(directory), os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(descriptor)
        except OSError:
            pass
        finally:
            os.close(descriptor)

    def read_state(self, *, expected_identity: Mapping[str, Any]) -> dict[str, Any]:
        state = self.read()
        validate_runtime_state(state)
        for key, expected in dict(expected_identity or {}).items():
            actual = state.get(key)
            if actual != expected:
                raise StateIdentityError(
                    f"{key}: expected {expected!r} but found {actual!r}"
                )
        return state

    def read_session(self) -> dict[str, Any]:
        payload = self.read()
        validate_strategy_event_payload(payload)
        return payload
