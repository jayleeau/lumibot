from __future__ import annotations

import pandas as pd
import pytest

from scripts.backtest_hts_v1_local import _fill
from strategy_lab.hts_v1_core import OrderIntent


def test_local_replay_applies_one_way_cost_at_the_next_bar_open() -> None:
    """The cached replay's ledger follows its documented full-fill convention."""
    intent = OrderIntent(
        decision_id="entry", timestamp=pd.Timestamp("2024-01-02 10:00", tz="America/New_York"),
        symbol="AAA", side="buy", quantity=10, reason="selection_entry", reference_price=99.0,
    )
    bar = pd.Series({"timestamp": pd.Timestamp("2024-01-02 11:00", tz="America/New_York"), "open": 100.0})
    cash, fill = _fill(intent, bar, 2_000.0, 0.00035)

    assert cash == pytest.approx(999.65)
    assert fill["fill_price"] == 100.0
    assert fill["fee"] == pytest.approx(0.35)
    assert fill["fill_timestamp"] == "2024-01-02T11:00:00-05:00"
