"""Versioned audit schema, atomic storage, and event-stream serialization tests.

These exercise the strategy-owned persistence contract from
``plans/fix-3-persist-parity-state.md``.  They are deterministic and use the real
``RegistryHtsStrategy`` decision/fill methods with only the broker-order boundary
faked, so no network, credentials, or live broker are required.
"""
from __future__ import annotations

import copy
import json
import sys
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from hts_production_fixture import (  # noqa: E402
    FakeOrder,
    complete_entry_lifecycle,
    make_live_strategy,
    make_production_strategy,
    run_production_rebalance,
    txn,
)
from strategy_lab.hts_audit import (  # noqa: E402
    DECISION_SCHEMA_VERSION,
    EVENT_SCHEMA_VERSION,
    STATE_SCHEMA_VERSION,
    AtomicJsonStore,
    AuditError,
    AuditValidationError,
    StateIdentityError,
    build_decision_snapshot,
    build_strategy_event_payload,
    decode_decision_snapshot,
    encode_decision_snapshot,
    merge_session_events,
    serialize_runtime_state,
    validate_decision_snapshot,
    validate_runtime_state,
    validate_strategy_event_payload,
)
from strategy_lab.native_experiments import RegistryHtsStrategy  # noqa: E402


# --- fixtures -----------------------------------------------------------------

def _identity() -> dict[str, str]:
    return {
        "strategy_name": "w0007",
        "catalog_id": "w0007",
        "implementation_revision": "hts-native-v2-test",
        "resolved_parameters_hash": "params-w0007",
        "feature_hash": "feature-w0007",
        "decision_id": "w0007:2026-09-23:10",
    }


def _two_symbol_snapshot() -> dict:
    return build_decision_snapshot(
        identity=_identity(),
        time={
            "session": "2026-09-23",
            "signal_session": "2026-09-22",
            "signal_observed_at": "2026-09-22T15:00:00-04:00",
            "native_iteration_time": "2026-09-23T10:00:00-04:00",
            "logical_rebalance_time": "2026-09-23T10:00:00-04:00",
            "completed_source_bar_by_symbol": {
                "AAA": "2026-09-23T09:00:00",
                "BBB": "2026-09-23T09:00:00",
            },
            "quote_snapshot_captured_at": "2026-09-23T10:00:05-04:00",
            "timezone": "America/New_York",
            "hourly_convention": "clock-hour 09:00-15:00 ET; bar T known at T+1h",
        },
        selection={
            "rank_mode": "r20",
            "rank_input_rows": {
                "AAA": {"ret": 0.10, "close": 101.0, "sma": 100.0, "mdv": 10_000_000.0},
                "BBB": {"ret": 0.20, "close": 51.0, "sma": 50.0, "mdv": 10_000_000.0},
            },
            "rank_scores": {"AAA": 0.10, "BBB": 0.20},
            "ordinal_ranks": {"AAA": 2, "BBB": 1},
            "ranked_symbols": ["BBB", "AAA"],
            "held_symbols": [],
            "mandatory_held_symbols": [],
            "exposure_groups": {"AAA": "broad", "BBB": "tech"},
            "pairwise_correlations": {"AAA|BBB": 0.10},
            "selected_symbols": ["BBB", "AAA"],
        },
        allocation={
            "selected_order": ["BBB", "AAA"],
            "event_time_atr": {"AAA": 1.0, "BBB": 2.0},
            "executable_prices": {"AAA": 100.0, "BBB": 50.0},
            "full_live_price_snapshot": {"AAA": 100.0, "BBB": 50.0},
            "volatility_inputs": {"AAA": 0.20, "BBB": 0.30},
            "covariance_symbols": ["AAA", "BBB"],
            "covariance_matrix": [[0.04, 0.01], [0.01, 0.09]],
            "base_forecast_volatility": 0.15,
            "pre_cap_forecast_volatility": 0.15,
            "post_cap_forecast_volatility": 0.15,
            "pre_risk_contribution_cap_weights": {"AAA": 0.4975, "BBB": 0.4975},
            "post_risk_contribution_cap_weights": {"AAA": 0.4975, "BBB": 0.375},
            "final_weights_after_leveraged_cap": {"AAA": 0.4975, "BBB": 0.375},
            "risk_contribution_cap": 0.03,
            "risk_cap_binding_by_symbol": {"AAA": False, "BBB": True},
            "risk_cap_bound_any": True,
        },
        account={
            "cash": 100_000.0,
            "portfolio_value": 100_000.0,
            "balance_observed_at": "2026-09-23T10:00:05-04:00",
            "fee_rate": 0.00035,
        },
        book={
            "existing_positions": [],
            "pending_buys": [],
            "pending_sells": [],
            "occupied_symbols": [],
        },
        planned_orders=[
            {
                "symbol": "BBB",
                "side": "buy",
                "reference_price": 50.0,
                "requested_quantity": 749,
                "intended_notional": 37_500.0,
                "budget_before": 100_000.0,
                "budget_after": 62_536.8925,
                "intent_id": "intent-bbb",
            },
        ],
        parameters={
            "weight_mode": "equal-slots",
            "gross_target": 0.995,
            "per_symbol_cap": None,
            "atr_k": 2.0,
            "stop_distance_budget": None,
            "vol_target": None,
            "risk_contribution_cap": 0.03,
            "leveraged_cap": None,
            "top_n": 2,
            "rank_buffer": None,
            "exposure_group_limit": None,
            "correlation_screen": None,
        },
        fills=[
            {"symbol": "BBB", "side": "buy", "fill_event_quantity": 749.0,
             "cumulative_filled_quantity": 749.0},
        ],
        protective_stop={
            "symbol": "BBB",
            "expected": True,
            "quantity": 749.0,
            "entry_price": 50.0,
            "level": 46.0,
        },
    )


def _complete_runtime_strategy() -> RegistryHtsStrategy:
    s = make_live_strategy()
    s._positions["AAA"] = {
        "quantity": 10.0,
        "entry_price": 100.0,
        "entry_atr": 2.0,
        "entry_session": date(2026, 9, 22),
        "entry_session_index": 0,
        "stop": 96.0,
        "highest_high": 105.0,
        "post_entry_highs": [101.0, 105.0],
        "breach_count": 1,
        "seeded": True,
        "protective_level": 96.0,
        "entry_intent_id": "buy-AAA-1",
        "entry_decision_id": "w0007:2026-09-23:10:1",
    }
    s._protective["AAA"] = FakeOrder("AAA", 10.0, buy=False, identifier="stop-1", stop_price=96.0)
    s._protective_meta["AAA"] = {
        "intent_id": "prot-AAA-2",
        "decision_id": "w0007:2026-09-23:10:1",
        "parent_entry_intent_id": "buy-AAA-1",
        "broker_order_id": "stop-1",
        "quantity": 10.0,
        "level": 96.0,
    }
    s._pending_buys["BBB"] = {
        "quantity": 5.0,
        "atr": 1.5,
        "order": FakeOrder("BBB", 5.0, buy=True, identifier="buy-1"),
        "requested_quantity": 5.0,
        "event_time_atr": 1.5,
        "intent_id": "buy-BBB-3",
        "decision_id": "w0007:2026-09-23:10:1",
        "local_order_id": "buy-1",
        "broker_order_id": "buy-1",
        "cumulative_filled_quantity": 2.0,
        "last_fill_event_quantity": 2.0,
        "reason": "selection_entry",
    }
    s._pending_sells.add("CCC")
    s._pending_sell_reason["CCC"] = "selection_change"
    s._pending_sell_meta["CCC"] = {
        "intent_id": "sell-CCC-4",
        "decision_id": "w0007:2026-09-23:10:2",
        "symbol": "CCC",
        "side": "sell",
        "local_order_id": "sell-1",
        "broker_order_id": "sell-1",
        "requested_quantity": 3.0,
        "reason": "selection_change",
        "cumulative_filled_quantity": 0.0,
        "last_fill_event_quantity": 0.0,
    }
    s._selected = ("AAA", "BBB")
    s._ranks = {"AAA": 1, "BBB": 2}
    s._signal_day = date(2026, 9, 23)
    s._event_sequence = 12
    s._decision_sequence = 2
    s._intent_sequence = 4
    s._active_decision_id = "w0007:2026-09-23:10:2"
    s._last_decision_id = "w0007:2026-09-23:10:2"
    s._processed_decision_ids = {
        "w0007:2026-09-23:10:1",
        "w0007:2026-09-23:15:3",
    }
    s._risk_off_active = True
    s._re_risk_deadline_index = 7
    s._last_risk_gate_session = date(2026, 9, 22)
    s._risk_off_race_symbols = {"AAA"}
    s._deferred_rebalance = {
        "kind": "rebalance",
        "decision_day": "2026-09-23",
        "decision_id": "w0007:2026-09-23:15:3",
        "logical_rebalance_hour": 15,
        "submission_started": False,
        "buys": [{"symbol": "BBB", "quantity": 5.0, "reference": 50.0, "atr": 1.5,
                  "reason": "selection_entry", "intent_id": "buy-BBB-5"}],
        "sells": [],
    }
    s._last_quote_snapshot = {
        "captured_at": "2026-09-23T10:00:05-04:00",
        "prices": {"AAA": 100.0, "BBB": 50.0},
    }
    return s


# --- schema round trip --------------------------------------------------------

def test_decision_snapshot_schema_round_trip_preserves_policy_inputs() -> None:
    snapshot = _two_symbol_snapshot()
    assert snapshot["schema_version"] == DECISION_SCHEMA_VERSION
    assert snapshot["decision_id"] == "w0007:2026-09-23:10"

    encoded = encode_decision_snapshot(snapshot)
    restored = decode_decision_snapshot(encoded)

    assert restored == snapshot
    # Symbol ordering survives the JSON object round trip.
    assert list(restored["selection"]["rank_input_rows"]) == ["AAA", "BBB"]
    assert restored["selection"]["selected_symbols"] == ["BBB", "AAA"]
    # float64 values and matrix shape are preserved exactly.
    assert restored["allocation"]["covariance_matrix"] == [[0.04, 0.01], [0.01, 0.09]]
    assert restored["allocation"]["covariance_matrix"][0][0] == snapshot["allocation"]["covariance_matrix"][0][0]
    assert isinstance(restored["allocation"]["covariance_matrix"][0][0], float)
    assert restored["account"]["portfolio_value"] == 100_000.0
    # Timestamps remain ISO strings.
    assert restored["time"]["quote_snapshot_captured_at"] == "2026-09-23T10:00:05-04:00"
    # The planned-order budget trail survives intact.
    order = restored["planned_orders"][0]
    assert order["budget_before"] == 100_000.0
    assert order["budget_after"] == 62_536.8925


def test_decision_snapshot_rejects_nonfinite_or_missing_required_inputs() -> None:
    snapshot = _two_symbol_snapshot()

    nan_atr = copy.deepcopy(snapshot)
    nan_atr["allocation"]["event_time_atr"]["AAA"] = float("nan")
    with pytest.raises(AuditValidationError) as excinfo:
        validate_decision_snapshot(nan_atr)
    assert excinfo.value.field == "allocation.event_time_atr.AAA"

    malformed = copy.deepcopy(snapshot)
    malformed["allocation"]["covariance_matrix"] = [[0.04, 0.01]]
    with pytest.raises(AuditValidationError) as excinfo:
        validate_decision_snapshot(malformed)
    assert excinfo.value.field == "allocation.covariance_matrix"

    missing_equity = copy.deepcopy(snapshot)
    del missing_equity["account"]["portfolio_value"]
    with pytest.raises(AuditValidationError) as excinfo:
        validate_decision_snapshot(missing_equity)
    assert excinfo.value.field == "account.portfolio_value"

    unknown_version = copy.deepcopy(snapshot)
    unknown_version["schema_version"] = 999
    with pytest.raises(AuditValidationError) as excinfo:
        validate_decision_snapshot(unknown_version)
    assert excinfo.value.field == "schema_version"

    missing_mandatory = copy.deepcopy(snapshot)
    del missing_mandatory["selection"]["mandatory_held_symbols"]
    with pytest.raises(AuditValidationError) as excinfo:
        validate_decision_snapshot(missing_mandatory)
    assert excinfo.value.field == "selection.mandatory_held_symbols"

    missing_quote = copy.deepcopy(snapshot)
    del missing_quote["allocation"]["full_live_price_snapshot"]
    with pytest.raises(AuditValidationError) as excinfo:
        validate_decision_snapshot(missing_quote)
    assert excinfo.value.field == "allocation.full_live_price_snapshot"

    missing_stage = copy.deepcopy(snapshot)
    del missing_stage["allocation"]["pre_risk_contribution_cap_weights"]
    with pytest.raises(AuditValidationError) as excinfo:
        validate_decision_snapshot(missing_stage)
    assert excinfo.value.field == "allocation.pre_risk_contribution_cap_weights"

    bad_timezone = copy.deepcopy(snapshot)
    bad_timezone["time"]["timezone"] = "Mars/Phobos"
    with pytest.raises(AuditValidationError) as excinfo:
        validate_decision_snapshot(bad_timezone)
    assert excinfo.value.field == "time.timezone"

    vol_target_no_cov = copy.deepcopy(snapshot)
    vol_target_no_cov["parameters"]["weight_mode"] = "vol-target"
    del vol_target_no_cov["allocation"]["covariance_matrix"]
    with pytest.raises(AuditValidationError) as excinfo:
        validate_decision_snapshot(vol_target_no_cov)
    assert excinfo.value.field == "allocation.covariance_matrix"

    no_cov_no_reason = copy.deepcopy(snapshot)
    del no_cov_no_reason["allocation"]["covariance_matrix"]
    with pytest.raises(AuditValidationError) as excinfo:
        validate_decision_snapshot(no_cov_no_reason)
    assert excinfo.value.field == "allocation.covariance_not_required"


@pytest.mark.parametrize(
    "field",
    (
        "base_forecast_volatility",
        "pre_cap_forecast_volatility",
        "post_cap_forecast_volatility",
    ),
)
def test_vol_target_snapshot_requires_every_forecast_stage(field: str) -> None:
    snapshot = _two_symbol_snapshot()
    snapshot["parameters"]["weight_mode"] = "vol-target"
    snapshot["parameters"]["vol_target"] = 0.10
    del snapshot["allocation"][field]

    with pytest.raises(AuditValidationError) as excinfo:
        validate_decision_snapshot(snapshot)
    assert excinfo.value.field == f"allocation.{field}"


# --- production snapshot ------------------------------------------------------

def test_event_payload_contains_full_production_rebalance_snapshot() -> None:
    strategy = make_production_strategy()
    run_production_rebalance(strategy)

    payload = build_strategy_event_payload(strategy, session="2026-09-23")
    assert payload["schema_version"] == EVENT_SCHEMA_VERSION
    assert len(payload["decisions"]) == 1

    decision = payload["decisions"][0]
    for section in ("identity", "time", "selection", "allocation", "account", "book", "parameters"):
        assert section in decision, section
    assert decision["selection"]["rank_input_rows"]
    assert decision["allocation"]["executable_prices"]
    assert decision["allocation"]["final_weights_after_leveraged_cap"]
    assert decision["planned_orders"]
    # It is a full snapshot, not a decision-ID stub.
    assert "selection" in decision and "allocation" in decision
    assert decision["decision_id"] == strategy._last_decision_id

    rebalance_events = [e for e in strategy._journal if e.get("event") == "decision_emitted"]
    assert len(payload["decisions"]) == len(rebalance_events)

    # The full snapshot validates end to end.
    validate_strategy_event_payload(payload)


# --- lifecycle serialization --------------------------------------------------

def test_strategy_event_payload_serializes_complete_joinable_order_lifecycle() -> None:
    strategy = make_live_strategy()
    strategy._submit_buy("BITX", 316, 20.97, 0.5, "selection_entry")

    order = strategy._pending_buys["BITX"]["order"]
    broker_id = str(order.identifier)

    order.transactions.append(txn(20.97, 253))
    strategy.on_partially_filled_order(None, order, 20.97, 253, 1)
    order.transactions.append(txn(20.97, 34))
    strategy.on_partially_filled_order(None, order, 20.97, 34, 1)
    order.transactions.append(txn(20.97, 29))
    strategy.on_filled_order(None, order, 20.97, 29, 1)

    payload = build_strategy_event_payload(strategy)
    lifecycle = payload["lifecycle_trace"]

    intent = next(event for event in lifecycle if event["event"] == "intent_created")
    assert intent["decision_id"]
    assert intent["intent_id"]
    assert intent["local_order_id"] == broker_id

    for event in lifecycle:
        assert event.get("event_time"), event
        assert isinstance(event.get("event_sequence"), int)
        assert event.get("decision_id"), event
        assert event.get("intent_id"), event
        assert event.get("symbol"), event
        assert event.get("side"), event
        assert (
            event.get("order_id") or event.get("broker_order_id") or event.get("local_order_id")
        ), event

    partials = [event for event in lifecycle if event["event"] == "partial_fill"]
    assert [event["fill_event_quantity"] for event in partials] == [253.0, 34.0]
    assert [event["cumulative_filled_quantity"] for event in partials] == [253.0, 287.0]
    # Equal-sized repeated fills at one timestamp stay distinct via sequence.
    assert partials[0]["event_id"] != partials[1]["event_id"]

    final = next(event for event in lifecycle if event["event"] == "final_fill")
    assert final["fill_event_quantity"] == 29.0
    assert final["cumulative_filled_quantity"] == 316.0
    assert final["intent_id"] == intent["intent_id"]

    protective = next(event for event in lifecycle if event["event"] == "protective_order_submitted")
    assert protective["quantity"] == 316.0
    assert protective["parent_entry_intent_id"] == intent["intent_id"]
    assert strategy._positions["BITX"]["quantity"] == 316.0


def test_order_rejection_is_terminal_and_preserves_position_safety() -> None:
    # Rejected entry buy: pending buy is removed.
    strategy = make_live_strategy()
    strategy._submit_buy("AAA", 5, 100.0, 1.0, "selection_entry")
    buy_order = strategy._pending_buys["AAA"]["order"]
    strategy.on_error_order(buy_order, RuntimeError("broker rejected"))
    assert "AAA" not in strategy._pending_buys
    rejected = [e for e in strategy._lifecycle_trace if e["event"] == "order_rejected"]
    assert rejected and rejected[-1]["symbol"] == "AAA"
    assert rejected[-1]["decision_id"]
    assert rejected[-1]["intent_id"]

    # Rejected ordinary sell: pending-sell state clears but the position remains.
    strategy2 = make_live_strategy()
    strategy2._positions["AAA"] = {
        "quantity": 10.0, "entry_price": 100.0, "entry_atr": 2.0, "stop": 96.0,
    }
    strategy2._submit_sell("AAA", "selection_change", 100.0)
    sell_order = strategy2._created[-1]
    strategy2.on_error_order(sell_order, RuntimeError("broker rejected"))
    assert "AAA" not in strategy2._pending_sells
    assert "AAA" in strategy2._positions
    assert any(e["event"] == "order_rejected" for e in strategy2._lifecycle_trace)

    # Rejected protective stop: never leave _protective claiming coverage.
    strategy3 = make_live_strategy()
    strategy3._positions["AAA"] = {
        "quantity": 10.0, "entry_price": 100.0, "entry_atr": 2.0, "stop": 96.0,
        "entry_intent_id": "buy-AAA-1", "entry_decision_id": "w0007:2026-09-23:10:1",
    }
    strategy3._place_protective_stop("AAA")
    stop_order = strategy3._protective["AAA"]
    strategy3.on_error_order(stop_order, RuntimeError("broker rejected"))
    assert "AAA" not in strategy3._protective
    assert any(e["event"] == "order_rejected" for e in strategy3._lifecycle_trace)
    assert strategy3._stop_gap_events


# --- atomic storage -----------------------------------------------------------

def test_atomic_state_store_round_trip_is_versioned_and_identity_checked(tmp_path: Path) -> None:
    strategy = _complete_runtime_strategy()
    state = serialize_runtime_state(strategy, reason="unit")
    assert state["schema_version"] == STATE_SCHEMA_VERSION

    path = tmp_path / "state" / "w0007.json"
    store = AtomicJsonStore(path)
    store.write_state(state)

    loaded = store.read_state(expected_identity=strategy._audit_identity)
    assert loaded == state

    with pytest.raises(StateIdentityError):
        store.read_state(expected_identity={**strategy._audit_identity, "strategy_name": "w0006"})
    with pytest.raises(StateIdentityError):
        store.read_state(
            expected_identity={**strategy._audit_identity, "resolved_parameters_hash": "deadbeef"}
        )

    wrong_version = dict(state)
    wrong_version["schema_version"] = 999
    store.write(wrong_version)
    with pytest.raises(AuditError):
        store.read_state(expected_identity=strategy._audit_identity)


def test_corrupt_state_is_rejected_before_strategy_mutation(tmp_path: Path) -> None:
    strategy = make_live_strategy()
    strategy._positions = {"KEEP": {"quantity": 5.0, "entry_price": 10.0}}
    strategy._pending_buys = {}
    strategy._processed_decision_ids = set()
    strategy._selected = ("KEEP",)

    path = tmp_path / "w0007.json"
    path.write_text('{"schema_version": 2, "strategy_name": "w0007"', encoding="utf-8")
    strategy._state_path = path

    assert strategy._restore_persisted_state() is False
    assert strategy._positions == {"KEEP": {"quantity": 5.0, "entry_price": 10.0}}
    assert strategy._pending_buys == {}
    assert strategy._processed_decision_ids == set()
    assert strategy._selected == ("KEEP",)
    assert any(event.get("event") == "state_restore_failed" for event in strategy._diag)


def test_runtime_state_validator_rejects_incomplete_state_before_replace(tmp_path: Path) -> None:
    strategy = _complete_runtime_strategy()
    valid = serialize_runtime_state(strategy, reason="unit")
    validate_runtime_state(valid)

    path = tmp_path / "w0007.json"
    store = AtomicJsonStore(path)
    store.write_state(valid)
    original = path.read_bytes()

    cases = []
    missing_sequence = copy.deepcopy(valid)
    del missing_sequence["event_sequence"]
    cases.append((missing_sequence, "event_sequence"))

    bad_deadline = copy.deepcopy(valid)
    bad_deadline["risk_off_state"]["deadline_index"] = "soon"
    cases.append((bad_deadline, "risk_off_state.deadline_index"))

    missing_sell_intent = copy.deepcopy(valid)
    missing_sell_intent["pending_sells"]["CCC"]["intent_id"] = ""
    cases.append((missing_sell_intent, "pending_sells.CCC.intent_id"))

    incomplete_deferred = copy.deepcopy(valid)
    incomplete_deferred["deferred_rebalance"]["decision_id"] = ""
    cases.append((incomplete_deferred, "deferred_rebalance.decision_id"))

    missing_deferred_submission_gate = copy.deepcopy(valid)
    del missing_deferred_submission_gate["deferred_rebalance"]["submission_started"]
    cases.append((
        missing_deferred_submission_gate,
        "deferred_rebalance.submission_started",
    ))

    for invalid, field in cases:
        with pytest.raises(AuditValidationError) as excinfo:
            validate_runtime_state(invalid)
        assert excinfo.value.field == field
        with pytest.raises(AuditValidationError):
            store.write_state(invalid)
        assert path.read_bytes() == original


def test_session_artifact_merges_full_decisions_and_distinct_equal_fills(tmp_path: Path) -> None:
    first = make_production_strategy()
    run_production_rebalance(first)
    complete_entry_lifecycle(first, "BBB", fragments=(500.0, 249.0))
    first_payload = build_strategy_event_payload(first, session="2026-09-23")

    path = tmp_path / "2026-09-23.json"
    store = AtomicJsonStore(path)
    store.write_session(first_payload)

    second = make_production_strategy()
    # Simulate a restart that re-emits the same decision with an equal-sized fill
    # fragment but a different event sequence.
    run_production_rebalance(second)
    second._event_sequence = 50
    complete_entry_lifecycle(second, "BBB", fragments=(500.0, 249.0))
    second_payload = build_strategy_event_payload(second, session="2026-09-23")

    merged = merge_session_events(store.read(), second_payload)
    store.write_session(merged)
    final = store.read()

    decisions = final["decisions"]
    assert len(decisions) == 1
    assert decisions[0]["decision_id"] == first._last_decision_id

    partials = [e for e in final["lifecycle_trace"] if e["event"] == "partial_fill"]
    assert len(partials) == 2

    merged_again = merge_session_events(final, second_payload)
    assert len([e for e in merged_again["lifecycle_trace"] if e["event"] == "partial_fill"]) == 2
    assert len(merged_again["decisions"]) == 1

    conflicting = copy.deepcopy(second_payload)
    conflicting["decisions"][0]["account"]["portfolio_value"] = 999_999.0
    with pytest.raises(AuditError):
        merge_session_events(final, conflicting)
