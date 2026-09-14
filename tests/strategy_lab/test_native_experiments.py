"""Tests for the registry-driven native experiment engine.

These cover the pieces that decide whether a run is trustworthy without paying
for a full backtest: the mechanism support gate, the standard metric definitions,
the result invariants, and the declared hourly bar mapping.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import pandas as pd
import pytest

from strategy_lab.experiment_config import EXECUTION_ENGINE, KIND_ALTERNATIVE
from strategy_lab.experiment_registry import get_registry
from strategy_lab.feature_store import daily_feature_frame
from strategy_lab.hts_variants import HTS_BASELINE
from strategy_lab.native_alternatives import BLOCKED_ALTERNATIVES, DAILY_ALTERNATIVES
from strategy_lab.native_experiments import (
    ExperimentWindow,
    IMPLEMENTED_MECHANISMS,
    WINDOW_BY_LABEL,
    _hourly_features,
    _lumibot_hourly,
    check_supported,
    standard_metrics,
    validate_result,
    validate_warnings,
)


def test_execution_engine_constant_names_the_native_path() -> None:
    assert EXECUTION_ENGINE == "native-lumibot-backtesting"


def test_every_hts_candidate_is_now_implemented() -> None:
    registry = get_registry()
    for candidate in registry.all_candidates():
        if candidate.kind == KIND_ALTERNATIVE:
            continue
        assert check_supported(dict(candidate.parameters), HTS_BASELINE) == (), candidate.candidate_id


def test_unknown_parameter_is_reported_not_silently_ignored() -> None:
    params = dict(HTS_BASELINE)
    params["invented_knob"] = 1
    missing = check_supported(params, HTS_BASELINE)
    assert missing and "invented_knob" in missing[0]


def test_implemented_mechanism_sets_cover_the_catalog() -> None:
    registry = get_registry()
    seen: dict[str, set] = {param: set() for param in IMPLEMENTED_MECHANISMS}
    for candidate in registry.all_candidates():
        for name in seen:
            value = dict(candidate.parameters).get(name)
            if value is not None:
                seen[name].add(value)
    for name, values in seen.items():
        assert values <= IMPLEMENTED_MECHANISMS[name], (name, values - IMPLEMENTED_MECHANISMS[name])


def test_daily_alternatives_are_runnable_and_blocked_ones_are_named() -> None:
    registry = get_registry()
    alternatives = [c for c in registry.all_candidates() if c.kind == KIND_ALTERNATIVE]
    assert len(alternatives) == 10
    for candidate in alternatives:
        if candidate.candidate_id in DAILY_ALTERNATIVES:
            continue
        assert candidate.candidate_id in BLOCKED_ALTERNATIVES
    assert set(BLOCKED_ALTERNATIVES) == {"A02", "A07", "A08", "A10"}


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
    features = daily_feature_frame(frame, trend_sma=2, return_period=2, liquidity_period=2)
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


def test_zero_sharpe_is_a_warning_not_a_failure() -> None:
    payload = {
        "metrics": {"sessions": 500, "final_equity": 1.0, "total_return": 0.0, "cagr": 0.0,
                    "volatility": 0.0, "sharpe": 0.0, "max_drawdown": 0.0},
        "invariants": {"min_cash": 5.0, "nan_fills": False},
    }
    assert validate_result(payload) == ()
    assert validate_warnings(payload) == ("zero_sharpe_flag",)


def test_windows_carry_a_prior_session_warmup() -> None:
    window = WINDOW_BY_LABEL["six_year"]
    assert window.start == "2020-09-08"
    assert window.warmup_start < window.start
    assert window.start_dt.year == 2020
    custom = ExperimentWindow("custom", "2025-01-02", "2025-06-30")
    assert custom.warmup_start < custom.start
