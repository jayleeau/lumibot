from __future__ import annotations

import pandas as pd
import pytest

from strategy_lab.hts_v1_core import (
    DecisionJournal,
    HtsV1Config,
    HtsV1DecisionCore,
    compare_decision_journals,
    prepare_features,
)


def _bars(symbol: str, timestamps: pd.DatetimeIndex, prices: list[float]) -> pd.DataFrame:
    return pd.DataFrame({
        "symbol": symbol,
        "timestamp": timestamps,
        "open": prices,
        "high": [value + 1 for value in prices],
        "low": [max(value - 1, 0.01) for value in prices],
        "close": prices,
        "volume": [1_000_000] * len(prices),
    })


def _prepared() -> tuple[HtsV1Config, object]:
    config = HtsV1Config(
        universe=("AAA", "BBB"), top_n=1, trend_sma=2, return_period=1,
        atr_period=2, liquidity_period=2, min_median_dollar_volume=1, market_data_feed="TEST",
    )
    daily_times = pd.date_range("2024-01-02", periods=4, freq="B", tz="UTC")
    daily = pd.concat([_bars("AAA", daily_times, [10, 11, 12, 13]), _bars("BBB", daily_times, [10, 10.5, 11, 11.5])])
    hourly_times = pd.DatetimeIndex([
        "2024-01-05 14:00:00+00:00",
        "2024-01-05 15:00:00+00:00",
        "2024-01-08 14:00:00+00:00",  # 09:00 New York: select only prior daily session
        "2024-01-08 15:00:00+00:00",  # 10:00 New York: queue entry
        "2024-01-08 16:00:00+00:00",  # next executable bar
        "2024-01-08 17:00:00+00:00",
        "2024-01-08 18:00:00+00:00",
    ])
    hourly = pd.concat([_bars("AAA", hourly_times, [20, 20, 20, 20, 1, 1, 1]), _bars("BBB", hourly_times, [20, 20, 20, 20, 20, 20, 20])])
    return config, prepare_features(daily, hourly, config)


def test_selection_uses_only_previous_completed_daily_session() -> None:
    config, features = _prepared()
    core = HtsV1DecisionCore(config, features)

    core.process_completed_bar(pd.Timestamp("2024-01-08 09:00", tz="America/New_York"), 10_000)

    # The Jan 8 daily close is not used during its intraday session.  AAA wins
    # because Jan 5 is the latest available completed daily session.
    assert core.selected == ("AAA",)


def test_virtual_stop_is_an_order_on_next_executable_bar_not_a_retroactive_fill() -> None:
    config, features = _prepared()
    core = HtsV1DecisionCore(config, features)
    nine = pd.Timestamp("2024-01-08 09:00", tz="America/New_York")
    ten = pd.Timestamp("2024-01-08 10:00", tz="America/New_York")
    eleven = pd.Timestamp("2024-01-08 11:00", tz="America/New_York")
    noon = pd.Timestamp("2024-01-08 12:00", tz="America/New_York")
    one = pd.Timestamp("2024-01-08 13:00", tz="America/New_York")

    core.process_completed_bar(nine, 10_000)
    orders = core.process_completed_bar(ten, 10_000)
    buy = next(order for order in orders if order.side == "buy")
    assert buy.timestamp == ten
    # The buy becomes executable on the later completed bar; it is then moved
    # from queued to in-flight before a broker fill can be acknowledged.
    assert buy in core.process_completed_bar(eleven, 10_000)
    core.acknowledge_fill(buy, 20.0, eleven)

    orders = core.process_completed_bar(noon, 10_000)
    stop = next(order for order in orders if order.reason == "virtual_stop_breach")
    assert stop.timestamp == noon
    # It is queued; it cannot appear as an executable sell in the same callback.
    assert stop in core.pending
    next_orders = core.process_completed_bar(one, 10_000)
    assert stop in next_orders


def test_replay_comparison_reports_first_different_field() -> None:
    row = {"event": "decision", "timestamp": "2024-01-01T00:00:00Z", "input_hash": "a", "config_fingerprint": "b", "selected": ["AAA"], "orders": []}
    compare_decision_journals([row], [dict(row)])
    changed = dict(row, selected=["BBB"])
    with pytest.raises(AssertionError, match="selected"):
        compare_decision_journals([row], [changed])


def test_restore_rejects_different_configuration() -> None:
    config, features = _prepared()
    state = HtsV1DecisionCore(config, features).snapshot()
    other = HtsV1Config(universe=("AAA", "BBB"), top_n=2, market_data_feed="TEST")
    with pytest.raises(ValueError, match="another config"):
        HtsV1DecisionCore(other, features).restore(state)


def test_config_accepts_each_supported_stock_adjustment_basis() -> None:
    for adjustment in ("raw", "split", "dividend", "all"):
        assert HtsV1Config(
            universe=("AAA",), top_n=1, market_data_feed="TEST", price_adjustment=adjustment
        ).price_adjustment == adjustment
