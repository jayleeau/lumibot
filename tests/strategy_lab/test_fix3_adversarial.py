"""Adversarial regressions for the fix-3 decision and restart contracts.

These counterexamples reproduce the independent re-validation failures.  They
exercise the real strategy methods while keeping the broker boundary fake and
offline.
"""
from __future__ import annotations

import copy
import sys
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from hts_production_fixture import (  # noqa: E402
    FakeOrder,
    make_live_strategy,
    make_production_strategy,
    run_production_rebalance,
)
from strategy_lab.hts_audit import (  # noqa: E402
    AtomicJsonStore,
    AuditValidationError,
    build_strategy_event_payload,
    serialize_runtime_state,
    validate_runtime_state,
)

import verify_paper_six_parity as cli  # noqa: E402


def _held_position(quantity: float = 10.0) -> dict[str, object]:
    return {
        "quantity": quantity,
        "entry_price": 100.0,
        "entry_atr": 2.0,
        "entry_session": date(2026, 9, 22),
        "entry_session_index": 0,
        "stop": 96.0,
        "highest_high": 105.0,
        "post_entry_highs": [101.0, 105.0],
        "breach_count": 0,
        "seeded": True,
        "protective_level": 96.0,
        "entry_intent_id": "buy-AAA-1",
        "entry_decision_id": "w0007:2026-09-23:10:1",
    }


def test_snapshot_exists_before_every_broker_submission_and_captures_pre_submit_book() -> None:
    strategy = make_production_strategy()
    snapshots_at_submit: list[int] = []
    original_submit = strategy.submit_order

    def observe_submit(order):
        snapshots_at_submit.append(len(strategy._decision_snapshots))
        return original_submit(order)

    strategy.submit_order = observe_submit
    run_production_rebalance(strategy)

    assert snapshots_at_submit
    assert all(count >= 1 for count in snapshots_at_submit)
    snapshot = strategy._decision_snapshots[0]
    assert snapshot["book"]["pending_buys"] == []
    assert snapshot["book"]["pending_sells"] == []


@pytest.mark.parametrize("risk_off", (False, True))
def test_deferred_queue_emits_snapshot_without_contacting_broker(risk_off: bool) -> None:
    strategy = make_production_strategy()
    day = date(2026, 9, 23)
    strategy._begin_decision(day, 15)
    if risk_off:
        strategy._positions["AAA"] = _held_position()
        strategy._risk_off_active = True
        strategy._queue_risk_off_flatten_after_last_observable_iteration(day, 15)
    else:
        strategy._selected = strategy._select(day)
        strategy._completed_row = lambda symbol, _stamp: pd.Series({
            "close": 100.0 if symbol == "AAA" else 50.0,
            "atr": 2.0 if symbol == "AAA" else 1.5,
        })
        strategy._queue_rebalance_after_last_observable_iteration(day, 15)

    assert strategy._created == []
    assert strategy._submitted == []
    assert strategy._decision_snapshots
    assert strategy._decision_snapshots[-1]["book"]["pending_buys"] == []
    assert strategy._deferred_rebalance["submission_started"] is False
    assert strategy._deferred_rebalance["decision_id"] in strategy._processed_decision_ids


def test_processed_sell_decision_never_contacts_broker() -> None:
    strategy = make_live_strategy()
    decision_id = "w0007:2026-09-23:10:processed-sell"
    strategy._positions["AAA"] = _held_position()
    strategy._processed_decision_ids = {decision_id}

    result = strategy._submit_sell(
        "AAA",
        "selection_change",
        100.0,
        decision_id=decision_id,
        intent_id="sell-AAA-fixed",
    )

    assert result is None
    assert strategy._created == []
    assert strategy._submitted == []
    assert strategy._pending_sells == set()
    assert strategy._pending_sell_meta == {}
    assert any(
        event.get("event") == "duplicate_decision_suppressed"
        and event.get("decision_id") == decision_id
        for event in strategy._diag
    )


def test_deferred_batch_that_already_started_never_contacts_broker() -> None:
    strategy = make_live_strategy()
    decision_id = "w0007:2026-09-22:15:deferred"
    strategy._positions["AAA"] = _held_position()
    strategy._processed_decision_ids = {decision_id}
    strategy._deferred_rebalance = {
        "kind": "risk_off_flatten",
        "decision_day": "2026-09-22",
        "decision_id": decision_id,
        "logical_rebalance_hour": 15,
        "submission_started": True,
        "sells": [{
            "symbol": "AAA",
            "reason": "global_risk_off",
            "reference": 100.0,
            "intent_id": "sell-AAA-deferred",
            "decision_id": decision_id,
        }],
    }
    strategy._execution_price = lambda *_args, **_kwargs: 100.0

    strategy._flush_deferred_rebalance(date(2026, 9, 23), 9)

    assert strategy._created == []
    assert strategy._submitted == []
    assert strategy._pending_sells == set()
    assert strategy._deferred_rebalance is None
    assert any(
        event.get("event") == "duplicate_decision_suppressed"
        and event.get("decision_id") == decision_id
        and event.get("path") == "deferred"
        for event in strategy._diag
    )


@pytest.mark.parametrize(
    "field",
    (
        "symbol",
        "side",
        "local_order_id",
        "broker_order_id",
        "requested_quantity",
        "reason",
        "cumulative_filled_quantity",
        "last_fill_event_quantity",
    ),
)
def test_incomplete_pending_sell_lineage_is_rejected(field: str) -> None:
    strategy = make_live_strategy()
    strategy._positions["AAA"] = _held_position()
    strategy._pending_sells.add("AAA")
    strategy._pending_sell_reason["AAA"] = "selection_change"
    strategy._pending_sell_meta["AAA"] = {
        "intent_id": "sell-AAA-2",
        "decision_id": "w0007:2026-09-23:10:2",
        "symbol": "AAA",
        "side": "sell",
        "local_order_id": "sell-1",
        "broker_order_id": "sell-1",
        "requested_quantity": 10.0,
        "reason": "selection_change",
        "cumulative_filled_quantity": 0.0,
        "last_fill_event_quantity": 0.0,
    }
    state = serialize_runtime_state(strategy, reason="adversarial")
    del state["pending_sells"]["AAA"][field]

    with pytest.raises(AuditValidationError) as excinfo:
        validate_runtime_state(state)
    assert excinfo.value.field == f"pending_sells.AAA.{field}"


def test_unmatched_pending_state_is_removed_during_reconciliation() -> None:
    strategy = make_live_strategy()
    strategy._pending_buys["BBB"] = {
        "quantity": 5.0,
        "atr": 1.5,
        "requested_quantity": 5.0,
        "event_time_atr": 1.5,
        "intent_id": "buy-BBB-3",
        "decision_id": "w0007:2026-09-23:10:3",
        "local_order_id": "buy-3",
        "broker_order_id": "buy-3",
        "cumulative_filled_quantity": 0.0,
        "last_fill_event_quantity": 0.0,
        "reason": "selection_entry",
        "order": None,
    }
    strategy._pending_sells.add("CCC")
    strategy._pending_sell_reason["CCC"] = "selection_change"
    strategy._pending_sell_meta["CCC"] = {
        "intent_id": "sell-CCC-4",
        "decision_id": "w0007:2026-09-23:10:4",
        "symbol": "CCC",
        "side": "sell",
        "local_order_id": "sell-4",
        "broker_order_id": "sell-4",
        "requested_quantity": 3.0,
        "reason": "selection_change",
        "cumulative_filled_quantity": 0.0,
        "last_fill_event_quantity": 0.0,
        "order": None,
    }

    summary = strategy._reconcile_broker_state_live(
        broker_positions={},
        open_orders=[],
    )

    assert "BBB" not in strategy._pending_buys
    assert "CCC" not in strategy._pending_sells
    assert "CCC" not in strategy._pending_sell_meta
    assert "CCC" not in strategy._pending_sell_reason
    assert {"BBB", "CCC"}.issubset(set(summary["unmatched_persisted"]))
    assert {
        "w0007:2026-09-23:10:3",
        "w0007:2026-09-23:10:4",
    }.issubset(strategy._processed_decision_ids)


def test_non_order_virtual_stop_diagnostic_never_enters_typed_lifecycle() -> None:
    strategy = make_live_strategy()
    strategy._positions["AAA"] = _held_position()
    strategy._execution_price = lambda *_args, **_kwargs: None
    row = pd.Series(
        {"close": 95.0, "atr": 2.0},
        name=pd.Timestamp("2026-09-23 09:00:00", tz="America/New_York"),
    )
    event_time = row.name + pd.Timedelta(hours=1)

    submitted = strategy._submit_virtual_stop(
        "AAA",
        mode="virtual-close-confirm-1",
        reason="virtual_stop",
        row=row,
        stamp=event_time,
        engine_time=event_time,
    )

    assert submitted is False
    assert strategy._lifecycle_trace == []
    assert any(
        event.get("event") == "virtual_stop_deferred_no_executable_bar"
        for event in strategy._journal + strategy._diag
    )


def test_repeatable_sessions_are_aggregated_into_one_native_strategy_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    strategy = make_production_strategy()
    run_production_rebalance(strategy)
    for pending in strategy._pending_buys.values():
        strategy.on_new_order(pending["order"])

    paths: list[str] = []
    for session, equity in (("2026-09-22", 100_000.0), ("2026-09-23", 101_000.0)):
        payload = build_strategy_event_payload(strategy, session=session)
        payload["session_end_events"] = [
            {"event": "session_end", "session": session, "equity": equity, "cash": equity}
        ]
        path = tmp_path / f"{session}.json"
        AtomicJsonStore(path).write_session(payload)
        paths.append(str(path))

    native_calls: list[dict[str, object]] = []

    def fake_run_native(payload, native_out):
        native_calls.append(dict(payload))
        return {"2026-09-22": 100_000.0, "2026-09-23": 101_000.0}, None

    monkeypatch.setattr(cli, "_run_native", fake_run_native)
    report = cli.verify_sessions(
        paths,
        run_native=True,
        native_out=tmp_path / "native",
        compare_pnl=True,
    )

    assert len(native_calls) == 1
    assert native_calls[0]["window"] == {"start": "2026-09-22", "end": "2026-09-23"}
    assert report["status"] == "pass"
