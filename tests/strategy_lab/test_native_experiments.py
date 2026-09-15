"""Tests for the registry-driven native experiment engine.

These cover the pieces that decide whether a run is trustworthy without paying
for a full backtest: the mechanism support gate, the standard metric definitions,
the result invariants, and the declared hourly bar mapping.
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from strategy_lab.experiment_config import EXECUTION_ENGINE, KIND_ALTERNATIVE
from strategy_lab.experiment_registry import get_registry
from strategy_lab.feature_store import daily_feature_frame
from strategy_lab.hts_variants import HTS_BASELINE
from strategy_lab.native_alternatives import BLOCKED_ALTERNATIVES, DAILY_ALTERNATIVES
from strategy_lab.native_experiments import (
    ExperimentWindow,
    HTS_HOURLY_CONVENTION,
    IMPLEMENTED_MECHANISMS,
    RegistryHtsStrategy,
    WINDOW_BY_LABEL,
    _hourly_features,
    _lumibot_hourly,
    build_virtual_stop_gap_event,
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


def test_lumibot_hour_mapping_preserves_exact_clock_hours() -> None:
    index = pd.DatetimeIndex([
        "2024-09-25 08:00", "2024-09-25 09:00", "2024-09-25 09:30",
        "2024-09-25 10:00", "2024-09-25 15:00", "2024-09-25 16:00",
    ])
    frame = pd.DataFrame(
        {"open": [1.0, 2.0, 3.0, 4.0, 5.0, 6.0], "high": [1.0, 2.0, 3.0, 4.0, 5.0, 6.0],
         "low": [1.0, 2.0, 3.0, 4.0, 5.0, 6.0], "close": [1.0, 2.0, 3.0, 4.0, 5.0, 6.0],
         "volume": [1, 1, 1, 1, 1, 1]},
        index=index,
    )
    mapped = _lumibot_hourly(frame)
    assert list(mapped.index) == [
        pd.Timestamp("2024-09-25 09:00"),
        pd.Timestamp("2024-09-25 10:00"),
        pd.Timestamp("2024-09-25 15:00"),
    ]
    assert mapped["open"].tolist() == [2.0, 4.0, 5.0]
    assert not (mapped.index.minute != 0).any()
    assert not (mapped.index.second != 0).any()
    assert not (mapped.index.microsecond != 0).any()
    assert HTS_HOURLY_CONVENTION == (
        "clock-hour 09:00-15:00 ET; a bar labelled T contains [T,T+1h) and is only known at T+1h"
    )


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


class _LifecycleOrder:
    """Small order double for exercising the strategy's real lifecycle method."""

    def __init__(self, symbol: str, identifier: str) -> None:
        self.asset = SimpleNamespace(symbol=symbol)
        self.identifier = identifier

    def is_buy_order(self) -> bool:
        return False


def _lifecycle_strategy(frame: pd.DataFrame) -> tuple[RegistryHtsStrategy, dict[str, pd.Timestamp], list[_LifecycleOrder]]:
    """Build only the state used by ``on_trading_iteration`` and fill callbacks."""
    strategy = object.__new__(RegistryHtsStrategy)
    clock = {"now": pd.Timestamp("2024-09-24 14:00")}
    submitted: list[_LifecycleOrder] = []
    strategy._params = {
        "exit_mode": "virtual-trail-baseline",
        "atr_k": 2.0,
        "time_exit_sessions": None,
        "signal_hour": 99,
        "rebalance_hour": 99,
        "reentry_cooldown_bars": 0,
    }
    strategy._ctx = SimpleNamespace(inputs=SimpleNamespace(hourly={"AAA": frame}))
    strategy._positions = {
        "AAA": {
            "quantity": 2.0,
            "entry_price": 100.0,
            "entry_atr": 1.0,
            "entry_session": pd.Timestamp("2024-09-24").date(),
            "stop": 100.0,
            "highest_high": 100.0,
            "post_entry_highs": [],
            "breach_count": 0,
            "seeded": True,
        }
    }
    strategy._pending_buys = {}
    strategy._pending_sells = set()
    strategy._pending_sell_reason = {}
    strategy._stop_exit_context = {}
    strategy._stop_gap_events = []
    strategy._lifecycle_trace = []
    strategy._protective = {}
    strategy._journal = []
    strategy._rejections = []
    strategy._diag = []
    strategy._sessions = [pd.Timestamp("2024-09-24").date(), pd.Timestamp("2024-09-25").date()]
    strategy._session_index = {day: index for index, day in enumerate(strategy._sessions)}
    strategy.get_datetime = lambda: clock["now"].to_pydatetime()
    strategy.create_order = lambda symbol, **_kwargs: _LifecycleOrder(symbol, f"order-{len(submitted) + 1}")
    strategy.submit_order = lambda order: submitted.append(order)
    return strategy, clock, submitted


def test_virtual_stop_lifecycle_respects_completed_bar_causality() -> None:
    # The raw source includes postmarket and next-session premarket rows. The
    # canonical frame must make the next 09:00 decision use the prior 15:00 close.
    raw_index = pd.DatetimeIndex([
        "2024-09-24 13:00", "2024-09-24 14:00", "2024-09-24 15:00",
        "2024-09-24 17:00", "2024-09-25 08:00", "2024-09-25 09:00",
    ])
    raw = pd.DataFrame(
        {
            "open": [110.0, 100.0, 90.0, 5.0, 6.0, 80.0],
            "high": [111.0, 101.0, 91.0, 6.0, 7.0, 81.0],
            "low": [109.0, 89.0, 79.0, 4.0, 5.0, 79.0],
            "close": [110.0, 90.0, 90.0, 5.0, 6.0, 80.0],
            "volume": [1, 1, 1, 1, 1, 1],
        },
        index=raw_index,
    )
    frame = _lumibot_hourly(raw).assign(atr=1.0)

    same_day, clock, submitted = _lifecycle_strategy(frame)
    same_day.on_trading_iteration()  # 14:00 sees the completed 13:00 close only.
    assert submitted == []
    clock["now"] = pd.Timestamp("2024-09-24 15:00")
    same_day.on_trading_iteration()
    assert len(submitted) == 1
    assert same_day._lifecycle_trace[-1] == {
        "event": "virtual_stop_submitted",
        "symbol": "AAA",
        "order_id": "order-1",
        "engine_time": "2024-09-24T15:00:00",
        "completed_source_bar": "2024-09-24T14:00:00",
        "submission_time": "2024-09-24T15:00:00",
        "source_fill_bar": "2024-09-24T15:00:00",
    }
    same_day.on_filled_order(None, submitted[0], 90.0, 2.0, 1.0)
    assert same_day._stop_gap_events[0]["fill_time"] == "2024-09-24T15:00:00"

    overnight, clock, submitted = _lifecycle_strategy(frame)
    clock["now"] = pd.Timestamp("2024-09-24 16:00")
    overnight.on_trading_iteration()
    assert submitted == []
    assert overnight._lifecycle_trace[-1]["event"] == "virtual_stop_deferred_no_executable_bar"
    assert overnight._lifecycle_trace[-1]["completed_source_bar"] == "2024-09-24T15:00:00"
    clock["now"] = pd.Timestamp("2024-09-25 09:00")
    overnight.on_trading_iteration()
    assert len(submitted) == 1
    assert overnight._lifecycle_trace[-1]["completed_source_bar"] == "2024-09-24T15:00:00"
    assert overnight._lifecycle_trace[-1]["source_fill_bar"] == "2024-09-25T09:00:00"
    assert "AAA" in overnight._pending_sells  # A recovery cannot cancel the latched exit.
    overnight.on_filled_order(None, submitted[0], 80.0, 2.0, 1.0)
    event = overnight._stop_gap_events[0]
    assert event["overnight_gap_exposed"] is True
    assert event["fill_time"] == "2024-09-25T09:00:00"
    assert "AAA" not in overnight._pending_sells


def test_virtual_stop_gap_event_uses_actual_fill_not_trigger_low() -> None:
    trigger = {
        "symbol": "AAA",
        "mode": "virtual-trail-baseline",
        "reason": "stop_breach",
        "order_id": "order-7",
        "entry_price": 100.0,
        "stop_level": 95.0,
        "trigger_timestamp": "2024-09-24T15:00:00",
        "trigger_close": 90.0,
        "trigger_session": "2024-09-24",
        "overnight_gap_exposed": True,
        "engine_time": "2024-09-25T09:00:00",
        "submission_time": "2024-09-25T09:00:00",
        "source_fill_bar": "2024-09-25T09:00:00",
    }
    first = build_virtual_stop_gap_event(
        {**trigger, "trigger_low": 1.0}, fill_time=pd.Timestamp("2024-09-25 09:00"),
        fill_price=90.0, fill_quantity=2.0, fill_session="2024-09-25",
    )
    second = build_virtual_stop_gap_event(
        {**trigger, "trigger_low": 0.01}, fill_time=pd.Timestamp("2024-09-25 09:00"),
        fill_price=90.0, fill_quantity=2.0, fill_session="2024-09-25",
    )
    assert first["fill_minus_stop_dollars_per_share"] == pytest.approx(-5.0)
    assert first["fill_minus_stop_pct"] == pytest.approx(-5.0 / 95.0)
    assert first["entry_to_fill_return"] == pytest.approx(-0.10)
    assert first["overnight_gap_exposed"] is True
    for field in ("fill_minus_stop_dollars_per_share", "fill_minus_stop_pct", "entry_to_fill_return"):
        assert first[field] == second[field]


def _cooldown_strategy(cooldown: int) -> tuple[RegistryHtsStrategy, list[object]]:
    """Build a minimal selection/fill state for checking session-index expiry."""
    sessions = [stamp.date() for stamp in pd.bdate_range("2024-01-02", periods=8)]
    daily = pd.DataFrame(
        {"close": 101.0, "sma": 100.0, "ret": 0.10, "mdv": 10_000_000.0},
        index=pd.DatetimeIndex(sessions),
    )
    strategy = object.__new__(RegistryHtsStrategy)
    strategy._params = {**HTS_BASELINE, "reentry_cooldown_bars": cooldown}
    strategy._ctx = SimpleNamespace(inputs=SimpleNamespace(daily={"AAA": daily}, benchmark={}, breadth=()))
    strategy._ordered = ("AAA",)
    strategy._universe_order = {"AAA": 0}
    strategy._sessions = sessions
    strategy._session_index = {day: index for index, day in enumerate(sessions)}
    strategy._positions = {"AAA": {}}
    strategy._pending_buys = {}
    strategy._pending_sells = {"AAA"}
    strategy._pending_sell_reason = {"AAA": "stop_breach"}
    strategy._stop_exit_context = {}
    strategy._stop_gap_events = []
    strategy._lifecycle_trace = []
    strategy._protective = {}
    strategy._journal = []
    strategy._cooldowns = {}
    strategy._ranks = {}
    strategy.get_datetime = lambda: pd.Timestamp(sessions[0]).replace(hour=15).to_pydatetime()
    return strategy, sessions


@pytest.mark.parametrize("cooldown", [-1, 0, 1, 3, 5])
def test_reentry_cooldown_blocks_exactly_completed_sessions(cooldown: int) -> None:
    # A fill on index i is selected using the previous completed session. With
    # an expiry of i+N, exactly prior-session indices i..i+N-1 are barred.
    strategy, sessions = _cooldown_strategy(cooldown)
    strategy.on_filled_order(None, _LifecycleOrder("AAA", "cooldown-order"), 90.0, 1.0, 1.0)
    assert ("AAA" in strategy._cooldowns) is (cooldown > 0)
    if cooldown > 0:
        assert strategy._cooldowns["AAA"] == cooldown
    for previous_session_index in range(0, 6):
        selected = strategy._select(sessions[previous_session_index + 1])
        assert (selected == ("AAA",)) is (cooldown <= 0 or previous_session_index >= cooldown)


def test_reentry_cooldown_registry_migrates_the_only_active_key() -> None:
    registry = get_registry()
    legacy_name = "stop" + "_cooldown_sessions"
    assert legacy_name not in HTS_BASELINE
    assert HTS_BASELINE["reentry_cooldown_bars"] == 0
    for candidate_id, expected in {"H088": 1, "H089": 3, "H090": 5}.items():
        params = dict(registry.get(candidate_id).parameters)
        assert params["reentry_cooldown_bars"] == expected
        assert legacy_name not in params
    assert legacy_name not in dict(registry.get("H099").parameters)
