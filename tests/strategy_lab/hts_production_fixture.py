"""Shared deterministic fixtures for the fix-3 production decision contract.

These build a broker-free ``RegistryHtsStrategy`` whose only faked boundary is
``create_order``/``submit_order``.  All data access is injected so the *real*
``_select``/``_target_weights``/``_rebalance``/fill-callback methods run without
network, credentials, or a data archive.
"""
from __future__ import annotations

import sys
import types
from datetime import date
from pathlib import Path

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from strategy_lab.native_experiments import RegistryHtsStrategy  # noqa: E402

SESSION = date(2026, 9, 23)
SIGNAL_SESSION = date(2026, 9, 22)
ITERATION_TIME = pd.Timestamp("2026-09-23 10:00:05", tz="America/New_York")


class FakeOrder:
    def __init__(self, symbol, quantity, buy=True, transactions=None, identifier=None,
                 stop_price=None):
        self.asset = types.SimpleNamespace(symbol=symbol)
        self.quantity = quantity
        self.transactions = list(transactions or [])
        self.identifier = identifier or f"ord-{symbol}-{'buy' if buy else 'sell'}"
        self.side = "buy" if buy else "sell"
        self.stop_price = stop_price
        self._buy = buy

    def is_buy_order(self) -> bool:
        return self._buy

    def is_canceled(self) -> bool:
        return False

    def is_filled(self) -> bool:
        return False


def txn(price: float, quantity: float) -> types.SimpleNamespace:
    return types.SimpleNamespace(price=price, quantity=quantity)


def identity() -> dict[str, str]:
    return {
        "strategy_name": "w0007",
        "catalog_id": "w0007",
        "implementation_revision": "hts-native-v2-test",
        "resolved_parameters_hash": "params-w0007",
        "feature_hash": "feature-w0007",
    }


class LiveStrategy(RegistryHtsStrategy):
    """RegistryHtsStrategy with a broker-free cash/equity surface."""

    @property
    def cash(self) -> float:
        return float(getattr(self, "_cash", 0.0) or 0.0)

    @property
    def portfolio_value(self) -> float:
        return float(getattr(self, "_portfolio_value", 0.0) or 0.0)


def _base_params() -> dict:
    return {
        "exit_mode": "resting-stop-atr",
        "atr_k": 2.0,
        "reentry_cooldown_bars": 0,
        "cost_bps_per_side": 3.5,
        "top_n": 2,
        "rank_score": "r20",
        "min_median_dollar_volume": 0.0,
        "require_positive_return": False,
        "correlation_screen": None,
        "exposure_group_limit": None,
        "rank_buffer": None,
        "min_position_holding_bars": 0,
        "weight_mode": "equal-slots",
        "gross_target": 0.995,
        "per_symbol_cap": None,
        "stop_distance_budget": None,
        "vol_target": None,
        "risk_contribution_cap": None,
        "leveraged_cap": None,
        "rebalance_hour": 10,
        "signal_hour": 9,
        "rebalance_schedule": "daily",
        "market_gate": "none",
        "risk_off_gate": "none",
        "vol_covariance_sessions": 20,
        "return_period": 20,
        "min_trade_edge_bps": 0.0,
        "universe_symbols": ["AAA", "BBB"],
    }


def _init_streams(s: LiveStrategy) -> None:
    s._positions = {}
    s._pending_buys = {}
    s._cancelled_pending_buys = {}
    s._pending_sells = set()
    s._pending_sell_reason = {}
    s._pending_sell_meta = {}
    s._protective = {}
    s._protective_meta = {}
    s._protective_state = {}
    s._stop_exit_context = {}
    s._stop_gap_events = []
    s._lifecycle_trace = []
    s._journal = []
    s._rejections = []
    s._diag = []
    s._risk_cap_events = []
    s._entry_edge_events = []
    s._risk_off_transitions = []
    s._deferred_rebalance_events = []
    s._session_end_events = []
    s._decision_snapshots = []
    s._decision_capture = None
    s._submission_scope_decision_id = None
    s._cooldowns = {}
    s._sessions = [SIGNAL_SESSION, SESSION]
    s._session_index = {SIGNAL_SESSION: 0, SESSION: 1}
    s._ordered = ["AAA", "BBB"]
    s._universe_order = {"AAA": 0, "BBB": 1}
    s._selected = ()
    s._ranks = {}
    s._signal_day = None
    s._risk_off_active = False
    s._last_risk_gate_session = None
    s._re_risk_deadline_index = None
    s._risk_off_race_symbols = set()
    s._risk_off_flatten_count = 0
    s._minimum_hold_deferrals = 0
    s._event_sequence = 0
    s._decision_sequence = 0
    s._intent_sequence = 0
    s._active_decision_id = None
    s._last_decision_id = None
    s._processed_decision_ids = set()
    s._order_index = {}
    s._unmatched_broker_orders = {}
    s._deferred_rebalance = None
    s._audit_identity = identity()
    s._strategy_name = "w0007"
    s._catalog_id = "w0007"
    s._implementation_revision = "hts-native-v2-test"
    s._resolved_parameters_hash = "params-w0007"
    s._feature_hash = "feature-w0007"
    s._portfolio_value = 100_000.0
    s._cash = 100_000.0
    s._last_quote_snapshot = None
    s._persistence_callback = None
    s._state_path = None
    s._created = []
    s._submitted = []


def make_live_strategy(params: dict | None = None) -> LiveStrategy:
    """An empty live strategy with deterministic timestamps and a fake broker."""
    s = LiveStrategy.__new__(LiveStrategy)
    s._params = dict(params or _base_params())
    _init_streams(s)
    s.get_datetime = lambda: ITERATION_TIME
    _install_fake_broker(s)
    return s


def make_production_strategy(params: dict | None = None) -> LiveStrategy:
    """A strategy whose injected market data drives the real rebalance path."""
    s = make_live_strategy(params)

    def previous_session(day):
        return SIGNAL_SESSION

    def daily_row(symbol, session):
        return pd.Series({
            "close": 101.0 if symbol == "AAA" else 51.0,
            "sma": 100.0 if symbol == "AAA" else 50.0,
            "ret": 0.10 if symbol == "AAA" else 0.20,
            "mdv": 10_000_000.0,
            "vol20": 0.20 if symbol == "AAA" else 0.30,
        })

    def completed_row(symbol, stamp):
        return pd.Series({"atr": 1.0 if symbol == "AAA" else 2.0})

    def execution_price(symbol, stamp):
        return 100.0 if symbol == "AAA" else 50.0

    s._previous_session = previous_session
    s._daily_row = daily_row
    s._completed_row = completed_row
    s._execution_price = execution_price
    s._market_gate = lambda session: True
    s._correlation = lambda a, b, session: 0.1
    s._ctx = types.SimpleNamespace(inputs=types.SimpleNamespace(daily={}))
    return s


def _install_fake_broker(s: LiveStrategy) -> None:
    order_sequence = 0

    def create_order(symbol, quantity=None, side=None, order_type=None, stop_price=None, **_kw):
        nonlocal order_sequence
        order_sequence += 1
        order = FakeOrder(
            symbol,
            quantity,
            buy=(side == "buy"),
            stop_price=stop_price,
            identifier=f"ord-{order_sequence}-{symbol}-{side}",
        )
        order.side = side
        s._created.append(order)
        return order

    def submit_order(order):
        s._submitted.append(order)
        return order

    s.create_order = create_order
    s.submit_order = submit_order
    s.cancel_order = lambda order=None: s.on_canceled_order(order) if order is not None else None
    s.update_broker_balances = lambda **_kwargs: None
    s.get_tracked_positions = lambda: ()


def run_production_rebalance(s: LiveStrategy, day: date = SESSION, hour: int = 10):
    """Run the real decision path: begin -> select -> rebalance."""
    s._begin_decision(day, hour)
    s._selected = s._select(day)
    s._rebalance(day, hour)
    return s._selected


def complete_entry_lifecycle(s: LiveStrategy, symbol: str, *,
                             fragments=(500.0, 249.0), price: float = 50.0) -> None:
    """Drive one buy order through partial fills and a final fill."""
    pending = s._pending_buys[symbol]
    order = pending["order"]
    for fragment in fragments[:-1]:
        order.transactions.append(txn(price, fragment))
        s.on_partially_filled_order(None, order, price, fragment, 1)
    final = fragments[-1]
    order.transactions.append(txn(price, final))
    s.on_filled_order(None, order, price, final, 1)
