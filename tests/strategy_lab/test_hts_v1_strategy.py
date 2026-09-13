from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

from strategy_lab.hts_v1_core import DecisionJournal, OrderIntent
from strategy_lab.hts_v1_strategy import HtsV1PaperStrategy


class _CallbackCore:
    def __init__(self, intent: OrderIntent) -> None:
        self.inflight = [intent]
        self.journal = DecisionJournal()
        self.fill_calls: list[tuple[OrderIntent, float, pd.Timestamp]] = []

    def acknowledge_fill(self, intent: OrderIntent, price: float, filled_at: pd.Timestamp) -> None:
        self.fill_calls.append((intent, price, filled_at))
        self.inflight.remove(intent)

    def snapshot(self) -> dict:
        return {"config_fingerprint": "test", "inflight": [item.as_dict() for item in self.inflight]}


def _intent() -> OrderIntent:
    return OrderIntent(
        decision_id="decision-1", timestamp=pd.Timestamp("2024-01-08 10:00", tz="America/New_York"),
        symbol="AAA", side="buy", quantity=5, reason="selection_entry", reference_price=20.0,
    )


def _strategy(tmp_path: Path, core: _CallbackCore) -> HtsV1PaperStrategy:
    # Callback tests do not need Strategy.__init__; they isolate the adapter's
    # order mapping and persistence behaviour from a real broker connection.
    strategy = object.__new__(HtsV1PaperStrategy)
    strategy._core = core
    strategy._config = SimpleNamespace(timezone="America/New_York")
    strategy._state_path = tmp_path / "state.json"
    strategy._broker_order_to_intent = {}
    strategy._partial_fills = {}
    strategy._dry_run = False
    strategy._allow_paper_orders = True
    strategy.log_message = lambda *args, **kwargs: None
    strategy.get_datetime = lambda: pd.Timestamp("2024-01-08 12:00", tz="America/New_York")
    strategy.create_order = lambda symbol, quantity, side: SimpleNamespace(identifier="local-1")
    strategy.submit_order = lambda order: SimpleNamespace(identifier="broker-9")
    strategy.reconciliation_calls = 0

    def reconcile() -> None:
        strategy.reconciliation_calls += 1

    strategy._reconcile_broker_positions = reconcile
    return strategy


def test_created_and_broker_order_ids_both_map_to_one_intent_and_full_callback_persists(tmp_path: Path) -> None:
    intent = _intent()
    core = _CallbackCore(intent)
    strategy = _strategy(tmp_path, core)

    strategy._submit_intent(intent)

    assert strategy._broker_order_to_intent == {"local-1": "decision-1", "broker-9": "decision-1"}
    state = json.loads(strategy._state_path.read_text())
    assert state["broker_order_to_intent"]["broker-9"] == "decision-1"

    strategy.on_filled_order(None, SimpleNamespace(identifier="broker-9"), 20.25, 5, 1)

    assert core.fill_calls[0][0] == intent
    assert core.fill_calls[0][1] == 20.25
    assert strategy._broker_order_to_intent == {}
    assert json.loads(strategy._state_path.read_text())["broker_order_to_intent"] == {}


def test_partial_callback_is_journaled_reconciled_and_never_acknowledges_full_fill(tmp_path: Path) -> None:
    intent = _intent()
    core = _CallbackCore(intent)
    strategy = _strategy(tmp_path, core)
    strategy._submit_intent(intent)

    strategy.on_partially_filled_order(None, SimpleNamespace(identifier="broker-9"), 20.0, 2, 1)

    assert core.fill_calls == []
    assert strategy.reconciliation_calls == 1
    partial = strategy._partial_fills["broker-9"]
    assert partial["expected_quantity"] == 5
    assert partial["reported_quantity"] == 2.0
    assert core.journal.rows[-1]["event"] == "partial_fill"
    assert json.loads(strategy._state_path.read_text())["partial_fills"]["broker-9"]["decision_id"] == "decision-1"


def test_short_full_callback_is_treated_as_partial_not_a_position_fill(tmp_path: Path) -> None:
    intent = _intent()
    core = _CallbackCore(intent)
    strategy = _strategy(tmp_path, core)
    strategy._submit_intent(intent)

    strategy.on_filled_order(None, SimpleNamespace(identifier="broker-9"), 20.0, 4, 1)

    assert core.fill_calls == []
    assert strategy.reconciliation_calls == 1
    assert strategy._partial_fills["broker-9"]["reported_quantity"] == 4.0


def test_mapping_exists_before_a_synchronous_submit_callback(tmp_path: Path) -> None:
    intent = _intent()
    core = _CallbackCore(intent)
    strategy = _strategy(tmp_path, core)

    def synchronous_submit(order):
        strategy.on_filled_order(None, order, 20.0, 5, 1)
        return SimpleNamespace(identifier="broker-9")

    strategy.submit_order = synchronous_submit
    strategy._submit_intent(intent)

    assert core.fill_calls[0][0] == intent
    assert strategy._broker_order_to_intent == {}
