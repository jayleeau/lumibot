"""Formula tests for the daily-data alternative strategies."""
from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from strategy_lab.native_alternatives import (
    daily_atr,
    equal_risk_contributions,
    wilder_rsi,
)


def _closes(values: list[float]) -> pd.Series:
    return pd.Series(values, index=pd.date_range("2024-01-01", periods=len(values), freq="D"), dtype="float64")


def test_wilder_rsi_handles_all_gains_losses_and_a_flat_series() -> None:
    gains = wilder_rsi(_closes([1.0, 2.0, 3.0, 4.0, 5.0, 6.0]), 2)
    assert float(gains.iloc[-1]) == pytest.approx(100.0)
    losses = wilder_rsi(_closes([6.0, 5.0, 4.0, 3.0, 2.0, 1.0]), 2)
    assert float(losses.iloc[-1]) == pytest.approx(0.0)
    flat = wilder_rsi(_closes([5.0] * 6), 2)
    assert float(flat.iloc[-1]) == pytest.approx(50.0)


def test_wilder_rsi_matches_a_hand_computed_two_bar_value() -> None:
    closes = _closes([10.0, 11.0, 10.5, 12.0])
    rsi = wilder_rsi(closes, 2)
    # Recompute the Wilder recursion explicitly for the final bar.
    alpha = 0.5
    gains = [max(closes.iloc[i] - closes.iloc[i - 1], 0.0) for i in range(1, len(closes))]
    losses = [max(closes.iloc[i - 1] - closes.iloc[i], 0.0) for i in range(1, len(closes))]
    avg_gain = gains[0]
    avg_loss = losses[0]
    for gain, loss in zip(gains[1:], losses[1:]):
        avg_gain = alpha * gain + (1 - alpha) * avg_gain
        avg_loss = alpha * loss + (1 - alpha) * avg_loss
    expected = 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)
    assert float(rsi.iloc[-1]) == pytest.approx(expected)


def test_daily_atr_is_a_trailing_true_range_mean() -> None:
    index = pd.date_range("2024-01-01", periods=4, freq="D")
    frame = pd.DataFrame(
        {"open": [10.0] * 4, "high": [11.0, 12.0, 13.0, 14.0], "low": [9.0] * 4,
         "close": [10.0, 11.0, 12.0, 13.0], "volume": [1] * 4},
        index=index,
    )
    atr = daily_atr(frame, 2)
    assert math.isnan(float(atr.iloc[0]))
    assert float(atr.iloc[2]) == pytest.approx((3.0 + 4.0) / 2.0)


def test_equal_risk_contributions_are_equal_for_identical_assets() -> None:
    covariance = np.array([[0.04, 0.01], [0.01, 0.04]])
    weights = equal_risk_contributions(covariance, cap=1.0)
    assert weights is not None
    assert weights[0] == pytest.approx(0.5, abs=1e-6)
    assert weights[1] == pytest.approx(0.5, abs=1e-6)


def test_equal_risk_contributions_respect_the_cap() -> None:
    covariance = np.array([[0.01, 0.0], [0.0, 0.25]])
    weights = equal_risk_contributions(covariance, cap=0.35)
    assert weights is not None
    assert float(weights.max()) <= 0.35 + 1e-12
    assert weights[0] >= weights[1]


def test_equal_risk_contributions_fail_closed_on_a_degenerate_matrix() -> None:
    assert equal_risk_contributions(np.array([[0.0, 0.0], [0.0, 0.0]]), cap=1.0) is None
    assert equal_risk_contributions(np.array([[float("nan")]]), cap=1.0) is None
