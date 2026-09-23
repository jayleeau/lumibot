"""Run registered HTS research candidates on LumiBot's native backtest engine.

This is the execution layer for ``docs/HTS_100_VARIATIONS_RESEARCH_PLAN.md`` and
``docs/HTS_REMAINING_IMPLEMENTATION_PLAN.md``.  It reads a resolved
:class:`~strategy_lab.experiment_config.CandidateSpec`, prepares the archived
bars it needs, and runs one native LumiBot backtest per candidate and window.
LumiBot's broker and portfolio own simulated time, order lifecycle, fills, fees,
cash, and valuation; this module owns the strategy decisions and the independent
metrics.

Design rules:

* A candidate is only run when this module actually implements every mechanism it
  asks for.  :func:`check_supported` returns the mechanisms that are not
  implemented, and the runner reports those candidates rather than silently
  running them with control behaviour and calling the result H031.
* Metrics are computed here from the saved equity curve, independent of LumiBot's
  own analysis.  LumiBot's ``sharpe`` is ``(CAGR - risk_free) / volatility``; it is
  reported as ``cagr_over_volatility`` and never as a Sharpe ratio.
* Feature calculations live in :mod:`strategy_lab.feature_store` and pure policy
  calculations in :mod:`strategy_lab.hts_policies`.  This module owns the bridge,
  the event loop, orders, fills, and run artifacts.
"""
from __future__ import annotations

import bisect
import gc
import json
import math
import os
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any, Mapping, Sequence

# Mark the process as a backtest before importing LumiBot so repository-adjacent
# .env files cannot create a live broker or start a trading stream.
os.environ.setdefault("IS_BACKTESTING", "true")
os.environ.setdefault("LUMIBOT_DISABLE_DOTENV", "true")
# Pin BLAS/OpenMP to one thread *before* numpy is imported.  Eight worker
# processes each spawning a full thread pool multiplies resident memory and
# thrashes this machine; the feature math here is small and vectorized already.
for _thread_var in (
    "OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS",
    "NUMEXPR_NUM_THREADS", "VECLIB_MAXIMUM_THREADS",
):
    os.environ.setdefault(_thread_var, "1")

import duckdb
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]

from lumibot.backtesting import PandasDataBacktesting  # noqa: E402
from lumibot.entities import Asset, Data, Order, TradingFee  # noqa: E402
from lumibot.strategies import Strategy  # noqa: E402

from strategy_lab.experiment_config import (  # noqa: E402
    EXECUTION_ENGINE,
    KIND_ALTERNATIVE,
    KIND_HTS_V2,
    CandidateSpec,
)
from strategy_lab.experiment_universes import (  # noqa: E402
    BREADTH_BASKET,
    LEVERAGED_PRODUCTS,
    exposure_group,
    resolve_universe,
)
from strategy_lab.feature_store import (  # noqa: E402
    covariance_matrix,
    daily_feature_frame,
    finite,
    sample_correlation,
)
from strategy_lab.hts_policies import (  # noqa: E402
    PolicyError,
    apply_leveraged_cap,
    cap_risk_contributions,
    expected_trade_move_bps,
    forecast_volatility,
    market_gate_open,
    rank_scores,
    risk_off_gate_open,
    schedule_due,
    select_holdings,
    target_weights,
)
from strategy_lab.hts_audit import (  # noqa: E402
    AtomicJsonStore,
    build_decision_snapshot,
    build_strategy_event_payload,
    deserialize_runtime_state,
    serialize_runtime_state,
)

DAILY_DB = ROOT / "short" / "suite_monitored_xnas_itch_daily_adjusted.duckdb"
HOURLY_DB = ROOT / "short" / "suite_v2_xnas_itch_hourly_adjusted.duckdb"
ET = "America/New_York"
USD = Asset("USD", "forex")
INITIAL_CASH = 100_000.0
WARMUP_DAYS = 500


def _add_months(value: str, months: int) -> str:
    """Add whole calendar months to an ISO ``YYYY-MM-DD`` date string."""
    return (pd.Timestamp(value) + pd.DateOffset(months=months)).normalize().date().isoformat()


BENCHMARK_SYMBOLS: tuple[str, ...] = ("SPY", "QQQ")
ENGINE_LABEL = "lumibot.strategies.Strategy.run_backtest + BacktestingBroker"
HTS_HOURLY_CONVENTION = (
    "clock-hour 09:00-15:00 ET; a bar labelled T contains [T,T+1h) and is only known at T+1h"
)
# Bump this whenever a strategy mechanism changes.  The runner refuses to treat
# an artifact from a different revision as a completed result under --resume,
# so results from two implementations can never be mixed.
IMPLEMENTATION_REVISION = "hts-native-v2-2026-09-16-2"


@dataclass(frozen=True)
class ExperimentWindow:
    """One descriptive backtest window."""

    label: str
    start: str
    end: str

    @property
    def start_dt(self) -> datetime:
        return datetime.fromisoformat(f"{self.start}T00:00:00")

    @property
    def end_dt(self) -> datetime:
        return datetime.fromisoformat(f"{self.end}T23:59:59")

    @property
    def warmup_start(self) -> str:
        return (pd.Timestamp(self.start) - pd.Timedelta(days=WARMUP_DAYS)).date().isoformat()


WINDOW_SIX_YEAR = ExperimentWindow("six_year", "2020-09-08", "2026-09-08")
WINDOW_TWO_YEAR = ExperimentWindow("two_year", "2024-09-08", "2026-09-08")
# Pre-2020 out-of-sample window (2018-05-01 -> 2020-04-30): a long-before-current
# regime covering the 2018 H2 selloff, 2019, and the start of the 2020 COVID crash.
# NOTE: the archives begin exactly at the window start, so there is NO 500-day
# warmup history before it; short-indicator strategies warm up within the window,
# but SMA200 / vol-covariance(60) / liquidity(63) signals are partial early on.
WINDOW_PRE_2020 = ExperimentWindow("pre_2020", "2018-05-01", "2020-04-30")
# 2022-01-01 -> 2024-12-31: a distinct mid-cycle regime (2022 bear, 2023 grind,
# 2024 recovery). Warmup_start (2020-08) is inside the 2018-May archive, so SMA200 /
# vol-cov(60) / liquidity(63) all have full pre-history -- no edge-of-data gap.
WINDOW_2022_2024 = ExperimentWindow("y2022_2024", "2022-01-01", "2024-12-31")
# 2018-05-01 -> 2021-12-31 (data begins 2018-05): early regime = 2018 H2 selloff,
# 2019, 2020 COVID crash + recovery, 2021 bull. Paired with y2022_2024 it splits
# pre-2025 history into two comparable multi-year windows for the graph pages.
WINDOW_EARLY = ExperimentWindow("early", "2018-05-01", "2021-12-31")
WINDOWS: tuple[ExperimentWindow, ...] = (
    WINDOW_SIX_YEAR, WINDOW_TWO_YEAR, WINDOW_PRE_2020, WINDOW_2022_2024,
    WINDOW_EARLY,
)

# Retrospective walk-forward fold schedule (plan Phase 4).  Six rolling outer
# folds; each fold has a discovery interval, two inner-validation sub-windows
# (months 24-30 and 30-36 of the discovery interval) used only for selection,
# and a frozen outer test interval (the next six months) scored after selection.
# Dates follow the plan table exactly.  Only the inner-validation and outer-test
# windows need to be run as backtests; the discovery labels are kept for
# provenance/feature-warmup context and are intentionally not scheduled.
_WF_FOLDS: tuple[
    tuple[str, str, str, str, str, str, str], ...
] = (
    # (fold, disc_start, disc_end, innerA_start, innerA_end, innerB_start, innerB_end)
    # innerA = months 24-30, innerB = months 30-36 of the discovery interval;
    # outer test = disc_end .. disc_end+6m.
    (
        "f1", "2020-09-09", "2023-09-09",
        "2022-09-09", "2023-03-09", "2023-03-09", "2023-09-09",
    ),
    (
        "f2", "2021-03-09", "2024-03-09",
        "2023-03-09", "2023-09-09", "2023-09-09", "2024-03-09",
    ),
    (
        "f3", "2021-09-09", "2024-09-09",
        "2023-09-09", "2024-03-09", "2024-03-09", "2024-09-09",
    ),
    (
        "f4", "2022-03-09", "2025-03-09",
        "2024-03-09", "2024-09-09", "2024-09-09", "2025-03-09",
    ),
    (
        "f5", "2022-09-09", "2025-09-09",
        "2024-09-09", "2025-03-09", "2025-03-09", "2025-09-09",
    ),
    (
        "f6", "2023-03-09", "2026-03-09",
        "2025-03-09", "2025-09-09", "2025-09-09", "2026-03-09",
    ),
)

WALK_FORWARD_WINDOWS: tuple[ExperimentWindow, ...] = tuple(
    ExperimentWindow(f"{fold}_{kind}", start, end)
    for fold, _disc_start, disc_end, a_s, a_e, b_s, b_e in _WF_FOLDS
    for start, end, kind in (
        (a_s, a_e, "innerA"),
        (b_s, b_e, "innerB"),
        (disc_end, _add_months(disc_end, 6), "test"),
    )
)

# V2 selection uses the predeclared six-month block programme rather than the
# former two-inner-window score.  Blocks run from cash and are shared by all
# rolling folds; the fold mapping itself lives alongside the evaluator.
V2_DISCOVERY_BLOCKS: tuple[ExperimentWindow, ...] = tuple(
    ExperimentWindow(label, start, end)
    for label, start, end in (
        ("b01", "2020-09-09", "2021-03-09"),
        ("b02", "2021-03-09", "2021-09-09"),
        ("b03", "2021-09-09", "2022-03-09"),
        ("b04", "2022-03-09", "2022-09-09"),
        ("b05", "2022-09-09", "2023-03-09"),
        ("b06", "2023-03-09", "2023-09-09"),
        ("b07", "2023-09-09", "2024-03-09"),
        ("b08", "2024-03-09", "2024-09-09"),
        ("b09", "2024-09-09", "2025-03-09"),
        ("b10", "2025-03-09", "2025-09-09"),
        ("b11", "2025-09-09", "2026-03-09"),
        ("b12", "2026-03-09", "2026-09-09"),
    )
)
V2_FOLD_BLOCKS: Mapping[str, tuple[tuple[str, ...], str]] = {
    "f1": (("b01", "b02", "b03", "b04", "b05", "b06"), "b07"),
    "f2": (("b02", "b03", "b04", "b05", "b06", "b07"), "b08"),
    "f3": (("b03", "b04", "b05", "b06", "b07", "b08"), "b09"),
    "f4": (("b04", "b05", "b06", "b07", "b08", "b09"), "b10"),
    "f5": (("b05", "b06", "b07", "b08", "b09", "b10"), "b11"),
    "f6": (("b06", "b07", "b08", "b09", "b10", "b11"), "b12"),
}
WINDOW_BY_LABEL: dict[str, ExperimentWindow] = {
    window.label: window for window in WINDOWS + WALK_FORWARD_WINDOWS + V2_DISCOVERY_BLOCKS
}


class UnsupportedCandidateError(RuntimeError):
    """Raised when a candidate needs a mechanism this module does not implement."""


def _sql_frames(
    db: Path,
    table: str,
    symbols: Sequence[str],
    start: str,
    end: str,
    *,
    hourly: bool,
) -> dict[str, pd.DataFrame]:
    """Load adjusted OHLCV frames from a local DuckDB archive.

    Hourly timestamps convert to exchange-local naive time because the strategy's
    signal and rebalance hours are New York clocks.  Daily timestamps keep the
    archive's UTC session date, because converting them to ET moves a session to
    the prior evening and keys every daily bar one day early.
    """
    if not symbols:
        return {}
    placeholders = ", ".join("?" for _ in symbols)
    query = (
        f"SELECT symbol, ts, open, high, low, close, volume FROM {table} "
        f"WHERE symbol IN ({placeholders}) AND ts >= ? AND ts <= ? ORDER BY symbol, ts"
    )
    end_bound = pd.Timestamp(end, tz="UTC") + pd.Timedelta(days=1) - pd.Timedelta(microseconds=1)
    with duckdb.connect(str(db), read_only=True) as con:
        rows = con.execute(query, [*symbols, pd.Timestamp(start, tz="UTC"), end_bound]).fetchdf()
    if rows.empty:
        return {}
    rows["ts"] = pd.to_datetime(rows["ts"], utc=True)
    out: dict[str, pd.DataFrame] = {}
    for symbol, group in rows.groupby("symbol", sort=False):
        group = group.sort_values("ts").copy()
        stamps = group.pop("ts")
        if hourly:
            index = stamps.dt.tz_convert(ET).dt.tz_localize(None)
        else:
            index = stamps.dt.tz_localize(None).dt.normalize()
        frame = group[["open", "high", "low", "close", "volume"]].astype(float)
        frame.index = pd.DatetimeIndex(index)
        frame = frame[~frame.index.duplicated(keep="last")].sort_index()
        out[str(symbol)] = frame
    return out


def _hourly_features(frame: pd.DataFrame, *, atr_period: int) -> pd.DataFrame:
    previous_close = frame["close"].shift(1)
    true_range = np.maximum(
        frame["high"] - frame["low"],
        np.maximum((frame["high"] - previous_close).abs(), (frame["low"] - previous_close).abs()),
    )
    result = frame.copy()
    result["atr"] = pd.Series(true_range, index=frame.index, dtype="float64").rolling(atr_period).mean()
    return result


def _canonical_hourly_frame(frame: pd.DataFrame) -> pd.DataFrame:
    """Return the one exact clock-hour frame shared by HTS features and broker data."""
    index = frame.index
    exact_clock_hour = (
        (index.hour >= 9)
        & (index.hour <= 15)
        & (index.minute == 0)
        & (index.second == 0)
        & (index.microsecond == 0)
        & (index.nanosecond == 0)
    )
    return frame.loc[exact_clock_hour, ["open", "high", "low", "close", "volume"]].copy()


def _lumibot_hourly(frame: pd.DataFrame) -> pd.DataFrame:
    """Retain canonical 09:00-15:00 clock-hour labels for LumiBot ``Data``."""
    return _canonical_hourly_frame(frame)


def _benchmark_features(frame: pd.DataFrame) -> pd.DataFrame:
    """Daily features plus the SMA columns the documented gates read."""
    result = daily_feature_frame(
        frame, trend_sma=200, return_period=1, liquidity_period=1, benchmark_close=None,
    )
    close = frame["close"].astype("float64")
    result["sma50"] = close.rolling(50, min_periods=50).mean()
    result["sma100"] = close.rolling(100, min_periods=100).mean()
    result["sma200"] = result["sma"]
    return result


@dataclass(frozen=True)
class PreparedInputs:
    """Everything one candidate needs to run, prepared once per window."""

    window: ExperimentWindow
    universe_keyword: str
    ordered_symbols: tuple[str, ...]
    daily: Mapping[str, pd.DataFrame]
    hourly: Mapping[str, pd.DataFrame]
    benchmark: Mapping[str, pd.DataFrame]
    breadth: tuple[pd.DataFrame, ...]
    breadth_expected_count: int
    sessions: tuple[date, ...]
    lumibot_data: tuple[Data, ...]
    feature_hash: str


def prepare_inputs(params: Mapping[str, Any], window: ExperimentWindow) -> PreparedInputs:
    """Load and prepare the bars a candidate needs for one window."""
    universe_keyword = str(params["universe"])
    universe_symbols = tuple(resolve_universe(universe_keyword))
    support = set(BENCHMARK_SYMBOLS) | set(BREADTH_BASKET)
    need = sorted(set(universe_symbols) | support)

    daily_raw = _sql_frames(DAILY_DB, "bars_daily", need, window.warmup_start, window.end, hourly=False)
    hourly_raw = _sql_frames(HOURLY_DB, "bars_hourly", list(universe_symbols),
                             window.warmup_start, window.end, hourly=True)

    benchmark_close = None
    if "QQQ" in daily_raw:
        benchmark_close = daily_raw["QQQ"]["close"]

    trend_sma = int(params["trend_sma"])
    return_period = int(params["return_period"])
    liquidity_period = int(params["liquidity_period"])
    atr_period = int(params["atr_period"])
    breadth_sma = int(params["breadth_sma"])

    hourly_canonical = {
        symbol: _canonical_hourly_frame(frame) for symbol, frame in hourly_raw.items()
    }
    ordered = tuple(
        symbol
        for symbol in universe_symbols
        if symbol in daily_raw and not hourly_canonical.get(symbol, pd.DataFrame()).empty
    )
    daily = {
        symbol: daily_feature_frame(
            daily_raw[symbol],
            trend_sma=trend_sma,
            return_period=return_period,
            liquidity_period=liquidity_period,
            benchmark_close=benchmark_close,
        )
        for symbol in ordered
    }
    hourly = {
        symbol: _hourly_features(hourly_canonical[symbol], atr_period=atr_period)
        for symbol in ordered
    }
    benchmark = {
        symbol: _benchmark_features(daily_raw[symbol])
        for symbol in BENCHMARK_SYMBOLS
        if symbol in daily_raw
    }
    breadth_frames: list[pd.DataFrame] = []
    for symbol in BREADTH_BASKET:
        if symbol not in daily_raw:
            continue
        frame = daily_feature_frame(
            daily_raw[symbol],
            trend_sma=breadth_sma,
            return_period=1,
            liquidity_period=1,
            benchmark_close=None,
        )
        # V2 breadth is fixed SMA50 regardless of v1's configurable legacy
        # market gate.  It remains a completed-session feature.
        frame["sma50"] = daily_raw[symbol]["close"].astype("float64").rolling(50, min_periods=50).mean()
        breadth_frames.append(frame)
    breadth = tuple(breadth_frames)
    sessions = tuple(sorted({stamp.date() for frame in daily.values() for stamp in frame.index}))
    lumibot_data = tuple(
        # Hand each Data its own canonical frame; LumiBot rewrites the index of
        # what it is given, so sharing the strategy feature frame would corrupt it.
        Data(Asset(symbol), _lumibot_hourly(hourly_canonical[symbol]), timestep="hour", quote=USD)
        for symbol in ordered
    )
    # The raw per-symbol frames are no longer needed once the derived feature
    # frames and the LumiBot payloads exist.  Releasing them here roughly halves
    # peak resident memory per worker, which is what lets several workers share
    # this machine without exhausting RAM.
    del daily_raw, hourly_raw, hourly_canonical
    gc.collect()
    payload = {
        "window": [window.label, window.start, window.end],
        "universe": universe_keyword,
        "symbols": list(ordered),
        "lookbacks": [trend_sma, return_period, liquidity_period, atr_period, breadth_sma],
        "daily_rows": int(sum(len(frame) for frame in daily.values())),
        "hourly_rows": int(sum(len(frame) for frame in hourly.values())),
        "hourly_convention": HTS_HOURLY_CONVENTION,
    }
    feature_hash = sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    return PreparedInputs(
        window=window,
        universe_keyword=universe_keyword,
        ordered_symbols=ordered,
        daily=daily,
        hourly=hourly,
        benchmark=benchmark,
        breadth=breadth,
        breadth_expected_count=len(BREADTH_BASKET),
        sessions=sessions,
        lumibot_data=lumibot_data,
        feature_hash=feature_hash,
    )


# --- mechanism coverage -------------------------------------------------------

# Every value the catalog declares for these knobs is implemented by
# ``RegistryHtsStrategy`` and the pure policies it calls.  ``check_supported``
# still verifies each value against this single list, so a future catalog edit
# that adds an unimplemented mode fails loudly rather than silently running with
# control behaviour.
IMPLEMENTED_MECHANISMS: Mapping[str, frozenset[str]] = {
    "rank_score": frozenset({
        "r20", "r20-over-vol20", "r60-over-vol60", "r20-over-ddvol20",
        "pctrank-10-20-60", "pctrank-20-60-120", "regression-momentum-60",
        "efficiency-ratio-20", "qqq-residual-momentum-20", "r20-skip-5",
    }),
    "weight_mode": frozenset({"equal-slots", "inverse-vol-20", "stop-distance-budget", "vol-target"}),
    "exit_mode": frozenset({
        "virtual-trail-baseline", "confirm-two-closes", "breach-buffer-0.25-atr",
        "breach-buffer-0.50-atr", "chandelier-since-entry", "chandelier-14-bar",
        "fixed-entry-atr", "virtual-trail-breakeven-2r", "resting-stop-2atr",
        "resting-stop-atr", "virtual-trail-plus-emergency-4atr",
    }),
    "market_gate": frozenset({
        "none", "spy-sma50", "spy-sma100", "spy-sma200", "qqq-sma100", "qqq-sma200",
        "spy-and-qqq-sma200", "breadth-sma50", "spy-vol-ratio-1.5",
    }),
    "rebalance_schedule": frozenset({
        "daily", "weekly-monday", "weekly-wednesday", "weekly-friday",
        "every-2nd-session", "every-3rd-session",
    }),
    "fill_convention": frozenset({"next-executable-open"}),
    "risk_off_gate": frozenset({
        "none", "spy-sma100", "spy-sma200", "qqq-sma100", "breadth-50", "breadth-60",
    }),
}

# The canonical feed contains 09:00 through 15:00 source bars, but the native
# hourly loop presents callbacks only through 14:00.  A configured 15:00
# rebalance therefore has to be triggered by that final callback while keeping
# its *logical* target clock at 15:00: completed source = 14:00.  Do not
# collapse those clocks: passing 14 to ``_rebalance`` would instead consume the
# 13:00 bar and price the 14:00 open, which is a different (and earlier)
# strategy.  The actual native fallback is queued to the next session open.
NATIVE_REBALANCE_ITERATION_HOURS = frozenset(range(9, 15))
REBALANCE_ITERATION_HOUR_MAP: Mapping[int, int] = {15: 14}


def effective_rebalance_iteration_hour(rebalance_hour: int) -> int:
    """Return the native callback hour that executes one configured rebalance."""
    return REBALANCE_ITERATION_HOUR_MAP.get(rebalance_hour, rebalance_hour)


def check_supported(params: Mapping[str, Any], control_baseline: Mapping[str, Any]) -> tuple[str, ...]:
    """Return the mechanisms this candidate needs but this module does not implement."""
    missing: list[str] = []
    for name, value in params.items():
        if name not in control_baseline:
            missing.append(f"{name}={value!r} (unknown parameter)")
            continue
        allowed = IMPLEMENTED_MECHANISMS.get(name)
        if allowed is not None and value not in allowed:
            missing.append(f"{name}={value!r}")
    rebalance_hour = params.get("rebalance_hour")
    if isinstance(rebalance_hour, int) and not isinstance(rebalance_hour, bool):
        effective_hour = effective_rebalance_iteration_hour(rebalance_hour)
        if effective_hour not in NATIVE_REBALANCE_ITERATION_HOURS:
            missing.append(
                f"rebalance_hour={rebalance_hour!r} (no native LumiBot iteration; "
                "supported clock hours are 09:00-15:00, with 15:00 mapped to the 14:00 callback)"
            )
    return tuple(sorted(missing))


# --- the native strategy ------------------------------------------------------

@dataclass
class RunContext:
    """Module-level handoff into the strategy instance LumiBot constructs."""

    inputs: PreparedInputs
    params: Mapping[str, Any]
    candidate_id: str
    fingerprint: str


_CONTEXT: RunContext | None = None


def build_virtual_stop_gap_event(
    trigger: Mapping[str, Any],
    *,
    fill_time: pd.Timestamp,
    fill_price: float,
    fill_quantity: float,
    fill_session: Any,
) -> dict[str, Any]:
    """Join immutable virtual-stop trigger context to its actual broker fill."""
    entry_price = float(trigger["entry_price"])
    stop_level = float(trigger["stop_level"])
    price = float(fill_price)
    gap = price - stop_level
    return {
        **dict(trigger),
        "fill_time": pd.Timestamp(fill_time).isoformat(),
        "fill_price": price,
        "fill_quantity": float(fill_quantity),
        "fill_session": str(fill_session),
        "fill_minus_stop_dollars_per_share": gap,
        "fill_minus_stop_pct": gap / stop_level,
        "entry_to_fill_return": price / entry_price - 1.0,
    }


def _normalize_position_quantities(value: Any) -> dict[str, float] | None:
    """Normalize broker positions into a symbol -> quantity map."""
    if value is None:
        return None
    if isinstance(value, Mapping):
        quantities: dict[str, float] = {}
        for symbol, quantity in value.items():
            try:
                quantities[str(symbol)] = float(quantity)
            except (TypeError, ValueError):
                continue
        return quantities
    quantities = {}
    for position in value:
        symbol = getattr(getattr(position, "asset", None), "symbol", None)
        quantity = getattr(position, "quantity", None)
        if symbol is None or quantity is None:
            continue
        try:
            quantities[str(symbol)] = float(quantity)
        except (TypeError, ValueError):
            continue
    return quantities


def _parse_state_day(value: Any) -> Any:
    if value is None or isinstance(value, date):
        return value
    try:
        return datetime.fromisoformat(str(value)).date()
    except ValueError:
        return value


def _match_open_order(open_orders: Sequence[Any], symbol: str, *, buy: bool) -> Any:
    """Find an order for ``symbol`` on the requested side, if unambiguous."""
    matches = []
    for order in open_orders or []:
        order_symbol = getattr(getattr(order, "asset", None), "symbol", None)
        if str(order_symbol) != str(symbol):
            continue
        is_buy = getattr(order, "is_buy_order", None)
        if callable(is_buy) and bool(is_buy()) != bool(buy):
            continue
        matches.append(order)
    return matches[0] if len(matches) == 1 else None


class RegistryHtsStrategy(Strategy):
    """The control contract with every catalog mechanism exposed and consumed."""

    def initialize(self) -> None:
        self.set_market("NYSE")
        self.sleeptime = "1H"
        context = _CONTEXT
        if context is None:
            raise RuntimeError("run context was not initialized")
        self._ctx = context
        self._params = dict(context.params)
        self._ordered = context.inputs.ordered_symbols
        self._universe_order = {symbol: index for index, symbol in enumerate(self._ordered)}
        self._sessions = list(context.inputs.sessions)
        self._session_index = {day: index for index, day in enumerate(self._sessions)}
        self._positions: dict[str, dict[str, Any]] = {}
        self._pending_buys: dict[str, dict[str, Any]] = {}
        self._cancelled_pending_buys: dict[str, dict[str, Any]] = {}
        self._pending_sells: set[str] = set()
        self._pending_sell_reason: dict[str, str] = {}
        self._pending_sell_meta: dict[str, dict[str, Any]] = {}
        self._protective_meta: dict[str, dict[str, Any]] = {}
        self._stop_exit_context: dict[str, dict[str, Any]] = {}
        self._stop_gap_events: list[dict[str, Any]] = []
        self._lifecycle_trace: list[dict[str, Any]] = []
        self._protective: dict[str, Order] = {}
        self._cooldowns: dict[str, int] = {}
        self._selected: tuple[str, ...] = ()
        self._ranks: dict[str, int] = {}
        self._signal_day: Any = None
        self._journal: list[dict[str, Any]] = []
        self._rejections: list[dict[str, Any]] = []
        self._diag: list[dict[str, Any]] = []
        # V2 global-gate state is separate from the frozen per-symbol v1
        # market_gate.  A gate observation is evaluated once per completed
        # session and can therefore neither look ahead nor oscillate hourly.
        self._risk_off_active = False
        self._last_risk_gate_session: Any | None = None
        self._re_risk_deadline_index: int | None = None
        self._risk_off_transitions: list[dict[str, Any]] = []
        self._risk_off_race_symbols: set[str] = set()
        self._risk_off_flatten_count = 0
        self._risk_cap_events: list[dict[str, Any]] = []
        self._entry_edge_events: list[dict[str, Any]] = []
        self._minimum_hold_deferrals = 0
        # LumiBot exposes no 15:00 strategy callback for this feed.  A logical
        # 15:00 rebalance is therefore planned at the observable 14:00
        # callback and submitted at the next bar the engine can actually fill.
        self._deferred_rebalance: dict[str, Any] | None = None
        self._deferred_rebalance_events: list[dict[str, Any]] = []
        # Audit/persistence state.  Persistence is fail-open: a checkpoint
        # callback may raise but must never alter the trading path.
        self._event_sequence = 0
        self._decision_sequence = 0
        self._intent_sequence = 0
        self._active_decision_id: str | None = None
        self._last_decision_id: str | None = None
        self._processed_decision_ids: set[str] = set()
        self._order_index: dict[str, dict[str, Any]] = {}
        self._unmatched_broker_orders: dict[str, Any] = {}
        self._protective_state: dict[str, dict[str, Any]] = {}
        self._session_end_events: list[dict[str, Any]] = []
        self._decision_snapshots: list[dict[str, Any]] = []
        self._decision_capture: dict[str, Any] | None = None
        self._submission_scope_decision_id: str | None = None
        self._persistence_callback = None
        self._state_path = None

    # -- audit / persistence --------------------------------------------------
    def _audit_stream(self, name: str) -> list[dict[str, Any]]:
        """Return a strategy stream, lazily creating it when a lightweight
        harness (or a partially-constructed instance) does not define it."""
        stream = getattr(self, name, None)
        if stream is None:
            stream = []
            setattr(self, name, stream)
        return stream

    def _state_dict(self, name: str) -> dict[str, Any]:
        """Return a strategy mapping, lazily creating it for lightweight harnesses."""
        value = getattr(self, name, None)
        if not isinstance(value, dict):
            value = {}
            setattr(self, name, value)
        return value

    def _next_sequence(self, name: str) -> int:
        value = int(getattr(self, name, 0) or 0) + 1
        setattr(self, name, value)
        return value

    def _audit_identity_fields(self) -> dict[str, Any]:
        base = getattr(self, "_audit_identity", None)
        if isinstance(base, Mapping) and base:
            return dict(base)
        return {
            "strategy_name": getattr(self, "_strategy_name", None) or self.__class__.__name__,
            "catalog_id": getattr(self, "_catalog_id", None),
            "implementation_revision": getattr(self, "_implementation_revision", IMPLEMENTATION_REVISION),
            "resolved_parameters_hash": getattr(self, "_resolved_parameters_hash", None),
            "feature_hash": getattr(self, "_feature_hash", None),
        }

    def _current_decision_id(self) -> str:
        active = getattr(self, "_active_decision_id", None)
        if active:
            return str(active)
        name = getattr(self, "_strategy_name", None) or self.__class__.__name__
        try:
            day = self._current_day()
        except Exception:
            day = None
        decision_id = f"{name}:{day}:{self._next_sequence('_decision_sequence')}"
        self._active_decision_id = decision_id
        self._last_decision_id = decision_id
        return decision_id

    def _begin_decision(self, day: Any, hour: int) -> str:
        name = getattr(self, "_strategy_name", None) or self.__class__.__name__
        decision_id = f"{name}:{day}:{hour}:{self._next_sequence('_decision_sequence')}"
        self._active_decision_id = decision_id
        self._last_decision_id = decision_id
        self._decision_capture = {
            "decision_id": decision_id,
            "day": str(day),
            "hour": int(hour),
            "selection": None,
            "allocation": None,
        }
        return decision_id

    def _new_decision_id(self, day: Any | None = None, hour: int | None = None) -> str:
        """Allocate a fresh decision ID for a reactive order outside a rebalance."""
        name = getattr(self, "_strategy_name", None) or self.__class__.__name__
        if day is None:
            try:
                day = self._current_day()
            except Exception:
                day = None
        suffix = f":{hour}" if hour is not None else ""
        decision_id = f"{name}:{day}{suffix}:{self._next_sequence('_decision_sequence')}"
        self._active_decision_id = decision_id
        self._last_decision_id = decision_id
        return decision_id

    def _event_time_iso(self) -> str:
        try:
            return pd.Timestamp(self.get_datetime()).isoformat()
        except Exception:
            return datetime.now(timezone.utc).isoformat()

    def _record_event(self, event: dict[str, Any], *, lifecycle: bool = False) -> dict[str, Any]:
        if lifecycle:
            return self._record_lifecycle_event(event)
        self._audit_stream("_journal").append(event)
        return event

    def _record_lifecycle_event(self, event: Mapping[str, Any]) -> dict[str, Any]:
        """Append one immutable order-lifecycle event with authoritative time/sequence.

        The same object is shared by the journal and the lifecycle trace so a
        fill can never appear in one stream and not the other.
        """
        record = dict(event)
        record.setdefault("event_time", self._event_time_iso())
        record["event_sequence"] = self._next_sequence("_event_sequence")
        self._audit_stream("_journal").append(record)
        self._audit_stream("_lifecycle_trace").append(record)
        return record

    def _order_meta_store(self, order: Any, meta: Mapping[str, Any]) -> None:
        index = getattr(self, "_order_index", None)
        if index is None:
            index = {}
            self._order_index = index
        index[str(getattr(order, "identifier", "") or "")] = dict(meta)

    def _order_meta(self, order: Any) -> dict[str, Any]:
        index = getattr(self, "_order_index", None)
        if not isinstance(index, dict):
            return {}
        return dict(index.get(str(getattr(order, "identifier", "") or ""), {}))

    def _persistence_checkpoint(self, reason: str) -> bool:
        """Run the live persistence callback.  Never propagate a failure."""
        callback = getattr(self, "_persistence_callback", None)
        if callback is None:
            return False
        try:
            callback(reason, self)
        except Exception as error:  # fail-open by design
            self._audit_stream("_diag").append({
                "event": "persistence_failure",
                "reason": str(reason),
                "error": type(error).__name__,
            })
            return False
        return True

    def _restore_persisted_state(self) -> bool:
        """Load, strictly validate, then apply a persisted restart checkpoint."""
        path = getattr(self, "_state_path", None)
        if path is None:
            return False
        try:
            state = AtomicJsonStore(path).read_state(expected_identity=self._audit_identity_fields())
            restored = deserialize_runtime_state(state)
        except Exception as error:
            self._audit_stream("_diag").append({
                "event": "state_restore_failed",
                "error": type(error).__name__,
                "reason": str(error),
            })
            return False
        self._apply_runtime_state(restored)
        return True

    def _apply_runtime_state(self, restored: Mapping[str, Any]) -> None:
        self._event_sequence = int(restored["event_sequence"])
        self._decision_sequence = int(restored["decision_sequence"])
        self._intent_sequence = int(restored["intent_sequence"])
        self._active_decision_id = restored["active_decision_id"]
        self._last_decision_id = restored["last_decision_id"]
        self._processed_decision_ids = set(restored["processed_decision_ids"])
        self._signal_day = restored["signal_day"]
        self._selected = tuple(restored["selected"])
        self._ranks = dict(restored["ranks"])
        self._positions = dict(restored["positions"])
        self._pending_buys = dict(restored["pending_buys"])
        self._pending_sells = set(restored["pending_sells"])
        self._pending_sell_reason = dict(restored["pending_sell_reason"])
        self._pending_sell_meta = dict(restored["pending_sell_meta"])
        self._stop_exit_context = dict(restored["stop_exit_context"])
        self._protective = {}
        self._protective_state = dict(restored["protective_orders"])
        self._protective_meta = {
            str(symbol): dict(meta)
            for symbol, meta in restored["protective_orders"].items()
            if isinstance(meta, Mapping)
        }
        self._cooldowns = dict(restored["cooldowns"])
        risk_state = dict(restored["risk_off_state"])
        self._risk_off_active = bool(restored["risk_off_active"])
        self._re_risk_deadline_index = risk_state["deadline_index"]
        self._last_risk_gate_session = _parse_state_day(risk_state["last_gate_session"])
        self._risk_off_race_symbols = set(restored["risk_off_race_symbols"])
        self._deferred_rebalance = restored["deferred_rebalance"]
        self._last_quote_snapshot = restored["last_quote_snapshot"]

    def _broker_position_quantities(self) -> dict[str, float] | None:
        positions = self.get_positions()
        return _normalize_position_quantities(positions)

    def _reconcile_broker_state_live(
        self,
        *,
        broker_positions: Any = None,
        open_orders: Any = None,
        session_artifact: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Reconcile persisted lineage against broker truth.  Never trades.

        Broker positions and active orders are the only authority for current
        quantity/existence.  Reconciliation never creates, submits, cancels, or
        replaces an order; it only re-points in-memory lineage or drops stale
        persisted state.
        """
        summary: dict[str, Any] = {
            "matched": [],
            "unmatched_persisted": [],
            "unmatched_broker": [],
            "dropped_positions": [],
            "quantity_mismatches": [],
            "legacy_unverifiable": [],
        }
        if broker_positions is None:
            try:
                broker_positions = self._broker_position_quantities()
            except Exception:
                broker_positions = None
        if open_orders is None:
            try:
                open_orders = list(self.get_orders(statuses=Order.ACTIVE_STATUSES))
            except Exception:
                open_orders = []
        open_orders = list(open_orders or [])
        quantities = _normalize_position_quantities(broker_positions)

        # Broker quantity/status is authoritative for held positions.
        if quantities is not None:
            for symbol, state in list(getattr(self, "_positions", {}).items()):
                if symbol not in quantities:
                    self._positions.pop(symbol, None)
                    self._protective.pop(symbol, None)
                    self._state_dict("_protective_meta").pop(symbol, None)
                    self._protective_state.pop(symbol, None)
                    self._state_dict("_pending_sell_meta").pop(symbol, None)
                    self._pending_sells.discard(symbol)
                    self._pending_sell_reason.pop(symbol, None)
                    summary["unmatched_persisted"].append(symbol)
                    summary["dropped_positions"].append(symbol)
                    self._audit_stream("_diag").append({
                        "event": "reconcile_position_dropped",
                        "symbol": symbol,
                        "persisted_quantity": state.get("quantity"),
                    })
                elif finite(state.get("quantity")) and not math.isclose(
                    float(state["quantity"]), float(quantities[symbol]), rel_tol=1e-9, abs_tol=1e-9
                ):
                    summary["quantity_mismatches"].append({
                        "symbol": symbol,
                        "persisted": state.get("quantity"),
                        "broker": float(quantities[symbol]),
                    })
                    self._audit_stream("_diag").append({
                        "event": "reconcile_quantity_replaced_by_broker",
                        "symbol": symbol,
                        "persisted_quantity": state.get("quantity"),
                        "broker_quantity": float(quantities[symbol]),
                    })
                    state["quantity"] = float(quantities[symbol])
            for symbol, quantity in quantities.items():
                if symbol not in getattr(self, "_positions", {}):
                    # Broker-only position: record only broker-visible facts.
                    self._positions[symbol] = {
                        "quantity": float(quantity),
                        "legacy_unverifiable": True,
                    }
                    summary["legacy_unverifiable"].append(symbol)
                    self._audit_stream("_diag").append({
                        "event": "reconcile_broker_only_position",
                        "symbol": symbol,
                        "broker_quantity": float(quantity),
                    })

        by_id = {str(getattr(order, "identifier", "") or ""): order for order in open_orders}

        # Lineage index recovered from the session artifact for unmatched orders.
        lineage_by_id: dict[str, dict[str, Any]] = {}
        if isinstance(session_artifact, Mapping):
            for event in session_artifact.get("lifecycle_trace") or []:
                if not isinstance(event, Mapping):
                    continue
                if event.get("event") not in (
                    "intent_created", "order_submitted", "protective_order_submitted",
                ):
                    continue
                lineage = {
                    "intent_id": event.get("intent_id"),
                    "decision_id": event.get("decision_id"),
                    "symbol": event.get("symbol"),
                    "side": event.get("side"),
                }
                for identifier in (event.get("broker_order_id"), event.get("local_order_id")):
                    if identifier:
                        lineage_by_id[str(identifier)] = dict(lineage)

        matched_ids: set[str] = set()

        def _rebind_by_id(entry: Any) -> Any:
            target_id = entry.get("broker_order_id") or entry.get("local_order_id")
            if not target_id:
                return None
            return by_id.get(str(target_id))

        def _artifact_supported_fallback(
            symbol: str, *, buy: bool, entry: Mapping[str, Any]
        ) -> Any:
            """Use symbol/side fallback only with matching persisted artifact lineage."""
            candidate = _match_open_order(open_orders, symbol, buy=buy)
            if candidate is None:
                return None
            identifier = str(getattr(candidate, "identifier", "") or "")
            lineage = lineage_by_id.get(identifier)
            if not lineage:
                return None
            expected_side = "buy" if buy else "sell"
            if lineage.get("symbol") != symbol or lineage.get("side") != expected_side:
                return None
            for key in ("intent_id", "decision_id"):
                if entry.get(key) and lineage.get(key) != entry.get(key):
                    return None
            return candidate

        for symbol, pending in list(getattr(self, "_pending_buys", {}).items()):
            order = _rebind_by_id(pending)
            if order is None:
                order = _artifact_supported_fallback(symbol, buy=True, entry=pending)
            if order is not None:
                pending["order"] = order
                pending["broker_order_id"] = str(getattr(order, "identifier", "") or "") or pending.get("broker_order_id")
                matched_ids.add(str(getattr(order, "identifier", "") or ""))
                summary["matched"].append(symbol)
            else:
                summary["unmatched_persisted"].append(symbol)
                # Unresolved intent: drop it from claimed-active state and never
                # automatically resubmit its decision.
                decision_id = pending.get("decision_id")
                if decision_id:
                    processed = getattr(self, "_processed_decision_ids", None)
                    if processed is None:
                        processed = set()
                        self._processed_decision_ids = processed
                    processed.add(str(decision_id))
                self._audit_stream("_diag").append({
                    "event": "reconcile_pending_buy_without_open_order",
                    "symbol": symbol,
                    "broker_order_id": pending.get("broker_order_id"),
                })
                self._pending_buys.pop(symbol, None)
                self._cancelled_pending_buys.pop(symbol, None)

        for symbol, meta in list(getattr(self, "_pending_sell_meta", {}).items()):
            order = _rebind_by_id(meta)
            if order is None:
                order = _artifact_supported_fallback(symbol, buy=False, entry=meta)
            if order is not None:
                meta["order"] = order
                meta["broker_order_id"] = str(getattr(order, "identifier", "") or "") or meta.get("broker_order_id")
                matched_ids.add(str(getattr(order, "identifier", "") or ""))
                summary["matched"].append(symbol)
            else:
                summary["unmatched_persisted"].append(symbol)
                self._audit_stream("_diag").append({
                    "event": "reconcile_pending_sell_without_open_order",
                    "symbol": symbol,
                    "broker_order_id": meta.get("broker_order_id"),
                })
                decision_id = meta.get("decision_id")
                if decision_id:
                    self._processed_decision_ids.add(str(decision_id))
                self._pending_sell_meta.pop(symbol, None)
                self._pending_sells.discard(symbol)
                self._pending_sell_reason.pop(symbol, None)
                self._stop_exit_context.pop(symbol, None)

        protective_meta = getattr(self, "_protective_meta", {}) or {}
        if not protective_meta:
            protective_meta = getattr(self, "_protective_state", {}) or {}
        for symbol, meta in list(protective_meta.items()):
            if not isinstance(meta, Mapping):
                continue
            order = _rebind_by_id(meta)
            if order is None:
                order = _artifact_supported_fallback(symbol, buy=False, entry=meta)
            if order is not None:
                self._protective[symbol] = order
                merged = dict(meta)
                merged["broker_order_id"] = str(getattr(order, "identifier", "") or "") or meta.get("broker_order_id")
                self._state_dict("_protective_meta")[symbol] = merged
                matched_ids.add(str(getattr(order, "identifier", "") or ""))
                summary["matched"].append(symbol)
            else:
                summary["unmatched_persisted"].append(symbol)
                self._audit_stream("_diag").append({
                    "event": "reconcile_protective_order_missing",
                    "symbol": symbol,
                    "broker_order_id": meta.get("broker_order_id"),
                })
                decision_id = meta.get("decision_id")
                if decision_id:
                    self._processed_decision_ids.add(str(decision_id))
                self._protective.pop(symbol, None)
                self._state_dict("_protective_meta").pop(symbol, None)
                self._protective_state.pop(symbol, None)
                self._stop_gap_events.append({
                    "event": "reconcile_protective_order_unresolved",
                    "symbol": symbol,
                    "broker_order_id": meta.get("broker_order_id"),
                })

        unmatched_broker_orders: dict[str, Any] = {}
        for order in open_orders:
            identifier = str(getattr(order, "identifier", "") or "")
            if not identifier or identifier in matched_ids:
                continue
            lineage = lineage_by_id.get(identifier)
            if lineage and lineage.get("intent_id"):
                # Keep the symbol occupied but do not claim strategy ownership
                # without an unambiguous artifact match.
                summary["unmatched_broker"].append(identifier)
            else:
                summary["unmatched_broker"].append(identifier)
            self._audit_stream("_diag").append({
                "event": "reconcile_broker_order_without_persisted_lineage",
                "broker_order_id": identifier,
                "recovered_intent_id": (lineage or {}).get("intent_id"),
            })
            symbol = str(getattr(getattr(order, "asset", None), "symbol", "") or "")
            if symbol:
                unmatched_broker_orders[symbol] = order
        self._unmatched_broker_orders = unmatched_broker_orders

        # Rebuild the order index only after broker orders are rebound.
        index: dict[str, dict[str, Any]] = {}
        for symbol, pending in getattr(self, "_pending_buys", {}).items():
            order = pending.get("order")
            if order is not None:
                index[str(getattr(order, "identifier", "") or "")] = {
                    "intent_id": pending.get("intent_id"),
                    "decision_id": pending.get("decision_id"),
                    "symbol": symbol,
                    "side": "buy",
                }
        for symbol, meta in getattr(self, "_pending_sell_meta", {}).items():
            order = meta.get("order")
            if order is not None:
                index[str(getattr(order, "identifier", "") or "")] = {
                    "intent_id": meta.get("intent_id"),
                    "decision_id": meta.get("decision_id"),
                    "symbol": symbol,
                    "side": "sell",
                }
        for symbol, order in getattr(self, "_protective", {}).items():
            meta = getattr(self, "_protective_meta", {}).get(symbol, {})
            index[str(getattr(order, "identifier", "") or "")] = {
                "intent_id": meta.get("intent_id"),
                "decision_id": meta.get("decision_id"),
                "symbol": symbol,
                "side": "sell",
            }
        self._order_index = index

        self._persistence_checkpoint("startup_reconciled")
        return summary

    def _session_end_snapshot(self) -> dict[str, Any]:
        try:
            day = str(self._current_day())
        except Exception:
            day = None
        return {
            "event": "session_end",
            "session": day,
            "equity": float(getattr(self, "_portfolio_value", 0.0) or 0.0),
            "cash": float(getattr(self, "_cash", 0.0) or 0.0),
            "complete": True,
        }

    def after_market_closes(self) -> dict[str, Any]:
        snapshot = self._session_end_snapshot()
        self._audit_stream("_session_end_events").append(snapshot)
        self._persistence_checkpoint("after_market_closes")
        return snapshot

    def on_strategy_end(self) -> None:
        self._persistence_checkpoint("on_strategy_end")

    def on_abrupt_closing(self) -> None:
        self._persistence_checkpoint("on_abrupt_closing")

    # -- data helpers ---------------------------------------------------------
    @staticmethod
    def _frame_row(frame: pd.DataFrame | None, session: Any) -> pd.Series | None:
        if frame is None:
            return None
        try:
            row = frame.loc[pd.Timestamp(session)]
        except KeyError:
            return None
        return row if isinstance(row, pd.Series) else row.iloc[-1]

    def _daily_row(self, symbol: str, session: Any) -> pd.Series | None:
        return self._frame_row(self._ctx.inputs.daily.get(symbol), session)

    def _benchmark_row(self, symbol: str, session: Any) -> pd.Series | None:
        return self._frame_row(self._ctx.inputs.benchmark.get(symbol), session)

    def _stamp(self, day: Any, hour: int) -> pd.Timestamp:
        return pd.Timestamp(day) + pd.Timedelta(hours=hour)

    def _completed_row(self, symbol: str, stamp: pd.Timestamp) -> pd.Series | None:
        """The last bar that has fully completed at ``stamp``.

        LumiBot starts bar T at time T, so the bar at T is not yet complete when
        the strategy is called.  Decisions therefore use the bar before T, and the
        order that follows fills at T's open.
        """
        frame = self._ctx.inputs.hourly.get(symbol)
        if frame is None or frame.empty:
            return None
        position = bisect.bisect_left(frame.index, stamp) - 1
        if position < 0:
            return None
        return frame.iloc[position]

    def _execution_price(self, symbol: str, stamp: pd.Timestamp) -> float | None:
        """The price a market order submitted now would fill at: this bar's open."""
        frame = self._ctx.inputs.hourly.get(symbol)
        if frame is None or frame.empty:
            return None
        position = bisect.bisect_left(frame.index, stamp)
        if position >= len(frame.index) or frame.index[position] != stamp:
            return None
        price = float(frame.iloc[position]["open"])
        return price if math.isfinite(price) and price > 0.0 else None

    def _previous_session(self, day: Any) -> Any | None:
        index = bisect.bisect_left(self._sessions, day) - 1
        return self._sessions[index] if index >= 0 else None

    def _schedule_due(self, day: Any) -> bool:
        return schedule_due(
            str(self._params["rebalance_schedule"]),
            day,
            session_index=self._session_index.get(day),
        )

    def _effective_rebalance_iteration_hour(self) -> int:
        """Return the observable native callback for this configured rebalance."""
        return effective_rebalance_iteration_hour(int(self._params["rebalance_hour"]))

    def _correlation(self, first: str, second: str, session: Any) -> float:
        frame_a = self._ctx.inputs.daily.get(first)
        frame_b = self._ctx.inputs.daily.get(second)
        if frame_a is None or frame_b is None:
            return float("nan")
        boundary = pd.Timestamp(session)
        return sample_correlation(frame_a["ret1"].loc[:boundary], frame_b["ret1"].loc[:boundary], 60)

    def _market_gate(self, session: Any) -> bool:
        gate = str(self._params["market_gate"])
        if gate == "none":
            return True
        benchmark_rows = {
            symbol: self._benchmark_row(symbol, session) for symbol in BENCHMARK_SYMBOLS
        }
        breadth_rows = [self._frame_row(frame, session) for frame in self._ctx.inputs.breadth]
        return market_gate_open(
            gate,
            benchmark_rows=benchmark_rows,
            breadth_rows=[row for row in breadth_rows if row is not None],
            breadth_sma=int(self._params["breadth_sma"]),
            breadth_threshold=self._params["breadth_basket_threshold"],
        )

    def _risk_off_gate(self, session: Any) -> bool:
        """Evaluate the v2 portfolio-wide gate on one completed session."""
        gate = str(self._params.get("risk_off_gate", "none"))
        if gate == "none":
            return True
        benchmark_rows = {
            symbol: self._benchmark_row(symbol, session) for symbol in BENCHMARK_SYMBOLS
        }
        breadth_rows = [self._frame_row(frame, session) for frame in self._ctx.inputs.breadth]
        return risk_off_gate_open(
            gate,
            benchmark_rows=benchmark_rows,
            breadth_rows=breadth_rows,
            breadth_expected_count=int(getattr(self._ctx.inputs, "breadth_expected_count", len(BREADTH_BASKET))),
        )

    def _refresh_risk_off_state(self, day: Any) -> bool:
        """Refresh v2 gate state once, returning whether global risk is off."""
        session = self._previous_session(day)
        if session is None:
            return bool(getattr(self, "_risk_off_active", False))
        if session == getattr(self, "_last_risk_gate_session", None):
            return bool(getattr(self, "_risk_off_active", False))
        self._last_risk_gate_session = session
        session_index = self._session_index.get(session)
        gate = str(self._params.get("risk_off_gate", "none"))
        gate_open = self._risk_off_gate(session)
        was_active = bool(getattr(self, "_risk_off_active", False))
        if not gate_open:
            cooldown = int(self._params.get("risk_off_cooldown_bars", 0))
            # The closed observation itself is not one of the following N
            # complete sessions.  N=0 therefore permits re-risk at the next
            # open observation; N=3 waits three complete sessions longer.
            self._re_risk_deadline_index = (session_index + cooldown + 1) if session_index is not None else None
            self._risk_off_active = True
            reason = "gate_closed"
        else:
            deadline = getattr(self, "_re_risk_deadline_index", None)
            self._risk_off_active = bool(deadline is not None and session_index is not None and session_index < deadline)
            reason = "cooldown" if self._risk_off_active else "gate_open"
        if was_active != self._risk_off_active:
            event = {
                "session": str(session),
                "gate": gate,
                "gate_open": gate_open,
                "risk_off": self._risk_off_active,
                "reason": reason,
                "re_risk_deadline_index": self._re_risk_deadline_index,
            }
            self._risk_off_transitions.append(event)
            self._journal.append({"event": "risk_off_transition", **event})
        return bool(self._risk_off_active)

    # -- selection ------------------------------------------------------------
    def _select(self, day: Any) -> tuple[str, ...]:
        session = self._previous_session(day)
        if session is None:
            return ()
        if bool(getattr(self, "_risk_off_active", False)):
            return ()
        if not self._market_gate(session):
            return ()
        session_index = self._session_index[session]
        min_mdv = float(self._params["min_median_dollar_volume"])
        require_positive = bool(self._params["require_positive_return"])
        rows: dict[str, pd.Series] = {}
        for symbol in self._ordered:
            cooldown_until = self._cooldowns.get(symbol)
            if cooldown_until is not None and session_index < cooldown_until:
                continue
            row = self._daily_row(symbol, session)
            if row is None:
                continue
            if not all(finite(row.get(column)) for column in ("close", "sma", "ret", "mdv")):
                continue
            if not float(row["close"]) > float(row["sma"]):
                continue
            if float(row["mdv"]) < min_mdv:
                continue
            if require_positive and float(row["ret"]) <= 0.0:
                continue
            rows[symbol] = row
        scores = rank_scores(str(self._params["rank_score"]), rows)
        ranked = sorted(scores.items(), key=lambda item: (-item[1], self._universe_order[item[0]]))
        correlation_cap = self._params["correlation_screen"]
        exposure_limit = self._params["exposure_group_limit"]
        minimum_hold = int(self._params.get("min_position_holding_bars", 0))
        mandatory_held: list[str] = []
        if minimum_hold > 0:
            for symbol, state in self._positions.items():
                entry_session = state.get("entry_session")
                entry_index = self._session_index.get(entry_session)
                if entry_index is None:
                    continue
                # Selection reads the prior completed session.  Once N complete
                # sessions have elapsed after entry, the position is replaceable
                # on that exact expiry decision; ordinary exits before then are
                # deferred, while stops and global risk-off remain immediate.
                if session_index - entry_index < minimum_hold:
                    mandatory_held.append(symbol)
            self._minimum_hold_deferrals += len(mandatory_held)
        selection = select_holdings(
            ranked=ranked,
            held=tuple(self._positions),
            top_n=int(self._params["top_n"]),
            rank_buffer=self._params["rank_buffer"],
            exposure_limit=exposure_limit,
            exposure_of=exposure_group if exposure_limit is not None else None,
            correlation_of=((lambda a, b: self._correlation(a, b, session))
                            if correlation_cap is not None else None),
            correlation_cap=correlation_cap,
            mandatory_held=tuple(mandatory_held),
        )
        self._ranks = dict(selection.ranks)
        try:
            self._record_selection_event(day, session, rows, scores, selection, mandatory_held)
        except Exception:
            pass
        return selection.holdings

    def _record_selection_event(self, day: Any, session: Any, rows: Mapping[str, pd.Series],
                                scores: Mapping[str, float], selection: Any,
                                mandatory_held: Sequence[str] = ()) -> None:
        rank_inputs: dict[str, Any] = {}
        for symbol, row in rows.items():
            values: dict[str, Any] = {}
            for column in ("ret", "close", "sma", "mdv", "vol20"):
                value = row.get(column)
                values[column] = float(value) if finite(value) else None
            rank_inputs[symbol] = values
        ranked_symbols = [symbol for symbol, _score in sorted(
            scores.items(), key=lambda item: (-item[1], self._universe_order.get(item[0], 0))
        )]
        correlation_cap = self._params.get("correlation_screen")
        pairwise: dict[str, float] = {}
        if correlation_cap is not None:
            for index, first in enumerate(ranked_symbols):
                for second in ranked_symbols[index + 1:]:
                    value = self._correlation(first, second, session)
                    if finite(value):
                        pairwise[f"{first}|{second}"] = float(value)
        exposure_limit = self._params.get("exposure_group_limit")
        exposure_groups = (
            {symbol: exposure_group(symbol) for symbol in rows}
            if exposure_limit is not None else {}
        )
        event = {
            "event": "selection",
            "decision_id": self._current_decision_id(),
            "day": str(day),
            "signal_session": str(session),
            "rank_mode": str(self._params.get("rank_score")),
            "rank_input_rows": rank_inputs,
            "rank_scores": {symbol: float(value) for symbol, value in scores.items()},
            "ordinal_ranks": dict(selection.ranks),
            "ranked_symbols": ranked_symbols,
            "held_symbols": sorted(self._positions),
            "mandatory_held_symbols": list(mandatory_held),
            "exposure_groups": exposure_groups,
            "pairwise_correlations": pairwise,
            "selected_symbols": list(selection.holdings),
        }
        maybe_capture = getattr(self, "_decision_capture", None)
        if isinstance(maybe_capture, dict):
            maybe_capture["selection"] = event
        self._record_event(dict(event))

    # -- orders ---------------------------------------------------------------
    def _cancel_protective(self, symbol: str) -> None:
        order = self._protective.pop(symbol, None)
        self._state_dict("_protective_meta").pop(symbol, None)
        if order is None:
            return
        try:
            filled = getattr(order, "is_filled", None)
            if not (order.is_canceled() or (callable(filled) and filled())):
                self.cancel_order(order)
                self._journal.append({"event": "protective_cancel", "symbol": symbol})
        except Exception:
            pass

    def _resolve_decision_id(self, explicit: str | None) -> str:
        """Resolve the decision that owns a new order without reusing stale IDs."""
        if explicit:
            return str(explicit)
        scope = getattr(self, "_submission_scope_decision_id", None)
        if scope:
            return str(scope)
        active = getattr(self, "_active_decision_id", None)
        processed = getattr(self, "_processed_decision_ids", None) or set()
        if active and str(active) not in processed:
            return str(active)
        return self._new_decision_id()

    def _decision_submission_suppressed(self, decision_id: str) -> bool:
        processed = getattr(self, "_processed_decision_ids", None) or set()
        scope = getattr(self, "_submission_scope_decision_id", None)
        return str(decision_id) in processed and str(decision_id) != str(scope)

    def _has_decision_snapshot(self, decision_id: str) -> bool:
        return any(
            str(snapshot.get("decision_id") or "") == str(decision_id)
            for snapshot in self._audit_stream("_decision_snapshots")
            if isinstance(snapshot, Mapping)
        )

    def _ensure_direct_order_snapshot(
        self,
        *,
        decision_id: str,
        symbol: str,
        side: str,
        quantity: float,
        reference: float | None,
        intent_id: str,
        reason: str,
    ) -> None:
        """Commit evidence for a reactive/direct order before broker contact."""
        if self._has_decision_snapshot(decision_id):
            return
        try:
            now = pd.Timestamp(self.get_datetime())
        except Exception:
            # Lightweight offline harnesses may not initialize a broker clock.
            # Audit construction stays fail-open and must not alter order flow.
            now = pd.Timestamp(datetime.now(timezone.utc))
        day = now.date()
        hour = int(now.hour)
        intended_notional = None
        if reference is not None and finite(reference):
            intended_notional = float(quantity) * float(reference)
        self._emit_decision_snapshot(
            day,
            hour,
            decision_id,
            "reactive_order",
            reason,
            [{
                "symbol": symbol,
                "side": side,
                "requested_quantity": float(quantity),
                "reference_price": reference,
                "intended_notional": intended_notional,
                "budget_before": None,
                "budget_after": None,
                "intent_id": intent_id,
                "reason": reason,
            }],
        )
        processed = getattr(self, "_processed_decision_ids", None)
        if processed is None:
            processed = set()
            self._processed_decision_ids = processed
        processed.add(str(decision_id))
        self._persistence_checkpoint("decision_committed")

    def _submit_sell(
        self,
        symbol: str,
        reason: str,
        reference: float,
        *,
        stop_context: Mapping[str, Any] | None = None,
        decision_id: str | None = None,
        intent_id: str | None = None,
    ) -> str | None:
        state = self._positions.get(symbol)
        if state is None or symbol in self._pending_sells:
            return None
        resolved_decision = self._resolve_decision_id(decision_id)
        if self._decision_submission_suppressed(resolved_decision):
            self._audit_stream("_diag").append({
                "event": "duplicate_decision_suppressed",
                "decision_id": resolved_decision,
                "symbol": symbol,
                "side": "sell",
            })
            return None
        resolved_intent = intent_id or f"sell-{symbol}-{self._next_sequence('_intent_sequence')}"
        self._ensure_direct_order_snapshot(
            decision_id=resolved_decision,
            symbol=symbol,
            side="sell",
            quantity=float(state["quantity"]),
            reference=reference,
            intent_id=resolved_intent,
            reason=reason,
        )
        self._cancel_protective(symbol)
        order = self.create_order(symbol, quantity=state["quantity"], side="sell", order_type="market")
        self._pending_sells.add(symbol)
        self._pending_sell_reason[symbol] = reason
        local_order_id = str(getattr(order, "identifier", "") or "")
        meta = {
            "intent_id": resolved_intent,
            "decision_id": resolved_decision,
            "symbol": symbol,
            "side": "sell",
            "local_order_id": local_order_id,
            "broker_order_id": None,
            "requested_quantity": float(state["quantity"]),
            "reason": reason,
            "cumulative_filled_quantity": 0.0,
            "last_fill_event_quantity": 0.0,
            "order": order,
        }
        self._state_dict("_pending_sell_meta")[symbol] = meta
        self._order_meta_store(order, {
            "intent_id": resolved_intent,
            "decision_id": resolved_decision,
            "symbol": symbol,
            "side": "sell",
        })
        self._record_lifecycle_event({
            "event": "intent_created",
            "symbol": symbol,
            "side": "sell",
            "intent_id": resolved_intent,
            "decision_id": resolved_decision,
            "local_order_id": local_order_id,
            "broker_order_id": None,
            "requested_quantity": float(state["quantity"]),
            "reference_price": float(reference) if reference is not None else None,
            "reason": reason,
        })
        if stop_context is not None:
            context = dict(stop_context)
            context["order_id"] = str(order.identifier)
            self._stop_exit_context[symbol] = context
            self._record_lifecycle_event({
                "event": "virtual_stop_submitted",
                "symbol": symbol,
                "side": "sell",
                "intent_id": resolved_intent,
                "decision_id": resolved_decision,
                "order_id": context["order_id"],
                "broker_order_id": context["order_id"],
                "engine_time": context["engine_time"],
                "completed_source_bar": context["trigger_timestamp"],
                "submission_time": context["submission_time"],
                "source_fill_bar": context["source_fill_bar"],
            })
        self._persistence_checkpoint("intent_created")
        try:
            self.submit_order(order)
        except Exception as error:
            self.on_error_order(order, error)
            raise
        self._persistence_checkpoint("order_submitted")
        self._journal.append({"event": "intent", "side": "sell", "symbol": symbol,
                              "reason": reason, "reference": reference})
        return resolved_intent

    def _submit_buy(
        self,
        symbol: str,
        quantity: float,
        reference: float,
        atr: float,
        reason: str,
        *,
        decision_id: str | None = None,
        intent_id: str | None = None,
    ) -> str | None:
        if symbol in self._pending_buys:
            return None
        resolved_decision = self._resolve_decision_id(decision_id)
        if self._decision_submission_suppressed(resolved_decision):
            self._audit_stream("_diag").append({
                "event": "duplicate_decision_suppressed",
                "decision_id": resolved_decision,
                "symbol": symbol,
            })
            return None
        resolved_intent = intent_id or f"buy-{symbol}-{self._next_sequence('_intent_sequence')}"
        self._ensure_direct_order_snapshot(
            decision_id=resolved_decision,
            symbol=symbol,
            side="buy",
            quantity=float(quantity),
            reference=reference,
            intent_id=resolved_intent,
            reason=reason,
        )
        order = self.create_order(symbol, quantity=quantity, side="buy", order_type="market")
        local_order_id = str(getattr(order, "identifier", "") or "")
        self._pending_buys[symbol] = {
            "quantity": quantity,
            "atr": atr,
            "order": order,
            "requested_quantity": float(quantity),
            "event_time_atr": float(atr),
            "intent_id": resolved_intent,
            "decision_id": resolved_decision,
            "local_order_id": local_order_id,
            "broker_order_id": None,
            "cumulative_filled_quantity": 0.0,
            "last_fill_event_quantity": 0.0,
            "reason": reason,
        }
        self._order_meta_store(order, {
            "intent_id": resolved_intent,
            "decision_id": resolved_decision,
            "symbol": symbol,
            "side": "buy",
        })
        self._record_lifecycle_event({
            "event": "intent_created",
            "symbol": symbol,
            "side": "buy",
            "intent_id": resolved_intent,
            "decision_id": resolved_decision,
            "local_order_id": local_order_id,
            "broker_order_id": None,
            "requested_quantity": float(quantity),
            "reference_price": float(reference),
            "event_time_atr": float(atr),
            "reason": reason,
        })
        self._persistence_checkpoint("intent_created")
        try:
            self.submit_order(order)
        except Exception as error:
            self.on_error_order(order, error)
            raise
        self._persistence_checkpoint("order_submitted")
        self._journal.append({"event": "intent", "side": "buy", "symbol": symbol,
                              "quantity": quantity, "reason": reason, "reference": reference})
        return resolved_intent

    def _cancel_pending_buys(self, *, reason: str) -> int:
        """Send explicit cancels for every pending buy before a global flatten."""
        cancelled = 0
        for symbol, pending in list(self._pending_buys.items()):
            order = pending.get("order")
            if order is not None:
                try:
                    # A local CANCELLING-like state must never suppress the
                    # explicit broker cancel request.
                    self.cancel_order(order)
                except Exception:
                    pass
            self._pending_buys.pop(symbol, None)
            # Preserve entry ATR until the broker definitively confirms the
            # cancel.  A fill racing that cancellation must establish a valid
            # protective/risk-off liquidation state, never lose its inputs.
            self._cancelled_pending_buys[symbol] = pending
            self._journal.append({"event": "pending_buy_cancel", "symbol": symbol, "reason": reason})
            cancelled += 1
        return cancelled

    def _flatten_risk_off(self, day: Any, hour: int, *, race_only: bool = False) -> None:
        """Cancel buys and flatten all (or raced) positions for a closed v2 gate."""
        stamp = self._stamp(day, hour)
        cancelled = self._cancel_pending_buys(reason="global_risk_off")
        symbols = set(self._risk_off_race_symbols) if race_only else set(self._positions)
        submitted = 0
        for symbol in sorted(symbols):
            state = self._positions.get(symbol)
            if state is None:
                continue
            price = self._execution_price(symbol, stamp)
            self._submit_sell(symbol, "global_risk_off", price if price is not None else float("nan"))
            if symbol in self._pending_sells:
                submitted += 1
            self._risk_off_race_symbols.discard(symbol)
        if cancelled or submitted:
            self._risk_off_flatten_count += submitted
            self._journal.append({
                "event": "global_risk_off_flatten",
                "day": str(day),
                "hour": hour,
                "cancelled_buys": cancelled,
                "submitted_sells": submitted,
                "race_only": race_only,
            })

    def _place_protective_stop(self, symbol: str) -> None:
        mode = str(self._params["exit_mode"])
        state = self._positions.get(symbol)
        if state is None:
            return
        if mode in ("resting-stop-2atr", "resting-stop-atr"):
            level = float(state["entry_price"]) - float(self._params["atr_k"]) * float(state["entry_atr"])
        elif mode == "virtual-trail-plus-emergency-4atr":
            level = float(state["entry_price"]) - 4.0 * float(state["entry_atr"])
        else:
            return
        if not finite(level) or float(level) <= 0.0:
            return
        order = self.create_order(symbol, quantity=state["quantity"], side="sell", stop_price=float(level))
        local_order_id = str(getattr(order, "identifier", "") or "")
        parent_intent = state.get("entry_intent_id")
        decision_id = state.get("entry_decision_id") or self._current_decision_id()
        protective_meta = {
            "intent_id": f"prot-{symbol}-{self._next_sequence('_intent_sequence')}",
            "decision_id": decision_id,
            "symbol": symbol,
            "side": "sell",
            "parent_entry_intent_id": parent_intent,
            "parent_decision_id": decision_id,
            "broker_order_id": None,
            "local_order_id": local_order_id,
            "quantity": float(getattr(order, "quantity", 0.0) or 0.0),
            "level": float(level),
        }
        # Make the callback/rejection lineage authoritative before broker
        # contact.  A synchronous callback must never observe an unindexed stop.
        self._protective[symbol] = order
        state["protective_level"] = float(level)
        self._state_dict("_protective_meta")[symbol] = protective_meta
        self._order_meta_store(order, {
            "intent_id": protective_meta["intent_id"],
            "decision_id": decision_id,
            "symbol": symbol,
            "side": "sell",
        })
        self._record_lifecycle_event({
            "event": "protective_order_submitted",
            "symbol": symbol,
            "side": "sell",
            "order_id": local_order_id,
            "broker_order_id": local_order_id,
            "intent_id": protective_meta["intent_id"],
            "quantity": float(getattr(order, "quantity", 0.0) or 0.0),
            "level": float(level),
            "parent_entry_intent_id": parent_intent,
            "decision_id": decision_id,
        })
        self._persistence_checkpoint("protective_intent_created")
        try:
            self.submit_order(order)
        except Exception as error:
            self.on_error_order(order, error)
            raise
        self._persistence_checkpoint("protective_order")
        self._journal.append({"event": "protective_order", "symbol": symbol,
                              "level": float(level), "mode": mode})

    # -- risk -----------------------------------------------------------------
    def _ratchet(self, state: dict[str, Any], close: float, atr: float, atr_k: float) -> None:
        candidate = close - atr_k * atr
        if candidate > float(state["stop"]):
            state["stop"] = candidate

    def _submit_virtual_stop(
        self,
        symbol: str,
        *,
        mode: str,
        reason: str,
        row: pd.Series,
        stamp: pd.Timestamp,
        engine_time: pd.Timestamp,
    ) -> bool:
        """Latch a virtual stop only when the current source fill bar exists."""
        state = self._positions.get(symbol)
        if state is None or symbol in self._pending_sells:
            return symbol in self._pending_sells
        execution_price = self._execution_price(symbol, stamp)
        trigger_stamp = pd.Timestamp(row.name)
        if execution_price is None:
            self._record_event({
                "event": "virtual_stop_deferred_no_executable_bar",
                "symbol": symbol,
                "engine_time": pd.Timestamp(engine_time).isoformat(),
                "completed_source_bar": trigger_stamp.isoformat(),
                "submission_time": None,
                "fill_time": None,
                "source_fill_bar": None,
            })
            return False
        context = {
            "symbol": symbol,
            "mode": mode,
            "reason": reason,
            "entry_price": float(state["entry_price"]),
            "stop_level": float(state["stop"]),
            "trigger_timestamp": trigger_stamp.isoformat(),
            "trigger_close": float(row["close"]),
            "trigger_session": str(trigger_stamp.date()),
            "overnight_gap_exposed": bool(trigger_stamp.hour == 15),
            "engine_time": pd.Timestamp(engine_time).isoformat(),
            "submission_time": pd.Timestamp(engine_time).isoformat(),
            "source_fill_bar": pd.Timestamp(stamp).isoformat(),
        }
        self._submit_sell(symbol, reason, float(row["close"]), stop_context=context)
        return symbol in self._pending_sells

    def _update_risk(self, day: Any, hour: int, *, engine_time: pd.Timestamp | None = None) -> None:
        mode = str(self._params["exit_mode"])
        atr_k = float(self._params["atr_k"])
        stamp = self._stamp(day, hour)
        engine_time = pd.Timestamp(engine_time if engine_time is not None else stamp)
        time_exit = self._params["time_exit_sessions"]
        for symbol, state in list(self._positions.items()):
            if symbol in self._pending_sells:
                continue
            row = self._completed_row(symbol, stamp)
            if row is None:
                continue
            close = float(row["close"])
            atr = float(row["atr"])
            if not finite(atr) or atr <= 0.0:
                continue
            high = float(row["high"])
            state["highest_high"] = max(float(state.get("highest_high", state["entry_price"])), high)
            state.setdefault("post_entry_highs", []).append(high)
            if time_exit is not None:
                held = self._session_index.get(day, 0) - self._session_index.get(state["entry_session"], 0)
                if held >= int(time_exit):
                    if self._execution_price(symbol, stamp) is not None:
                        self._submit_sell(symbol, "time_exit", close)
                    continue
            if mode in ("resting-stop-2atr", "resting-stop-atr"):
                # The resting broker order owns this exit; there is no virtual trail.
                continue
            seeded = bool(state.get("seeded"))
            stop = float(state["stop"]) if seeded else float("nan")
            if mode == "confirm-two-closes":
                if seeded and close <= stop:
                    state["breach_count"] = int(state.get("breach_count", 0)) + 1
                    if state["breach_count"] >= 2:
                        if self._submit_virtual_stop(
                            symbol, mode=mode, reason="stop_confirm", row=row,
                            stamp=stamp, engine_time=engine_time,
                        ):
                            continue
                else:
                    state["breach_count"] = 0
                self._ratchet(state, close, atr, atr_k)
                continue
            if mode in ("breach-buffer-0.25-atr", "breach-buffer-0.50-atr"):
                buffer = 0.25 if "0.25" in mode else 0.50
                if seeded and close <= stop - buffer * atr:
                    if self._submit_virtual_stop(
                        symbol, mode=mode, reason="stop_buffer", row=row,
                        stamp=stamp, engine_time=engine_time,
                    ):
                        continue
                self._ratchet(state, close, atr, atr_k)
                continue
            if mode == "chandelier-since-entry":
                if seeded and close <= stop:
                    if self._submit_virtual_stop(
                        symbol, mode=mode, reason="chandelier", row=row,
                        stamp=stamp, engine_time=engine_time,
                    ):
                        continue
                candidate = float(state["highest_high"]) - 3.0 * atr
                state["stop"] = candidate if not seeded else max(stop, candidate)
                state["seeded"] = True
                continue
            if mode == "chandelier-14-bar":
                highs = state.get("post_entry_highs", [])[-14:]
                if not highs:
                    continue
                if seeded and close <= stop:
                    if self._submit_virtual_stop(
                        symbol, mode=mode, reason="chandelier14", row=row,
                        stamp=stamp, engine_time=engine_time,
                    ):
                        continue
                candidate = max(highs) - 3.0 * atr
                state["stop"] = candidate if not seeded else max(stop, candidate)
                state["seeded"] = True
                continue
            if mode == "fixed-entry-atr":
                if seeded and close <= stop:
                    self._submit_virtual_stop(
                        symbol, mode=mode, reason="fixed_entry_stop", row=row,
                        stamp=stamp, engine_time=engine_time,
                    )
                continue
            if mode == "virtual-trail-breakeven-2r":
                risk = atr_k * float(state["entry_atr"])
                if close >= float(state["entry_price"]) + 2.0 * risk:
                    state["stop"] = max(float(state["stop"]), float(state["entry_price"]))
                if close <= float(state["stop"]):
                    if self._submit_virtual_stop(
                        symbol, mode=mode, reason="stop_breach", row=row,
                        stamp=stamp, engine_time=engine_time,
                    ):
                        continue
                self._ratchet(state, close, atr, atr_k)
                continue
            # ``virtual-trail-baseline`` and ``virtual-trail-plus-emergency-4atr``.
            if close <= float(state["stop"]):
                if self._submit_virtual_stop(
                    symbol, mode=mode, reason="stop_breach", row=row,
                    stamp=stamp, engine_time=engine_time,
                ):
                    continue
            self._ratchet(state, close, atr, atr_k)

    # -- allocation -----------------------------------------------------------
    def _target_weights(self, day: Any, hour: int, session: Any) -> dict[str, float]:
        selected = list(self._selected)
        if not selected:
            return {}
        mode = str(self._params["weight_mode"])
        stamp = self._stamp(day, hour)
        volatility: dict[str, float] = {}
        atr: dict[str, float] = {}
        price: dict[str, float] = {}
        for symbol in selected:
            row = self._daily_row(symbol, session)
            volatility[symbol] = float(row["vol20"]) if row is not None and finite(row.get("vol20")) else float("nan")
            completed = self._completed_row(symbol, stamp)
            atr[symbol] = float(completed["atr"]) if completed is not None else float("nan")
            execution = self._execution_price(symbol, stamp)
            price[symbol] = float(execution) if execution is not None else float("nan")
        covariance = None
        if mode == "vol-target":
            boundary = pd.Timestamp(session)
            frames = [self._ctx.inputs.daily[symbol]["ret1"].loc[:boundary] for symbol in selected]
            covariance = covariance_matrix(frames, int(self._params["vol_covariance_sessions"]))
        weights = target_weights(
            mode,
            selected=selected,
            gross_target=float(self._params["gross_target"]),
            per_symbol_cap=self._params["per_symbol_cap"],
            atr_k=float(self._params["atr_k"]),
            volatility=volatility,
            atr=atr,
            price=price,
            stop_distance_budget=self._params["stop_distance_budget"],
            vol_target=self._params["vol_target"],
            covariance=covariance,
        )
        pre_cap = dict(weights)
        cap = self._params.get("risk_contribution_cap")
        weights = cap_risk_contributions(
            weights,
            atr=atr,
            price=price,
            atr_k=float(self._params["atr_k"]),
            risk_contribution_cap=cap,
        )
        if cap is not None:
            for symbol in selected:
                atr_value = atr.get(symbol, float("nan"))
                price_value = price.get(symbol, float("nan"))
                relative_stop_distance = (
                    float(self._params["atr_k"]) * float(atr_value) / float(price_value)
                    if finite(atr_value) and finite(price_value) and float(atr_value) > 0.0 and float(price_value) > 0.0
                    else None
                )
                post_weight = weights.get(symbol)
                self._risk_cap_events.append({
                    "day": str(day),
                    "hour": hour,
                    "symbol": symbol,
                    "pre_cap_weight": pre_cap.get(symbol),
                    "post_cap_weight": post_weight,
                    "atr": float(atr_value) if finite(atr_value) else None,
                    "executable_price": float(price_value) if finite(price_value) else None,
                    "relative_stop_distance": relative_stop_distance,
                    "risk_contribution": (
                        float(post_weight) * float(relative_stop_distance)
                        if post_weight is not None and relative_stop_distance is not None else None
                    ),
                    "risk_contribution_cap": float(cap),
                    "binding": (
                        post_weight is not None
                        and pre_cap.get(symbol) is not None
                        and float(post_weight) < float(pre_cap[symbol])
                    ),
                })
        final = apply_leveraged_cap(
            weights,
            leveraged_symbols=[symbol for symbol in selected if symbol in LEVERAGED_PRODUCTS],
            cap=self._params["leveraged_cap"],
        )
        try:
            self._record_allocation_event(
                day, hour, selected, pre_cap, weights, final, atr, price, covariance, volatility,
                mode,
            )
        except Exception:
            pass
        return final

    def _record_allocation_event(self, day: Any, hour: int, selected: Sequence[str],
                                 pre_cap: Mapping[str, float], post_cap: Mapping[str, float],
                                 final: Mapping[str, float], atr: Mapping[str, float],
                                 price: Mapping[str, float], covariance: Any,
                                 volatility: Mapping[str, float] | None = None,
                                 mode: str = "equal-slots") -> None:
        matrix = None
        if covariance is not None:
            try:
                matrix = [[float(value) for value in row] for row in covariance]
            except TypeError:
                matrix = None
        full_snapshot = {
            symbol: float(value) for symbol, value in price.items() if finite(value)
        }
        quote = getattr(self, "_last_quote_snapshot", None)
        if isinstance(quote, Mapping) and isinstance(quote.get("prices"), Mapping):
            captured = {
                symbol: float(value)
                for symbol, value in quote["prices"].items() if finite(value)
            }
            if captured:
                full_snapshot = captured
        allocation: dict[str, Any] = {
            "selected_order": list(selected),
            "event_time_atr": {
                symbol: float(value) for symbol, value in atr.items() if finite(value) and float(value) > 0.0
            },
            "executable_prices": {
                symbol: float(value) for symbol, value in price.items() if finite(value) and float(value) > 0.0
            },
            "full_live_price_snapshot": full_snapshot,
            "volatility_inputs": {
                symbol: float(value) if finite(value) else None
                for symbol, value in (volatility or {}).items()
            },
            "covariance_symbols": list(selected) if matrix is not None else None,
            "covariance_matrix": matrix,
            "pre_risk_contribution_cap_weights": {
                symbol: float(value) for symbol, value in pre_cap.items()
            },
            "post_risk_contribution_cap_weights": {
                symbol: float(value) for symbol, value in post_cap.items()
            },
            "final_weights_after_leveraged_cap": {
                symbol: float(value) for symbol, value in final.items()
            },
            "risk_contribution_cap": self._params.get("risk_contribution_cap"),
            "risk_cap_binding_by_symbol": {
                symbol: float(post_cap.get(symbol, 0.0)) < float(pre_cap.get(symbol, 0.0))
                for symbol in selected
            },
            "risk_cap_bound_any": any(
                float(post_cap.get(symbol, 0.0)) < float(pre_cap.get(symbol, 0.0))
                for symbol in selected
            ),
        }
        if str(mode) == "vol-target" and covariance is not None and selected:
            base = {symbol: 1.0 / len(selected) for symbol in selected}
            allocation.update({
                "base_forecast_volatility": forecast_volatility(base, covariance),
                "pre_cap_forecast_volatility": forecast_volatility(pre_cap, covariance),
                "post_cap_forecast_volatility": forecast_volatility(final, covariance),
            })
        if matrix is None and str(mode) != "vol-target":
            allocation["covariance_not_required"] = (
                f"weight_mode {mode!r} does not consume covariance"
            )
        event = {
            "event": "allocation",
            "decision_id": self._current_decision_id(),
            "day": str(day),
            "hour": hour,
            **allocation,
        }
        maybe_capture = getattr(self, "_decision_capture", None)
        if isinstance(maybe_capture, dict):
            maybe_capture["allocation"] = allocation
        self._record_event(dict(event))

    def _rebalance(self, day: Any, hour: int) -> None:
        decision_id = self._current_decision_id()
        processed = getattr(self, "_processed_decision_ids", None)
        if processed is None:
            processed = set()
            self._processed_decision_ids = processed
        if decision_id in processed:
            # Exactly-once protection: a committed decision is never re-submitted.
            self._audit_stream("_diag").append({
                "event": "duplicate_decision_suppressed",
                "decision_id": decision_id,
                "day": str(day),
                "hour": hour,
            })
            return

        # First calculate the complete order batch without touching the broker.
        # Stable intent IDs allocated here are reused verbatim after the full
        # decision snapshot has been checkpointed.
        planned_orders: list[dict[str, Any]] = []
        submission_actions: list[dict[str, Any]] = []
        synthetic_pending_sells: set[str] = set()
        original_submit_buy = self._submit_buy
        original_submit_sell = self._submit_sell

        def capture_buy(
            symbol: str,
            quantity: float,
            reference: float,
            atr: float,
            reason: str,
            *,
            decision_id: str | None = None,
            intent_id: str | None = None,
        ) -> str:
            resolved_decision = str(decision_id or self._current_decision_id())
            resolved_intent = intent_id or f"buy-{symbol}-{self._next_sequence('_intent_sequence')}"
            submission_actions.append({
                "side": "buy",
                "symbol": symbol,
                "quantity": float(quantity),
                "reference": float(reference),
                "atr": float(atr),
                "reason": str(reason),
                "decision_id": resolved_decision,
                "intent_id": resolved_intent,
            })
            return resolved_intent

        def capture_sell(
            symbol: str,
            reason: str,
            reference: float,
            *,
            stop_context: Mapping[str, Any] | None = None,
            decision_id: str | None = None,
            intent_id: str | None = None,
        ) -> str | None:
            state = self._positions.get(symbol)
            if state is None or symbol in self._pending_sells:
                return None
            resolved_decision = str(decision_id or self._current_decision_id())
            resolved_intent = intent_id or f"sell-{symbol}-{self._next_sequence('_intent_sequence')}"
            submission_actions.append({
                "side": "sell",
                "symbol": symbol,
                "reference": float(reference),
                "reason": str(reason),
                "stop_context": dict(stop_context) if stop_context is not None else None,
                "decision_id": resolved_decision,
                "intent_id": resolved_intent,
            })
            # Preserve the existing pending-exit slot invariant during planning
            # without mutating restartable order lineage.
            self._pending_sells.add(symbol)
            self._pending_sell_reason[symbol] = str(reason)
            synthetic_pending_sells.add(symbol)
            return resolved_intent

        try:
            if bool(getattr(self, "_risk_off_active", False)):
                stamp = self._stamp(day, hour)
                symbols = sorted(self._positions)
                for symbol in symbols:
                    if symbol in self._pending_sells:
                        continue
                    state = self._positions[symbol]
                    reference = self._execution_price(symbol, stamp)
                    intent_id = f"sell-{symbol}-{self._next_sequence('_intent_sequence')}"
                    submission_actions.append({
                        "side": "sell",
                        "symbol": symbol,
                        "reference": float(reference) if reference is not None else float("nan"),
                        "reason": "global_risk_off",
                        "stop_context": None,
                        "decision_id": decision_id,
                        "intent_id": intent_id,
                    })
                    planned_orders.append({
                        "symbol": symbol,
                        "side": "sell",
                        "requested_quantity": float(state["quantity"]),
                        "reference_price": reference,
                        "intended_notional": None,
                        "budget_before": None,
                        "budget_after": None,
                        "intent_id": intent_id,
                        "reason": "global_risk_off",
                    })
                outcome, outcome_reason = "risk_off", "global risk-off flatten"
            else:
                self._submit_buy = capture_buy
                self._submit_sell = capture_sell
                outcome, outcome_reason = self._execute_rebalance(
                    day, hour, decision_id, planned_orders
                )
        finally:
            self._submit_buy = original_submit_buy
            self._submit_sell = original_submit_sell
            for symbol in synthetic_pending_sells:
                self._pending_sells.discard(symbol)
                self._pending_sell_reason.pop(symbol, None)

        # Snapshot and checkpoint the pre-submission book before any create,
        # cancel, or submit call can reach the broker boundary.
        self._emit_decision_snapshot(
            day, hour, decision_id, outcome, outcome_reason, planned_orders
        )
        processed.add(decision_id)
        self._persistence_checkpoint("decision_committed")

        self._submission_scope_decision_id = decision_id
        try:
            if bool(getattr(self, "_risk_off_active", False)):
                self._cancel_pending_buys(reason="global_risk_off")
            for action in submission_actions:
                if action["side"] == "sell":
                    try:
                        original_submit_sell(
                            action["symbol"],
                            action["reason"],
                            action["reference"],
                            stop_context=action["stop_context"],
                            decision_id=action["decision_id"],
                            intent_id=action["intent_id"],
                        )
                    except TypeError as error:
                        if "unexpected keyword argument" not in str(error):
                            raise
                        original_submit_sell(
                            action["symbol"], action["reason"], action["reference"]
                        )
                else:
                    try:
                        original_submit_buy(
                            action["symbol"],
                            action["quantity"],
                            action["reference"],
                            action["atr"],
                            action["reason"],
                            decision_id=action["decision_id"],
                            intent_id=action["intent_id"],
                        )
                    except TypeError as error:
                        # Small policy harnesses historically replace the submit
                        # hook with a positional-only recorder.  The real method
                        # still receives explicit decision/intent ownership.
                        if "unexpected keyword argument" not in str(error):
                            raise
                        original_submit_buy(
                            action["symbol"],
                            action["quantity"],
                            action["reference"],
                            action["atr"],
                            action["reason"],
                        )
        finally:
            self._submission_scope_decision_id = None

    def _execute_rebalance(
        self, day: Any, hour: int, decision_id: str, planned_orders: list[dict[str, Any]],
    ) -> tuple[str, str | None]:
        """Calculate, plan, and submit one rebalance.  Returns its outcome."""
        if bool(getattr(self, "_risk_off_active", False)):
            self._selected = ()
            self._flatten_risk_off(day, hour)
            return "risk_off", "global risk-off flatten"
        stamp = self._stamp(day, hour)
        target = set(self._selected)
        for symbol in list(self._positions):
            if symbol not in target:
                price = self._execution_price(symbol, stamp)
                intent_id = self._submit_sell(
                    symbol, "selection_change",
                    price if price is not None else float("nan"),
                    decision_id=decision_id,
                )
                if intent_id:
                    planned_orders.append({
                        "symbol": symbol,
                        "side": "sell",
                        "requested_quantity": float(self._positions.get(symbol, {}).get("quantity", 0.0)),
                        "reference_price": price if price is not None else None,
                        "intended_notional": None,
                        "budget_before": None,
                        "budget_after": None,
                        "intent_id": intent_id,
                        "reason": "selection_change",
                    })
        try:
            self.update_broker_balances(force_update=True)
        except Exception:
            pass
        budget = max(0.0, float(self.cash or 0.0))
        portfolio_value = float(self.portfolio_value or 0.0)
        fee_rate = float(self._params["cost_bps_per_side"]) / 10_000.0
        session = self._previous_session(day)
        try:
            weights = self._target_weights(day, hour, session) if session is not None else {}
        except PolicyError as error:
            self._rejections.append({"reason": "policy_error", "detail": str(error), "day": str(day)})
            weights = {}
        try:
            tracked = len(self.get_tracked_positions())
        except Exception:
            tracked = -1
        self._diag.append({
            "day": str(day), "hour": hour, "cash": budget, "portfolio_value": portfolio_value,
            "book_positions": sorted(self._positions), "broker_positions": tracked,
            "pending_buys": sorted(self._pending_buys), "pending_sells": sorted(self._pending_sells),
            "weights": {symbol: round(weight, 6) for symbol, weight in weights.items()},
            "selected": list(self._selected), "buys": [],
        })
        if self._pending_sells:
            # A pending exit continues to occupy its slot until the broker fill
            # is confirmed.  Waiting prevents a replacement from temporarily
            # exceeding top_n or the gross budget.
            self._rejections.append({
                "reason": "pending_exit_occupies_slot",
                "day": str(day),
                "hour": hour,
                "pending_sells": sorted(self._pending_sells),
            })
            return "pending_exit", "pending exit occupies its slot"
        if not self._selected:
            return "no_op", "no selected symbols"
        occupied = (
            set(self._positions)
            | set(self._pending_buys)
            | set(getattr(self, "_unmatched_broker_orders", {}) or {})
        )
        for symbol in self._selected:
            if symbol in occupied:
                continue
            price = self._execution_price(symbol, stamp)
            completed = self._completed_row(symbol, stamp)
            if price is None or completed is None:
                self._rejections.append({"symbol": symbol, "reason": "no_bar_at_rebalance",
                                         "day": str(day), "hour": hour})
                continue
            atr = float(completed["atr"])
            if not (math.isfinite(atr) and atr > 0.0):
                self._rejections.append({"symbol": symbol, "reason": "invalid_price_or_atr",
                                         "day": str(day), "hour": hour})
                continue
            edge_threshold = float(self._params.get("min_trade_edge_bps", 0.0))
            edge_value: float | None = None
            edge_accepted = True
            if edge_threshold > 0.0:
                daily_row = self._daily_row(symbol, session) if session is not None else None
                edge_value = expected_trade_move_bps(
                    prior_return=float(daily_row["ret"]) if daily_row is not None and finite(daily_row.get("ret")) else float("nan"),
                    return_period=int(self._params["return_period"]),
                    holding_bars=int(self._params.get("min_position_holding_bars", 0)),
                    atr_k=float(self._params["atr_k"]),
                    atr=atr,
                    executable_price=price,
                )
                edge_accepted = edge_value is not None and edge_value >= edge_threshold
            edge_event = {
                "day": str(day),
                "hour": hour,
                "symbol": symbol,
                "expected_move_bps": edge_value,
                "threshold_bps": edge_threshold,
                "accepted": edge_accepted,
                "decision": "accepted" if edge_accepted else "rejected",
            }
            self._entry_edge_events.append(edge_event)
            self._journal.append({"event": "entry_edge", **edge_event})
            if not edge_accepted:
                self._rejections.append({"symbol": symbol, "reason": "min_trade_edge", **edge_event})
                continue
            notional = min(float(weights.get(symbol, 0.0)) * portfolio_value, budget)
            effective_price = price * (1.0 + fee_rate)
            quantity = math.floor(notional / effective_price)
            if quantity <= 0:
                self._rejections.append({"symbol": symbol, "reason": "insufficient_budget",
                                         "day": str(day), "hour": hour})
                continue
            budget_before = budget
            budget -= quantity * effective_price
            self._diag[-1]["buys"].append({
                "symbol": symbol, "price": price, "quantity": quantity,
                "notional": quantity * price, "budget_after": budget,
            })
            intent_id = self._submit_buy(
                symbol, quantity, price, atr, "selection_entry",
                decision_id=decision_id,
            )
            if intent_id:
                planned_orders.append({
                    "symbol": symbol,
                    "side": "buy",
                    "requested_quantity": float(quantity),
                    "reference_price": price,
                    "intended_notional": float(notional),
                    "budget_before": float(budget_before),
                    "budget_after": float(budget),
                    "intent_id": intent_id,
                    "reason": "selection_entry",
                })
        if not planned_orders:
            return "no_op", "no executable orders this decision"
        return "executed", None

    def _audit_parameters(self) -> dict[str, Any]:
        params = getattr(self, "_params", {}) or {}
        return {
            "weight_mode": params.get("weight_mode"),
            "gross_target": params.get("gross_target"),
            "per_symbol_cap": params.get("per_symbol_cap"),
            "atr_k": params.get("atr_k"),
            "stop_distance_budget": params.get("stop_distance_budget"),
            "vol_target": params.get("vol_target"),
            "risk_contribution_cap": params.get("risk_contribution_cap"),
            "leveraged_cap": params.get("leveraged_cap"),
            "top_n": params.get("top_n"),
            "rank_buffer": params.get("rank_buffer"),
            "exposure_group_limit": params.get("exposure_group_limit"),
            "correlation_screen": params.get("correlation_screen"),
            "universe_symbols": list(getattr(self, "_ordered", ()) or params.get("universe_symbols") or ()),
        }

    def _aware_stamp(self, day: Any, hour: int) -> pd.Timestamp:
        stamp = pd.Timestamp(day) + pd.Timedelta(hours=hour)
        if stamp.tzinfo is None:
            stamp = stamp.tz_localize("America/New_York")
        return stamp

    def _emit_decision_snapshot(
        self, day: Any, hour: int, decision_id: str, outcome: str,
        outcome_reason: str | None, planned_orders: Sequence[Mapping[str, Any]],
    ) -> None:
        """Assemble and append one full decision snapshot.  Never alters trading."""
        try:
            capture = getattr(self, "_decision_capture", None)
            if not isinstance(capture, dict) or capture.get("decision_id") != decision_id:
                capture = {"decision_id": decision_id, "selection": None, "allocation": None}
            quote = getattr(self, "_last_quote_snapshot", None)
            quote_captured = None
            if isinstance(quote, Mapping):
                quote_captured = quote.get("captured_at")
            session = self._previous_session(day)
            snapshot = build_decision_snapshot(
                identity={**self._audit_identity_fields(), "decision_id": decision_id},
                time={
                    "session": str(day),
                    "signal_session": str(session) if session is not None else None,
                    "native_iteration_time": self._stamp(day, hour).isoformat(),
                    "logical_rebalance_time": self._aware_stamp(day, hour).isoformat(),
                    "quote_snapshot_captured_at": quote_captured or self._event_time_iso(),
                    "timezone": "America/New_York",
                    "hourly_convention": HTS_HOURLY_CONVENTION,
                },
                selection=capture.get("selection") or {},
                allocation=capture.get("allocation") or {},
                account={
                    "cash": float(self.cash or 0.0),
                    "portfolio_value": float(self.portfolio_value or 0.0),
                    "fee_rate": float(self._params.get("cost_bps_per_side", 0.0)) / 10_000.0,
                },
                book={
                    "existing_positions": sorted(getattr(self, "_positions", {}) or {}),
                    "pending_buys": sorted(getattr(self, "_pending_buys", {}) or {}),
                    "pending_sells": sorted(getattr(self, "_pending_sells", None) or ()),
                    "occupied_symbols": sorted(
                        set(getattr(self, "_positions", {}) or {})
                        | set(getattr(self, "_pending_buys", {}) or {})
                    ),
                },
                planned_orders=planned_orders,
                parameters=self._audit_parameters(),
                outcome=outcome,
                outcome_reason=outcome_reason,
            )
            self._audit_stream("_decision_snapshots").append(snapshot)
            self._audit_stream("_journal").append({
                "event": "decision_emitted",
                "decision_id": decision_id,
                "outcome": outcome,
                "planned_orders": len(list(planned_orders)),
            })
        except Exception as error:
            self._audit_stream("_diag").append({
                "event": "decision_snapshot_failed",
                "decision_id": decision_id,
                "error": type(error).__name__,
                "reason": str(error),
            })

    def _queue_rebalance_after_last_observable_iteration(self, day: Any, logical_hour: int) -> None:
        """Plan a logical 15:00 rebalance without permitting a 14:00 fill.

        Market orders submitted from the final native (14:00) callback fill on
        that same callback.  That would receive the earlier 14:00 open, so it
        is not a valid next-bar fallback.  Capture the causal intent here and
        submit it at LumiBot's next observed bar.
        """
        if getattr(self, "_deferred_rebalance", None) is not None:
            self._rejections.append({
                "reason": "deferred_rebalance_already_pending",
                "day": str(day),
                "hour": logical_hour,
            })
            return
        captured_buys: list[dict[str, Any]] = []
        captured_sells: list[dict[str, Any]] = []
        synthetic_pending_sells: set[str] = set()
        original_submit_buy = self._submit_buy
        original_submit_sell = self._submit_sell
        original_execution_price = self._execution_price

        def capture_buy(
            symbol: str,
            quantity: float,
            reference: float,
            atr: float,
            reason: str,
            *,
            decision_id: str | None = None,
            intent_id: str | None = None,
        ) -> str:
            resolved = intent_id or f"buy-{symbol}-{self._next_sequence('_intent_sequence')}"
            captured_buys.append({
                "symbol": symbol,
                "quantity": float(quantity),
                "reference": float(reference),
                "atr": float(atr),
                "reason": reason,
                "intent_id": resolved,
                "decision_id": decision_id or self._current_decision_id(),
            })
            return resolved

        def capture_sell(
            symbol: str,
            reason: str,
            reference: float,
            *,
            stop_context: Mapping[str, Any] | None = None,
            decision_id: str | None = None,
            intent_id: str | None = None,
        ) -> str | None:
            if symbol not in self._positions or symbol in self._pending_sells:
                return None
            # Match `_rebalance`'s pending-exit slot invariant without sending
            # the order early.  The marker is removed before this method exits.
            self._pending_sells.add(symbol)
            self._pending_sell_reason[symbol] = reason
            synthetic_pending_sells.add(symbol)
            resolved = intent_id or f"sell-{symbol}-{self._next_sequence('_intent_sequence')}"
            captured_sells.append({
                "symbol": symbol,
                "reason": reason,
                "reference": float(reference),
                "intent_id": resolved,
                "decision_id": decision_id or self._current_decision_id(),
            })
            return resolved

        def completed_source_price(symbol: str, stamp: pd.Timestamp) -> float | None:
            """Price a deferred decision from its completed source bar only."""
            row = self._completed_row(symbol, stamp)
            if row is None:
                return None
            price = float(row["close"])
            return price if math.isfinite(price) and price > 0.0 else None

        self._submit_buy = capture_buy
        self._submit_sell = capture_sell
        self._execution_price = completed_source_price
        try:
            # Pass the logical clock, not the observable 14:00 callback.  This
            # makes the source bar 14:00.  Pricing is temporarily pinned to
            # that completed bar as well; no 15:00 OHLC field may influence a
            # deferred decision that ultimately fills next session.
            self._rebalance(day, logical_hour)
        finally:
            self._submit_buy = original_submit_buy
            self._submit_sell = original_submit_sell
            self._execution_price = original_execution_price
            for symbol in synthetic_pending_sells:
                self._pending_sells.discard(symbol)
                self._pending_sell_reason.pop(symbol, None)

        decision_stamp = self._stamp(day, logical_hour)
        completed_source = decision_stamp - pd.Timedelta(hours=1)
        self._deferred_rebalance = {
            "kind": "rebalance",
            "decision_day": str(day),
            "decision_id": self._current_decision_id(),
            "logical_rebalance_hour": logical_hour,
            "submission_started": False,
            "buys": captured_buys,
            "sells": captured_sells,
        }
        self._persistence_checkpoint("deferred_rebalance_queued")
        event = {
            "event": "deferred_rebalance_queued",
            "decision_day": str(day),
            "native_iteration_hour": self._effective_rebalance_iteration_hour(),
            "logical_rebalance_hour": logical_hour,
            "completed_source_bar": completed_source.isoformat(),
            "intended_source_fill_bar": decision_stamp.isoformat(),
            "decision_price_source": "completed source close; no 15:00 OHLC value read",
            "fallback": "submit at the next native iteration; run_trades.csv records the actual fill timestamp",
            "buys": len(captured_buys),
            "sells": len(captured_sells),
        }
        self._deferred_rebalance_events.append(event)
        self._journal.append(event)

    def _queue_risk_off_flatten_after_last_observable_iteration(self, day: Any, logical_hour: int) -> None:
        """Queue a risk-off flatten so it cannot fill at the earlier 14:00 open."""
        captured_sells: list[dict[str, Any]] = []
        original_submit_sell = self._submit_sell
        original_cancel_pending_buys = self._cancel_pending_buys

        def capture_sell(
            symbol: str,
            reason: str,
            reference: float,
            *,
            stop_context: Mapping[str, Any] | None = None,
            decision_id: str | None = None,
            intent_id: str | None = None,
        ) -> str:
            resolved_intent = intent_id or f"sell-{symbol}-{self._next_sequence('_intent_sequence')}"
            captured_sells.append({
                "symbol": symbol,
                "reason": reason,
                "reference": float(reference),
                "intent_id": resolved_intent,
                "decision_id": str(decision_id or self._current_decision_id()),
            })
            return resolved_intent

        self._submit_sell = capture_sell
        self._cancel_pending_buys = lambda *, reason: len(self._pending_buys)
        try:
            self._rebalance(day, logical_hour)
        finally:
            self._submit_sell = original_submit_sell
            self._cancel_pending_buys = original_cancel_pending_buys

        self._deferred_rebalance = {
            "kind": "risk_off_flatten",
            "decision_day": str(day),
            "decision_id": self._current_decision_id(),
            "logical_rebalance_hour": logical_hour,
            "submission_started": False,
            "sells": captured_sells,
        }
        self._persistence_checkpoint("deferred_risk_off_flatten_queued")
        decision_stamp = self._stamp(day, logical_hour)
        event = {
            "event": "deferred_risk_off_flatten_queued",
            "decision_day": str(day),
            "native_iteration_hour": self._effective_rebalance_iteration_hour(),
            "logical_rebalance_hour": logical_hour,
            "completed_source_bar": (decision_stamp - pd.Timedelta(hours=1)).isoformat(),
            "intended_source_fill_bar": decision_stamp.isoformat(),
            "fallback": "submit at the next native iteration; run_trades.csv records the actual fill timestamp",
        }
        self._deferred_rebalance_events.append(event)
        self._journal.append(event)

    def _flush_deferred_rebalance(self, day: Any, hour: int) -> None:
        """Submit a queued logical-15:00 intent at the next native fill bar."""
        intent = getattr(self, "_deferred_rebalance", None)
        if intent is None or pd.Timestamp(day).date() <= pd.Timestamp(intent["decision_day"]).date():
            return
        stamp = self._stamp(day, hour)
        deferred_decision = str(intent["decision_id"])
        if intent.get("submission_started") is True:
            # The pre-submit checkpoint is authoritative after a restart.  We
            # cannot know whether a process died before or after broker contact,
            # so retrying would be the unsafe choice.  Reconciliation owns any
            # broker order that actually exists.
            self._audit_stream("_diag").append({
                "event": "duplicate_decision_suppressed",
                "decision_id": deferred_decision,
                "path": "deferred",
                "day": str(day),
                "hour": hour,
            })
            self._deferred_rebalance = None
            self._persistence_checkpoint("deferred_duplicate_suppressed")
            return

        processed = getattr(self, "_processed_decision_ids", None)
        if processed is None:
            processed = set()
            self._processed_decision_ids = processed
        processed.add(deferred_decision)
        # Persist the one-way transition before allowing the scoped first
        # submission.  Persistence remains fail-open; a failed checkpoint is
        # diagnosed by `_persistence_checkpoint` and does not block trading.
        intent["submission_started"] = True
        self._persistence_checkpoint("deferred_submission_started")
        self._submission_scope_decision_id = deferred_decision
        try:
            if intent["kind"] == "risk_off_flatten":
                self._selected = ()
                cancelled = self._cancel_pending_buys(reason="global_risk_off")
                submitted = 0
                for sell in intent.get("sells", []):
                    symbol = str(sell["symbol"])
                    state = self._positions.get(symbol)
                    if state is None:
                        continue
                    price = self._execution_price(symbol, stamp)
                    result = self._submit_sell(
                        symbol,
                        str(sell["reason"]),
                        price if price is not None else float("nan"),
                        decision_id=deferred_decision,
                        intent_id=sell.get("intent_id"),
                    )
                    submitted += int(result is not None)
                event = {
                    "event": "deferred_risk_off_flatten_submitted",
                    "decision_day": intent["decision_day"],
                    "actual_submission_time": stamp.isoformat(),
                    "source_fill_bar": stamp.isoformat(),
                    "cancelled_buys": cancelled,
                    "submitted_sells": submitted,
                }
                self._deferred_rebalance_events.append(event)
                self._journal.append(event)
                if cancelled or submitted:
                    self._risk_off_flatten_count += submitted
                    self._journal.append({
                        "event": "global_risk_off_flatten",
                        "day": str(day),
                        "hour": hour,
                        "cancelled_buys": cancelled,
                        "submitted_sells": submitted,
                        "race_only": False,
                    })
                self._deferred_rebalance = None
                self._persistence_checkpoint("deferred_submission_complete")
                return

            # Submit planned exits first, then their replacements in the same
            # next-session callback.  Waiting for the fill callback would make a
            # capable 09:00 fallback become a 10:00 fill solely because LumiBot
            # has no 15:00 iteration.  Reserve only the contemporaneous proceeds
            # of these sells, so the combined market-order batch remains cash-safe.
            sale_proceeds = 0.0
            for sell in intent["sells"]:
                symbol = str(sell["symbol"])
                state = self._positions.get(symbol)
                if state is not None:
                    price = self._execution_price(symbol, stamp)
                    if price is not None:
                        sale_proceeds += float(state["quantity"]) * price * (1.0 - float(self._params["cost_bps_per_side"]) / 10_000.0)
                    self._submit_sell(
                        symbol, str(sell["reason"]),
                        price if price is not None else float("nan"),
                        decision_id=deferred_decision, intent_id=sell.get("intent_id"),
                    )

            try:
                self.update_broker_balances(force_update=True)
            except Exception:
                pass
            budget = max(0.0, float(self.cash or 0.0)) + sale_proceeds
            fee_rate = float(self._params["cost_bps_per_side"]) / 10_000.0
            submitted = 0
            for buy in intent["buys"]:
                symbol = str(buy["symbol"])
                if symbol in self._positions or symbol in self._pending_buys:
                    continue
                price = self._execution_price(symbol, stamp)
                if price is None:
                    self._rejections.append({
                        "symbol": symbol,
                        "reason": "no_bar_at_deferred_rebalance",
                        "day": str(day),
                        "hour": hour,
                    })
                    continue
                # Retain the completed-14:00 causal notional decision while sizing
                # against the actual fallback open, which cannot overspend cash on
                # a gap.
                planned_notional = float(buy["quantity"]) * float(buy["reference"]) * (1.0 + fee_rate)
                effective_price = price * (1.0 + fee_rate)
                quantity = math.floor(min(planned_notional, budget) / effective_price)
                if quantity <= 0:
                    self._rejections.append({
                        "symbol": symbol,
                        "reason": "insufficient_budget_at_deferred_rebalance",
                        "day": str(day),
                        "hour": hour,
                    })
                    continue
                budget -= quantity * effective_price
                self._submit_buy(
                    symbol, quantity, price, float(buy["atr"]), str(buy["reason"]),
                    decision_id=deferred_decision, intent_id=buy.get("intent_id"),
                )
                submitted += 1
            event = {
                "event": "deferred_rebalance_submitted",
                "decision_day": intent["decision_day"],
                "actual_submission_time": stamp.isoformat(),
                "source_fill_bar": stamp.isoformat(),
                "buys": submitted,
            }
            self._deferred_rebalance_events.append(event)
            self._journal.append(event)
            self._deferred_rebalance = None
            self._persistence_checkpoint("deferred_submission_complete")
        finally:
            self._submission_scope_decision_id = None

    # -- lifecycle ------------------------------------------------------------
    def on_trading_iteration(self) -> None:
        now = pd.Timestamp(self.get_datetime())
        day, hour = now.date(), int(now.hour)
        rebalance_hour = int(self._params["rebalance_hour"])
        effective_rebalance_hour = self._effective_rebalance_iteration_hour()
        risk_off = self._refresh_risk_off_state(day)
        if risk_off:
            self._cancel_pending_buys(reason="global_risk_off")
        deferred = getattr(self, "_deferred_rebalance", None)
        if deferred is not None and pd.Timestamp(day).date() > pd.Timestamp(deferred["decision_day"]).date():
            if deferred["kind"] == "risk_off_flatten":
                self._flush_deferred_rebalance(day, hour)
            elif risk_off:
                # A next-session global gate closure supersedes an unfilled
                # entry intent; never permit it to re-risk the book.
                self._deferred_rebalance_events.append({
                    "event": "deferred_rebalance_cancelled_risk_off",
                    "decision_day": deferred["decision_day"],
                    "cancellation_time": self._stamp(day, hour).isoformat(),
                })
                self._deferred_rebalance = None
            else:
                self._flush_deferred_rebalance(day, hour)
        if hour == int(self._params["signal_hour"]) and self._schedule_due(day):
            self._begin_decision(day, hour)
            self._selected = self._select(day)
            self._signal_day = day
        self._update_risk(day, hour, engine_time=now)
        if risk_off and self._risk_off_race_symbols:
            # A buy that filled after its cancellation raced the global gate;
            # liquidate it at the next executable bar rather than waiting for
            # the scheduled rebalance hour.
            self._flatten_risk_off(day, hour, race_only=True)
        if risk_off and hour == effective_rebalance_hour:
            self._selected = ()
            if rebalance_hour in REBALANCE_ITERATION_HOUR_MAP:
                self._queue_risk_off_flatten_after_last_observable_iteration(day, rebalance_hour)
            else:
                self._rebalance(day, rebalance_hour)
            return
        if hour == effective_rebalance_hour and self._schedule_due(day):
            if self._signal_day != day:
                # No completed signal bar this session; recompute causally from
                # the previous completed session instead of reusing a stale pick.
                self._begin_decision(day, hour)
                self._selected = self._select(day)
                self._signal_day = day
            if rebalance_hour in REBALANCE_ITERATION_HOUR_MAP:
                self._queue_rebalance_after_last_observable_iteration(day, rebalance_hour)
            else:
                self._rebalance(day, rebalance_hour)

    @staticmethod
    def _cumulative_filled_quantity(order: Order, event_quantity: float) -> float:
        """Cumulative filled quantity for an order, not one broker fill event.

        LumiBot calls ``on_filled_order`` once per broker fill event and passes
        that event's quantity (``broker._process_filled_order`` appends every
        event to ``order.transactions``).  A market order that fills in several
        executions therefore arrives with only the final fragment as
        ``quantity``; sizing the recorded position (and its protective stop)
        from that fragment would leave most of the real position unprotected.
        Prefer the cumulative filled quantity, then the order's total size.
        """
        try:
            filled = sum(float(getattr(t, "quantity", 0) or 0)
                         for t in (getattr(order, "transactions", None) or []))
        except (TypeError, ValueError):
            filled = 0.0
        if math.isfinite(filled) and filled > 0.0:
            return filled
        try:
            ordered = float(getattr(order, "quantity", 0) or 0)
        except (TypeError, ValueError):
            ordered = 0.0
        if math.isfinite(ordered) and ordered > 0.0:
            return ordered
        value = float(event_quantity)
        if not math.isfinite(value) or value <= 0.0:
            raise RuntimeError("cannot determine filled entry quantity")
        return value

    def on_filled_order(self, position: Any, order: Order, price: float, quantity: float,
                        multiplier: float) -> None:
        symbol = order.asset.symbol
        if order.is_buy_order():
            pending = self._pending_buys.pop(symbol, None)
            if pending is None:
                pending = getattr(self, "_cancelled_pending_buys", {}).pop(symbol, {})
            atr = float(pending.get("atr", float("nan")))
            if not math.isfinite(atr) or atr <= 0.0:
                raise RuntimeError(f"cannot establish a stop for {symbol}: no entry ATR")
            mode = str(self._params["exit_mode"])
            # The callback ``quantity`` is a single fill event; the held position
            # and its protective stop must use the cumulative filled quantity or a
            # multi-execution market fill leaves the bulk of the position exposed.
            entry_quantity = self._cumulative_filled_quantity(order, quantity)
            meta = pending if isinstance(pending, Mapping) else {}
            if not meta:
                meta = self._order_meta(order)
            intent_id = meta.get("intent_id")
            decision_id = meta.get("decision_id")
            self._positions[symbol] = {
                "quantity": entry_quantity,
                "entry_price": float(price),
                "entry_atr": atr,
                "entry_session": self._current_day(),
                "entry_session_index": self._session_index.get(self._current_day()),
                "stop": float(price) - float(self._params["atr_k"]) * atr,
                "highest_high": float(price),
                "post_entry_highs": [],
                "breach_count": 0,
                "seeded": mode not in ("chandelier-since-entry", "chandelier-14-bar"),
                "entry_intent_id": intent_id,
                "entry_decision_id": decision_id,
            }
            self._journal.append({"event": "fill", "side": "buy", "symbol": symbol,
                                  "price": float(price), "quantity": entry_quantity})
            self._record_event({
                "event": "final_fill",
                "symbol": symbol,
                "side": "buy",
                "order_id": str(getattr(order, "identifier", "") or ""),
                "broker_order_id": str(getattr(order, "identifier", "") or ""),
                "fill_event_price": float(price),
                "fill_event_quantity": float(quantity),
                "cumulative_filled_quantity": float(entry_quantity),
                "intent_id": intent_id,
                "decision_id": decision_id,
            }, lifecycle=True)
            self._place_protective_stop(symbol)
            if decision_id:
                processed = getattr(self, "_processed_decision_ids", None)
                if processed is None:
                    processed = set()
                    self._processed_decision_ids = processed
                processed.add(str(decision_id))
            self._persistence_checkpoint("entry_filled")
            if bool(getattr(self, "_risk_off_active", False)):
                self._risk_off_race_symbols.add(symbol)
                self._journal.append({
                    "event": "risk_off_race_fill",
                    "symbol": symbol,
                    "day": str(self._current_day()),
                })
            return

        protective = self._protective.get(symbol)
        was_protective = protective is not None and protective is order
        self._protective.pop(symbol, None)
        protective_meta = dict(self._state_dict("_protective_meta").pop(symbol, {}) or {})
        sell_meta = dict(self._state_dict("_pending_sell_meta").pop(symbol, {}) or {})
        reason = self._pending_sell_reason.pop(symbol, "protective_stop" if was_protective else "unknown")
        stop_context = self._stop_exit_context.pop(symbol, None)
        self._pending_sells.discard(symbol)
        self._positions.pop(symbol, None)
        intent_id = sell_meta.get("intent_id")
        decision_id = sell_meta.get("decision_id")
        if was_protective and protective_meta:
            intent_id = protective_meta.get("intent_id") or intent_id
            decision_id = protective_meta.get("decision_id") or decision_id
        if not intent_id or not decision_id:
            fallback = self._order_meta(order)
            intent_id = intent_id or fallback.get("intent_id")
            decision_id = decision_id or fallback.get("decision_id")
        self._journal.append({"event": "fill", "side": "sell", "symbol": symbol,
                              "price": float(price), "quantity": float(quantity), "reason": reason})
        self._record_lifecycle_event({
            "event": "exit_fill",
            "symbol": symbol,
            "side": "sell",
            "order_id": str(getattr(order, "identifier", "") or ""),
            "broker_order_id": str(getattr(order, "identifier", "") or ""),
            "fill_event_price": float(price),
            "fill_event_quantity": float(quantity),
            "cumulative_filled_quantity": self._cumulative_filled_quantity(order, quantity),
            "intent_id": intent_id,
            "decision_id": decision_id,
            "reason": reason,
        })
        if stop_context is not None:
            event = build_virtual_stop_gap_event(
                stop_context,
                fill_time=pd.Timestamp(self.get_datetime()),
                fill_price=float(price),
                fill_quantity=float(quantity),
                fill_session=self._current_day(),
            )
            self._stop_gap_events.append(event)
            self._record_lifecycle_event({
                "event": "virtual_stop_filled",
                "symbol": symbol,
                "side": "sell",
                "order_id": event["order_id"],
                "broker_order_id": event["order_id"],
                "intent_id": intent_id,
                "decision_id": decision_id,
                "engine_time": event["engine_time"],
                "completed_source_bar": event["trigger_timestamp"],
                "submission_time": event["submission_time"],
                "fill_time": event["fill_time"],
                "source_fill_bar": event["source_fill_bar"],
            })
        cooldown = int(self._params["reentry_cooldown_bars"])
        if cooldown > 0 and (stop_context is not None or "stop" in reason or was_protective):
            index = self._session_index.get(self._current_day())
            if index is not None:
                # Selection looks up the previous completed session.  Therefore
                # ``i + N`` blocks exactly N completed sessions after a stop fill.
                self._cooldowns[symbol] = index + cooldown

    def on_partially_filled_order(self, position: Any, order: Order, price: float,
                                  quantity: float, multiplier: float) -> None:
        """Additive journaling only: never promotes a partial fill."""
        symbol = getattr(getattr(order, "asset", None), "symbol", None)
        meta = self._order_meta(order)
        cumulative = self._cumulative_filled_quantity(order, quantity)
        order_id = str(getattr(order, "identifier", "") or "")
        pending = getattr(self, "_pending_buys", {}).get(symbol) if symbol is not None else None
        if isinstance(pending, Mapping):
            pending["cumulative_filled_quantity"] = float(cumulative)
            pending["last_fill_event_quantity"] = float(quantity)
        event = {
            "event": "partial_fill",
            "symbol": symbol,
            "side": "buy" if order.is_buy_order() else "sell",
            "order_id": order_id,
            "broker_order_id": order_id,
            "fill_event_price": float(price),
            "fill_event_quantity": float(quantity),
            "cumulative_filled_quantity": float(cumulative),
            "intent_id": meta.get("intent_id"),
            "decision_id": meta.get("decision_id"),
        }
        self._record_event(event, lifecycle=True)
        self._persistence_checkpoint("partial_fill")

    def on_new_order(self, order: Order) -> None:
        symbol = getattr(getattr(order, "asset", None), "symbol", None)
        meta = self._order_meta(order)
        self._record_lifecycle_event({
            "event": "order_submitted",
            "symbol": symbol,
            "side": "buy" if order.is_buy_order() else "sell",
            "order_id": str(getattr(order, "identifier", "") or ""),
            "broker_order_id": str(getattr(order, "identifier", "") or ""),
            "intent_id": meta.get("intent_id"),
            "decision_id": meta.get("decision_id"),
        })
        self._persistence_checkpoint("on_new_order")

    def on_canceled_order(self, order: Order) -> None:
        symbol = getattr(getattr(order, "asset", None), "symbol", None)
        meta = self._order_meta(order)
        # A canceled order terminates its intent; reconcile any pending state.
        pending = getattr(self, "_pending_buys", {}).pop(symbol, None)
        if pending is not None:
            getattr(self, "_cancelled_pending_buys", {})[symbol] = pending
        sell_meta = self._state_dict("_pending_sell_meta").pop(symbol, None)
        if sell_meta is not None:
            getattr(self, "_pending_sells", set()).discard(symbol)
            getattr(self, "_pending_sell_reason", {}).pop(symbol, None)
        self._protective.pop(symbol, None)
        self._state_dict("_protective_meta").pop(symbol, None)
        self._record_lifecycle_event({
            "event": "order_canceled",
            "symbol": symbol,
            "side": "buy" if order.is_buy_order() else "sell",
            "order_id": str(getattr(order, "identifier", "") or ""),
            "broker_order_id": str(getattr(order, "identifier", "") or ""),
            "intent_id": meta.get("intent_id"),
            "decision_id": meta.get("decision_id"),
        })
        self._persistence_checkpoint("on_canceled_order")

    def on_error_order(self, order: Order, error: Any = None) -> None:
        """Record a sanitized rejection and reconcile pending state safely.

        Only the exception *class* is recorded -- never arbitrary broker text.
        Rejections terminate the order's intent and must never leave strategy
        state claiming an order or a protection that the broker does not have.
        """
        symbol = getattr(getattr(order, "asset", None), "symbol", None)
        meta = self._order_meta(order)
        was_protective = order in (getattr(self, "_protective", {}) or {}).values()
        is_buy = bool(order.is_buy_order())
        reason_class = type(error).__name__ if error is not None else "BrokerRejection"

        if was_protective:
            self._protective.pop(symbol, None)
            self._state_dict("_protective_meta").pop(symbol, None)
            self._stop_gap_events.append({
                "event": "protective_order_rejected_uncovered_position",
                "symbol": symbol,
                "broker_order_id": str(getattr(order, "identifier", "") or ""),
                "reason_class": reason_class,
            })
        elif is_buy:
            pending = getattr(self, "_pending_buys", {}).pop(symbol, None)
            if pending is not None:
                getattr(self, "_cancelled_pending_buys", {}).pop(symbol, None)
        else:
            # Ordinary sell rejection: clear the exit intent but keep the position.
            sell_meta = self._state_dict("_pending_sell_meta").pop(symbol, None)
            getattr(self, "_pending_sells", set()).discard(symbol)
            getattr(self, "_pending_sell_reason", {}).pop(symbol, None)
            self._stop_exit_context.pop(symbol, None)
            if sell_meta is not None:
                meta = {**meta, **{k: v for k, v in sell_meta.items() if v is not None}}

        self._record_lifecycle_event({
            "event": "order_rejected",
            "symbol": symbol,
            "side": "buy" if is_buy else "sell",
            "order_id": str(getattr(order, "identifier", "") or ""),
            "broker_order_id": str(getattr(order, "identifier", "") or ""),
            "intent_id": meta.get("intent_id"),
            "decision_id": meta.get("decision_id"),
            "reason_class": reason_class,
        })
        self._persistence_checkpoint("on_error_order")

    def _current_day(self) -> Any:
        return pd.Timestamp(self.get_datetime()).date()


# --- metrics ------------------------------------------------------------------

def load_session_end_equity(stats_path: Path, *, tz: Any = ET) -> dict[str, float]:
    """Return one genuine session-end equity per New York session.

    The last observation in each session is used; intermediate observations are
    never synthesized or replaced by a repeated final value.
    """
    frame = pd.read_csv(stats_path, usecols=["datetime", "portfolio_value"])
    stamps = pd.to_datetime(frame["datetime"], utc=True).dt.tz_convert(tz)
    frame = frame.assign(session=stamps.dt.date, stamp=stamps)
    daily = frame.sort_values("stamp").groupby("session", sort=True)["portfolio_value"].last()
    return {str(session): float(value) for session, value in daily.items()}


def standard_metrics(stats_path: Path, *, initial_cash: float = INITIAL_CASH) -> dict[str, Any]:
    """Compute standard daily metrics from LumiBot's saved equity curve.

    ``initial_cash`` anchors total return and CAGR.  It defaults to the engine's
    starting budget and is explicit so a synthetic curve can be measured against
    its own first value.
    """
    curve = load_session_end_equity(stats_path)
    equity = pd.Series(curve, dtype="float64")
    equity.index = pd.to_datetime(equity.index)
    returns = equity.pct_change().dropna()
    sessions = int(len(equity))
    total_return = float(equity.iloc[-1] / initial_cash - 1.0) if sessions else float("nan")
    span_years = max((equity.index[-1] - equity.index[0]).days / 365.25, 1e-9) if sessions > 1 else 1e-9
    cagr = float((equity.iloc[-1] / initial_cash) ** (1.0 / span_years) - 1.0) if sessions > 1 else float("nan")
    volatility = float(returns.std(ddof=1) * math.sqrt(252.0)) if len(returns) > 1 else 0.0
    # Standard arithmetic excess-return Sharpe, zero risk-free convention.
    sharpe = float(returns.mean() / returns.std(ddof=1) * math.sqrt(252.0)) if len(returns) > 1 and returns.std(ddof=1) else 0.0
    drawdown = equity / equity.cummax() - 1.0
    return {
        "sessions": sessions,
        "first_session": str(equity.index[0].date()) if sessions else None,
        "last_session": str(equity.index[-1].date()) if sessions else None,
        "final_equity": float(equity.iloc[-1]) if sessions else float("nan"),
        "total_return": total_return,
        "cagr": cagr,
        "volatility": volatility,
        "sharpe": sharpe,
        "max_drawdown": float(-drawdown.min()) if sessions else float("nan"),
        "sharpe_convention": "sqrt(252)*mean(daily returns)/std(daily returns), zero risk-free",
    }


def validate_result(payload: Mapping[str, Any], *, tolerance_cash: float = 1.0) -> tuple[str, ...]:
    """Return a tuple of invariant violations; empty means the run passed."""
    problems: list[str] = []
    metrics = payload.get("metrics") or {}
    if not metrics:
        problems.append("missing metrics")
        return tuple(problems)
    sessions = int(metrics.get("sessions") or 0)
    if sessions < 100:
        problems.append(f"only {sessions} sessions")
    final_equity = metrics.get("final_equity")
    if final_equity is None or not math.isfinite(float(final_equity)) or float(final_equity) <= 0.0:
        problems.append(f"nonpositive or nonfinite final equity: {final_equity!r}")
    for key in ("total_return", "cagr", "volatility", "sharpe", "max_drawdown"):
        value = metrics.get(key)
        if value is None or not math.isfinite(float(value)):
            problems.append(f"nonfinite {key}")
    minima = payload.get("invariants") or {}
    if minima.get("min_cash") is not None and float(minima["min_cash"]) < -tolerance_cash:
        problems.append(f"cash went negative: {minima['min_cash']}")
    if minima.get("nan_fills"):
        problems.append("nonfinite fill price")
    return tuple(problems)


def validate_warnings(payload: Mapping[str, Any]) -> tuple[str, ...]:
    """Diagnostics that are visible but never invalidate an otherwise clean run."""
    warnings: list[str] = []
    metrics = payload.get("metrics") or {}
    if float(metrics.get("sharpe", 0.0)) == 0.0 and int(metrics.get("sessions") or 0) > 100:
        warnings.append("zero_sharpe_flag")
    return tuple(warnings)


@dataclass(frozen=True)
class CandidateRun:
    """Artifact record for one candidate and window."""

    candidate_id: str
    window: str
    out_dir: Path
    payload: Mapping[str, Any]
    problems: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.problems


def _write_payload(payload: dict[str, Any], prefix: Path) -> None:
    tmp_path = prefix.with_name(prefix.name + "_result.json.tmp")
    final_path = prefix.with_name(prefix.name + "_result.json")
    tmp_path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    tmp_path.replace(final_path)


def _run_engine(
    *,
    strategy_class: type[Strategy],
    inputs: PreparedInputs,
    params: Mapping[str, Any],
    prefix: Path,
    tearsheet_dir: Path | None = None,
) -> tuple[Strategy, float]:
    started = time.perf_counter()
    tearsheet_path = None
    tearsheet_metrics_path = None
    if tearsheet_dir is not None:
        tearsheet_path = str(tearsheet_dir / "tearsheet.html")
        tearsheet_metrics_path = str(tearsheet_dir / "tearsheet_metrics.json")
    _, strategy = strategy_class.run_backtest(
        datasource_class=PandasDataBacktesting,
        pandas_data=list(inputs.lumibot_data),
        backtesting_start=inputs.window.start_dt,
        backtesting_end=inputs.window.end_dt,
        sleeptime="1H",
        minutes_before_opening=0,
        minutes_before_closing=0,
        stats_file=str(prefix.with_name(prefix.name + "_stats.csv")),
        trades_file=str(prefix.with_name(prefix.name + "_trades.csv")),
        settings_file=str(prefix.with_name(prefix.name + "_settings.json")),
        benchmark_asset=None,
        budget=INITIAL_CASH,
        risk_free_rate=0.0,
        parameters={
            "cash_financing": {
                "enabled": True,
                "account_mode": "margin",
                "day_count_basis": 365,
                "credit_rate_annual": 0.0,
                "debit_rate_annual": 0.0,
            }
        },
        buy_trading_fees=[TradingFee(percent_fee=float(params["cost_bps_per_side"]) / 10_000.0)],
        sell_trading_fees=[TradingFee(percent_fee=float(params["cost_bps_per_side"]) / 10_000.0)],
        auto_adjust=False,
        analyze_backtest=True,
        show_plot=False,
        show_tearsheet=False,
        save_tearsheet=tearsheet_dir is not None,
        tearsheet_file=tearsheet_path,
        tearsheet_metrics_file=tearsheet_metrics_path,
        show_indicators=False,
        save_logfile=False,
        show_progress_bar=False,
        quiet_logs=True,
    )
    return strategy, time.perf_counter() - started


def _resolved_parameters_hash(params: Mapping[str, Any]) -> str:
    """Hash the semantic parameter map without candidate identity metadata."""
    encoded = json.dumps(dict(params), sort_keys=True, separators=(",", ":"), default=str)
    return sha256(encoded.encode("utf-8")).hexdigest()


def _position_pnl_records(
    trades: pd.DataFrame,
    *,
    strategy: Strategy,
    inputs: PreparedInputs | None,
    window: ExperimentWindow,
) -> list[dict[str, Any]]:
    """Create auditable FIFO position PnL records from filled broker trades.

    This is artifact construction, not policy: costs are allocated to each
    closed lot and any still-open strategy position is explicitly terminally
    marked using the final available completed hourly close.
    """
    if "status" not in trades:
        return []
    filled = trades[trades["status"].astype(str).str.lower().eq("fill")].copy()
    if filled.empty:
        return []
    filled = filled.sort_values("time") if "time" in filled else filled
    lots: dict[str, list[dict[str, float | str]]] = {}
    records: list[dict[str, Any]] = []
    for _, row in filled.iterrows():
        symbol = str(row.get("symbol", ""))
        side = str(row.get("side", "")).lower()
        quantity = float(pd.to_numeric(row.get("filled_quantity"), errors="coerce") or 0.0)
        price = float(pd.to_numeric(row.get("price"), errors="coerce") or float("nan"))
        fee = float(pd.to_numeric(row.get("trade_cost"), errors="coerce") or 0.0)
        if not symbol or quantity <= 0.0 or not math.isfinite(price):
            continue
        if side == "buy":
            lots.setdefault(symbol, []).append({
                "quantity": quantity,
                "price": price,
                "fee": fee,
                "time": str(row.get("time", "")),
            })
            continue
        if side != "sell":
            continue
        remaining = quantity
        while remaining > 1e-12 and lots.get(symbol):
            lot = lots[symbol][0]
            lot_quantity = float(lot["quantity"])
            matched = min(remaining, lot_quantity)
            fraction = matched / lot_quantity
            entry_fee = float(lot["fee"]) * fraction
            exit_fee = fee * (matched / quantity)
            records.append({
                "symbol": symbol,
                "entry_time": lot["time"],
                "exit_time": str(row.get("time", "")),
                "quantity": matched,
                "entry_price": float(lot["price"]),
                "exit_price": price,
                "entry_fee": entry_fee,
                "exit_fee": exit_fee,
                "net_pnl": matched * (price - float(lot["price"])) - entry_fee - exit_fee,
                "terminal_marked": False,
            })
            remaining -= matched
            lot["quantity"] = lot_quantity - matched
            lot["fee"] = float(lot["fee"]) - entry_fee
            if float(lot["quantity"]) <= 1e-12:
                lots[symbol].pop(0)
    terminal = getattr(strategy, "_positions", {})
    for symbol, open_lots in lots.items():
        state = terminal.get(symbol)
        if state is None or inputs is None:
            continue
        # Alternatives carry a daily-only inputs object (no hourly array); the
        # terminal mark needs whichever cadence the candidate actually traded.
        mark_frame = getattr(inputs, "hourly", None) or getattr(inputs, "daily", None)
        frame = (mark_frame or {}).get(symbol) if mark_frame else None
        if frame is None or frame.empty:
            continue
        before_end = frame.loc[:pd.Timestamp(window.end) + pd.Timedelta(hours=15)]
        if before_end.empty:
            continue
        mark = float(before_end.iloc[-1]["close"])
        for lot in open_lots:
            quantity = float(lot["quantity"])
            records.append({
                "symbol": symbol,
                "entry_time": lot["time"],
                "exit_time": None,
                "quantity": quantity,
                "entry_price": float(lot["price"]),
                "exit_price": mark,
                "entry_fee": float(lot["fee"]),
                "exit_fee": 0.0,
                "net_pnl": quantity * (mark - float(lot["price"])) - float(lot["fee"]),
                "terminal_marked": True,
            })
    return records


def build_payload(
    *,
    candidate: CandidateSpec,
    window: ExperimentWindow,
    strategy: Strategy,
    params: Mapping[str, Any],
    prefix: Path,
    elapsed: float,
    inputs: PreparedInputs | None,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Assemble and validate one run artifact record."""
    analysis = dict(strategy.analysis)
    metrics = standard_metrics(prefix.with_name(prefix.name + "_stats.csv"))
    trades = pd.read_csv(prefix.with_name(prefix.name + "_trades.csv"))
    stats = pd.read_csv(prefix.with_name(prefix.name + "_stats.csv"), usecols=["cash"])
    min_cash = float(stats["cash"].min()) if len(stats) else None
    filled = trades[trades["status"].astype(str).str.lower().eq("fill")] if "status" in trades else trades
    nan_fills = bool(
        len(filled)
        and not np.isfinite(pd.to_numeric(filled["price"], errors="coerce").fillna(np.nan)).all()
    )
    resolved = dict(candidate.parameters)
    overrides = dict(candidate.overrides)
    risk_cap_events = list(getattr(strategy, "_risk_cap_events", ()))
    entry_edge_events = list(getattr(strategy, "_entry_edge_events", ()))
    position_pnl_records = _position_pnl_records(
        trades, strategy=strategy, inputs=inputs, window=window,
    )
    payload: dict[str, Any] = {
        "candidate_id": candidate.candidate_id,
        "name": candidate.name,
        "family_id": candidate.family_id,
        "kind": candidate.kind,
        "parent_candidate_id": candidate.parent_candidate_id,
        "fingerprint": candidate.fingerprint(),
        "resolved_parameters_hash": _resolved_parameters_hash(resolved),
        "resolved_parameters": resolved,
        "override_parameters": overrides,
        "window": {"label": window.label, "start": window.start, "end": window.end},
        "universe": inputs.universe_keyword if inputs is not None else list(params.get("universe_symbols", ())),
        "symbols": list(inputs.ordered_symbols) if inputs is not None else list(params.get("universe_symbols", ())),
        "feature_hash": inputs.feature_hash if inputs is not None else None,
        "execution_engine": EXECUTION_ENGINE,
        "engine": ENGINE_LABEL,
        "implementation_revision": IMPLEMENTATION_REVISION,
        "cadence": "1H",
        "cost_bps_per_side": float(params["cost_bps_per_side"]),
        "runtime_seconds": elapsed,
        "metrics": metrics,
        "lumibot_analysis": {
            **analysis,
            # LumiBot's field is CAGR over volatility; keep its own name visible.
            "cagr_over_volatility": analysis.get("sharpe"),
        },
        "fills": int(len(filled)),
        "journal_events": len(getattr(strategy, "_journal", ())),
        "diag": getattr(strategy, "_diag", [])[:12] + getattr(strategy, "_diag", [])[-12:],
        "rejections": getattr(strategy, "_rejections", [])[:50],
        "terminal_positions": {
            symbol: {key: value for key, value in state.items() if key != "entry_atr"}
            for symbol, state in getattr(strategy, "_positions", {}).items()
        },
        "invariants": {"min_cash": min_cash, "nan_fills": nan_fills},
        "stop_gap_event_count": len(getattr(strategy, "_stop_gap_events", ())),
        "stop_gap_events": list(getattr(strategy, "_stop_gap_events", ())),
        "risk_off_transitions": list(getattr(strategy, "_risk_off_transitions", ())),
        "risk_off_flatten_count": int(getattr(strategy, "_risk_off_flatten_count", 0)),
        "risk_contribution_events": risk_cap_events,
        "risk_contribution_cap_binding_count": sum(1 for event in risk_cap_events if event.get("binding")),
        "entry_edge_events": entry_edge_events,
        "entry_edge_rejection_count": sum(1 for event in entry_edge_events if not event.get("accepted")),
        "minimum_hold_deferral_count": int(getattr(strategy, "_minimum_hold_deferrals", 0)),
        "deferred_rebalance_events": list(getattr(strategy, "_deferred_rebalance_events", ())),
        "position_pnl_records": position_pnl_records,
    }
    # One common serializer feeds both native artifacts and the live writer so
    # the two schemas cannot drift.
    try:
        payload["strategy_events"] = build_strategy_event_payload(
            strategy, session=str(window.end) if window is not None else None,
        )
    except Exception as error:  # audit must never fail a completed backtest
        payload["strategy_events"] = None
        payload["strategy_events_error"] = type(error).__name__
    if candidate.kind in {"control", "hts", KIND_HTS_V2}:
        payload["hour_mapping_convention"] = HTS_HOURLY_CONVENTION
    if candidate.kind == KIND_HTS_V2:
        payload["v2_contract"] = {
            "clock_hour": HTS_HOURLY_CONVENTION,
            "cost_bps_per_side": float(params["cost_bps_per_side"]),
            "cost_role": "primary" if float(params["cost_bps_per_side"]) == 3.5 else "stress",
            "daily_sharpe": "independently recomputed arithmetic daily-return Sharpe",
            "lumibot_sharpe": "cagr_over_volatility",
            "rebalance_hour_15": (
                "native callback 14:00 plans from the completed 14:00 bar; because the engine has no "
                "15:00 callback, the strategy submits at the next native bar and run_trades.csv is the "
                "authoritative actual fill timestamp"
            ),
        }
    if extra:
        payload.update(dict(extra))
    problems = validate_result(payload)
    payload["problems"] = list(problems)
    payload["warnings"] = list(validate_warnings(payload))
    journal = getattr(strategy, "_journal", None)
    if journal:
        payload["journal_tail"] = list(journal[-20:])
    return payload


def run_candidate(
    candidate: CandidateSpec,
    window: ExperimentWindow,
    out_dir: Path,
    *,
    control_baseline: Mapping[str, Any],
    tearsheet_dir: Path | None = None,
) -> CandidateRun:
    """Run one candidate over one window on the native engine."""
    params = dict(candidate.parameters)
    if candidate.kind == KIND_ALTERNATIVE:
        from strategy_lab.native_alternatives import run_alternative_candidate

        return run_alternative_candidate(candidate, window, out_dir)

    # V2 adds a declared surface without widening the frozen v1 baseline.
    # Passing the combined known map here lets a direct V2 smoke run fail only
    # for an actually unimplemented enum, not for a valid v2 ParameterSpec.
    known_baseline = dict(control_baseline)
    if candidate.kind == KIND_HTS_V2:
        from strategy_lab.hts_variants import HTS_V2_BASELINE

        known_baseline.update(HTS_V2_BASELINE)
    missing = check_supported(params, known_baseline)
    if missing:
        raise UnsupportedCandidateError(
            f"{candidate.candidate_id} needs unimplemented mechanism(s): {', '.join(missing)}"
        )

    global _CONTEXT
    run_dir = Path(out_dir) / candidate.candidate_id / window.label
    run_dir.mkdir(parents=True, exist_ok=True)
    inputs = prepare_inputs(params, window)
    _CONTEXT = RunContext(
        inputs=inputs,
        params=params,
        candidate_id=candidate.candidate_id,
        fingerprint=candidate.fingerprint(),
    )
    prefix = run_dir / "run"
    try:
        strategy, elapsed = _run_engine(
            strategy_class=RegistryHtsStrategy, inputs=inputs, params=params, prefix=prefix,
            tearsheet_dir=tearsheet_dir,
        )
        payload = build_payload(
            candidate=candidate, window=window, strategy=strategy, params=params,
            prefix=prefix, elapsed=elapsed, inputs=inputs,
        )
    finally:
        _CONTEXT = None
    _write_payload(payload, prefix)
    return CandidateRun(
        candidate_id=candidate.candidate_id,
        window=window.label,
        out_dir=run_dir,
        payload=payload,
        problems=tuple(payload["problems"]),
    )
