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

from strategy_lab.experiment_config import EXECUTION_ENGINE, KIND_ALTERNATIVE, KIND_HTS_V2
from strategy_lab.experiment_registry import get_registry
from strategy_lab.feature_store import daily_feature_frame
from strategy_lab.hts_variants import HTS_BASELINE, HTS_V2_BASELINE
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
from strategy_lab.experiment_validation import stable_hash


def test_execution_engine_constant_names_the_native_path() -> None:
    assert EXECUTION_ENGINE == "native-lumibot-backtesting"


def test_every_hts_candidate_is_now_implemented() -> None:
    registry = get_registry()
    for candidate in registry.all_candidates():
        if candidate.kind == KIND_ALTERNATIVE:
            continue
        baseline = HTS_V2_BASELINE if candidate.kind == KIND_HTS_V2 else HTS_BASELINE
        assert check_supported(dict(candidate.parameters), baseline) == (), candidate.candidate_id


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


def test_rebalance_hour_ten_and_fifteen_use_prior_completed_sources() -> None:
    day = pd.Timestamp("2024-09-25")
    frame = pd.DataFrame(
        {"open": [9.0, 10.0, 14.0, 15.0], "high": [9.0, 10.0, 14.0, 15.0],
         "low": [9.0, 10.0, 14.0, 15.0], "close": [90.0, 100.0, 140.0, 999.0],
         "volume": [1, 1, 1, 1], "atr": [1.0, 1.0, 1.0, 1.0]},
        index=pd.DatetimeIndex([day.replace(hour=9), day.replace(hour=10), day.replace(hour=14), day.replace(hour=15)]),
    )
    strategy = object.__new__(RegistryHtsStrategy)
    strategy._ctx = SimpleNamespace(inputs=SimpleNamespace(hourly={"AAA": frame}))
    ten = strategy._stamp(day.date(), 10)
    fifteen = strategy._stamp(day.date(), 15)
    assert strategy._completed_row("AAA", ten).name == day.replace(hour=9)
    assert strategy._execution_price("AAA", ten) == pytest.approx(10.0)
    assert strategy._completed_row("AAA", fifteen).name == day.replace(hour=14)
    assert strategy._execution_price("AAA", fifteen) == pytest.approx(15.0)


class _SyntheticRebalanceStrategy(RegistryHtsStrategy):
    """Native strategy double with deterministic cash for rebalance wiring."""

    @property
    def cash(self) -> float:
        return 100_000.0

    @property
    def portfolio_value(self) -> float:
        return 100_000.0


def _synthetic_rebalance_strategy(rebalance_hour: int) -> tuple[RegistryHtsStrategy, list[object]]:
    """Build one causal rebalance fixture using the production lifecycle method."""
    day = pd.Timestamp("2024-09-25")
    next_day = day + pd.offsets.BDay(1)
    prior_day = (day - pd.offsets.BDay(1)).date()
    frame = pd.DataFrame(
        {
            "open": [9.0, 10.0, 14.0, 15.0, 19.0],
            "high": [9.5, 10.5, 14.5, 15.5, 19.5],
            "low": [8.5, 9.5, 13.5, 14.5, 18.5],
            # The distinct 15:00 values make accidental use of its close/ATR
            # visible: the mapped variant must retain the completed 14:00 row.
            "close": [90.0, 100.0, 140.0, 9_999.0, 190.0],
            "volume": [1.0, 1.0, 1.0, 1.0, 1.0],
            "atr": [9.0, 10.0, 14.0, 999.0, 19.0],
        },
        index=pd.DatetimeIndex([
            day.replace(hour=9), day.replace(hour=10), day.replace(hour=14), day.replace(hour=15),
            next_day.replace(hour=9),
        ]),
    )
    strategy = object.__new__(_SyntheticRebalanceStrategy)
    strategy._params = {**HTS_BASELINE, "signal_hour": 9, "rebalance_hour": rebalance_hour}
    strategy._ctx = SimpleNamespace(inputs=SimpleNamespace(hourly={"AAA": frame}))
    strategy._sessions = [prior_day, day.date(), next_day.date()]
    strategy._session_index = {session: index for index, session in enumerate(strategy._sessions)}
    strategy._selected = ()
    strategy._signal_day = None
    strategy._positions = {}
    strategy._pending_buys = {}
    strategy._pending_sells = set()
    strategy._pending_sell_reason = {}
    strategy._rejections = []
    strategy._diag = []
    strategy._journal = []
    strategy._entry_edge_events = []
    strategy._risk_off_race_symbols = set()
    strategy._deferred_rebalance = None
    strategy._deferred_rebalance_events = []
    strategy._test_clock = {"now": day.replace(hour=10 if rebalance_hour == 10 else 14)}
    strategy.get_datetime = lambda: strategy._test_clock["now"].to_pydatetime()
    strategy._refresh_risk_off_state = lambda _day: False
    strategy._schedule_due = lambda _day: True
    strategy._select = lambda _day: ("AAA",)
    strategy._update_risk = lambda *_args, **_kwargs: None
    strategy.update_broker_balances = lambda **_kwargs: None
    strategy.get_tracked_positions = lambda: ()
    strategy._target_weights = lambda *_args: {"AAA": 0.10}
    submitted: list[object] = []
    strategy.create_order = lambda symbol, **kwargs: SimpleNamespace(
        asset=SimpleNamespace(symbol=symbol), identifier=f"synthetic-{len(submitted) + 1}", **kwargs,
    )
    strategy.submit_order = submitted.append
    return strategy, submitted


@pytest.mark.parametrize(
    ("rebalance_hour", "expected_atr", "expected_price"),
    ((10, 9.0, 10.0), (15, 14.0, 19.0)),
)
def test_rebalance_hour_ten_and_fifteen_submit_causal_entries(
    rebalance_hour: int, expected_atr: float, expected_price: float,
) -> None:
    # This runs the actual lifecycle and _rebalance path.  The 15:00 case is
    # triggered at native hour 14 but must retain the 14:00 completed source,
    # never the synthetic 15:00 close/ATR sentinel.
    strategy, submitted = _synthetic_rebalance_strategy(rebalance_hour)
    strategy.on_trading_iteration()
    if rebalance_hour == 15:
        assert submitted == []
        queued = strategy._deferred_rebalance_events[-1]
        assert queued["completed_source_bar"] == "2024-09-25T14:00:00"
        assert queued["intended_source_fill_bar"] == "2024-09-25T15:00:00"
        assert queued["decision_price_source"] == "completed source close; no 15:00 OHLC value read"
        assert strategy._deferred_rebalance["buys"][0]["reference"] == pytest.approx(140.0)
        strategy._test_clock["now"] = pd.Timestamp("2024-09-26 09:00")
        strategy.on_trading_iteration()
    assert len(submitted) == 1
    assert submitted[0].side == "buy"
    assert strategy._pending_buys["AAA"]["atr"] == pytest.approx(expected_atr)
    intent = next(event for event in reversed(strategy._journal) if event.get("event") == "intent")
    assert intent["reference"] == pytest.approx(expected_price)


def test_rebalance_hour_fifteen_maps_risk_off_flatten_to_the_native_hour_fourteen_iteration() -> None:
    day = pd.Timestamp("2024-09-25")
    frame = pd.DataFrame(
        {"open": [14.0, 15.0], "high": [14.0, 15.0], "low": [14.0, 15.0],
         "close": [140.0, 150.0], "volume": [1.0, 1.0], "atr": [1.0, 1.0]},
        index=pd.DatetimeIndex([day.replace(hour=14), day.replace(hour=15)]),
    )
    strategy = object.__new__(RegistryHtsStrategy)
    strategy._params = {**HTS_V2_BASELINE, "rebalance_hour": 15}
    strategy._ctx = SimpleNamespace(inputs=SimpleNamespace(hourly={"AAA": frame}))
    strategy._selected = ("AAA",)
    strategy._positions = {"AAA": {"quantity": 1.0}}
    strategy._pending_buys = {}
    strategy._cancelled_pending_buys = {}
    strategy._pending_sells = set()
    strategy._pending_sell_reason = {}
    strategy._protective = {}
    strategy._journal = []
    strategy._risk_off_race_symbols = set()
    strategy._risk_off_flatten_count = 0
    strategy._risk_off_active = True
    strategy._deferred_rebalance = None
    strategy._deferred_rebalance_events = []
    strategy._sessions = [day.date()]
    strategy._session_index = {day.date(): 0}
    clock = {"now": day.replace(hour=14)}
    strategy.get_datetime = lambda: clock["now"].to_pydatetime()
    strategy._refresh_risk_off_state = lambda _day: True
    strategy._update_risk = lambda *_args, **_kwargs: None
    submitted: list[object] = []
    strategy.create_order = lambda symbol, **kwargs: SimpleNamespace(
        asset=SimpleNamespace(symbol=symbol), identifier="risk-off", **kwargs,
    )
    strategy.submit_order = submitted.append
    strategy.on_trading_iteration()
    assert submitted == []
    assert strategy._deferred_rebalance_events[-1]["completed_source_bar"] == "2024-09-25T14:00:00"
    clock["now"] = pd.Timestamp("2024-09-26 09:00")
    strategy.on_trading_iteration()
    assert len(submitted) == 1
    assert submitted[0].side == "sell"
    assert any(event["event"] == "global_risk_off_flatten" and event["hour"] == 9 for event in strategy._journal)
    assert strategy._deferred_rebalance_events[-1]["actual_submission_time"] == "2024-09-26T09:00:00"


@pytest.mark.parametrize("rebalance_hour", (16, 23))
def test_check_supported_rejects_rebalance_hours_without_a_native_iteration(rebalance_hour: int) -> None:
    missing = check_supported({**HTS_V2_BASELINE, "rebalance_hour": rebalance_hour}, HTS_V2_BASELINE)
    assert missing == (
        f"rebalance_hour={rebalance_hour!r} (no native LumiBot iteration; "
        "supported clock hours are 09:00-15:00, with 15:00 mapped to the 14:00 callback)",
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


def _risk_gate_strategy(gate: str, cooldown: int) -> tuple[RegistryHtsStrategy, list[object]]:
    """Minimal native strategy state for exercising the real v2 gate refresh."""
    sessions = [stamp.date() for stamp in pd.bdate_range("2024-01-02", periods=7)]
    # The first completed session is below SMA200, then every later one is open.
    spy = pd.DataFrame(
        {"close": [99.0, 101.0, 101.0, 101.0, 101.0, 101.0, 101.0],
         "sma100": [100.0] * 7, "sma200": [100.0] * 7},
        index=pd.DatetimeIndex(sessions),
    )
    strategy = object.__new__(RegistryHtsStrategy)
    strategy._params = {**HTS_V2_BASELINE, "risk_off_gate": gate, "risk_off_cooldown_bars": cooldown}
    strategy._ctx = SimpleNamespace(inputs=SimpleNamespace(
        benchmark={"SPY": spy, "QQQ": spy}, breadth=(), breadth_expected_count=0,
    ))
    strategy._sessions = sessions
    strategy._session_index = {day: index for index, day in enumerate(sessions)}
    strategy._journal = []
    strategy._risk_off_active = False
    strategy._last_risk_gate_session = None
    strategy._re_risk_deadline_index = None
    strategy._risk_off_transitions = []
    return strategy, sessions


def test_v2_risk_off_gate_and_cooldown_change_native_state_not_legacy_market_gate() -> None:
    ungated, sessions = _risk_gate_strategy("none", 0)
    gated, _ = _risk_gate_strategy("spy-sma200", 0)
    for day in sessions[1:]:
        assert ungated._refresh_risk_off_state(day) is False
    states = [gated._refresh_risk_off_state(day) for day in sessions[1:]]
    assert states[0] is True  # SPY below SMA200 cancels/suppresses risk only in the gated run.
    assert states[1] is False
    assert gated._risk_off_transitions[0]["reason"] == "gate_closed"

    delayed, delayed_sessions = _risk_gate_strategy("spy-sma200", 3)
    delayed_states = [delayed._refresh_risk_off_state(day) for day in delayed_sessions[1:]]
    # After the single closed observation, exactly three later completed
    # sessions remain cash; the fourth open observation permits re-risk.
    assert delayed_states[:4] == [True, True, True, True]
    assert delayed_states[4] is False


def test_v2_global_risk_off_cancels_buys_and_flattens_the_entire_native_book() -> None:
    strategy = object.__new__(RegistryHtsStrategy)
    strategy._positions = {
        "AAA": {"quantity": 1.0},
        "BBB": {"quantity": 2.0},
    }
    pending_order = SimpleNamespace()
    strategy._pending_buys = {"CCC": {"order": pending_order, "atr": 1.0}}
    strategy._cancelled_pending_buys = {}
    strategy._pending_sells = set()
    strategy._pending_sell_reason = {}
    strategy._protective = {}
    strategy._journal = []
    strategy._risk_off_race_symbols = set()
    strategy._risk_off_flatten_count = 0
    strategy._stamp = lambda _day, _hour: pd.Timestamp("2024-01-03 10:00")
    strategy._execution_price = lambda _symbol, _stamp: 100.0
    cancelled: list[object] = []
    submitted: list[object] = []
    strategy.cancel_order = cancelled.append
    strategy.create_order = lambda symbol, **kwargs: SimpleNamespace(asset=SimpleNamespace(symbol=symbol), **kwargs)
    strategy.submit_order = submitted.append
    strategy._flatten_risk_off(pd.Timestamp("2024-01-03").date(), 10)
    assert cancelled == [pending_order]
    assert {order.asset.symbol for order in submitted} == {"AAA", "BBB"}
    assert strategy._risk_off_flatten_count == 2


def test_v2_resting_atr_alias_and_stop_multiple_change_native_stop_levels() -> None:
    submitted: list[object] = []
    strategy = object.__new__(RegistryHtsStrategy)
    strategy._positions = {"AAA": {"quantity": 2.0, "entry_price": 100.0, "entry_atr": 5.0}}
    strategy._protective = {}
    strategy._journal = []
    strategy.create_order = lambda symbol, **kwargs: SimpleNamespace(asset=SimpleNamespace(symbol=symbol), **kwargs)
    strategy.submit_order = submitted.append
    strategy._params = {"exit_mode": "resting-stop-atr", "atr_k": 2.0}
    strategy._place_protective_stop("AAA")
    assert strategy._positions["AAA"]["protective_level"] == pytest.approx(90.0)
    strategy._positions["AAA"].pop("protective_level")
    strategy._params["atr_k"] = 4.0
    strategy._place_protective_stop("AAA")
    assert strategy._positions["AAA"]["protective_level"] == pytest.approx(80.0)


def _v2_weight_strategy(cap: float | None) -> tuple[RegistryHtsStrategy, object, object]:
    sessions = [stamp.date() for stamp in pd.bdate_range("2024-01-02", periods=2)]
    hourly = pd.DataFrame(
        {"open": [100.0, 100.0], "high": [100.0, 100.0], "low": [100.0, 100.0],
         "close": [100.0, 100.0], "volume": [1.0, 1.0], "atr": [5.0, 5.0]},
        index=pd.DatetimeIndex([pd.Timestamp(sessions[1]).replace(hour=9), pd.Timestamp(sessions[1]).replace(hour=10)]),
    )
    daily = pd.DataFrame({"vol20": [0.20], "ret1": [0.01]}, index=pd.DatetimeIndex([sessions[0]]))
    strategy = object.__new__(RegistryHtsStrategy)
    strategy._params = {**HTS_V2_BASELINE, "risk_contribution_cap": cap}
    strategy._ctx = SimpleNamespace(inputs=SimpleNamespace(daily={"AAA": daily}, hourly={"AAA": hourly}))
    strategy._selected = ("AAA",)
    strategy._risk_cap_events = []
    return strategy, sessions[1], sessions[0]


def test_v2_risk_cap_changes_the_native_target_weight_after_parent_sizing() -> None:
    uncapped, day, session = _v2_weight_strategy(None)
    capped, _day, _session = _v2_weight_strategy(0.005)
    uncapped_weights = uncapped._target_weights(day, 10, session)
    capped_weights = capped._target_weights(day, 10, session)
    assert uncapped_weights["AAA"] == pytest.approx(0.995)
    # 2 * 5 / 100 = 10% stop room; 0.5% NAV risk caps the .995 parent
    # allocation at 5% with no redistribution.
    assert capped_weights["AAA"] == pytest.approx(0.05)
    assert capped._risk_cap_events[-1]["risk_contribution"] == pytest.approx(0.005)


def _minimum_hold_strategy(holding_bars: int) -> tuple[RegistryHtsStrategy, list[object]]:
    sessions = [stamp.date() for stamp in pd.bdate_range("2024-01-02", periods=5)]
    daily = {
        "AAA": pd.DataFrame({"close": [101.0] * 5, "sma": [100.0] * 5, "ret": [0.10] * 5,
                             "mdv": [10_000_000.0] * 5}, index=pd.DatetimeIndex(sessions)),
        "BBB": pd.DataFrame({"close": [101.0] * 5, "sma": [100.0] * 5, "ret": [0.20] * 5,
                             "mdv": [10_000_000.0] * 5}, index=pd.DatetimeIndex(sessions)),
    }
    strategy = object.__new__(RegistryHtsStrategy)
    strategy._params = {**HTS_V2_BASELINE, "top_n": 1, "min_position_holding_bars": holding_bars}
    strategy._ctx = SimpleNamespace(inputs=SimpleNamespace(daily=daily, benchmark={}, breadth=()))
    strategy._ordered = ("AAA", "BBB")
    strategy._universe_order = {"AAA": 0, "BBB": 1}
    strategy._sessions = sessions
    strategy._session_index = {day: index for index, day in enumerate(sessions)}
    strategy._positions = {"AAA": {"entry_session": sessions[0]}}
    strategy._cooldowns = {}
    strategy._ranks = {}
    strategy._minimum_hold_deferrals = 0
    strategy._risk_off_active = False
    return strategy, sessions


def test_v2_minimum_hold_changes_native_selection_but_not_stop_ownership() -> None:
    immediate, sessions = _minimum_hold_strategy(0)
    held, _ = _minimum_hold_strategy(3)
    # At the next selection, BBB ranks higher.  Only the v2 hold protects the
    # existing slot; virtual/protective stop processing remains outside _select.
    assert immediate._select(sessions[2]) == ("BBB",)
    assert held._select(sessions[2]) == ("AAA",)
    assert held._minimum_hold_deferrals == 1
    # At the exact three-complete-session expiry the incumbent is replaceable.
    assert held._select(sessions[4]) == ("BBB",)


class _EdgeStrategy(RegistryHtsStrategy):
    @property
    def cash(self) -> float:
        return 100_000.0

    @property
    def portfolio_value(self) -> float:
        return 100_000.0


def _v2_edge_rebalance_strategy(prior_return: float, threshold: float) -> tuple[RegistryHtsStrategy, list[tuple]]:
    sessions = [stamp.date() for stamp in pd.bdate_range("2024-01-02", periods=2)]
    hourly = pd.DataFrame(
        {"open": [100.0, 100.0], "high": [101.0, 101.0], "low": [99.0, 99.0],
         "close": [100.0, 100.0], "volume": [1.0, 1.0], "atr": [5.0, 5.0]},
        index=pd.DatetimeIndex([pd.Timestamp(sessions[1]).replace(hour=9), pd.Timestamp(sessions[1]).replace(hour=10)]),
    )
    daily = pd.DataFrame({"ret": [prior_return]}, index=pd.DatetimeIndex([sessions[0]]))
    strategy = object.__new__(_EdgeStrategy)
    strategy._params = {**HTS_V2_BASELINE, "min_trade_edge_bps": threshold}
    strategy._ctx = SimpleNamespace(inputs=SimpleNamespace(daily={"AAA": daily}, hourly={"AAA": hourly}))
    strategy._selected = ("AAA",)
    strategy._positions = {}
    strategy._pending_buys = {}
    strategy._pending_sells = set()
    strategy._rejections = []
    strategy._diag = []
    strategy._journal = []
    strategy._entry_edge_events = []
    strategy._risk_off_active = False
    strategy._sessions = sessions
    strategy._session_index = {day: index for index, day in enumerate(sessions)}
    strategy.update_broker_balances = lambda **_kwargs: None
    strategy.get_tracked_positions = lambda: ()
    strategy._target_weights = lambda *_args: {"AAA": 0.10}
    submitted: list[tuple] = []
    strategy._submit_buy = lambda *args: submitted.append(args)
    return strategy, submitted


def test_v2_edge_floor_is_applied_immediately_before_native_new_buys() -> None:
    # About 0.5 bps, so a 14 bps hurdle rejects while zero remains disabled.
    zero, zero_buys = _v2_edge_rebalance_strategy(0.001, 0.0)
    floor, floor_buys = _v2_edge_rebalance_strategy(0.001, 14.0)
    day = zero._sessions[1]
    zero._rebalance(day, 10)
    floor._rebalance(day, 10)
    assert len(zero_buys) == 1
    assert floor_buys == []
    assert floor._entry_edge_events[-1]["accepted"] is False
    # Exact threshold uses >=, so equality accepts.
    exact, exact_buys = _v2_edge_rebalance_strategy(math.expm1(math.log1p(0.0014) * 20.0), 14.0)
    exact._rebalance(exact._sessions[1], 10)
    assert len(exact_buys) == 1
    assert exact._entry_edge_events[-1]["expected_move_bps"] == pytest.approx(14.0)


def test_v2_default_parent_paths_are_native_event_identical(tmp_path: Path) -> None:
    """The five default overlays must not perturb their frozen v1 parents."""
    from strategy_lab.native_experiments import WINDOW_BY_LABEL, run_candidate

    registry = get_registry()
    for parent_id, v2_id in (
        ("H100", "V001"), ("H027", "V021"), ("H022", "V041"),
        ("H095", "V061"), ("HTS_CONTROL_1", "V081"),
    ):
        parent = run_candidate(registry.get(parent_id), WINDOW_BY_LABEL["b01"], tmp_path, control_baseline=HTS_BASELINE)
        child = run_candidate(registry.get(v2_id), WINDOW_BY_LABEL["b01"], tmp_path, control_baseline=HTS_BASELINE)
        assert parent.ok and child.ok
        assert stable_hash(parent.out_dir) == stable_hash(child.out_dir), (parent_id, v2_id)


def test_v2_hour_fifteen_native_pilot_candidates_have_causal_non_flat_fills(tmp_path: Path) -> None:
    """The formerly dead P02/P20 paths must enter through native LumiBot."""
    from strategy_lab.native_experiments import run_candidate

    registry = get_registry()
    for candidate_id in ("V002", "V100"):
        run = run_candidate(
            registry.get(candidate_id), WINDOW_BY_LABEL["b01"], tmp_path, control_baseline=HTS_BASELINE,
        )
        assert run.ok, run.problems
        assert run.payload["fills"] > 0
        assert run.payload["metrics"]["total_return"] != 0.0
        events = run.payload["deferred_rebalance_events"]
        queued = next(event for event in events if event["event"] == "deferred_rebalance_queued")
        submitted = next(event for event in events if event["event"] == "deferred_rebalance_submitted")
        assert queued["native_iteration_hour"] == 14
        assert queued["completed_source_bar"].endswith("T14:00:00")
        assert submitted["actual_submission_time"].endswith("T09:00:00")
