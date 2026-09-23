"""Deterministic pure-policy replay and native/live parity comparison.

The harness re-runs the *actual* production policy functions
(``rank_scores``, ``select_holdings``, ``target_weights``,
``cap_risk_contributions``, ``apply_leveraged_cap``) against the inputs recorded
in a decision snapshot.  It never fetches data or recalculates from a current
frame: a missing required input is reported as ``UNVERIFIABLE`` rather than
silently substituted.

This module is offline and has no broker, network, or credential dependency.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping

import numpy as np
import pandas as pd

from strategy_lab.experiment_universes import LEVERAGED_PRODUCTS
from strategy_lab.hts_policies import (
    apply_leveraged_cap,
    cap_risk_contributions,
    forecast_volatility,
    rank_scores,
    select_holdings,
    target_weights,
)


class UnverifiableError(Exception):
    """A required recorded input is missing; the decision cannot be replayed."""


class ParityMismatch(Exception):
    """A replayed value disagrees with the recorded decision."""

    def __init__(self, field: str, expected: Any, recorded: Any, message: str = "") -> None:
        detail = message or f"expected {expected!r}, recorded {recorded!r}"
        super().__init__(f"{field}: {detail}")
        self.field = field
        self.expected = expected
        self.recorded = recorded
        self.message = detail


@dataclass(frozen=True)
class ParityTolerances:
    """Fixed default tolerances for decision and PnL parity."""

    rtol: float = 1e-12
    atol: float = 1e-12
    stop_atol: float = 1e-8
    abs_return_tol: float = 1e-4
    rel_tol: float = 1e-3
    dollar_guard: float = 1.0
    quote_delay_seconds: float = 300.0


def _require(snapshot: Mapping[str, Any], path: str) -> Any:
    current: Any = snapshot
    for part in path.split("."):
        if not isinstance(current, Mapping) or part not in current:
            raise UnverifiableError(f"missing required input: {path}")
        current = current[part]
    if current is None:
        raise UnverifiableError(f"missing required input: {path}")
    return current


def _require_finite(snapshot: Mapping[str, Any], path: str) -> float:
    value = _require(snapshot, path)
    try:
        number = float(value)
    except (TypeError, ValueError) as error:
        raise UnverifiableError(f"non-numeric required input: {path}") from error
    if not math.isfinite(number):
        raise UnverifiableError(f"non-finite required input: {path}")
    return number


def _isclose(left: float, right: float, tolerances: ParityTolerances, *, atol: float | None = None) -> bool:
    return math.isclose(
        float(left), float(right),
        rel_tol=tolerances.rtol, abs_tol=tolerances.atol if atol is None else atol,
    )


def _compare_map(recorded: Mapping[str, Any], recomputed: Mapping[str, float],
                 path: str, tolerances: ParityTolerances) -> dict[str, Any] | None:
    for symbol, value in recorded.items():
        if symbol not in recomputed:
            return {
                "field": f"{path}.{symbol}",
                "expected": recomputed.get(symbol),
                "recorded": value,
                "message": "replayed policy no longer produces this symbol",
            }
        if not _isclose(recomputed[symbol], value, tolerances):
            return {
                "field": f"{path}.{symbol}",
                "expected": float(recomputed[symbol]),
                "recorded": float(value),
                "message": "replayed weight disagrees with recorded weight",
            }
    for symbol in recomputed:
        if symbol not in recorded:
            return {
                "field": f"{path}.{symbol}",
                "expected": float(recomputed[symbol]),
                "recorded": None,
                "message": "replayed policy produced an unrecorded symbol",
            }
    return None


def _replay_weights(snapshot: Mapping[str, Any], selected: list[str]) -> dict[str, Any]:
    parameters = _require(snapshot, "parameters")
    allocation = _require(snapshot, "allocation")
    atr = {symbol: float(value) for symbol, value in _require(snapshot, "allocation.event_time_atr").items()}
    price = {
        symbol: float(value)
        for symbol, value in _require(snapshot, "allocation.executable_prices").items()
    }
    volatility = {
        symbol: float(value)
        for symbol, value in (allocation.get("volatility_inputs") or {}).items()
    }
    mode = str(parameters.get("weight_mode"))
    matrix = allocation.get("covariance_matrix")
    if mode == "vol-target":
        covariance = np.array(_require(snapshot, "allocation.covariance_matrix"), dtype="float64")
    elif matrix is None:
        if not allocation.get("covariance_not_required"):
            raise UnverifiableError(
                "missing required input: allocation.covariance_matrix "
                "(or an explicit covariance_not_required reason)"
            )
        covariance = None
    else:
        covariance = np.array(matrix, dtype="float64")
    pre = target_weights(
        mode,
        selected=selected,
        gross_target=float(parameters["gross_target"]),
        per_symbol_cap=parameters.get("per_symbol_cap"),
        atr_k=float(parameters["atr_k"]),
        volatility=volatility,
        atr=atr,
        price=price,
        stop_distance_budget=parameters.get("stop_distance_budget"),
        vol_target=parameters.get("vol_target"),
        covariance=covariance,
    )
    post = cap_risk_contributions(
        pre,
        atr=atr,
        price=price,
        atr_k=float(parameters["atr_k"]),
        risk_contribution_cap=parameters.get("risk_contribution_cap"),
    )
    final = apply_leveraged_cap(
        post,
        leveraged_symbols=[symbol for symbol in selected if symbol in LEVERAGED_PRODUCTS],
        cap=parameters.get("leveraged_cap"),
    )
    forecasts: dict[str, float | None] = {}
    if mode == "vol-target":
        base = {symbol: 1.0 / len(selected) for symbol in selected} if selected else {}
        forecasts = {
            "base_forecast_volatility": forecast_volatility(base, covariance),
            "pre_cap_forecast_volatility": forecast_volatility(pre, covariance),
            "post_cap_forecast_volatility": forecast_volatility(final, covariance),
        }
    return {
        "pre": pre,
        "post": post,
        "final": final,
        "atr": atr,
        "price": price,
        "atr_k": float(parameters["atr_k"]),
        "forecasts": forecasts,
    }


def _replay_selection(snapshot: Mapping[str, Any]) -> Any:
    selection = _require(snapshot, "selection")
    parameters = _require(snapshot, "parameters")
    rank_rows = _require(snapshot, "selection.rank_input_rows")
    rank_mode = selection.get("rank_mode") or parameters.get("rank_mode") or "r20"
    rows: dict[str, pd.Series] = {}
    for symbol, values in rank_rows.items():
        clean = {
            key: float(value)
            for key, value in (values or {}).items()
            if value is not None and math.isfinite(float(value))
        }
        rows[str(symbol)] = pd.Series(clean, dtype="float64")
    scores = rank_scores(str(rank_mode), rows)
    universe = list(parameters.get("universe_symbols") or rows)
    universe_order = {symbol: index for index, symbol in enumerate(universe)}
    ranked = sorted(
        scores.items(),
        key=lambda item: (-item[1], universe_order.get(item[0], len(universe))),
    )
    exposure_groups = selection.get("exposure_groups") or {}
    correlations = selection.get("pairwise_correlations") or {}
    exposure_limit = parameters.get("exposure_group_limit")
    correlation_cap = parameters.get("correlation_screen")

    def correlation_of(first: str, second: str) -> float:
        for key in (f"{first}|{second}", f"{second}|{first}"):
            if key in correlations:
                return float(correlations[key])
        return float("nan")

    return select_holdings(
        ranked=ranked,
        held=tuple(selection.get("held_symbols") or ()),
        top_n=int(parameters["top_n"]),
        rank_buffer=parameters.get("rank_buffer"),
        exposure_limit=exposure_limit,
        exposure_of=(lambda symbol: exposure_groups.get(symbol, symbol)) if exposure_limit is not None else None,
        correlation_of=correlation_of if correlation_cap is not None else None,
        correlation_cap=correlation_cap,
        mandatory_held=tuple(selection.get("mandatory_held_symbols") or ()),
    ), scores, ranked


def replay_decision(snapshot: Mapping[str, Any], tolerances: ParityTolerances | None = None) -> dict[str, Any]:
    """Re-run the production policies against a recorded decision snapshot."""
    tolerances = tolerances or ParityTolerances()
    if not isinstance(snapshot, Mapping):
        raise UnverifiableError("decision snapshot must be an object")

    # Fail closed on any missing required input before recomputation.
    _require(snapshot, "selection.rank_input_rows")
    _require(snapshot, "allocation.event_time_atr")
    _require(snapshot, "allocation.executable_prices")
    parameters = _require(snapshot, "parameters")
    if str(parameters.get("weight_mode")) == "vol-target":
        _require(snapshot, "allocation.covariance_matrix")
        for field in (
            "base_forecast_volatility",
            "pre_cap_forecast_volatility",
            "post_cap_forecast_volatility",
        ):
            _require_finite(snapshot, f"allocation.{field}")
    else:
        allocation = _require(snapshot, "allocation")
        if allocation.get("covariance_matrix") is None and not allocation.get("covariance_not_required"):
            raise UnverifiableError(
                "missing required input: allocation.covariance_matrix "
                "(or an explicit covariance_not_required reason)"
            )
    _require(snapshot, "account.portfolio_value")
    _require_finite(snapshot, "account.portfolio_value")
    _require_finite(snapshot, "account.cash")

    selection, scores, ranked = _replay_selection(snapshot)
    selected = list(selection.holdings)
    weights = _replay_weights(snapshot, selected)

    mismatches: list[dict[str, Any]] = []

    recorded_selected = list((_require(snapshot, "selection")).get("selected_symbols") or [])
    if recorded_selected != selected:
        mismatches.append({
            "field": "selection.selected_symbols",
            "expected": selected,
            "recorded": recorded_selected,
            "message": "replayed selection disagrees with recorded selection",
        })

    recorded_ranks = dict((_require(snapshot, "selection")).get("ordinal_ranks") or {})
    if recorded_ranks != dict(selection.ranks):
        mismatches.append({
            "field": "selection.ordinal_ranks",
            "expected": dict(selection.ranks),
            "recorded": recorded_ranks,
            "message": "replayed ranks disagree with recorded ranks",
        })

    allocation = _require(snapshot, "allocation")
    stage_fields = {
        "pre": "pre_risk_contribution_cap_weights",
        "post": "post_risk_contribution_cap_weights",
        "final": "final_weights_after_leveraged_cap",
    }
    for stage, field_name in stage_fields.items():
        recorded = allocation.get(field_name)
        if recorded is None:
            # A policy stage the replay consumed must be recorded; a missing
            # stage is unverifiable, never silently skipped.
            raise UnverifiableError(f"missing required input: allocation.{field_name}")
        mismatch = _compare_map(recorded, weights[stage], f"allocation.{field_name}", tolerances)
        if mismatch is not None:
            mismatches.append(mismatch)

    if str(parameters.get("weight_mode")) == "vol-target":
        for field, expected in weights["forecasts"].items():
            recorded = float(allocation[field])
            if expected is None or not _isclose(float(expected), recorded, tolerances):
                mismatches.append({
                    "field": f"allocation.{field}",
                    "expected": expected,
                    "recorded": recorded,
                    "message": "replayed volatility forecast disagrees with recorded stage",
                })

    # Whole-share quantity arithmetic from recorded equity/cash/fee/price.
    account = _require(snapshot, "account")
    equity = float(account["portfolio_value"])
    cash = float(account["cash"])
    fee_rate = float(account.get("fee_rate", 0.0))
    order_symbols = list(allocation.get("selected_order") or selected)
    budget = cash
    quantities: dict[str, float] = {}
    for symbol in order_symbols:
        price = float(weights["price"].get(symbol, float("nan")))
        weight = float(weights["final"].get(symbol, 0.0))
        if not math.isfinite(price) or price <= 0.0:
            quantities[symbol] = 0.0
            continue
        notional = min(weight * equity, budget)
        effective_price = price * (1.0 + fee_rate)
        quantity = math.floor(notional / effective_price) if effective_price > 0 else 0
        budget -= quantity * effective_price
        quantities[symbol] = float(quantity)

    planned = list(_require(snapshot, "planned_orders"))
    for order in planned:
        symbol = order.get("symbol")
        recorded_quantity = order.get("requested_quantity")
        if recorded_quantity is None:
            continue
        recomputed = quantities.get(symbol, 0.0)
        if float(recorded_quantity) != recomputed:
            mismatches.append({
                "field": f"planned_orders.{symbol}.requested_quantity",
                "expected": recomputed,
                "recorded": float(recorded_quantity),
                "message": "replayed whole-share quantity disagrees with recorded order",
            })

    # Fill accumulation and protective-stop formula.
    fills = list(snapshot.get("fills") or [])
    cumulative = 0.0
    for fill in fills:
        cumulative += float(fill.get("fill_event_quantity", 0.0))
        recorded_cumulative = fill.get("cumulative_filled_quantity")
        if recorded_cumulative is not None and not _isclose(cumulative, recorded_cumulative, tolerances):
            mismatches.append({
                "field": "fills.cumulative_filled_quantity",
                "expected": cumulative,
                "recorded": float(recorded_cumulative),
                "message": "replayed cumulative fill disagrees with recorded cumulative",
            })

    protective = snapshot.get("protective_stop")
    if isinstance(protective, Mapping) and protective.get("expected"):
        symbol = protective.get("symbol")
        atr_value = weights["atr"].get(symbol)
        entry_price = protective.get("entry_price")
        if atr_value is not None and entry_price is not None:
            expected_level = float(entry_price) - float(weights["atr_k"]) * float(atr_value)
            recorded_level = protective.get("level")
            if recorded_level is not None and not _isclose(
                expected_level, recorded_level, tolerances, atol=tolerances.stop_atol
            ):
                mismatches.append({
                    "field": "protective_stop.level",
                    "expected": expected_level,
                    "recorded": float(recorded_level),
                    "message": "replayed stop level disagrees with recorded level",
                })
        recorded_quantity = protective.get("quantity")
        if recorded_quantity is not None and not _isclose(cumulative, recorded_quantity, tolerances):
            mismatches.append({
                "field": "protective_stop.quantity",
                "expected": cumulative,
                "recorded": float(recorded_quantity),
                "message": "replayed stop quantity disagrees with recorded quantity",
            })

    # Causal timing: quote capture may lag the logical rebalance by <= 300s.
    timing = _check_timing(snapshot, tolerances)
    if timing is not None:
        mismatches.append(timing)

    first = mismatches[0] if mismatches else None
    return {
        "status": "pass" if not mismatches else "mismatch",
        "selected_symbols": selected,
        "ordinal_ranks": dict(selection.ranks),
        "rank_scores": {symbol: float(score) for symbol, score in scores.items()},
        "weights": weights["final"],
        "quantities": quantities,
        "cumulative_filled_quantity": cumulative,
        "first_mismatch": first["field"] if first else None,
        "mismatches": mismatches,
    }


def _parse_timestamp(value: Any) -> datetime | None:
    if value is None:
        return None
    try:
        return pd.Timestamp(value).to_pydatetime()
    except (TypeError, ValueError):
        return None


def _check_timing(snapshot: Mapping[str, Any], tolerances: ParityTolerances) -> dict[str, Any] | None:
    time = snapshot.get("time") or {}
    logical = _parse_timestamp(time.get("logical_rebalance_time"))
    captured = _parse_timestamp(time.get("quote_snapshot_captured_at"))
    if logical is None:
        raise UnverifiableError("missing required input: time.logical_rebalance_time")
    if captured is None:
        raise UnverifiableError("missing required input: time.quote_snapshot_captured_at")
    if logical.tzinfo is None or captured.tzinfo is None:
        raise UnverifiableError("time fields must be timezone-aware")
    delay = (captured - logical).total_seconds()
    if delay < 0 or delay > tolerances.quote_delay_seconds:
        return {
            "field": "time.quote_snapshot_captured_at",
            "expected": f"within {tolerances.quote_delay_seconds}s of logical rebalance",
            "recorded": delay,
            "message": "quote capture delay is outside the allowed window",
        }
    return None


def assert_decision_parity(snapshot: Mapping[str, Any],
                           tolerances: ParityTolerances | None = None) -> dict[str, Any]:
    """Replay a decision and raise ``ParityMismatch`` on the first divergence."""
    result = replay_decision(snapshot, tolerances)
    if result["status"] != "pass":
        first = result["mismatches"][0]
        raise ParityMismatch(first["field"], first.get("expected"), first.get("recorded"), first.get("message", ""))
    return result


_LIFECYCLE_ORDER_EVENTS = frozenset({
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

_LIFECYCLE_TERMINAL_EVENTS = frozenset({
    "final_fill",
    "exit_fill",
    "order_canceled",
    "order_rejected",
})


def validate_lifecycle(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Return structural/semantic lifecycle defects for a session payload.

    An empty list means the order lifecycle is complete and joinable:
    ``intent_created -> order_submitted -> partial_fill* -> terminal`` with strictly
    increasing sequences, nondecreasing timestamps, and every intent either
    terminal or declared in the payload's open-order lineage.
    """
    issues: list[dict[str, Any]] = []

    def add(field: str, message: str, *, intent_id: Any = None,
            decision_id: Any = None) -> None:
        issues.append({"field": field, "message": message,
                       "intent_id": intent_id, "decision_id": decision_id})

    lifecycle = payload.get("lifecycle_trace")
    if not isinstance(lifecycle, list):
        return [{"field": "lifecycle_trace", "message": "missing lifecycle list"}]
    decisions = payload.get("decisions")
    if not isinstance(decisions, list):
        return [{"field": "decisions", "message": "missing decision snapshots"}]
    open_orders = payload.get("open_orders")
    if not isinstance(open_orders, list):
        add("open_orders", "missing open-order lineage list")
        open_orders = []

    decision_ids: set[str] = set()
    planned_intent_claims: set[str] = set()
    for index, decision in enumerate(decisions):
        if not isinstance(decision, Mapping):
            add(f"decisions.{index}", "decision snapshot must be an object")
            continue
        identity = decision.get("identity")
        decision_id = decision.get("decision_id") or (
            identity.get("decision_id") if isinstance(identity, Mapping) else None
        )
        if not decision_id:
            add(f"decisions.{index}.decision_id", "missing decision ID")
        elif str(decision_id) in decision_ids:
            add(f"decisions.{index}.decision_id", "duplicate decision ID",
                decision_id=decision_id)
        else:
            decision_ids.add(str(decision_id))

    # State is keyed by intent, so a terminal for one order cannot satisfy a
    # different order that happens to have the same symbol or decision ID.
    intents: dict[str, dict[str, Any]] = {}
    previous_sequence: int | None = None
    previous_time: datetime | None = None
    for index, event in enumerate(lifecycle):
        prefix = f"lifecycle_trace.{index}"
        if not isinstance(event, Mapping):
            add(prefix, "lifecycle event must be an object")
            continue
        kind = event.get("event")
        intent_id = event.get("intent_id")
        decision_id = event.get("decision_id")
        sequence = event.get("event_sequence")
        if isinstance(sequence, int) and not isinstance(sequence, bool):
            if previous_sequence is not None and sequence <= previous_sequence:
                add(f"{prefix}.event_sequence", "event sequence must strictly increase",
                    intent_id=intent_id, decision_id=decision_id)
            previous_sequence = sequence
        else:
            add(f"{prefix}.event_sequence", "missing event sequence",
                intent_id=intent_id, decision_id=decision_id)
        stamp = _parse_timestamp(event.get("event_time"))
        if stamp is None or stamp.tzinfo is None or stamp.utcoffset() is None:
            add(f"{prefix}.event_time", "missing timezone-aware event time",
                intent_id=intent_id, decision_id=decision_id)
        else:
            if previous_time is not None and stamp < previous_time:
                add(f"{prefix}.event_time", "event times must be nondecreasing",
                    intent_id=intent_id, decision_id=decision_id)
            previous_time = stamp

        if kind not in _LIFECYCLE_ORDER_EVENTS:
            add(f"{prefix}.event", "unknown non-order lifecycle event",
                intent_id=intent_id, decision_id=decision_id)
            continue
        for key in ("decision_id", "intent_id", "symbol", "side"):
            if not event.get(key):
                add(f"{prefix}.{key}", "required order-lifecycle field is missing",
                    intent_id=intent_id, decision_id=decision_id)
        if not any(event.get(key) for key in ("local_order_id", "broker_order_id", "order_id")):
            add(f"{prefix}.order_id", "required order identifier is missing",
                intent_id=intent_id, decision_id=decision_id)
        if decision_id and str(decision_id) not in decision_ids:
            add(f"{prefix}.decision_id", "no decision snapshot resolves lifecycle decision ID",
                intent_id=intent_id, decision_id=decision_id)
        if not intent_id:
            continue

        key = str(intent_id)
        state = intents.get(key)
        if kind in {"intent_created", "protective_order_submitted"}:
            if state is not None:
                add(f"{prefix}.event", "invalid transition: intent already exists",
                    intent_id=intent_id, decision_id=decision_id)
                continue
            state = {
                "phase": "submitted" if kind == "protective_order_submitted" else "created",
                "decision_id": decision_id, "symbol": event.get("symbol"),
                "side": event.get("side"),
                "local_order_id": event.get("local_order_id"),
                "broker_order_id": event.get("broker_order_id"),
                "submitted_callback": False, "protective": kind == "protective_order_submitted",
                "cumulative": 0.0, "requested_quantity": event.get("requested_quantity"),
            }
            intents[key] = state
            if kind == "intent_created" and not event.get("local_order_id"):
                add(f"{prefix}.local_order_id", "missing local order ID at intent creation",
                    intent_id=intent_id, decision_id=decision_id)
            continue
        if state is None:
            add(f"{prefix}.event", "invalid transition: event precedes intent creation",
                intent_id=intent_id, decision_id=decision_id)
            continue

        for field in ("decision_id", "symbol", "side", "local_order_id", "broker_order_id"):
            value = event.get(field)
            prior = state.get(field)
            if value is not None and prior is not None and str(value) != str(prior):
                add(f"{prefix}.{field}", f"{field} changed within intent",
                    intent_id=intent_id, decision_id=decision_id)
            elif value is not None and prior is None:
                state[field] = value
        order_id = event.get("order_id")
        known_order_ids = {str(value) for value in (
            state.get("local_order_id"), state.get("broker_order_id")
        ) if value is not None}
        if order_id and str(order_id) not in known_order_ids:
            add(f"{prefix}.order_id", "order ID is outside intent lineage",
                intent_id=intent_id, decision_id=decision_id)

        phase = state["phase"]
        if kind == "virtual_stop_submitted":
            allowed = phase == "created"
        elif kind == "order_submitted":
            allowed = phase == "created" or (
                state["protective"] and phase == "submitted"
                and not state["submitted_callback"]
            )
            if allowed:
                state["phase"] = "submitted"
                state["submitted_callback"] = True
        elif kind == "partial_fill":
            allowed = phase in {"submitted", "partial"}
            if allowed:
                state["phase"] = "partial"
        elif kind in {"final_fill", "exit_fill"}:
            allowed = phase in {"submitted", "partial"}
            if allowed:
                state["phase"] = "terminal"
        elif kind in {"order_canceled", "order_rejected"}:
            # A synchronous broker rejection can precede on_new_order.
            allowed = phase in {"created", "submitted", "partial"}
            if allowed:
                state["phase"] = "terminal"
        elif kind == "virtual_stop_filled":
            allowed = phase == "terminal" and state.get("terminal_kind") == "exit_fill"
        else:
            allowed = False
        if not allowed:
            add(f"{prefix}.event", f"invalid transition: {kind} after {phase}",
                intent_id=intent_id, decision_id=decision_id)
        if kind in _LIFECYCLE_TERMINAL_EVENTS and allowed:
            state["terminal_kind"] = kind

        if kind in {"partial_fill", "final_fill", "exit_fill"}:
            fragment = event.get("fill_event_quantity")
            cumulative = event.get("cumulative_filled_quantity")
            try:
                fragment_number = float(fragment)
                cumulative_number = float(cumulative)
                numeric = (math.isfinite(fragment_number) and fragment_number > 0
                           and math.isfinite(cumulative_number) and cumulative_number >= 0)
            except (TypeError, ValueError):
                numeric = False
            if not numeric:
                add(f"{prefix}.fill_event_quantity", "missing positive finite fill arithmetic",
                    intent_id=intent_id, decision_id=decision_id)
                continue
            expected = state["cumulative"] + fragment_number
            if cumulative_number < state["cumulative"] or not math.isclose(
                cumulative_number, expected, rel_tol=1e-12, abs_tol=1e-9
            ):
                add(f"{prefix}.cumulative_filled_quantity",
                    "cumulative fill must equal previous cumulative plus this fragment",
                    intent_id=intent_id, decision_id=decision_id)
            state["cumulative"] = cumulative_number

    for index, entry in enumerate(open_orders):
        prefix = f"open_orders.{index}"
        if not isinstance(entry, Mapping):
            add(prefix, "open-order lineage must be an object")
            continue
        intent_id = entry.get("intent_id")
        state = intents.get(str(intent_id)) if intent_id else None
        if state is None:
            add(f"{prefix}.intent_id", "open order has no lifecycle intent",
                intent_id=intent_id)
            continue
        if state["phase"] == "terminal":
            add(f"{prefix}.intent_id", "terminal intent remains declared open",
                intent_id=intent_id, decision_id=state["decision_id"])
        for field in ("decision_id", "symbol", "side", "local_order_id", "broker_order_id"):
            expected = state.get(field)
            if field == "local_order_id" and state["protective"] and expected is None:
                # Historical protective events expose the broker identifier
                # alone; the serializer can retain the same ID as local lineage.
                expected = state.get("broker_order_id")
            if expected is None and field == "broker_order_id":
                # The broker ID can arrive at on_new_order after the session
                # checkpoint; the local ID still joins this open intent.
                if entry.get(field):
                    add(f"{prefix}.{field}", "open-order broker ID lacks lifecycle evidence",
                        intent_id=intent_id, decision_id=state["decision_id"])
                continue
            if not entry.get(field) or str(entry[field]) != str(expected):
                add(f"{prefix}.{field}", "open-order lineage does not match lifecycle intent",
                    intent_id=intent_id, decision_id=state["decision_id"])

    open_intents = {str(entry["intent_id"]) for entry in open_orders
                    if isinstance(entry, Mapping) and entry.get("intent_id")}
    for intent_id, state in intents.items():
        if state["phase"] == "created":
            add("lifecycle_trace.order_submitted", "missing order submission callback",
                intent_id=intent_id, decision_id=state["decision_id"])
        if state["phase"] != "terminal" and intent_id not in open_intents:
            add("open_orders", "nonterminal intent is missing open-order lineage",
                intent_id=intent_id, decision_id=state["decision_id"])

    for index, decision in enumerate(decisions):
        if not isinstance(decision, Mapping):
            continue
        identity = decision.get("identity")
        decision_id = decision.get("decision_id") or (
            identity.get("decision_id") if isinstance(identity, Mapping) else None
        )
        for order_index, order in enumerate(decision.get("planned_orders") or []):
            prefix = f"decisions.{index}.planned_orders.{order_index}"
            if not isinstance(order, Mapping):
                add(prefix, "planned order must be an object", decision_id=decision_id)
                continue
            intent_id = order.get("intent_id")
            if intent_id and str(intent_id) in planned_intent_claims:
                add(
                    f"{prefix}.intent_id",
                    "intent is claimed by multiple planned orders",
                    intent_id=intent_id,
                    decision_id=decision_id,
                )
            elif intent_id:
                planned_intent_claims.add(str(intent_id))
            state = intents.get(str(intent_id)) if intent_id else None
            if state is None:
                add(f"{prefix}.intent_id", "planned order has no joinable lifecycle intent",
                    intent_id=intent_id, decision_id=decision_id)
                continue
            for field, expected in (("decision_id", decision_id),
                                    ("symbol", order.get("symbol")),
                                    ("side", order.get("side"))):
                if expected and str(state.get(field)) != str(expected):
                    add(f"{prefix}.{field}", "planned order disagrees with lifecycle intent",
                        intent_id=intent_id, decision_id=decision_id)
    return issues


def compare_pnl(live: Mapping[str, float], native: Mapping[str, float], *,
                tolerances: ParityTolerances | None = None) -> dict[str, Any]:
    """Compare aligned session-end normalized equity curves within tolerances."""
    tolerances = tolerances or ParityTolerances()
    live_sessions = set(live)
    native_sessions = set(native)
    # Every requested live session must exist in the native curve; never compare
    # only the intersection.
    if not live_sessions or not native_sessions or (live_sessions - native_sessions):
        return {
            "status": "unverifiable",
            "first_divergent_session": None,
            "missing_sessions": sorted(live_sessions - native_sessions),
        }
    common = sorted(live_sessions & native_sessions)
    if len(common) < 2:
        # At least two aligned observations are required for a return comparison.
        return {"status": "unverifiable", "first_divergent_session": None}
    base_live = float(live[common[0]])
    base_native = float(native[common[0]])
    if base_live <= 0.0 or base_native <= 0.0:
        return {"status": "unverifiable", "first_divergent_session": None}
    for session in common[1:]:
        live_return = float(live[session]) / base_live - 1.0
        native_return = float(native[session]) / base_native - 1.0
        absolute = abs(live_return - native_return)
        relative = absolute / max(abs(native_return), 1e-12)
        dollar = abs(float(live[session]) - float(native[session]))
        guard = max(tolerances.dollar_guard, base_native * tolerances.abs_return_tol)
        if absolute > tolerances.abs_return_tol and relative > tolerances.rel_tol and dollar > guard:
            return {
                "status": "mismatch",
                "first_divergent_session": session,
                "live_return": live_return,
                "native_return": native_return,
                "absolute_difference": absolute,
                "relative_difference": relative,
            }
    return {"status": "pass", "first_divergent_session": None}
