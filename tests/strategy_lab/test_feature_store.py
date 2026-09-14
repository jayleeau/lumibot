"""Hand-calculated and reference tests for the vectorized daily features."""
from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from strategy_lab.feature_store import (
    annualized_volatility,
    covariance_matrix,
    daily_feature_frame,
    downside_deviation,
    efficiency_ratio,
    percentile_ranks,
    regression_slope_r2,
    residual_momentum,
    rolling_return,
    sample_correlation,
)


def _frame(closes: list[float]) -> pd.DataFrame:
    index = pd.date_range("2024-01-01", periods=len(closes), freq="D")
    values = np.array(closes, dtype="float64")
    return pd.DataFrame(
        {"open": values, "high": values + 1.0, "low": values - 1.0, "close": values,
         "volume": np.full(len(values), 100.0)},
        index=index,
    )


def test_rolling_return_is_the_parameterized_horizon() -> None:
    close = _frame([10.0, 11.0, 12.0])["close"]
    assert rolling_return(close, 2).iloc[2] == pytest.approx(12.0 / 10.0 - 1.0)
    assert math.isnan(rolling_return(close, 2).iloc[1])


def test_annualized_volatility_hand_value() -> None:
    returns = pd.Series([0.01, -0.02, 0.03, -0.01], index=pd.date_range("2024-01-01", periods=4))
    expected = returns.std(ddof=1) * math.sqrt(252.0)
    assert annualized_volatility(returns, 4).iloc[-1] == pytest.approx(float(expected))


def test_downside_deviation_ignores_positive_returns() -> None:
    returns = pd.Series([0.05, -0.02, 0.04, -0.04], index=pd.date_range("2024-01-01", periods=4))
    expected = math.sqrt(((-0.02) ** 2 + (-0.04) ** 2) / 4.0) * math.sqrt(252.0)
    assert downside_deviation(returns, 4).iloc[-1] == pytest.approx(expected)


def test_regression_slope_r2_matches_an_exact_reference() -> None:
    count = 40
    x = np.arange(count, dtype="float64")
    log_price = 3.0 + 0.004 * x
    close = pd.Series(np.exp(log_price), index=pd.date_range("2020-01-01", periods=count, freq="D"))
    slope, r_squared = regression_slope_r2(close, 20)
    assert slope.iloc[-1] == pytest.approx(0.004, rel=1e-9)
    assert r_squared.iloc[-1] == pytest.approx(1.0, rel=1e-9)


def test_regression_slope_matches_numpy_polyfit_on_noisy_data() -> None:
    count = 60
    rng = np.random.default_rng(7)
    x = np.arange(count, dtype="float64")
    log_price = 4.0 + 0.002 * x + rng.normal(0, 0.01, count)
    close = pd.Series(np.exp(log_price), index=pd.date_range("2020-01-01", periods=count, freq="D"))
    slope, _r2 = regression_slope_r2(close, 30)
    reference = float(np.polyfit(x[-30:], log_price[-30:], 1)[0])
    assert slope.iloc[-1] == pytest.approx(reference, rel=1e-6)


def test_efficiency_ratio_is_bounded_by_one() -> None:
    index = pd.date_range("2020-01-01", periods=10, freq="D")
    close = pd.Series([10.0, 11.0, 10.0, 11.0, 10.0, 11.0, 10.0, 11.0, 10.0, 11.0], index=index)
    ratio = efficiency_ratio(close, 5).iloc[-1]
    assert 0.0 <= float(ratio) <= 1.0


def test_residual_momentum_is_zero_against_itself() -> None:
    index = pd.date_range("2020-01-01", periods=40, freq="D")
    close = pd.Series(np.linspace(10.0, 20.0, 40), index=index)
    score = residual_momentum(close, close, 20)
    assert score.iloc[-1] == pytest.approx(0.0, abs=1e-12)


def test_percentile_ranks_are_average_tie() -> None:
    ranks = percentile_ranks({"a": 1.0, "b": 2.0, "b2": 2.0, "c": 3.0})
    assert ranks["a"] == pytest.approx(0.25)
    assert ranks["b"] == ranks["b2"] == pytest.approx(0.625)
    assert ranks["c"] == pytest.approx(1.0)


def test_sample_correlation_detects_a_perfect_relationship() -> None:
    index = pd.date_range("2020-01-01", periods=60, freq="D")
    base = pd.Series(np.linspace(-1.0, 1.0, 60), index=index)
    assert sample_correlation(base, 2.0 * base, 60) == pytest.approx(1.0)
    assert math.isnan(sample_correlation(base.iloc[:10], base.iloc[:10], 60))


def test_covariance_matrix_fails_closed_on_short_history() -> None:
    index = pd.date_range("2020-01-01", periods=5, freq="D")
    series = [pd.Series(np.arange(5, dtype="float64"), index=index) for _ in range(2)]
    assert covariance_matrix(series, 10) is None


def test_covariance_matrix_returns_a_square_matrix_for_one_asset() -> None:
    rng = np.random.default_rng(3)
    index = pd.date_range("2020-01-01", periods=40, freq="D")
    series = [pd.Series(rng.normal(0.001, 0.01, 40), index=index)]
    covariance = covariance_matrix(series, 20)
    assert covariance is not None
    assert covariance.shape == (1, 1)
    assert covariance[0, 0] > 0.0


def test_features_are_prefix_stable_when_future_rows_are_appended() -> None:
    closes = [10.0 + math.sin(step / 3.0) for step in range(80)]
    frame = _frame(closes)
    full = daily_feature_frame(frame, trend_sma=20, return_period=20, liquidity_period=20)
    prefix = daily_feature_frame(frame.iloc[:60], trend_sma=20, return_period=20, liquidity_period=20)
    for column in ("ret", "r20", "vol20", "ddvol20", "reg60", "eff20", "sma", "mdv"):
        left = float(prefix[column].iloc[-1])
        right = float(full[column].iloc[59])
        if math.isnan(left):
            assert math.isnan(right)
        else:
            assert left == pytest.approx(right, rel=1e-12, abs=1e-15), column


def test_extreme_current_session_value_does_not_leak_into_the_same_session() -> None:
    closes = [10.0] * 80
    frame = _frame(closes)
    shocked = frame.copy()
    shocked.loc[shocked.index[-1], "close"] = 1_000.0
    shocked.loc[shocked.index[-1], "high"] = 1_001.0
    base = daily_feature_frame(frame, trend_sma=20, return_period=20, liquidity_period=20)
    after = daily_feature_frame(shocked, trend_sma=20, return_period=20, liquidity_period=20)
    # Features at the shocked session itself are allowed to change; the *prior*
    # session that a decision would actually read must be identical.
    for column in ("ret", "r20", "vol20", "sma", "mdv"):
        assert float(base[column].iloc[-2]) == pytest.approx(float(after[column].iloc[-2]), rel=1e-12)
