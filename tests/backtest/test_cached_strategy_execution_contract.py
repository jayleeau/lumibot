"""Regression coverage for the cached strategy execution contract.

These tests were added with the corrected harness. They intentionally verify
timestamps and prices instead of historical performance totals so future data
refreshes cannot hide lookahead or same-bar execution regressions.
"""
from __future__ import annotations

import ast
from datetime import date, datetime

import pandas as pd
import pytest

import scripts.run_full_lumibot_backtests as native_runner
from lumibot.constants import LUMIBOT_DEFAULT_PYTZ
from lumibot.strategies.strategy_executor import StrategyExecutor
from strategy_lab.daily_fleet_backtest import Spec, prepare, run_backtest
from strategy_lab.hts_backtest import run_cash_state_machine, stop_fill_price


def _ohlcv(index: pd.DatetimeIndex) -> pd.DataFrame:
    closes = pd.Series([98.0, 99.0, 101.0, 103.0, 102.0, 104.0, 106.0, 105.0], index=index)
    opens = closes.shift(1).fillna(closes.iloc[0]) * 0.99
    return pd.DataFrame(
        {
            "open": opens,
            "high": pd.concat([opens, closes], axis=1).max(axis=1) + 1.0,
            "low": pd.concat([opens, closes], axis=1).min(axis=1) - 1.0,
            "close": closes,
            "volume": 1_000_000.0,
        },
        index=index,
    )


def test_native_hts_wrapper_preserves_exact_clock_hour_labels_and_ohlcv() -> None:
    """The frozen HTS convention excludes off-hours and sub-hour source rows."""
    index = pd.DatetimeIndex(
        [
            "2025-01-02 08:00",
            "2025-01-02 09:00",
            "2025-01-02 09:00:01",
            "2025-01-02 09:30",
            "2025-01-02 10:00",
            "2025-01-02 10:30",
            "2025-01-02 11:00",
            "2025-01-02 12:00",
            "2025-01-02 13:00",
            "2025-01-02 14:00",
            "2025-01-02 15:00",
            "2025-01-02 16:00",
            "2025-01-02 17:00",
        ]
    )
    values = pd.Series(range(1, len(index) + 1), index=index, dtype="float64")
    frame = pd.DataFrame(
        {
            "open": values,
            "high": values + 10.0,
            "low": values - 10.0,
            "close": values + 0.5,
            "volume": values * 100.0,
        }
    )

    mapped = native_runner._hts_lumibot_data(frame)
    expected_index = pd.date_range("2025-01-02 09:00", periods=7, freq="h")
    expected = frame.loc[expected_index, ["open", "high", "low", "close", "volume"]]

    # The historical 09:30 relabel was retired: values and source labels are
    # both preserved so a label T remains available only at T+1h.
    pd.testing.assert_frame_equal(mapped, expected)
    assert list(mapped.index) == list(expected_index)
    assert not (mapped.index.minute != 0).any()
    assert not (mapped.index.second != 0).any()
    assert not (mapped.index.microsecond != 0).any()
    assert not (mapped.index.nanosecond != 0).any()
    assert not mapped.index.isin(
        pd.to_datetime(
            [
                "2025-01-02 08:00",
                "2025-01-02 09:30",
                "2025-01-02 10:30",
                "2025-01-02 16:00",
                "2025-01-02 17:00",
            ]
        )
    ).any()


def test_daily_research_uses_completed_signal_then_next_open() -> None:
    index = pd.bdate_range("2025-01-02", periods=8)
    frame = _ohlcv(index)
    spec = Spec(
        name="test_gap",
        encoded="test",
        sleeve=1.0,
        stop=0.50,
        hold_days=1,
        rsi_exit=101.0,
        warmup=2,
        family="podhajsky_gap",
        elong=2,
        rsi_n=2,
        rsi_th=101.0,
        k_gap=0.002,
    )
    prepared = prepare({"AAPL": frame}, spec)["AAPL"]
    signal_days = list(prepared.index[prepared["sig_entry"]])
    assert signal_days
    first_signal = signal_days[0]
    execution_day = index[index.get_loc(first_signal) + 1]

    result = run_backtest(
        spec,
        {"AAPL": frame},
        start=str(index[0].date()),
        end=str(index[-1].date()),
        margin_rate=0.0,
    )
    first_fill = result["fills"].iloc[0]
    assert first_fill["time"] == execution_day
    assert first_fill["price"] == pytest.approx(frame.loc[execution_day, "open"])

    changed = frame.copy()
    changed.loc[execution_day, "close"] *= 20.0
    changed_result = run_backtest(
        spec,
        {"AAPL": changed},
        start=str(index[0].date()),
        end=str(index[-1].date()),
        margin_rate=0.0,
    )
    changed_first = changed_result["fills"].iloc[0]
    assert changed_first[["time", "symbol", "side", "price"]].to_dict() == first_fill[
        ["time", "symbol", "side", "price"]
    ].to_dict()


def test_hourly_research_next_open_and_intrabar_gap_stop() -> None:
    prior = date(2025, 1, 2)
    session = date(2025, 1, 3)
    hour_map = {
        "AAA.US": {
            session: (
                {9: 101.0, 10: 104.0, 11: 101.0},
                {9: 1.0, 10: 1.0, 11: 1.0},
                {9: 100.0, 10: 103.0, 11: 100.0},
                {9: 102.0, 10: 105.0, 11: 102.0},
                {9: 99.0, 10: 90.0, 11: 99.0},
            )
        }
    }
    valid_signal = (100.0, 90.0, 0.20, 0.25, 10_000_000.0, 90.0)
    current_bar_poison = (1.0, 999.0, -0.99, 9.0, 0.0, 999.0)
    daily = {"AAA.US": {prior: valid_signal, session: current_bar_poison}}
    params = {
        "universe": ["AAA.US"],
        "top_n": 1,
        "k_atr": 2.0,
        "entry_hour": 9,
        "execution_hour": 10,
        "exit_hour": 11,
        "min_mdv": 1.0,
        "trend_filter": True,
        "cost_per_side": 0.00035,
        "target_leverage": 1.0,
        "initial_cash": 1_000.0,
        "margin_rate": 0.0,
    }

    _returns, _trades, _signals, fills = run_cash_state_machine(
        hour_map,
        daily,
        {},
        params,
        session,
        session,
        record_fills=True,
    )
    assert [(fill["hour"], fill["side"], fill["price"]) for fill in fills] == [
        (10, "buy", 103.0),
        (11, "sell", 100.0),
    ]
    assert [fill["quantity"] for fill in fills] == [9, 9]
    assert fills[0]["trade_cost"] == pytest.approx(9 * 103.0 * 0.00035)
    assert stop_fill_price(100.0, 99.0, 102.0) == 100.0
    assert stop_fill_price(103.0, 102.01, 102.0) is None


def test_native_daily_wrapper_fills_signal_on_following_session_open(tmp_path) -> None:
    dates = pd.to_datetime(["2025-01-02", "2025-01-03", "2025-01-06", "2025-01-07"])
    raw = pd.DataFrame(
        {
            "open": [100.0, 110.0, 120.0, 130.0],
            "high": [101.0, 111.0, 121.0, 131.0],
            "low": [99.0, 109.0, 119.0, 129.0],
            "close": [100.5, 110.5, 120.5, 130.5],
            "volume": [1_000_000.0] * 4,
        },
        index=dates,
    )
    prepared = raw.copy()
    prepared["sig_entry"] = [True, False, False, False]
    prepared["rsi_exit"] = [0.0, 0.0, 0.0, 0.0]
    native_runner._DAILY_CONTEXT = native_runner.DailyContext(
        frames={"AAPL": prepared},
        ordered_symbols=["AAPL"],
        leverage=1.0,
        stop=0.50,
        hold_days=1,
        rsi_exit=101.0,
    )
    data = native_runner._asset_data({"AAPL": raw}, hourly=False)
    native_runner._run_native(
        native_runner.NativeDailyFleetStrategy,
        name="contract",
        pandas_data=data,
        start=datetime(2025, 1, 1),
        end=datetime(2025, 1, 7, 23, 59),
        out_dir=tmp_path,
        sleeptime="1D",
    )

    trades = pd.read_csv(tmp_path / "contract_trades.csv")
    fills = trades.loc[trades["status"] == "fill"]
    first = fills.iloc[0]
    assert str(first["time"]).startswith("2025-01-03 09:30:00")
    assert first["side"] == "buy"
    assert float(first["price"]) == pytest.approx(110.0)


def test_native_daily_final_stats_include_last_session_fill(tmp_path) -> None:
    dates = pd.to_datetime(["2025-01-02", "2025-01-03", "2025-01-06", "2025-01-07"])
    raw = pd.DataFrame(
        {
            "open": [100.0, 110.0, 120.0, 130.0],
            "high": [101.0, 111.0, 121.0, 131.0],
            "low": [99.0, 109.0, 119.0, 129.0],
            "close": [100.5, 110.5, 120.5, 130.5],
            "volume": [1_000_000.0] * 4,
        },
        index=dates,
    )
    prepared = raw.copy()
    prepared["sig_entry"] = [False, False, True, False]
    prepared["rsi_exit"] = 0.0
    native_runner._DAILY_CONTEXT = native_runner.DailyContext(
        frames={"AAPL": prepared},
        ordered_symbols=["AAPL"],
        leverage=1.0,
        stop=0.50,
        hold_days=10,
        rsi_exit=101.0,
    )
    data = native_runner._asset_data({"AAPL": raw}, hourly=False)
    result = native_runner._run_native(
        native_runner.NativeDailyFleetStrategy,
        name="final-fill-contract",
        pandas_data=data,
        start=datetime(2025, 1, 1),
        end=datetime(2025, 1, 7, 23, 59),
        out_dir=tmp_path,
        sleeptime="1D",
    )

    stats = pd.read_csv(tmp_path / "final-fill-contract_stats.csv")
    final = stats.iloc[-1]
    positions = ast.literal_eval(final["positions"])
    assert positions == [{"asset": {"symbol": "AAPL", "type": "stock"}, "quantity": 829.0}]
    assert float(final["portfolio_value"]) > float(final["cash"])
    expected_return = float(final["portfolio_value"]) / 100_000.0 - 1.0
    assert float(result["analysis"]["total_return"]) == pytest.approx(expected_return)


def test_backtest_close_hook_precedes_final_bar_order_processing(monkeypatch) -> None:
    """The exact close boundary still has one unprocessed simulated bar."""

    class DummyDataSource:
        datetime_end = LUMIBOT_DEFAULT_PYTZ.localize(datetime(2025, 1, 3, 23, 59))

    class DummyBroker:
        IS_BACKTESTING_BROKER = True
        market = "NYSE"
        data_source = DummyDataSource()
        datetime = LUMIBOT_DEFAULT_PYTZ.localize(datetime(2025, 1, 3, 16, 0))

        @staticmethod
        def is_market_open() -> bool:
            # LumiBot reports false at the exact close even though the final bar
            # remains pending until await_market_to_close() runs.
            return False

        @staticmethod
        def get_time_to_close() -> float:
            return 0.0

    events: list[str] = []

    class DummyStrategy:
        is_backtesting = True
        minutes_before_closing = 0
        name = "close-contract"

        def __init__(self) -> None:
            self.broker = DummyBroker()

        def get_datetime(self):
            return self.broker.datetime

        def on_trading_iteration(self) -> None:
            return None

        def await_market_to_close(self, timedelta=None) -> None:
            events.append(f"await:{timedelta}")

    strategy = DummyStrategy()
    executor = StrategyExecutor(strategy)
    monkeypatch.setattr(executor, "_is_continuous_market", lambda _market: False)
    monkeypatch.setattr(executor, "_setup_market_session", lambda _has_source: True)
    monkeypatch.setattr(executor, "_ensure_progress_inside_open_session", lambda value: value)
    monkeypatch.setattr(executor, "_run_backtesting_loop", lambda *_args: None)
    monkeypatch.setattr(executor, "_before_market_closes", lambda: events.append("before_close"))
    monkeypatch.setattr(executor, "_after_market_closes", lambda: events.append("after_close"))

    executor._run_trading_session()

    assert events == ["before_close", "await:None", "await:0", "after_close"]
