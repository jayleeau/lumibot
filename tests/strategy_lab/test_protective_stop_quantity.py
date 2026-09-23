"""Regression tests: protective stop / position size must use the CUMULATIVE
filled quantity, not a single broker fill event's fragment.

Live incident 2026-09-22: paper6 resting-stop bots (w0007/w0006) held BITX/MSTR
fully filled but placed protective stops for only the last fill fragment
(MSTR stopped at 1 share of 31/39). LumiBot delivers ``on_filled_order`` once per
fill event and passes that event's quantity (see brokers/broker.py
``_process_filled_order`` -> ``order.add_transaction``), so a multi-execution
market order arrives with only the final fragment as ``quantity``.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from strategy_lab.native_experiments import RegistryHtsStrategy  # noqa: E402


class FakeOrder:
    def __init__(self, symbol, quantity, buy=True, transactions=None):
        self.asset = types.SimpleNamespace(symbol=symbol)
        self.quantity = quantity
        self.transactions = list(transactions or [])
        self.identifier = f"ord-{symbol}-{'buy' if buy else 'sell'}"
        self.side = "buy" if buy else "sell"
        self.stop_price = None
        self._buy = buy

    def is_buy_order(self):
        return self._buy

    def add_transaction(self, price, quantity):
        self.transactions.append(types.SimpleNamespace(price=price, quantity=quantity))


def make_strategy(params=None):
    s = RegistryHtsStrategy.__new__(RegistryHtsStrategy)
    s._params = dict(params or {"exit_mode": "resting-stop-atr", "atr_k": 2.0})
    s._pending_buys = {}
    s._cancelled_pending_buys = {}
    s._positions = {}
    s._protective = {}
    s._pending_sells = set()
    s._pending_sell_reason = {}
    s._stop_exit_context = {}
    s._lifecycle_trace = []
    s._stop_gap_events = []
    s._journal = []
    s._cooldowns = {}
    s._session_index = {}
    s._risk_off_active = False
    s._risk_off_race_symbols = set()
    s.get_datetime = lambda: pd.Timestamp("2026-09-22 10:01:00", tz="America/New_York")
    s._created = []
    s._submitted = []

    def create_order(symbol, quantity=None, side=None, order_type=None, stop_price=None, **kw):
        o = FakeOrder(symbol, quantity, buy=(side == "buy"))
        o.side = side
        o.stop_price = stop_price
        s._created.append(o)
        return o

    def submit_order(order):
        s._submitted.append(order)
        return order

    s.create_order = create_order
    s.submit_order = submit_order
    s.cancel_order = lambda order=None: None
    s._cancel_protective = lambda symbol: None
    return s


def _multi_event(symbol, total, events):
    """An order whose transactions sum to ``total`` (the last event is the fragment)."""
    txns = [types.SimpleNamespace(price=1.0, quantity=q) for q in events]
    assert sum(events) == total
    return FakeOrder(symbol, total, transactions=txns)


def test_resting_stop_uses_cumulative_quantity_on_multi_event_fill():
    s = make_strategy()
    s._pending_buys["BITX"] = {"atr": 0.5}
    order = _multi_event("BITX", 316, [253, 34, 29])  # last event (29) is the fragment
    pos = types.SimpleNamespace(quantity=316)
    s.on_filled_order(pos, order, price=20.97, quantity=29, multiplier=1)

    assert s._positions["BITX"]["quantity"] == 316
    stops = [o for o in s._created if o.side == "sell" and o.stop_price is not None]
    assert len(stops) == 1
    assert stops[0].quantity == 316, "protective stop must cover the full held position"


def test_entry_quantity_falls_back_to_order_quantity():
    s = make_strategy()
    s._pending_buys["MSTR"] = {"atr": 1.0}
    order = FakeOrder("MSTR", 39, transactions=[])  # no transactions recorded by broker
    pos = types.SimpleNamespace(quantity=0)
    s.on_filled_order(pos, order, price=169.7, quantity=1, multiplier=1)
    assert s._positions["MSTR"]["quantity"] == 39


def test_single_event_fill_is_unchanged_backtest_parity():
    """Backtest parity guard: a single-event fill must size exactly as before."""
    s = make_strategy()
    s._pending_buys["BITX"] = {"atr": 0.5}
    order = _multi_event("BITX", 316, [316])  # one event, as in backtests
    pos = types.SimpleNamespace(quantity=316)
    s.on_filled_order(pos, order, price=20.97, quantity=316, multiplier=1)

    assert s._positions["BITX"]["quantity"] == 316
    stops = [o for o in s._created if o.side == "sell" and o.stop_price is not None]
    assert stops and stops[0].quantity == 316


def test_exit_sells_full_position_quantity():
    s = make_strategy()
    s._pending_buys["MSTR"] = {"atr": 1.0}
    order = _multi_event("MSTR", 31, [30, 1])
    pos = types.SimpleNamespace(quantity=31)
    s.on_filled_order(pos, order, price=169.7, quantity=1, multiplier=1)

    s._submit_sell("MSTR", "stop_confirm", 169.0)
    sells = [o for o in s._created if o.side == "sell" and o.stop_price is None]
    assert sells, "exit should submit a market sell"
    assert sells[-1].quantity == 31, "exit must sell the full held position, not the fragment"
