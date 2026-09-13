"""Tests for the registry-driven native experiment engine.

These cover the pieces that decide whether a run is trustworthy without paying for
a full backtest: the mechanism support gate, the standard metric definitions, the
result invariants, and the declared hourly bar mapping.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import pandas as pd
import pytest

from strategy_lab.experiment_config import EXECUTION_ENGINE
from strategy_lab.experiment_registry import get_registry
from strategy_lab.hts_variants import HTS_BASELINE
from strategy_lab.native_experiments import (
    ExperimentWindow,
    WINDOW_BY_LABEL,
    _daily_features,
    _hourly_features,
    _lumibot_hourly,
    check_supported,
    standard_metrics,
    validate_result,
)


def test_execution_engine_constant_names_the_native_path() -> None:
    assert EXECUTION_ENGINE == "native-lumibot-backtesting"


def test_control_and_parameter_only_families_are_supported() -> None:
    registry = get_registry()
    # H050-H054 exercise the already-honoured top_n / require_positive_return knobs.
    for candidate_id in ("HTS_CONTROL_1", "H001", "H020", "H030", "H050", "H054", "H091", "H094"):
        candidate = registry.get(candidate_id)
        assert check_supported(dict(candidate.parameters), HTS_BASELINE) == ()


def test_unimplemented_mechanisms_are_reported_not_silently_ignored() -> None:
    registry = get_registry()
    cases = (("H031", "exit_mode"), ("H063", "vol_target"), ("H060", "exposure_group_limit"))
    for candidate_id, expected in cases:
        missing = check_supported(dict(registry.get(candidate_id).parameters), HTS_BASELINE)
        assert missing, f"{candidate_id} should not be reported as runnable"
        assert any(item.startswith(expected) for item in missing)


def test_lumibot_hour_mapping_drops_premarket_and_relabels() -> None:
    index = pd.DatetimeIndex([
        "2024-09-25 08:00", "2024-09-25 09:00", "2024-09-25 10:00", "2024-09-25 15:00",
    ])
    frame = pd.DataFrame(
        {"open": [1.0, 2.0, 3.0, 4.0], "high": [1.0, 2.0, 3.0, 4.0], "low": [1.0, 2.0, 3.0, 4.0],
         "close": [1.0, 2.0, 3.0, 4.0], "volume": [1, 1, 1, 1]},
        index=index,
    )
    mapped = _lumibot_hourly(frame)
    assert list(mapped.index) == [
        pd.Timestamp("2024-09-25 09:30"),
        pd.Timestamp("2024-09-25 10:30"),
        pd.Timestamp("2024-09-25 15:30"),
    ]
    # Values are preserved; only the label moves, matching the existing native runner.
    assert mapped["open"].tolist() == [2.0, 3.0, 4.0]


def test_daily_features_use_only_prior_sessions() -> None:
    index = pd.date_range("2024-01-01", periods=5, freq="D")
    frame = pd.DataFrame(
        {"open": [1, 2, 3, 4, 5], "high": [1, 2, 3, 4, 5], "low": [1, 2, 3, 4, 5],
         "close": [10.0, 11.0, 12.0, 13.0, 14.0], "volume": [100, 100, 100, 100, 100]},
        index=index,
    )
    features = _daily_features(frame, trend_sma=2, return_period=2, liquidity_period=2)
    assert features["ret"].iloc[2] == pytest.approx(12.0 / 10.0 - 1.0)
    assert math.isnan(features["ret"].iloc[1])
    assert features["sma"].iloc[1] == pytest.approx(10.5)
    # Median of the two completed dollar volumes (1000, 1100).
    assert features["mdv"].iloc[1] == pytest.approx(1050.0)


def test_hourly_atr_is_a_trailing_mean_of_true_range() -> None:
    index = pd.date_range("2024-01-01 09:00", periods=4, freq="h")
    frame = pd.DataFrame(
        {"open": [10.0, 10.0, 10.0, 10.0], "high": [11.0, 12.0, 13.0, 14.0],
         "low": [9.0, 9.0, 9.0, 9.0], "close": [10.0, 11.0, 12.0, 13.0],
         "volume": [1, 1, 1, 1]},
        index=index,
    )
    features = _hourly_features(frame, atr_period=2)
    assert math.isnan(features["atr"].iloc[0])
    # True ranges at bars 1 and 2 are 3.0 and 4.0.
    assert features["atr"].iloc[2] == pytest.approx((3.0 + 4.0) / 2.0)


def test_standard_metrics_use_an_arithmetic_daily_sharpe(tmp_path: Path) -> None:
    values = [100.0, 110.0, 121.0, 121.0]
    dates = ["2024-01-02 09:30:00-05:00", "2024-01-03 09:30:00-05:00",
             "2024-01-04 09:30:00-05:00", "2024-01-05 09:30:00-05:00"]
    stats = tmp_path / "stats.csv"
    pd.DataFrame({"datetime": dates, "portfolio_value": values}).to_csv(stats, index=False)
    metrics = standard_metrics(stats, initial_cash=100.0)
    assert metrics["sessions"] == 4
    assert metrics["final_equity"] == pytest.approx(121.0)
    assert metrics["total_return"] == pytest.approx(0.21)
    returns = pd.Series(values).pct_change().dropna()
    expected = returns.mean() / returns.std(ddof=1) * math.sqrt(252.0)
    assert metrics["sharpe"] == pytest.approx(float(expected))
    assert "zero risk-free" in metrics["sharpe_convention"]


def test_validate_result_flags_negative_cash_and_missing_metrics() -> None:
    assert validate_result({}) == ("missing metrics",)
    good = {
        "metrics": {"sessions": 500, "final_equity": 1.0, "total_return": 0.1, "cagr": 0.1,
                    "volatility": 0.2, "sharpe": 0.5, "max_drawdown": 0.2},
        "invariants": {"min_cash": 5.0, "nan_fills": False},
    }
    assert validate_result(good) == ()
    bad = json.loads(json.dumps(good))
    bad["invariants"]["min_cash"] = -10.0
    assert any("cash went negative" in problem for problem in validate_result(bad))


def test_windows_carry_a_prior_session_warmup() -> None:
    window = WINDOW_BY_LABEL["six_year"]
    assert window.start == "2020-09-08"
    assert window.warmup_start < window.start
    assert window.start_dt.year == 2020
    custom = ExperimentWindow("custom", "2025-01-02", "2025-06-30")
    assert custom.warmup_start < custom.start
