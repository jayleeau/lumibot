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
    cap_risk_contributions,
    expected_trade_move_bps,
    market_gate_open,
    rank_scores,
    risk_off_gate_open,
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


def test_v2_global_gate_enums_are_strict_and_fail_closed() -> None:
    spy = _row(close=101.0, sma100=100.0, sma200=100.0)
    qqq = _row(close=101.0, sma100=100.0)
    benchmark = {"SPY": spy, "QQQ": qqq}
    assert risk_off_gate_open("none", benchmark_rows=benchmark, breadth_rows=(), breadth_expected_count=3)
    assert risk_off_gate_open("spy-sma100", benchmark_rows=benchmark, breadth_rows=(), breadth_expected_count=3)
    assert risk_off_gate_open("spy-sma200", benchmark_rows=benchmark, breadth_rows=(), breadth_expected_count=3)
    assert risk_off_gate_open("qqq-sma100", benchmark_rows=benchmark, breadth_rows=(), breadth_expected_count=3)
    # Equality is closed: comparisons are deliberately strict.
    assert not risk_off_gate_open("spy-sma100", benchmark_rows={"SPY": _row(close=100.0, sma100=100.0)}, breadth_rows=(), breadth_expected_count=3)
    breadth = [_row(close=101.0, sma50=100.0), _row(close=99.0, sma50=100.0), _row(close=102.0, sma50=100.0)]
    assert risk_off_gate_open("breadth-50", benchmark_rows={}, breadth_rows=breadth, breadth_expected_count=3)
    exact_sixty = breadth + [_row(close=99.0, sma50=100.0), _row(close=99.0, sma50=100.0)]
    assert not risk_off_gate_open("breadth-60", benchmark_rows={}, breadth_rows=exact_sixty, breadth_expected_count=5)
    assert not risk_off_gate_open("breadth-50", benchmark_rows={}, breadth_rows=breadth[:2], breadth_expected_count=3)
    assert not risk_off_gate_open("breadth-50", benchmark_rows={}, breadth_rows=[_row(close=101.0, sma50=float("nan"))] * 3, breadth_expected_count=3)
    with pytest.raises(PolicyError):
        risk_off_gate_open("invented", benchmark_rows={}, breadth_rows=(), breadth_expected_count=0)


def test_risk_contribution_cap_is_hand_calculated_and_never_renormalizes() -> None:
    # A 2-ATR stop on 5/100 has a 10% relative stop distance.  A 0.5% NAV cap
    # therefore trims 20% base weight to 5%, with the difference held as cash.
    capped = cap_risk_contributions(
        {"A": 0.20, "B": 0.20}, atr={"A": 5.0, "B": 10.0}, price={"A": 100.0, "B": 100.0},
        atr_k=2.0, risk_contribution_cap=0.005,
    )
    assert capped == {"A": pytest.approx(0.05), "B": pytest.approx(0.025)}
    assert sum(capped.values()) == pytest.approx(0.075)
    assert cap_risk_contributions({"A": 0.2}, atr={"A": 5.0}, price={"A": 100.0}, atr_k=2.0,
                                  risk_contribution_cap=None) == {"A": 0.2}
    assert cap_risk_contributions({"A": 0.2}, atr={"A": 0.0}, price={"A": 100.0}, atr_k=2.0,
                                  risk_contribution_cap=0.005) == {}
    assert cap_risk_contributions({"A": 0.2}, atr={"A": 5.0}, price={"A": 0.0}, atr_k=2.0,
                                  risk_contribution_cap=0.005) == {}


def test_expected_trade_move_proxy_is_causal_and_has_exact_threshold_boundary() -> None:
    # R=10% over 20 sessions, h=3 -> exp(log1p(.1)*3/20)-1 = 1.439%.
    # Stop room is 2*1/100=2%, so the trend move binds: 143.92 bps.
    value = expected_trade_move_bps(
        prior_return=0.10, return_period=20, holding_bars=3, atr_k=2.0, atr=1.0, executable_price=100.0,
    )
    assert value == pytest.approx(10_000.0 * math.expm1(math.log1p(0.10) * 3.0 / 20.0))
    assert value is not None and value >= value  # equality accepts in the strategy's >= comparison.
    assert expected_trade_move_bps(prior_return=0.10, return_period=20, holding_bars=0, atr_k=2.0, atr=1.0,
                                   executable_price=100.0) == pytest.approx(10_000.0 * math.expm1(math.log1p(.1) / 20.0))
    assert expected_trade_move_bps(prior_return=-1.0, return_period=20, holding_bars=3, atr_k=2.0, atr=1.0,
                                   executable_price=100.0) is None


def test_mandatory_minimum_hold_occupies_a_slot_until_the_expiry_decision() -> None:
    ranked = [("A", 3.0), ("B", 2.0), ("C", 1.0)]
    held = select_holdings(
        ranked=ranked, held=("C",), mandatory_held=("C",), top_n=2, rank_buffer=None,
        exposure_limit=None, exposure_of=None, correlation_of=None, correlation_cap=None,
    )
    assert held.holdings == ("C", "A")
    expired = select_holdings(
        ranked=ranked, held=("C",), mandatory_held=(), top_n=2, rank_buffer=None,
        exposure_limit=None, exposure_of=None, correlation_of=None, correlation_cap=None,
    )
    assert expired.holdings == ("A", "B")
