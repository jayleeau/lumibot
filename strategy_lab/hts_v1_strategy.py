"""LumiBot paper wrapper for the provider-neutral HTS v1 decision core.

The wrapper is intentionally conservative: it runs dry by default and only
submits orders when both ``dry_run=False`` and ``allow_paper_orders=True`` are
set.  A caller supplies normalized completed-bar features through a provider;
this prevents the live loop from quietly using a different bar definition than
the backtest.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping, Protocol

import pandas as pd

from lumibot.strategies.strategy import Strategy
from strategy_lab.hts_v1_core import (
    DecisionJournal,
    HtsV1Config,
    HtsV1DecisionCore,
    OrderIntent,
    PreparedFeatures,
)


class HtsV1PaperDataProvider(Protocol):
    """Supplies exactly the normalized cached/bar-recorded input for HTS v1."""

    def prepared_features(self) -> PreparedFeatures:
        """Return features with a row for every completed hourly bar."""


class HtsV1PaperStrategy(Strategy):
    """Paper-only execution adapter that delegates every decision to HTS v1 core."""

    parameters = {
        "dry_run": True,
        "allow_paper_orders": False,
        "state_path": None,
        "journal_path": None,
        "data_provider": None,
        "config": None,
    }

    def initialize(self) -> None:
        self.sleeptime = "1H"
        self.minutes_before_closing = 1
        config = self.parameters.get("config")
        if isinstance(config, Mapping):
            config = HtsV1Config(**config)
        if not isinstance(config, HtsV1Config):
            raise ValueError("parameters['config'] must be an HtsV1Config or its mapping")
        provider = self.parameters.get("data_provider")
        if provider is None or not hasattr(provider, "prepared_features"):
            raise ValueError("parameters['data_provider'] must provide normalized HTS features")
        self._config = config
        self._provider: HtsV1PaperDataProvider = provider
        self._features = provider.prepared_features()
        journal_path = self.parameters.get("journal_path")
        self._core = HtsV1DecisionCore(
            config, self._features, DecisionJournal(Path(journal_path)) if journal_path else None
        )
        self._state_path = Path(self.parameters["state_path"]) if self.parameters.get("state_path") else None
        self._dry_run = bool(self.parameters.get("dry_run", True))
        self._allow_paper_orders = bool(self.parameters.get("allow_paper_orders", False))
        self._broker_order_to_intent: dict[str, str] = {}
        self._partial_fills: dict[str, dict[str, Any]] = {}
        if not self._dry_run and not self._allow_paper_orders:
            raise ValueError("set allow_paper_orders=True before HTS may submit broker orders")
        self._restore_state()
        self._reconcile_broker_positions()

    def _restore_state(self) -> None:
        if self._state_path is None or not self._state_path.exists():
            return
        with self._state_path.open(encoding="utf-8") as source:
            state = json.load(source)
        self._core.restore(state)
        self._broker_order_to_intent = {
            str(order_id): str(decision_id)
            for order_id, decision_id in state.get("broker_order_to_intent", {}).items()
        }
        self._partial_fills = {
            str(order_id): dict(value)
            for order_id, value in state.get("partial_fills", {}).items()
        }

    def _save_state(self) -> None:
        if self._state_path is None:
            return
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._state_path.with_suffix(self._state_path.suffix + ".tmp")
        with temporary.open("w", encoding="utf-8") as output:
            state = self._core.snapshot()
            state["broker_order_to_intent"] = self._broker_order_to_intent
            state["partial_fills"] = self._partial_fills
            json.dump(state, output, indent=2, sort_keys=True)
        temporary.replace(self._state_path)

    def _reconcile_broker_positions(self) -> None:
        """Read broker state at startup and reject unknown holdings deterministically."""
        quantities: dict[str, int] = {}
        for position in self.get_positions():
            symbol = getattr(getattr(position, "asset", None), "symbol", None)
            if symbol:
                quantities[str(symbol)] = int(position.quantity)
        self._core.reconcile(quantities)

    def on_trading_iteration(self) -> None:
        now = pd.Timestamp(self.get_datetime())
        if now.tzinfo is None:
            now = now.tz_localize(self._config.timezone)
        timestamp = now.tz_convert(self._config.timezone).floor("h")
        # A runtime adapter must only invoke us once its provider has confirmed
        # this timestamp is complete.  The core rejects duplicates/out-of-order bars.
        portfolio_value = float(self.get_portfolio_value())
        orders = self._core.process_completed_bar(timestamp, portfolio_value)
        self._save_state()
        for intent in orders:
            if intent.timestamp >= timestamp:
                # This was just queued from a completed decision and cannot be
                # executed until a subsequent bar.  It remains in core state.
                continue
            self._submit_intent(intent)

    @staticmethod
    def _order_identifier(order: object) -> str | None:
        """Extract the local/broker identifier used by LumiBot callbacks."""
        for field in ("identifier", "id", "order_id"):
            value = getattr(order, field, None)
            if value is not None and str(value):
                return str(value)
        return None

    def _event_timestamp(self) -> pd.Timestamp:
        now = pd.Timestamp(self.get_datetime())
        if now.tzinfo is None:
            return now.tz_localize(self._config.timezone)
        return now.tz_convert(self._config.timezone)

    def _intent_for_order(self, order: object) -> tuple[str, OrderIntent] | None:
        order_id = self._order_identifier(order)
        if order_id is None:
            self.log_message("HTS v1 ignored callback without a broker order identifier", color="red")
            return None
        decision_id = self._broker_order_to_intent.get(order_id)
        intent = next((item for item in self._core.inflight if item.decision_id == decision_id), None)
        if intent is None:
            self.log_message(f"HTS v1 ignored callback for unknown order {order_id}", color="red")
            return None
        return order_id, intent

    def _remove_order_mapping(self, decision_id: str) -> None:
        self._broker_order_to_intent = {
            order_id: mapped_id
            for order_id, mapped_id in self._broker_order_to_intent.items()
            if mapped_id != decision_id
        }
        self._partial_fills = {
            order_id: row
            for order_id, row in self._partial_fills.items()
            if row.get("decision_id") != decision_id
        }

    def _submit_intent(self, intent: OrderIntent) -> None:
        if self._dry_run:
            self.log_message(f"HTS v1 dry run: {intent.side} {intent.quantity} {intent.symbol} ({intent.reason})")
            return
        order = self.create_order(intent.symbol, intent.quantity, intent.side)
        created_id = self._order_identifier(order)
        if created_id is None:
            raise RuntimeError("HTS v1 broker order has no identifier before submission")
        # Register before dispatch: a backtesting broker or a fast paper
        # broker may invoke on_filled_order synchronously from submit_order.
        self._broker_order_to_intent[created_id] = intent.decision_id
        self._save_state()
        try:
            submitted = self.submit_order(order)
        except Exception:
            self._remove_order_mapping(intent.decision_id)
            self._save_state()
            raise
        submitted_id = self._order_identifier(submitted) if submitted is not None else created_id
        # Some broker adapters mutate the identifier during submission. Keep
        # both aliases until the fill callback arrives, then remove both.
        still_inflight = any(item.decision_id == intent.decision_id for item in self._core.inflight)
        if submitted_id is not None and still_inflight:
            self._broker_order_to_intent[submitted_id] = intent.decision_id
        self._save_state()
        self.log_message(f"HTS v1 submitted {intent.decision_id}: {intent.side} {intent.quantity} {intent.symbol}")

    def on_partially_filled_order(self, position, order, price, quantity, multiplier) -> None:
        """Journal and fail closed on partial fills; never promote them to a position."""
        resolved = self._intent_for_order(order)
        if resolved is None:
            return
        order_id, intent = resolved
        self._partial_fills[order_id] = {
            "decision_id": intent.decision_id,
            "symbol": intent.symbol,
            "expected_quantity": intent.quantity,
            "reported_quantity": float(quantity),
            "price": float(price),
            "timestamp": self._event_timestamp().isoformat(),
        }
        self._core.journal.append({"event": "partial_fill", "order_id": order_id, **self._partial_fills[order_id]})
        self._save_state()
        # Reconciliation intentionally raises if the broker now holds a partial
        # position. That stops new decisions before they can compound exposure.
        self._reconcile_broker_positions()

    def on_filled_order(self, position, order, price, quantity, multiplier) -> None:
        """Apply only a complete broker fill matched to an HTS intent."""
        resolved = self._intent_for_order(order)
        if resolved is None:
            return
        _order_id, intent = resolved
        if int(quantity) != intent.quantity:
            # A broker must not be allowed to invoke a full-fill callback with
            # a smaller quantity and make the core believe the target filled.
            self.on_partially_filled_order(position, order, price, quantity, multiplier)
            return
        self._core.acknowledge_fill(intent, float(price), self._event_timestamp())
        self._remove_order_mapping(intent.decision_id)
        self._save_state()
