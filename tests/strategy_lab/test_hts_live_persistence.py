"""Restart-state persistence, reconciliation, and fail-open order-flow tests.

These exercise the strategy-owned live persistence contract from
``plans/fix-3-persist-parity-state.md``.  The real ``RegistryHtsStrategy``
submission and reconciliation methods run with only the broker-order boundary
faked, so they are deterministic and require no network or credentials.
"""
from __future__ import annotations

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
    make_live_strategy,
    txn,
)
from strategy_lab.hts_audit import (  # noqa: E402
    AtomicJsonStore,
    build_strategy_event_payload,
    serialize_runtime_state,
    validate_strategy_event_payload,
)
from strategy_lab.native_experiments import RegistryHtsStrategy  # noqa: E402


def _populated_live_strategy() -> RegistryHtsStrategy:
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


def test_restart_state_round_trip_restores_complete_runtime_and_buy_sell_lineage(
    tmp_path: Path,
) -> None:
    source = _populated_live_strategy()
    state_path = tmp_path / "state" / "w0007.json"
    source._state_path = state_path
    AtomicJsonStore(state_path).write_state(serialize_runtime_state(source, reason="unit"))

    target = make_live_strategy()
    target._state_path = state_path
    assert target._restore_persisted_state() is True

    assert target._event_sequence == 12
    assert target._decision_sequence == 2
    assert target._intent_sequence == 4
    assert target._active_decision_id == "w0007:2026-09-23:10:2"
    assert target._last_decision_id == "w0007:2026-09-23:10:2"
    assert target._processed_decision_ids == {
        "w0007:2026-09-23:10:1",
        "w0007:2026-09-23:15:3",
    }
    assert target._selected == ("AAA", "BBB")
    assert target._ranks == {"AAA": 1, "BBB": 2}
    assert target._signal_day == date(2026, 9, 23)

    # Risk-off and deferred state.
    assert target._risk_off_active is True
    assert target._re_risk_deadline_index == 7
    assert target._last_risk_gate_session == date(2026, 9, 22)
    assert target._risk_off_race_symbols == {"AAA"}
    assert target._deferred_rebalance["decision_id"] == "w0007:2026-09-23:15:3"
    assert target._last_quote_snapshot["prices"] == {"AAA": 100.0, "BBB": 50.0}

    # Buy lineage.
    pending = target._pending_buys["BBB"]
    assert pending["intent_id"] == "buy-BBB-3"
    assert pending["decision_id"] == "w0007:2026-09-23:10:1"
    assert pending["requested_quantity"] == 5.0
    assert pending["cumulative_filled_quantity"] == 2.0

    # Sell lineage.
    assert target._pending_sells == {"CCC"}
    assert target._pending_sell_meta["CCC"]["intent_id"] == "sell-CCC-4"
    assert target._pending_sell_meta["CCC"]["broker_order_id"] == "sell-1"

    # Protective lineage and position stop state.
    assert target._protective_meta["AAA"]["intent_id"] == "prot-AAA-2"
    position = target._positions["AAA"]
    assert position["stop"] == 96.0
    assert position["protective_level"] == 96.0
    assert position["entry_intent_id"] == "buy-AAA-1"

    # Reconcile against broker truth without mutating broker state.
    open_orders = [
        FakeOrder("AAA", 10.0, buy=False, identifier="stop-1", stop_price=96.0),
        FakeOrder("BBB", 5.0, buy=True, identifier="buy-1", transactions=[txn(50.0, 2.0)]),
        FakeOrder("CCC", 3.0, buy=False, identifier="sell-1"),
    ]
    target._reconcile_broker_state_live(
        broker_positions={"AAA": 10.0}, open_orders=open_orders,
    )
    assert target._protective["AAA"].identifier == "stop-1"
    assert target._pending_buys["BBB"]["order"].identifier == "buy-1"
    assert target._pending_sell_meta["CCC"]["order"].identifier == "sell-1"


def test_restore_rejects_incomplete_state_before_any_strategy_mutation(tmp_path: Path) -> None:
    target = make_live_strategy()
    target._positions = {"KEEP": {"quantity": 5.0, "entry_price": 10.0, "entry_atr": 1.0, "stop": 9.0}}
    target._pending_buys = {"KEEP": {"intent_id": "keep-intent"}}
    target._selected = ("KEEP",)
    target._processed_decision_ids = {"sentinel-decision"}

    path = tmp_path / "w0007.json"
    # Syntactically valid JSON, but missing the required event_sequence.
    path.write_text(
        '{"schema_version": 2, "strategy_name": "w0007", "catalog_id": "w0007",'
        ' "implementation_revision": "x", "resolved_parameters_hash": "y",'
        ' "feature_hash": "z", "saved_at": "2026-09-23T10:00:00+00:00"}',
        encoding="utf-8",
    )
    target._state_path = path

    assert target._restore_persisted_state() is False
    assert target._positions == {
        "KEEP": {"quantity": 5.0, "entry_price": 10.0, "entry_atr": 1.0, "stop": 9.0}
    }
    assert target._pending_buys == {"KEEP": {"intent_id": "keep-intent"}}
    assert target._selected == ("KEEP",)
    assert target._processed_decision_ids == {"sentinel-decision"}


def test_processed_decision_id_blocks_create_and_submit_without_pending_order() -> None:
    strategy = make_live_strategy()
    decision_id = "w0007:2026-09-23:10:5"
    strategy._active_decision_id = decision_id
    strategy._last_decision_id = decision_id
    strategy._processed_decision_ids = {decision_id}
    strategy._selected = ("BBB",)
    strategy._target_weights = lambda *_a, **_k: {"BBB": 0.5}
    strategy._execution_price = lambda *_a, **_k: 50.0
    strategy._completed_row = lambda *_a, **_k: pd.Series({"atr": 1.5})

    strategy._rebalance(date(2026, 9, 23), 10)

    assert strategy._created == []
    assert strategy._submitted == []
    assert "BBB" not in strategy._pending_buys
    assert any(e.get("event") == "duplicate_decision_suppressed" for e in strategy._diag)


def test_unprocessed_decision_submits_expected_batch_once() -> None:
    strategy = make_live_strategy()
    strategy._selected = ("BBB",)
    strategy._target_weights = lambda *_a, **_k: {"BBB": 0.5}
    strategy._execution_price = lambda *_a, **_k: 50.0
    strategy._completed_row = lambda *_a, **_k: pd.Series({"atr": 1.5})

    strategy._rebalance(date(2026, 9, 23), 10)

    assert len(strategy._created) == 1
    assert len(strategy._submitted) == 1
    assert "BBB" in strategy._pending_buys


def test_broker_authoritative_reconciliation_drops_missing_persisted_position() -> None:
    strategy = _populated_live_strategy()
    strategy._positions["BBB"] = {
        "quantity": 5.0,
        "entry_price": 50.0,
        "entry_atr": 1.5,
        "stop": 47.0,
        "protective_level": 47.0,
        "entry_intent_id": "buy-BBB-3",
        "entry_decision_id": "w0007:2026-09-23:10:1",
    }
    strategy._pending_buys.pop("BBB", None)

    mutations = []
    strategy.create_order = lambda *a, **k: mutations.append("create")
    strategy.submit_order = lambda *a, **k: mutations.append("submit")
    strategy.cancel_order = lambda *a, **k: mutations.append("cancel")

    summary = strategy._reconcile_broker_state_live(
        broker_positions={"AAA": 12.0},
        open_orders=[FakeOrder("AAA", 12.0, buy=False, identifier="stop-1")],
    )

    # AAA is present at the broker but with a different quantity.
    assert strategy._positions["AAA"]["quantity"] == 12.0
    # BBB was persisted but is absent at the broker: removed and flagged.
    assert "BBB" not in strategy._positions
    assert "BBB" in summary["unmatched_persisted"]
    assert any(e.get("event") == "reconcile_position_dropped" for e in strategy._diag)
    # Reconciliation never trades.
    assert mutations == []


def test_reconciliation_rebinds_pending_sell_and_uses_session_artifact_lineage() -> None:
    strategy = _populated_live_strategy()
    # Drop the protective order object so it must be rebound from the artifact.
    strategy._protective = {}
    strategy._order_index = {}

    session_artifact = {
        "kind": "strategy_events",
        "schema_version": 2,
        "lifecycle_trace": [
            {"event": "intent_created", "symbol": "CCC", "side": "sell",
             "intent_id": "sell-CCC-4", "decision_id": "w0007:2026-09-23:10:2",
             "local_order_id": "sell-1", "broker_order_id": None},
            {"event": "intent_created", "symbol": "AAA", "side": "sell",
             "intent_id": "prot-AAA-2", "decision_id": "w0007:2026-09-23:10:1",
             "local_order_id": "stop-1", "broker_order_id": None},
        ],
    }

    open_orders = [
        FakeOrder("AAA", 10.0, buy=False, identifier="stop-1", stop_price=96.0),
        FakeOrder("CCC", 3.0, buy=False, identifier="sell-1"),
    ]

    mutations = []
    strategy.create_order = lambda *a, **k: mutations.append("create")
    strategy.submit_order = lambda *a, **k: mutations.append("submit")
    strategy.cancel_order = lambda *a, **k: mutations.append("cancel")

    strategy._reconcile_broker_state_live(
        broker_positions={"AAA": 10.0},
        open_orders=open_orders,
        session_artifact=session_artifact,
    )

    assert strategy._pending_sell_meta["CCC"]["order"].identifier == "sell-1"
    assert strategy._protective["AAA"].identifier == "stop-1"
    assert "sell-1" in strategy._order_index
    assert strategy._order_index["sell-1"]["intent_id"] == "sell-CCC-4"
    assert mutations == []


def test_persistence_failure_is_fail_open_and_preserves_complete_order_flow() -> None:
    control = make_live_strategy()
    control._persistence_callback = lambda reason, strategy: None

    failing = make_live_strategy()

    def _boom(reason: str, strategy: RegistryHtsStrategy) -> None:
        raise OSError("simulated disk failure")

    failing._persistence_callback = _boom

    control._submit_buy("BBB", 5, 50.0, 1.5, "selection_entry")
    failing._submit_buy("BBB", 5, 50.0, 1.5, "selection_entry")

    def _order_view(order):
        return {
            "symbol": order.asset.symbol,
            "quantity": order.quantity,
            "side": order.side,
            "stop_price": order.stop_price,
            "identifier": order.identifier,
        }

    assert [_order_view(o) for o in control._created] == [_order_view(o) for o in failing._created]
    assert [_order_view(o) for o in control._submitted] == [_order_view(o) for o in failing._submitted]

    control_pending = dict(control._pending_buys["BBB"])
    failing_pending = dict(failing._pending_buys["BBB"])
    for pending in (control_pending, failing_pending):
        pending.pop("order", None)
    assert control_pending == failing_pending
    assert control_pending["intent_id"] == failing_pending["intent_id"]
    assert control_pending["decision_id"] == failing_pending["decision_id"]
    assert control_pending["requested_quantity"] == failing_pending["requested_quantity"] == 5.0

    assert any(event.get("event") == "persistence_failure" for event in failing._diag)
    assert not any(event.get("event") == "persistence_failure" for event in control._diag)


def test_partial_fill_round_trip_retains_event_and_cumulative_quantities(tmp_path: Path) -> None:
    source = make_live_strategy()
    source._pending_buys["BBB"] = {
        "quantity": 10.0,
        "atr": 1.5,
        "order": FakeOrder("BBB", 10.0, buy=True, identifier="buy-2"),
        "requested_quantity": 10.0,
        "event_time_atr": 1.5,
        "intent_id": "buy-BBB-9",
        "decision_id": "w0007:2026-09-23:10:9",
        "local_order_id": "buy-2",
        "broker_order_id": "buy-2",
        "cumulative_filled_quantity": 4.0,
        "last_fill_event_quantity": 2.0,
        "reason": "selection_entry",
    }
    source._event_sequence = 7

    state_path = tmp_path / "w0007.json"
    source._state_path = state_path
    AtomicJsonStore(state_path).write_state(serialize_runtime_state(source, reason="partial"))

    target = make_live_strategy()
    target._state_path = state_path
    assert target._restore_persisted_state() is True

    pending = target._pending_buys["BBB"]
    assert pending["requested_quantity"] == 10.0
    assert pending["cumulative_filled_quantity"] == 4.0
    assert pending["last_fill_event_quantity"] == 2.0
    assert pending["local_order_id"] == "buy-2"
    assert target._event_sequence == 7


def test_session_end_checkpoint_marks_artifact_complete() -> None:
    strategy = make_live_strategy()
    checkpoints: list[str] = []
    strategy._persistence_callback = lambda reason, source: checkpoints.append(reason)
    strategy._portfolio_value = 101_500.0
    strategy._cash = 50_000.0

    result = strategy.after_market_closes()

    assert result["complete"] is True
    assert result["equity"] == 101_500.0
    assert result["cash"] == 50_000.0
    assert result["session"] == "2026-09-23"
    assert "after_market_closes" in checkpoints
    assert strategy._session_end_events[-1] == result

    payload = build_strategy_event_payload(strategy, session="2026-09-23")
    validate_strategy_event_payload(payload)
