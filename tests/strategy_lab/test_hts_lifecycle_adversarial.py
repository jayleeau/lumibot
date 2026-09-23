"""Adversarial lifecycle cases that must fail session verification."""

from __future__ import annotations

from strategy_lab.hts_parity import validate_lifecycle


def _complete_order_session() -> dict:
    """Build one fully joined intent with a single complete fill."""
    identity = {"decision_id": "decision-1"}
    base = {
        "decision_id": "decision-1",
        "intent_id": "intent-1",
        "symbol": "AAA",
        "side": "buy",
        "local_order_id": "local-1",
        "broker_order_id": "broker-1",
        "order_id": "broker-1",
        "event_time": "2026-09-23T10:00:00+00:00",
    }
    return {
        "decisions": [{
            "identity": identity,
            "decision_id": "decision-1",
            "planned_orders": [{"intent_id": "intent-1", "symbol": "AAA", "side": "buy"}],
        }],
        "open_orders": [],
        "lifecycle_trace": [
            {**base, "event": "intent_created", "event_sequence": 1,
             "requested_quantity": 1.0},
            {**base, "event": "order_submitted", "event_sequence": 2},
            {**base, "event": "final_fill", "event_sequence": 3,
             "fill_event_quantity": 1.0, "cumulative_filled_quantity": 1.0},
        ],
    }


def test_complete_joined_lifecycle_passes() -> None:
    assert validate_lifecycle(_complete_order_session()) == []


def test_ghost_decision_id_fails() -> None:
    payload = _complete_order_session()
    for event in payload["lifecycle_trace"]:
        event["decision_id"] = "decision-ghost"

    issues = validate_lifecycle(payload)
    assert any(issue["field"].endswith("decision_id") for issue in issues)


def test_impossible_cumulative_fill_fails() -> None:
    payload = _complete_order_session()
    payload["lifecycle_trace"][-1]["cumulative_filled_quantity"] = 2.0

    issues = validate_lifecycle(payload)
    assert any(issue["field"].endswith("cumulative_filled_quantity") for issue in issues)


def test_final_fill_before_order_submitted_fails() -> None:
    payload = _complete_order_session()
    lifecycle = payload["lifecycle_trace"]
    lifecycle[1], lifecycle[2] = lifecycle[2], lifecycle[1]
    lifecycle[1]["event_sequence"] = 2
    lifecycle[2]["event_sequence"] = 3

    issues = validate_lifecycle(payload)
    assert any("transition" in issue["message"] for issue in issues)


def test_order_id_continuity_fails_on_broker_id_change() -> None:
    payload = _complete_order_session()
    payload["lifecycle_trace"][-1]["broker_order_id"] = "broker-other"
    payload["lifecycle_trace"][-1]["order_id"] = "broker-other"

    issues = validate_lifecycle(payload)
    assert any(issue["field"].endswith("broker_order_id") for issue in issues)


def test_unfinished_intent_requires_exact_open_order_lineage() -> None:
    payload = _complete_order_session()
    payload["lifecycle_trace"].pop()
    payload["open_orders"] = [{"intent_id": "intent-1", "decision_id": "decision-1",
                               "symbol": "AAA", "side": "buy",
                               "local_order_id": "local-1", "broker_order_id": "wrong-id"}]

    issues = validate_lifecycle(payload)
    assert any(issue["field"].startswith("open_orders") for issue in issues)


def test_two_planned_orders_cannot_claim_one_intent() -> None:
    payload = _complete_order_session()
    payload["decisions"][0]["planned_orders"].append(
        {"intent_id": "intent-1", "symbol": "AAA", "side": "buy"}
    )

    issues = validate_lifecycle(payload)
    assert any("claimed by multiple planned orders" in issue["message"] for issue in issues)


def test_declared_open_order_with_matching_lineage_passes() -> None:
    payload = _complete_order_session()
    payload["lifecycle_trace"].pop()
    payload["open_orders"] = [{"intent_id": "intent-1", "decision_id": "decision-1",
                               "symbol": "AAA", "side": "buy",
                               "local_order_id": "local-1", "broker_order_id": "broker-1"}]

    assert validate_lifecycle(payload) == []
