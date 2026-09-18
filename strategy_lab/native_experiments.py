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
from datetime import date, datetime
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
    market_gate_open,
    rank_scores,
    risk_off_gate_open,
    schedule_due,
    select_holdings,
    target_weights,
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
WINDOWS: tuple[ExperimentWindow, ...] = (WINDOW_SIX_YEAR, WINDOW_TWO_YEAR, WINDOW_PRE_2020)

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
        return selection.holdings

    # -- orders ---------------------------------------------------------------
    def _cancel_protective(self, symbol: str) -> None:
        order = self._protective.pop(symbol, None)
        if order is None:
            return
        try:
            filled = getattr(order, "is_filled", None)
            if not (order.is_canceled() or (callable(filled) and filled())):
                self.cancel_order(order)
                self._journal.append({"event": "protective_cancel", "symbol": symbol})
        except Exception:
            pass

    def _submit_sell(
        self,
        symbol: str,
        reason: str,
        reference: float,
        *,
        stop_context: Mapping[str, Any] | None = None,
    ) -> None:
        state = self._positions.get(symbol)
        if state is None or symbol in self._pending_sells:
            return
        self._cancel_protective(symbol)
        order = self.create_order(symbol, quantity=state["quantity"], side="sell", order_type="market")
        self._pending_sells.add(symbol)
        self._pending_sell_reason[symbol] = reason
        if stop_context is not None:
            context = dict(stop_context)
            context["order_id"] = str(order.identifier)
            self._stop_exit_context[symbol] = context
            self._lifecycle_trace.append({
                "event": "virtual_stop_submitted",
                "symbol": symbol,
                "order_id": context["order_id"],
                "engine_time": context["engine_time"],
                "completed_source_bar": context["trigger_timestamp"],
                "submission_time": context["submission_time"],
                "source_fill_bar": context["source_fill_bar"],
            })
        self.submit_order(order)
        self._journal.append({"event": "intent", "side": "sell", "symbol": symbol,
                              "reason": reason, "reference": reference})

    def _submit_buy(self, symbol: str, quantity: float, reference: float, atr: float, reason: str) -> None:
        if symbol in self._pending_buys:
            return
        order = self.create_order(symbol, quantity=quantity, side="buy", order_type="market")
        self._pending_buys[symbol] = {"quantity": quantity, "atr": atr, "order": order}
        self.submit_order(order)
        self._journal.append({"event": "intent", "side": "buy", "symbol": symbol,
                              "quantity": quantity, "reason": reason, "reference": reference})

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
        self.submit_order(order)
        self._protective[symbol] = order
        state["protective_level"] = float(level)
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
            self._lifecycle_trace.append({
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
        return apply_leveraged_cap(
            weights,
            leveraged_symbols=[symbol for symbol in selected if symbol in LEVERAGED_PRODUCTS],
            cap=self._params["leveraged_cap"],
        )

    def _rebalance(self, day: Any, hour: int) -> None:
        if bool(getattr(self, "_risk_off_active", False)):
            self._selected = ()
            self._flatten_risk_off(day, hour)
            return
        stamp = self._stamp(day, hour)
        target = set(self._selected)
        for symbol in list(self._positions):
            if symbol not in target:
                price = self._execution_price(symbol, stamp)
                self._submit_sell(symbol, "selection_change",
                                  price if price is not None else float("nan"))
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
            return
        occupied = set(self._positions) | set(self._pending_buys)
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
            budget -= quantity * effective_price
            self._diag[-1]["buys"].append({
                "symbol": symbol, "price": price, "quantity": quantity,
                "notional": quantity * price, "budget_after": budget,
            })
            self._submit_buy(symbol, quantity, price, atr, "selection_entry")

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

        def capture_buy(symbol: str, quantity: float, reference: float, atr: float, reason: str) -> None:
            captured_buys.append({
                "symbol": symbol,
                "quantity": float(quantity),
                "reference": float(reference),
                "atr": float(atr),
                "reason": reason,
            })

        def capture_sell(
            symbol: str,
            reason: str,
            reference: float,
            *,
            stop_context: Mapping[str, Any] | None = None,
        ) -> None:
            if symbol not in self._positions or symbol in self._pending_sells:
                return
            # Match `_rebalance`'s pending-exit slot invariant without sending
            # the order early.  The marker is removed before this method exits.
            self._pending_sells.add(symbol)
            self._pending_sell_reason[symbol] = reason
            synthetic_pending_sells.add(symbol)
            captured_sells.append({"symbol": symbol, "reason": reason, "reference": float(reference)})

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
            "logical_rebalance_hour": logical_hour,
            "buys": captured_buys,
            "sells": captured_sells,
        }
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
        self._deferred_rebalance = {
            "kind": "risk_off_flatten",
            "decision_day": str(day),
            "logical_rebalance_hour": logical_hour,
        }
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
        if intent["kind"] == "risk_off_flatten":
            self._selected = ()
            self._flatten_risk_off(day, hour)
            event = {
                "event": "deferred_risk_off_flatten_submitted",
                "decision_day": intent["decision_day"],
                "actual_submission_time": stamp.isoformat(),
                "source_fill_bar": stamp.isoformat(),
            }
            self._deferred_rebalance_events.append(event)
            self._journal.append(event)
            self._deferred_rebalance = None
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
                self._submit_sell(symbol, str(sell["reason"]), price if price is not None else float("nan"))

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
            self._submit_buy(symbol, quantity, price, float(buy["atr"]), str(buy["reason"]))
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
                self._selected = self._select(day)
                self._signal_day = day
            if rebalance_hour in REBALANCE_ITERATION_HOUR_MAP:
                self._queue_rebalance_after_last_observable_iteration(day, rebalance_hour)
            else:
                self._rebalance(day, rebalance_hour)

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
            self._positions[symbol] = {
                "quantity": float(quantity),
                "entry_price": float(price),
                "entry_atr": atr,
                "entry_session": self._current_day(),
                "entry_session_index": self._session_index.get(self._current_day()),
                "stop": float(price) - float(self._params["atr_k"]) * atr,
                "highest_high": float(price),
                "post_entry_highs": [],
                "breach_count": 0,
                "seeded": mode not in ("chandelier-since-entry", "chandelier-14-bar"),
            }
            self._journal.append({"event": "fill", "side": "buy", "symbol": symbol,
                                  "price": float(price), "quantity": float(quantity)})
            self._place_protective_stop(symbol)
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
        reason = self._pending_sell_reason.pop(symbol, "protective_stop" if was_protective else "unknown")
        stop_context = self._stop_exit_context.pop(symbol, None)
        self._pending_sells.discard(symbol)
        self._positions.pop(symbol, None)
        self._journal.append({"event": "fill", "side": "sell", "symbol": symbol,
                              "price": float(price), "quantity": float(quantity), "reason": reason})
        if stop_context is not None:
            event = build_virtual_stop_gap_event(
                stop_context,
                fill_time=pd.Timestamp(self.get_datetime()),
                fill_price=float(price),
                fill_quantity=float(quantity),
                fill_session=self._current_day(),
            )
            self._stop_gap_events.append(event)
            self._lifecycle_trace.append({
                "event": "virtual_stop_filled",
                "symbol": symbol,
                "order_id": event["order_id"],
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

    def _current_day(self) -> Any:
        return pd.Timestamp(self.get_datetime()).date()


# --- metrics ------------------------------------------------------------------

def standard_metrics(stats_path: Path, *, initial_cash: float = INITIAL_CASH) -> dict[str, Any]:
    """Compute standard daily metrics from LumiBot's saved equity curve.

    ``initial_cash`` anchors total return and CAGR.  It defaults to the engine's
    starting budget and is explicit so a synthetic curve can be measured against
    its own first value.
    """
    frame = pd.read_csv(stats_path, usecols=["datetime", "portfolio_value"])
    stamps = pd.to_datetime(frame["datetime"], utc=True).dt.tz_convert(ET)
    frame = frame.assign(session=stamps.dt.date, stamp=stamps)
    daily = frame.sort_values("stamp").groupby("session", sort=True)["portfolio_value"].last()
    daily.index = pd.to_datetime(daily.index)
    equity = daily.astype(float)
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
