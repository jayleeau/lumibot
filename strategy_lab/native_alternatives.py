"""Native LumiBot implementations of the daily-data alternative strategies.

Plan of record: ``docs/HTS_REMAINING_IMPLEMENTATION_PLAN.md`` section 6.

Implemented (daily bars, no new data gate): ``A01``, ``A03``, ``A04``, ``A05``,
``A06`` and ``A09``.  ``A02`` (verified BIL total-return distributions), ``A07``
(validated session-open bars and a session-integrity audit), ``A08`` (borrow
availability/rates plus a two-leg short execution contract) and ``A10``
(one-minute bars plus an exchange calendar) remain explicitly ``blocked-data``.
Their gates cannot be closed from the retained local archives, so - per the plan -
they are reported as blocked rather than run with fabricated inputs.

Every strategy here shares the audited artifact/accounting contract of
:mod:`strategy_lab.native_experiments` but not the HTS selection or exit rules.
Decisions use the previous completed session; orders fill at the next session's
open through LumiBot's ``BacktestingBroker``.
"""
from __future__ import annotations

import json
import math
import os
import time
from dataclasses import dataclass
from datetime import date
from hashlib import sha256
from pathlib import Path
from typing import Any, Mapping, Sequence

os.environ.setdefault("IS_BACKTESTING", "true")
os.environ.setdefault("LUMIBOT_DISABLE_DOTENV", "true")

import numpy as np
import pandas as pd

from lumibot.backtesting import PandasDataBacktesting  # noqa: E402
from lumibot.entities import Asset, Data, Order, TradingFee  # noqa: E402
from lumibot.strategies import Strategy  # noqa: E402

from strategy_lab.experiment_config import CandidateSpec  # noqa: E402
from strategy_lab.feature_store import covariance_matrix, finite  # noqa: E402
from strategy_lab.native_experiments import (  # noqa: E402
    DAILY_DB,
    INITIAL_CASH,
    USD,
    CandidateRun,
    ExperimentWindow,
    RegistryHtsStrategy,
    _sql_frames,
    _write_payload,
    build_payload,
)

DAILY_ALTERNATIVES: frozenset[str] = frozenset({"A01", "A03", "A04", "A05", "A06", "A09"})
BLOCKED_ALTERNATIVES: Mapping[str, str] = {
    "A02": "requires verified BIL total-return distributions; the retained archive has no distributions table",
    "A07": "requires validated regular-session open bars and a session-integrity audit",
    "A08": "requires borrow availability/rates and a two-leg short execution contract",
    "A10": "requires one-minute bars and an exchange calendar; no minute archive is retained",
}


@dataclass(frozen=True)
class AlternativeInputs:
    """Raw daily frames plus the bookkeeping fields ``build_payload`` reads."""

    window: ExperimentWindow
    universe_keyword: str
    ordered_symbols: tuple[str, ...]
    daily: Mapping[str, pd.DataFrame]
    sessions: tuple[date, ...]
    lumibot_data: tuple[Data, ...]
    feature_hash: str


def prepare_alternative_inputs(params: Mapping[str, Any], window: ExperimentWindow) -> AlternativeInputs:
    """Load daily bars for an alternative's explicit symbol list."""
    symbols = tuple(str(symbol) for symbol in params["universe_symbols"])
    daily_raw = _sql_frames(DAILY_DB, "bars_daily", list(symbols), window.warmup_start, window.end, hourly=False)
    ordered = tuple(symbol for symbol in symbols if symbol in daily_raw)
    daily = {symbol: daily_raw[symbol] for symbol in ordered}
    sessions = tuple(sorted({stamp.date() for frame in daily.values() for stamp in frame.index}))
    lumibot_data = tuple(
        # LumiBot localizes the index of the frame it is given, so hand it a copy
        # and keep the strategy's own frames tz-naive for causal slicing.
        Data(Asset(symbol), daily[symbol].copy(), timestep="day", quote=USD) for symbol in ordered
    )
    payload = {
        "window": [window.label, window.start, window.end],
        "symbols": list(ordered),
        "rows": int(sum(len(frame) for frame in daily.values())),
    }
    feature_hash = sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    return AlternativeInputs(
        window=window,
        universe_keyword=",".join(ordered),
        ordered_symbols=ordered,
        daily=daily,
        sessions=sessions,
        lumibot_data=lumibot_data,
        feature_hash=feature_hash,
    )


def wilder_rsi(close: pd.Series, period: int) -> pd.Series:
    """Wilder RSI with explicit all-gain / all-loss / flat handling."""
    delta = close.diff()
    gain = delta.clip(lower=0.0).ewm(alpha=1.0 / period, adjust=False).mean()
    loss = (-delta).clip(lower=0.0).ewm(alpha=1.0 / period, adjust=False).mean()
    rsi = 100.0 - 100.0 / (1.0 + gain / loss)
    rsi = rsi.where(loss != 0.0, 100.0)
    rsi = rsi.where(~((loss == 0.0) & (gain == 0.0)), 50.0)
    return rsi


def daily_atr(frame: pd.DataFrame, period: int) -> pd.Series:
    previous_close = frame["close"].shift(1)
    true_range = np.maximum(
        frame["high"] - frame["low"],
        np.maximum((frame["high"] - previous_close).abs(), (frame["low"] - previous_close).abs()),
    )
    return pd.Series(true_range, index=frame.index, dtype="float64").rolling(period).mean()


def equal_risk_contributions(covariance: np.ndarray, *, cap: float, iterations: int = 2000) -> np.ndarray | None:
    """Deterministic long-only equal-risk-contribution weights, capped per asset.

    Uses the standard fixed-point iteration ``w_i proportional to 1/(Sigma w)_i``.
    Returns ``None`` for a degenerate covariance so the caller can fail closed.
    """
    count = covariance.shape[0]
    if count == 0 or np.any(~np.isfinite(covariance)):
        return None
    weights = np.full(count, 1.0 / count)
    for _ in range(iterations):
        marginal = covariance @ weights
        if np.any(marginal <= 0.0) or not np.all(np.isfinite(marginal)):
            return None
        updated = 1.0 / marginal
        updated = updated / updated.sum()
        if float(np.max(np.abs(updated - weights))) < 1e-13:
            weights = updated
            break
        weights = updated
    return np.minimum(weights, float(cap))


class _AlternativeBase(RegistryHtsStrategy):
    """Shared plumbing; each A-id supplies its own decision core."""

    ALT_ID = ""
    MONTH_END = False

    def initialize(self) -> None:
        super().initialize()
        # Daily alternatives run one decision per session, not on the hourly grid.
        self.sleeptime = "1D"
        self._entry_index: dict[str, int] = {}
        self._entry_atr: dict[str, float] = {}
        self._decision_rejections: list[dict[str, Any]] = []

    # -- helpers --------------------------------------------------------------
    def _series(self, symbol: str, session: Any) -> pd.DataFrame | None:
        frame = self._ctx.inputs.daily.get(symbol)
        if frame is None:
            return None
        bound = pd.Timestamp(session)
        if isinstance(frame.index, pd.DatetimeIndex) and frame.index.tz is not None:
            bound = bound.tz_localize(frame.index.tz)
        sliced = frame.loc[:bound]
        return sliced if len(sliced) else None

    def _daily_row(self, symbol: str, session: Any) -> pd.Series | None:
        frame = self._ctx.inputs.daily.get(symbol)
        if frame is None:
            return None
        bound = pd.Timestamp(session)
        if isinstance(frame.index, pd.DatetimeIndex) and frame.index.tz is not None:
            bound = bound.tz_localize(frame.index.tz)
        try:
            row = frame.loc[bound]
        except KeyError:
            return None
        return row if isinstance(row, pd.Series) else row.iloc[-1]

    def _execution_price(self, symbol: str, stamp: Any) -> float | None:
        """A daily strategy fills a market order at the current session's open."""
        row = self._daily_row(symbol, pd.Timestamp(stamp).date())
        if row is None:
            return None
        price = float(row["open"])
        return price if math.isfinite(price) and price > 0.0 else None

    def _trailing_return(self, symbol: str, session: Any, sessions: int) -> float:
        sliced = self._series(symbol, session)
        if sliced is None or len(sliced) <= sessions:
            return float("nan")
        close = sliced["close"]
        return float(close.iloc[-1] / close.iloc[-1 - sessions] - 1.0)

    def _is_month_end(self, session: Any) -> bool:
        index = self._session_index.get(session)
        if index is None or index + 1 >= len(self._sessions):
            return False
        return self._sessions[index + 1].month != session.month

    def _is_decision(self, session: Any) -> bool:
        return self._is_month_end(session) if self.MONTH_END else True

    def _held_sessions(self, symbol: str, day: Any) -> int:
        entry = self._entry_index.get(symbol)
        if entry is None:
            return 0
        return self._session_index.get(day, 0) - entry

    # -- decision core hooks --------------------------------------------------
    def alt_targets(self, session: Any) -> dict[str, float]:
        raise NotImplementedError

    def alt_exit_reason(self, symbol: str, session: Any, day: Any) -> str | None:
        return None

    # -- execution ------------------------------------------------------------
    def _apply_targets(self, day: Any, hour: int, session: Any) -> None:
        targets = self.alt_targets(session)
        exits: list[tuple[str, str]] = []
        for symbol in list(self._positions):
            reason = self.alt_exit_reason(symbol, session, day)
            if symbol not in targets:
                exits.append((symbol, reason or "rebalance_exit"))
            elif reason is not None:
                exits.append((symbol, reason))
        stamp = self._stamp(day, hour)
        for symbol, reason in exits:
            price = self._execution_price(symbol, stamp)
            self._submit_sell(symbol, reason, price if price is not None else float("nan"))
        try:
            self.update_broker_balances(force_update=True)
        except Exception:
            pass
        budget = max(0.0, float(self.cash or 0.0))
        portfolio_value = float(self.portfolio_value or 0.0)
        fee_rate = float(self._params["cost_bps_per_side"]) / 10_000.0
        occupied = set(self._positions) | set(self._pending_buys) | set(self._pending_sells)
        orders: list[dict[str, Any]] = []
        for symbol, weight in targets.items():
            if symbol in occupied:
                continue
            price = self._execution_price(symbol, stamp)
            if price is None:
                self._decision_rejections.append({"symbol": symbol, "reason": "no_bar", "day": str(day)})
                continue
            notional = min(float(weight) * portfolio_value, budget)
            effective_price = price * (1.0 + fee_rate)
            quantity = math.floor(notional / effective_price)
            if quantity <= 0:
                self._decision_rejections.append({"symbol": symbol, "reason": "insufficient_budget", "day": str(day)})
                continue
            budget -= quantity * effective_price
            self._submit_buy(symbol, quantity, price, float("nan"), "alt_entry")
            orders.append({"symbol": symbol, "price": price, "quantity": quantity, "weight": round(weight, 6)})
        self._diag.append({
            "day": str(day), "hour": hour, "alt": self.ALT_ID, "cash": budget,
            "portfolio_value": portfolio_value, "weights": {k: round(v, 6) for k, v in targets.items()},
            "exits": [symbol for symbol, _reason in exits], "entries": orders,
        })

    def on_trading_iteration(self) -> None:
        now = pd.Timestamp(self.get_datetime())
        day, hour = now.date(), int(now.hour)
        session = self._previous_session(day)
        if session is None or not self._is_decision(session):
            return
        self._apply_targets(day, hour, session)

    def on_filled_order(self, position: Any, order: Order, price: float, quantity: float,
                        multiplier: float) -> None:
        symbol = order.asset.symbol
        if order.is_buy_order():
            self._pending_buys.pop(symbol, None)
            self._positions[symbol] = {"quantity": float(quantity), "entry_price": float(price),
                                       "entry_session": self._current_day()}
            self._entry_index[symbol] = self._session_index.get(self._current_day(), 0)
            self._journal.append({"event": "fill", "side": "buy", "symbol": symbol,
                                  "price": float(price), "quantity": float(quantity)})
            return
        self._pending_sells.discard(symbol)
        self._pending_sell_reason.pop(symbol, None)
        self._positions.pop(symbol, None)
        self._entry_index.pop(symbol, None)
        self._entry_atr.pop(symbol, None)
        self._journal.append({"event": "fill", "side": "sell", "symbol": symbol,
                              "price": float(price), "quantity": float(quantity)})


class MultiHorizonMomentum(_AlternativeBase):
    """A01: month-end multi-horizon time-series momentum sleeves."""

    ALT_ID = "A01"
    MONTH_END = True

    def alt_targets(self, session: Any) -> dict[str, float]:
        horizons = tuple(int(item) for item in self._params["horizons"])
        sleeve = 1.0 / len(self._params["universe_symbols"])
        targets: dict[str, float] = {}
        for symbol in self._ordered:
            votes = 0
            for horizon in horizons:
                value = self._trailing_return(symbol, session, horizon)
                if finite(value) and value > 0.0:
                    votes += 1
            if votes:
                targets[symbol] = sleeve * (votes / len(horizons))
        return targets


class SlowTrendAllocation(_AlternativeBase):
    """A03: month-end slow trend asset allocation."""

    ALT_ID = "A03"
    MONTH_END = True

    def alt_targets(self, session: Any) -> dict[str, float]:
        months = int(self._params["sma_months"])
        slot = float(self._params["slot_weight"])
        targets: dict[str, float] = {}
        for symbol in self._ordered:
            sliced = self._series(symbol, session)
            if sliced is None or len(sliced) < months * 21:
                continue
            monthly = sliced["close"].resample("ME").last().dropna()
            if len(monthly) < months:
                continue
            window = monthly.iloc[-months:]
            if float(window.iloc[-1]) > float(window.mean()):
                targets[symbol] = slot
        return targets


class ChannelBreakout(_AlternativeBase):
    """A04: daily 55/20 channel breakout with an entry-ATR stop."""

    ALT_ID = "A04"

    def _breakout(self, symbol: str, session: Any) -> tuple[bool, float]:
        sliced = self._series(symbol, session)
        entry = int(self._params["entry_lookback"])
        if sliced is None or len(sliced) < entry + 2:
            return False, float("nan")
        prior_high = float(sliced["high"].iloc[-entry - 1:-1].max())
        return float(sliced["close"].iloc[-1]) > prior_high, float(sliced["close"].iloc[-1])

    def alt_targets(self, session: Any) -> dict[str, float]:
        max_positions = int(self._params["max_positions"])
        slot = float(self._params["slot_weight"])
        lookback = int(self._params["rank_lookback"])
        candidates: list[tuple[str, float]] = []
        held = set(self._positions)
        for symbol in self._ordered:
            if symbol in held:
                candidates.append((symbol, float("inf")))
                continue
            broke, _close = self._breakout(symbol, session)
            if broke:
                candidates.append((symbol, self._trailing_return(symbol, session, lookback)))
        candidates.sort(key=lambda item: (-(item[1] if math.isfinite(item[1]) else -math.inf),
                                          self._universe_order[item[0]]))
        targets: dict[str, float] = {}
        for symbol, _score in candidates[:max_positions]:
            targets[symbol] = slot
            if symbol not in self._entry_atr:
                sliced = self._series(symbol, session)
                atr = float(daily_atr(sliced, int(self._params["daily_atr_period"])).iloc[-1]) if sliced is not None else float("nan")
                self._entry_atr[symbol] = atr
        return targets

    def alt_exit_reason(self, symbol: str, session: Any, day: Any) -> str | None:
        sliced = self._series(symbol, session)
        if sliced is None:
            return None
        close = float(sliced["close"].iloc[-1])
        exit_window = int(self._params["exit_lookback"])
        if len(sliced) > exit_window + 1:
            prior_low = float(sliced["low"].iloc[-exit_window - 1:-1].min())
            if close < prior_low:
                return "channel_exit"
        entry = self._positions.get(symbol, {}).get("entry_price")
        atr = self._entry_atr.get(symbol)
        if entry is not None and finite(atr) and close < float(entry) - float(self._params["daily_atr_k"]) * float(atr):
            return "entry_atr_stop"
        return None


class RsiPullback(_AlternativeBase):
    """A05: RSI(2) pullback inside an SMA200 uptrend."""

    ALT_ID = "A05"

    def alt_targets(self, session: Any) -> dict[str, float]:
        max_positions = int(self._params["max_positions"])
        slot = float(self._params["slot_weight"])
        trend = int(self._params["uptrend_sma_sessions"])
        period = int(self._params["rsi_period"])
        threshold = float(self._params["rsi_entry"])
        targets: dict[str, float] = {}
        for symbol in list(self._positions):
            targets[symbol] = slot
        for symbol in self._ordered:
            if len(targets) >= max_positions:
                break
            if symbol in targets:
                continue
            sliced = self._series(symbol, session)
            if sliced is None or len(sliced) < trend + 2:
                continue
            close = sliced["close"]
            if not float(close.iloc[-1]) > float(close.rolling(trend).mean().iloc[-1]):
                continue
            rsi = float(wilder_rsi(close, period).iloc[-1])
            if finite(rsi) and rsi < threshold:
                targets[symbol] = slot
        return targets

    def alt_exit_reason(self, symbol: str, session: Any, day: Any) -> str | None:
        if self._held_sessions(symbol, day) >= int(self._params["max_hold_sessions"]):
            return "time_exit"
        sliced = self._series(symbol, session)
        if sliced is None:
            return None
        exit_sma = int(self._params["exit_sma"])
        if len(sliced) < exit_sma + 1:
            return None
        if float(sliced["close"].iloc[-1]) > float(sliced["close"].rolling(exit_sma).mean().iloc[-1]):
            return "sma_exit"
        return None


class IbsRebound(_AlternativeBase):
    """A06: internal-bar-strength rebound."""

    ALT_ID = "A06"

    def _ibs(self, sliced: pd.DataFrame) -> float:
        high = float(sliced["high"].iloc[-1])
        low = float(sliced["low"].iloc[-1])
        if high <= low:
            return float("nan")
        return (float(sliced["close"].iloc[-1]) - low) / (high - low)

    def alt_targets(self, session: Any) -> dict[str, float]:
        max_positions = int(self._params["max_positions"])
        slot = float(self._params["slot_weight"])
        threshold = float(self._params["ibs_entry"])
        targets: dict[str, float] = {}
        for symbol in list(self._positions):
            targets[symbol] = slot
        for symbol in self._ordered:
            if len(targets) >= max_positions:
                break
            if symbol in targets:
                continue
            sliced = self._series(symbol, session)
            if sliced is None or len(sliced) < 7:
                continue
            ibs = self._ibs(sliced)
            sma5 = float(sliced["close"].rolling(5).mean().iloc[-1])
            if finite(ibs) and ibs < threshold and float(sliced["close"].iloc[-1]) < sma5:
                targets[symbol] = slot
        return targets

    def alt_exit_reason(self, symbol: str, session: Any, day: Any) -> str | None:
        if self._held_sessions(symbol, day) >= int(self._params["max_hold_sessions"]):
            return "time_exit"
        sliced = self._series(symbol, session)
        if sliced is None or len(sliced) < 2:
            return None
        ibs = self._ibs(sliced)
        if finite(ibs) and ibs > float(self._params["ibs_exit"]):
            return "ibs_exit"
        return None


class TrendFilteredRiskAllocation(_AlternativeBase):
    """A09: month-end trend-gated shrunk-covariance equal-risk allocation."""

    ALT_ID = "A09"
    MONTH_END = True

    def alt_targets(self, session: Any) -> dict[str, float]:
        gate = int(self._params["trend_gate_sma"])
        covariance_sessions = int(self._params["covariance_sessions"])
        shrinkage = float(self._params["shrinkage"])
        cap = float(self._params["max_weight"])
        gross = float(self._params["gross_target"])
        eligible: list[str] = []
        returns: list[pd.Series] = []
        boundary = pd.Timestamp(session)
        for symbol in self._ordered:
            sliced = self._series(symbol, session)
            if sliced is None or len(sliced) < max(gate, covariance_sessions) + 2:
                continue
            close = sliced["close"]
            if not float(close.iloc[-1]) > float(close.rolling(gate).mean().iloc[-1]):
                continue
            eligible.append(symbol)
            returns.append(close.pct_change().loc[:boundary])
        if not eligible:
            return {}
        covariance = covariance_matrix(returns, covariance_sessions)
        if covariance is None:
            return {}
        diagonal = np.diag(np.diag(covariance))
        shrunk = (1.0 - shrinkage) * covariance + shrinkage * diagonal
        weights = equal_risk_contributions(shrunk, cap=cap)
        if weights is None:
            return {}
        return {symbol: float(weight) * gross for symbol, weight in zip(eligible, weights)}


ALTERNATIVE_STRATEGIES: Mapping[str, type[_AlternativeBase]] = {
    "A01": MultiHorizonMomentum,
    "A03": SlowTrendAllocation,
    "A04": ChannelBreakout,
    "A05": RsiPullback,
    "A06": IbsRebound,
    "A09": TrendFilteredRiskAllocation,
}


def _run_alternative_engine(
    *,
    strategy_class: type[Strategy],
    inputs: AlternativeInputs,
    params: Mapping[str, Any],
    prefix: Path,
) -> tuple[Strategy, float]:
    started = time.perf_counter()
    _, strategy = strategy_class.run_backtest(
        datasource_class=PandasDataBacktesting,
        pandas_data=list(inputs.lumibot_data),
        backtesting_start=inputs.window.start_dt,
        backtesting_end=inputs.window.end_dt,
        sleeptime="1D",
        minutes_before_opening=0,
        minutes_before_closing=0,
        stats_file=str(prefix.with_name(prefix.name + "_stats.csv")),
        trades_file=str(prefix.with_name(prefix.name + "_trades.csv")),
        settings_file=str(prefix.with_name(prefix.name + "_settings.json")),
        benchmark_asset=None,
        budget=INITIAL_CASH,
        risk_free_rate=0.0,
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
    return strategy, time.perf_counter() - started


def run_alternative_candidate(
    candidate: CandidateSpec,
    window: ExperimentWindow,
    out_dir: Path,
) -> CandidateRun:
    """Run one daily-data alternative over one window."""
    from strategy_lab import native_experiments

    params = dict(candidate.parameters)
    strategy_class = ALTERNATIVE_STRATEGIES.get(candidate.candidate_id)
    if strategy_class is None:
        raise native_experiments.UnsupportedCandidateError(
            f"{candidate.candidate_id} is blocked-data: "
            f"{BLOCKED_ALTERNATIVES.get(candidate.candidate_id, 'no data gate is closed')}"
        )
    run_dir = Path(out_dir) / candidate.candidate_id / window.label
    run_dir.mkdir(parents=True, exist_ok=True)
    inputs = prepare_alternative_inputs(params, window)
    native_experiments._CONTEXT = native_experiments.RunContext(
        inputs=inputs,  # type: ignore[arg-type]
        params=params,
        candidate_id=candidate.candidate_id,
        fingerprint=candidate.fingerprint(),
    )
    prefix = run_dir / "run"
    try:
        strategy, elapsed = _run_alternative_engine(
            strategy_class=strategy_class, inputs=inputs, params=params, prefix=prefix,
        )
        payload = build_payload(
            candidate=candidate, window=window, strategy=strategy, params=params,
            prefix=prefix, elapsed=elapsed, inputs=inputs,  # type: ignore[arg-type]
            extra={"alternative": candidate.candidate_id, "cadence": "1D"},
        )
    finally:
        native_experiments._CONTEXT = None
    _write_payload(payload, prefix)
    return CandidateRun(
        candidate_id=candidate.candidate_id,
        window=window.label,
        out_dir=run_dir,
        payload=payload,
        problems=tuple(payload["problems"]),
    )
