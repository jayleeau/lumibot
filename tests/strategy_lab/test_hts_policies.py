"""Boundary and rule tests for the pure HTS policy layer."""
from __future__ import annotations

import math
from datetime import date

import numpy as np
import pandas as pd
import pytest

from strategy_lab.hts_policies import (
    PolicyError,
    apply_leveraged_cap,
    market_gate_open,
    rank_scores,
    schedule_due,
    select_holdings,
    target_weights,
    weekly_due_sessions,
)


def _row(**values: float) -> pd.Series:
    return pd.Series(values, dtype="float64")


def test_r20_rank_uses_the_parameterized_return_column() -> None:
    rows = {"A": _row(ret=0.05, r20=0.99), "B": _row(ret=0.10, r20=0.01)}
    scores = rank_scores("r20", rows)
    assert scores["A"] == pytest.approx(0.05)
    assert scores["B"] == pytest.approx(0.10)


def test_ratios_drop_rows_with_a_zero_or_missing_denominator() -> None:
    rows = {
        "ok": _row(r20=0.20, vol20=0.10),
        "zero": _row(r20=0.20, vol20=0.0),
        "missing": _row(r20=0.20, vol20=float("nan")),
    }
    scores = rank_scores("r20-over-vol20", rows)
    assert scores == {"ok": pytest.approx(2.0)}


def test_percentile_rank_modes_average_three_horizons() -> None:
    rows = {
        "A": _row(r10=0.1, r20=0.2, r60=0.3),
        "B": _row(r10=0.2, r20=0.3, r60=0.4),
    }
    scores = rank_scores("pctrank-10-20-60", rows)
    # Every horizon ranks A below B; with two candidates the average-tie
    # percentiles are 0.5 and 1.0.
    assert scores["A"] < scores["B"]
    assert scores["A"] == pytest.approx(0.5)
    assert scores["B"] == pytest.approx(1.0)


def test_efficiency_and_skip_and_residual_modes_read_the_right_column() -> None:
    rows = {"A": _row(r20=0.10, eff20=0.50, skip5=0.07, res20=0.03, reg60=0.02)}
    assert rank_scores("efficiency-ratio-20", rows)["A"] == pytest.approx(0.05)
    assert rank_scores("r20-skip-5", rows)["A"] == pytest.approx(0.07)
    assert rank_scores("qqq-residual-momentum-20", rows)["A"] == pytest.approx(0.03)
    assert rank_scores("regression-momentum-60", rows)["A"] == pytest.approx(0.02)


def test_unknown_rank_mode_raises() -> None:
    with pytest.raises(PolicyError):
        rank_scores("not-a-mode", {"A": _row(ret=0.1)})


def test_market_gates_close_when_evidence_is_missing() -> None:
    spy = _row(close=110.0, sma50=100.0, sma100=100.0, sma200=100.0, vol20=0.2, vol120=0.1)
    qqq = _row(close=90.0, sma50=100.0, sma100=100.0, sma200=100.0)
    benchmarks = {"SPY": spy, "QQQ": qqq}
    assert market_gate_open("none", benchmark_rows=benchmarks, breadth_rows=(), breadth_sma=50,
                            breadth_threshold=None) is True
    assert market_gate_open("spy-sma50", benchmark_rows=benchmarks, breadth_rows=(), breadth_sma=50,
                            breadth_threshold=None) is True
    assert market_gate_open("qqq-sma100", benchmark_rows=benchmarks, breadth_rows=(), breadth_sma=50,
                            breadth_threshold=None) is False
    assert market_gate_open("spy-and-qqq-sma200", benchmark_rows=benchmarks, breadth_rows=(),
                            breadth_sma=50, breadth_threshold=None) is False
    # Missing benchmark row must close the gate, never open it.
    assert market_gate_open("spy-sma200", benchmark_rows={}, breadth_rows=(), breadth_sma=50,
                            breadth_threshold=None) is False


def test_market_gate_volatility_ratio_and_breadth() -> None:
    spy = _row(close=110.0, sma50=100.0, vol20=0.15, vol120=0.10)
    assert market_gate_open("spy-vol-ratio-1.5", benchmark_rows={"SPY": spy}, breadth_rows=(),
                            breadth_sma=50, breadth_threshold=None) is True
    stressed = _row(close=110.0, vol20=0.30, vol120=0.10)
    assert market_gate_open("spy-vol-ratio-1.5", benchmark_rows={"SPY": stressed}, breadth_rows=(),
                            breadth_sma=50, breadth_threshold=None) is False
    above = [_row(close=10.0, sma=5.0)] * 6
    below = [_row(close=4.0, sma=5.0)] * 4
    assert market_gate_open("breadth-sma50", benchmark_rows={}, breadth_rows=above + below,
                            breadth_sma=50, breadth_threshold=0.5) is True
    assert market_gate_open("breadth-sma50", benchmark_rows={}, breadth_rows=below * 2,
                            breadth_sma=50, breadth_threshold=0.5) is False


def test_schedule_due_daily_weekly_and_every_n() -> None:
    assert schedule_due("daily", date(2024, 1, 3)) is True
    # 2024-01-03 is a Wednesday.
    assert schedule_due("weekly-wednesday", date(2024, 1, 3)) is True
    assert schedule_due("weekly-friday", date(2024, 1, 3)) is False
    assert schedule_due("weekly-friday", date(2024, 1, 5)) is True
    assert schedule_due("every-2nd-session", date(2024, 1, 3), session_index=4) is True
    assert schedule_due("every-2nd-session", date(2024, 1, 4), session_index=5) is False


def test_weekly_schedule_carries_a_holiday_week_forward() -> None:
    # A week with no Friday session (holiday) carries the decision to the next
    # available session, then resumes the following week's target.
    sessions = [date(2024, 1, 1), date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4),
                date(2024, 1, 8), date(2024, 1, 12)]
    chosen = weekly_due_sessions(sessions, target_weekday=4)
    assert date(2024, 1, 8) in chosen
    assert date(2024, 1, 12) in chosen


def test_select_holdings_applies_buffer_exposure_and_correlation() -> None:
    ranked = [("A", 5.0), ("B", 4.0), ("C", 3.0), ("D", 2.0)]
    # Retention buffer keeps a held symbol that still ranks inside the buffer.
    selection = select_holdings(ranked=ranked, held=("C",), top_n=3, rank_buffer=3,
                                exposure_limit=None, exposure_of=None,
                                correlation_of=None, correlation_cap=None)
    assert selection.holdings[0] == "C"
    assert len(selection.holdings) == 3
    # Exposure limit blocks a second member of the same group.
    groups = {"A": "tech", "B": "tech", "C": "energy", "D": "gold"}
    selection = select_holdings(ranked=ranked, held=(), top_n=4, rank_buffer=None,
                                exposure_limit=1, exposure_of=lambda s: groups[s],
                                correlation_of=None, correlation_cap=None)
    assert selection.holdings == ("A", "C", "D")
    # Correlation screen rejects a correlated candidate and an unverifiable one.
    correlations = {("A", "B"): 0.95, ("A", "C"): 0.10, ("A", "D"): float("nan")}
    selection = select_holdings(ranked=ranked, held=(), top_n=4, rank_buffer=None,
                                exposure_limit=None, exposure_of=None,
                                correlation_of=lambda a, b: correlations.get((a, b), correlations.get((b, a), 0.0)),
                                correlation_cap=0.80)
    assert "B" not in selection.holdings
    assert "D" not in selection.holdings
    assert selection.holdings == ("A", "C")


def test_target_weights_equal_inverse_vol_and_caps() -> None:
    equal = target_weights("equal-slots", selected=("A", "B"), gross_target=0.995,
                           per_symbol_cap=None, atr_k=2.0, volatility={}, atr={}, price={},
                           stop_distance_budget=None, vol_target=None, covariance=None)
    assert equal["A"] == pytest.approx(0.4975)
    assert equal["B"] == pytest.approx(0.4975)

    inverse = target_weights("inverse-vol-20", selected=("A", "B"), gross_target=1.0,
                             per_symbol_cap=None, atr_k=2.0, volatility={"A": 0.10, "B": 0.30},
                             atr={}, price={}, stop_distance_budget=None, vol_target=None, covariance=None)
    assert inverse["A"] == pytest.approx(0.75)
    assert inverse["B"] == pytest.approx(0.25)

    # A missing volatility makes inverse-vol fail closed rather than substitute.
    assert target_weights("inverse-vol-20", selected=("A", "B"), gross_target=1.0,
                          per_symbol_cap=None, atr_k=2.0, volatility={"A": 0.10}, atr={}, price={},
                          stop_distance_budget=None, vol_target=None, covariance=None) == {}


def test_per_symbol_cap_leaves_unused_capacity_in_cash() -> None:
    capped = target_weights("equal-slots", selected=("A", "B"), gross_target=1.0,
                            per_symbol_cap=0.30, atr_k=2.0, volatility={}, atr={}, price={},
                            stop_distance_budget=None, vol_target=None, covariance=None)
    assert capped == {"A": 0.30, "B": 0.30}
    assert sum(capped.values()) < 1.0


def test_stop_distance_weights_risk_a_fixed_nav_fraction() -> None:
    weights = target_weights("stop-distance-budget", selected=("A",), gross_target=0.995,
                             per_symbol_cap=None, atr_k=2.0, volatility={},
                             atr={"A": 5.0}, price={"A": 500.0}, stop_distance_budget=0.0025,
                             vol_target=None, covariance=None)
    # risk per share = 2 * 5 = 10; notional = 0.0025 * 500 / 10 = 0.125 of NAV.
    # The budget is the sizing rule here, so the 99.5% gross only binds as a cap.
    assert weights["A"] == pytest.approx(0.125)


def test_vol_target_scales_down_but_never_up() -> None:
    covariance = np.array([[0.04, 0.0], [0.0, 0.04]])
    # Forecast vol of two equal weights is sqrt(0.5^2*0.04*2) = 0.1414.
    scaled = target_weights("vol-target", selected=("A", "B"), gross_target=0.995,
                            per_symbol_cap=None, atr_k=2.0, volatility={}, atr={}, price={},
                            stop_distance_budget=None, vol_target=0.10, covariance=covariance)
    total = sum(scaled.values())
    assert total == pytest.approx(min(0.995, 0.10 / math.sqrt(0.02)), rel=1e-9)
    # A target above the achievable gross simply uses the 99.5% cap.
    capped = target_weights("vol-target", selected=("A", "B"), gross_target=0.995,
                            per_symbol_cap=None, atr_k=2.0, volatility={}, atr={}, price={},
                            stop_distance_budget=None, vol_target=0.50, covariance=covariance)
    assert sum(capped.values()) == pytest.approx(0.995)
    # Missing covariance history prevents entry rather than substituting full risk.
    assert target_weights("vol-target", selected=("A", "B"), gross_target=0.995,
                          per_symbol_cap=None, atr_k=2.0, volatility={}, atr={}, price={},
                          stop_distance_budget=None, vol_target=0.20, covariance=None) == {}


def test_leveraged_cap_trims_only_leveraged_holdings() -> None:
    weights = {"TQQQ": 0.40, "SPY": 0.40}
    trimmed = apply_leveraged_cap(weights, leveraged_symbols=["TQQQ"], cap=0.25)
    assert trimmed["TQQQ"] == pytest.approx(0.25)
    assert trimmed["SPY"] == pytest.approx(0.40)
    # No leveraged holdings means no change.
    assert apply_leveraged_cap(weights, leveraged_symbols=[], cap=0.25) == weights
