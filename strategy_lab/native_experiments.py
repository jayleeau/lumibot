"""Run registered HTS research candidates on LumiBot's native backtest engine.

This is the execution layer for ``docs/HTS_100_VARIATIONS_RESEARCH_PLAN.md``.  It
reads a resolved :class:`~strategy_lab.experiment_config.CandidateSpec`, prepares
the archived bars it needs, and runs one native LumiBot backtest per candidate and
window.  LumiBot's broker and portfolio own simulated time, order lifecycle,
fills, fees, cash, and valuation; this module owns the strategy decisions and the
independent metrics.

Design rules:

* A candidate is only run when this module actually implements every mechanism it
  asks for.  :func:`check_supported` returns the mechanisms that are not yet
  implemented, and the runner reports those candidates as ``unsupported`` rather
  than silently running them with control behaviour and calling the result H031.
* Metrics are computed here from the saved equity curve, independent of LumiBot's
  own analysis.  LumiBot's ``sharpe`` is ``(CAGR - risk_free) / volatility``; it is
  reported as ``cagr_over_volatility`` and never as a Sharpe ratio.
* The hourly bar mapping is the same convention the existing native runner uses.
  It is a declared convention, not yet a verified statement of bar completion
  times; see section 2 of the plan.
"""
from __future__ import annotations

import bisect
import json
import math
import os
import time
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from typing import Any, Mapping, Sequence

# Mark the process as a backtest before importing LumiBot so repository-adjacent
# .env files cannot create a live broker or start a trading stream.
os.environ.setdefault("IS_BACKTESTING", "true")
os.environ.setdefault("LUMIBOT_DISABLE_DOTENV", "true")

import duckdb
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]

from lumibot.backtesting import PandasDataBacktesting  # noqa: E402
from lumibot.entities import Asset, Data, Order, TradingFee  # noqa: E402
from lumibot.strategies import Strategy  # noqa: E402

from strategy_lab.experiment_config import CandidateSpec  # noqa: E402
from strategy_lab.experiment_universes import resolve_universe  # noqa: E402

DAILY_DB = ROOT / "short" / "suite_monitored_xnas_itch_daily_adjusted.duckdb"
HOURLY_DB = ROOT / "short" / "suite_v2_xnas_itch_hourly_adjusted.duckdb"
ET = "America/New_York"
USD = Asset("USD", "forex")
INITIAL_CASH = 100_000.0
WARMUP_DAYS = 500


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
WINDOWS: tuple[ExperimentWindow, ...] = (WINDOW_SIX_YEAR, WINDOW_TWO_YEAR)
WINDOW_BY_LABEL = {window.label: window for window in WINDOWS}


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


def _daily_features(
    frame: pd.DataFrame,
    *,
    trend_sma: int,
    return_period: int,
    liquidity_period: int,
) -> pd.DataFrame:
    close = frame["close"]
    result = pd.DataFrame(index=frame.index)
    result["close"] = close
    result["sma"] = close.rolling(trend_sma).mean()
    result["ret"] = close / close.shift(return_period) - 1.0
    result["mdv"] = (close * frame["volume"]).rolling(liquidity_period).median()
    return result


def _hourly_features(frame: pd.DataFrame, *, atr_period: int) -> pd.DataFrame:
    previous_close = frame["close"].shift(1)
    true_range = np.maximum(
        frame["high"] - frame["low"],
        np.maximum((frame["high"] - previous_close).abs(), (frame["low"] - previous_close).abs()),
    )
    result = frame.copy()
    result["atr"] = pd.Series(true_range, index=frame.index, dtype="float64").rolling(atr_period).mean()
    return result


def _lumibot_hourly(frame: pd.DataFrame) -> pd.DataFrame:
    """Map archived 09:00-15:00 ET bars onto NYSE hourly timestamps.

    This is the same convention ``scripts/run_full_lumibot_backtests.py`` uses so
    results remain comparable.  The source bars' contents are not validated here;
    that remains the open bar-cleanliness item in the plan.
    """
    source = frame[(frame.index.hour >= 9) & (frame.index.hour <= 15)].copy()
    if source.empty:
        return source
    source.index = source.index.normalize() + pd.to_timedelta(source.index.hour - 9, unit="h")
    source.index = source.index + pd.Timedelta(hours=9, minutes=30)
    return source[["open", "high", "low", "close", "volume"]]


@dataclass(frozen=True)
class PreparedInputs:
    """Everything one candidate needs to run, prepared once per window."""

    window: ExperimentWindow
    universe_keyword: str
    ordered_symbols: tuple[str, ...]
    daily: Mapping[str, pd.DataFrame]
    hourly: Mapping[str, pd.DataFrame]
    lumibot_data: tuple[Data, ...]
    feature_hash: str


def prepare_inputs(params: Mapping[str, Any], window: ExperimentWindow) -> PreparedInputs:
    """Load and prepare the bars a candidate needs for one window."""
    universe_keyword = str(params["universe"])
    symbols = list(resolve_universe(universe_keyword))
    daily_raw = _sql_frames(DAILY_DB, "bars_daily", symbols, window.warmup_start, window.end, hourly=False)
    hourly_raw = _sql_frames(HOURLY_DB, "bars_hourly", symbols, window.warmup_start, window.end, hourly=True)
    ordered = tuple(symbol for symbol in symbols if symbol in daily_raw and symbol in hourly_raw)

    trend_sma = int(params["trend_sma"])
    return_period = int(params["return_period"])
    liquidity_period = int(params["liquidity_period"])
    atr_period = int(params["atr_period"])

    daily = {
        symbol: _daily_features(daily_raw[symbol], trend_sma=trend_sma,
                                return_period=return_period, liquidity_period=liquidity_period)
        for symbol in ordered
    }
    hourly = {symbol: _hourly_features(hourly_raw[symbol], atr_period=atr_period) for symbol in ordered}
    lumibot_data = tuple(
        Data(Asset(symbol), _lumibot_hourly(hourly_raw[symbol]), timestep="hour", quote=USD)
        for symbol in ordered
    )
    payload = {
        "window": [window.label, window.start, window.end],
        "universe": universe_keyword,
        "symbols": list(ordered),
        "lookbacks": [trend_sma, return_period, liquidity_period, atr_period],
        "daily_rows": int(sum(len(frame) for frame in daily.values())),
        "hourly_rows": int(sum(len(frame) for frame in hourly.values())),
    }
    feature_hash = sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    return PreparedInputs(
        window=window,
        universe_keyword=universe_keyword,
        ordered_symbols=ordered,
        daily=daily,
        hourly=hourly,
        lumibot_data=lumibot_data,
        feature_hash=feature_hash,
    )


# --- mechanism coverage -------------------------------------------------------

# Parameters this module honours for every value.  Anything not listed here must
# still sit at its control value, or the candidate is reported unsupported.
# Every entry is read by the strategy or the feature preparation, so the gate
# never reports a knob as supported that nothing consumes.
FLEXIBLE_PARAMETERS = frozenset({
    "universe",              # resolve_universe + prepare_inputs
    "top_n",                 # _select cap and _rebalance slot sizing
    "trend_sma",             # _daily_features
    "return_period",         # _daily_features
    "atr_period",            # _hourly_features
    "atr_k",                 # stop level and ratchet
    "liquidity_period",      # _daily_features median dollar volume window
    "min_median_dollar_volume",  # _select eligibility
    "require_positive_return",   # _select eligibility
    "signal_hour",           # on_trading_iteration
    "rebalance_hour",        # on_trading_iteration
    "gross_target",          # _rebalance slot sizing
    "cost_bps_per_side",     # trading fee in run_candidate
})


def check_supported(params: Mapping[str, Any], control_baseline: Mapping[str, Any]) -> tuple[str, ...]:
    """Return the mechanisms this candidate needs but this module does not implement."""
    missing: list[str] = []
    for name, value in params.items():
        if name in FLEXIBLE_PARAMETERS:
            continue
        if name not in control_baseline:
            missing.append(f"{name}={value!r} (unknown parameter)")
            continue
        if value != control_baseline[name]:
            missing.append(f"{name}={value!r}")
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


class RegistryHtsStrategy(Strategy):
    """Control-contract HTS logic with the flexible parameters exposed."""

    def initialize(self) -> None:
        self.set_market("NYSE")
        self.sleeptime = "1H"
        context = _CONTEXT
        if context is None:
            raise RuntimeError("run context was not initialized")
        self._ctx = context
        self._params = context.params
        self._universe_order = {symbol: index for index, symbol in enumerate(context.inputs.ordered_symbols)}
        self._sessions = sorted({day for frame in context.inputs.daily.values() for day in frame.index.date})
        self._positions: dict[str, dict[str, Any]] = {}
        self._pending_buys: dict[str, dict[str, Any]] = {}
        self._pending_sells: set[str] = set()
        self._selected: tuple[str, ...] = ()
        self._signal_day: Any = None
        self._journal: list[dict[str, Any]] = []
        self._rejections: list[dict[str, Any]] = []
        self._diag: list[dict[str, Any]] = []

    # -- data helpers ---------------------------------------------------------
    def _daily_row(self, symbol: str, session: Any) -> pd.Series | None:
        frame = self._ctx.inputs.daily.get(symbol)
        if frame is None:
            return None
        try:
            # A ``datetime.date`` key raises KeyError against a DatetimeIndex, so
            # normalize to midnight Timestamp before the lookup.
            row = frame.loc[pd.Timestamp(session)]
        except KeyError:
            return None
        return row if isinstance(row, pd.Series) else row.iloc[-1]

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

    # -- selection ------------------------------------------------------------
    def _select(self, day: Any) -> tuple[str, ...]:
        session = self._previous_session(day)
        if session is None:
            return ()
        min_mdv = float(self._params["min_median_dollar_volume"])
        require_positive = bool(self._params["require_positive_return"])
        ranked: list[tuple[str, float]] = []
        for symbol in self._ctx.inputs.ordered_symbols:
            row = self._daily_row(symbol, session)
            if row is None:
                continue
            close, sma, ret, mdv = row["close"], row["sma"], row["ret"], row["mdv"]
            if any(pd.isna(value) for value in (close, sma, ret, mdv)):
                continue
            if not float(close) > float(sma):
                continue
            if float(mdv) < min_mdv:
                continue
            if require_positive and float(ret) <= 0.0:
                continue
            ranked.append((symbol, float(ret)))
        # Deterministic ties by frozen universe order.
        ranked.sort(key=lambda item: (-item[1], self._universe_order[item[0]]))
        return tuple(symbol for symbol, _score in ranked[: int(self._params["top_n"])])

    # -- orders ---------------------------------------------------------------
    def _submit_sell(self, symbol: str, reason: str, reference: float) -> None:
        state = self._positions.get(symbol)
        if state is None or symbol in self._pending_sells:
            return
        order = self.create_order(symbol, quantity=state["quantity"], side="sell", order_type="market")
        self._pending_sells.add(symbol)
        self.submit_order(order)
        self._journal.append({"event": "intent", "side": "sell", "symbol": symbol,
                              "reason": reason, "reference": reference})

    def _submit_buy(self, symbol: str, quantity: float, reference: float, atr: float, reason: str) -> None:
        if symbol in self._pending_buys:
            return
        order = self.create_order(symbol, quantity=quantity, side="buy", order_type="market")
        self._pending_buys[symbol] = {"quantity": quantity, "atr": atr}
        self.submit_order(order)
        self._journal.append({"event": "intent", "side": "buy", "symbol": symbol,
                              "quantity": quantity, "reason": reason, "reference": reference})

    # -- risk ----------------------------------------------------------------
    def _update_stops(self, day: Any, hour: int) -> None:
        atr_k = float(self._params["atr_k"])
        stamp = self._stamp(day, hour)
        for symbol, state in list(self._positions.items()):
            row = self._completed_row(symbol, stamp)
            if row is None:
                continue
            close, atr = float(row["close"]), float(row["atr"])
            if not math.isfinite(atr) or atr <= 0.0:
                continue
            # Compare the stored level first, then ratchet.  A revised trail can
            # never act on the bar that produced it, and a breach never fills at
            # the already-crossed level.
            if close <= float(state["stop"]):
                self._submit_sell(symbol, "stop_breach", close)
                continue
            candidate_stop = close - atr_k * atr
            if candidate_stop > float(state["stop"]):
                state["stop"] = candidate_stop
                state["stop_updated_at"] = f"{day}T{hour:02d}:00"

    # -- rebalance ------------------------------------------------------------
    def _rebalance(self, day: Any, hour: int) -> None:
        target = set(self._selected)
        stamp = self._stamp(day, hour)
        for symbol in list(self._positions):
            if symbol not in target:
                price = self._execution_price(symbol, stamp)
                reference = price if price is not None else float("nan")
                self._submit_sell(symbol, "selection_change", reference)

        # Refresh broker balances before sizing.  ``self.cash`` otherwise returns a
        # cached value, and sizing off stale cash lets a rebalance spend money that
        # an earlier fill already committed.
        try:
            self.update_broker_balances(force_update=True)
        except Exception:
            pass
        budget = max(0.0, float(self.cash or 0.0))
        portfolio_value = float(self.portfolio_value or 0.0)
        gross_target = float(self._params["gross_target"])
        top_n = int(self._params["top_n"])
        per_slot = portfolio_value * gross_target / top_n
        # Reserve the per-side fee so a fill cannot spend past the cash budget.
        fee_rate = float(self._params["cost_bps_per_side"]) / 10_000.0
        try:
            tracked = len(self.get_tracked_positions())
        except Exception:
            tracked = -1
        self._diag.append({
            "day": str(day), "hour": hour, "cash": budget, "portfolio_value": portfolio_value,
            "book_positions": sorted(self._positions), "broker_positions": tracked,
            "pending_buys": sorted(self._pending_buys), "pending_sells": sorted(self._pending_sells),
            "per_slot": per_slot, "selected": list(self._selected),
            "buys": [],
        })
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
            notional = min(per_slot, budget)
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

    # -- lifecycle ------------------------------------------------------------
    def on_trading_iteration(self) -> None:
        now = pd.Timestamp(self.get_datetime())
        day, hour = now.date(), int(now.hour)
        if hour == int(self._params["signal_hour"]):
            self._selected = self._select(day)
            self._signal_day = day
        self._update_stops(day, hour)
        if hour == int(self._params["rebalance_hour"]):
            if self._signal_day != day:
                # No completed signal bar this session; recompute causally from
                # the previous completed session instead of reusing a stale pick.
                self._selected = self._select(day)
                self._signal_day = day
            self._rebalance(day, hour)

    def on_filled_order(self, position: Any, order: Order, price: float, quantity: float,
                        multiplier: float) -> None:
        symbol = order.asset.symbol
        if order.is_buy_order():
            pending = self._pending_buys.pop(symbol, {})
            atr = float(pending.get("atr", float("nan")))
            if not math.isfinite(atr) or atr <= 0.0:
                raise RuntimeError(f"cannot establish a stop for {symbol}: no entry ATR")
            self._positions[symbol] = {
                "quantity": float(quantity),
                "entry_price": float(price),
                "stop": float(price) - float(self._params["atr_k"]) * atr,
                "entry_atr": atr,
            }
            self._journal.append({"event": "fill", "side": "buy", "symbol": symbol,
                                  "price": float(price), "quantity": float(quantity)})
            return
        self._pending_sells.discard(symbol)
        self._positions.pop(symbol, None)
        self._journal.append({"event": "fill", "side": "sell", "symbol": symbol,
                              "price": float(price), "quantity": float(quantity)})


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
    if float(metrics.get("sharpe", 0.0)) == 0.0 and sessions > 100:
        # Not a violation, but a zero Sharpe on a long run is worth surfacing.
        problems.append("zero_sharpe_flag")
    minima = payload.get("invariants") or {}
    if minima.get("min_cash") is not None and float(minima["min_cash"]) < -tolerance_cash:
        problems.append(f"cash went negative: {minima['min_cash']}")
    if minima.get("nan_fills"):
        problems.append("nonfinite fill price")
    return tuple(problems)


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


def run_candidate(
    candidate: CandidateSpec,
    window: ExperimentWindow,
    out_dir: Path,
    *,
    control_baseline: Mapping[str, Any],
) -> CandidateRun:
    """Run one candidate over one window on the native engine."""
    global _CONTEXT
    params = dict(candidate.parameters)
    missing = check_supported(params, control_baseline)
    if missing:
        raise UnsupportedCandidateError(
            f"{candidate.candidate_id} needs unimplemented mechanism(s): {', '.join(missing)}"
        )

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
    started = time.perf_counter()
    _, strategy = RegistryHtsStrategy.run_backtest(
        datasource_class=PandasDataBacktesting,
        pandas_data=list(inputs.lumibot_data),
        backtesting_start=window.start_dt,
        backtesting_end=window.end_dt,
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
        save_tearsheet=False,
        show_indicators=False,
        save_logfile=False,
        show_progress_bar=False,
        quiet_logs=True,
    )
    elapsed = time.perf_counter() - started
    analysis = dict(strategy.analysis)
    metrics = standard_metrics(prefix.with_name(prefix.name + "_stats.csv"))
    trades = pd.read_csv(prefix.with_name(prefix.name + "_trades.csv"))
    stats = pd.read_csv(prefix.with_name(prefix.name + "_stats.csv"), usecols=["cash"])
    min_cash = float(stats["cash"].min()) if len(stats) else None
    filled = trades[trades["status"].astype(str).str.lower().eq("fill")] if "status" in trades else trades
    nan_fills = bool(len(filled) and not np.isfinite(pd.to_numeric(filled["price"], errors="coerce").fillna(np.nan)).all())
    payload: dict[str, Any] = {
        "candidate_id": candidate.candidate_id,
        "name": candidate.name,
        "family_id": candidate.family_id,
        "fingerprint": candidate.fingerprint(),
        "window": {"label": window.label, "start": window.start, "end": window.end},
        "universe": inputs.universe_keyword,
        "symbols": list(inputs.ordered_symbols),
        "feature_hash": inputs.feature_hash,
        "engine": "lumibot.strategies.Strategy.run_backtest + BacktestingBroker",
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
        "journal_events": len(strategy._journal),
        "diag": strategy._diag[:12] + strategy._diag[-12:],
        "rejections": strategy._rejections[:50],
        "terminal_positions": {symbol: {k: v for k, v in state.items() if k != "entry_atr"}
                               for symbol, state in strategy._positions.items()},
        "invariants": {"min_cash": min_cash, "nan_fills": nan_fills},
        "hour_mapping_convention": "archive hours 09:00-15:00 ET relabelled to NYSE 09:30-15:30",
    }
    problems = validate_result(payload)
    payload["problems"] = list(problems)
    tmp_path = prefix.with_name(prefix.name + "_result.json.tmp")
    final_path = prefix.with_name(prefix.name + "_result.json")
    tmp_path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    tmp_path.replace(final_path)
    _CONTEXT = None
    return CandidateRun(
        candidate_id=candidate.candidate_id,
        window=window.label,
        out_dir=run_dir,
        payload=payload,
        problems=problems,
    )
