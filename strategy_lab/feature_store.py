"""Vectorized, causal daily features for the HTS research catalog.

Plan of record: ``docs/HTS_100_VARIATIONS_RESEARCH_PLAN.md`` sections 3 and 5,
and ``docs/HTS_REMAINING_IMPLEMENTATION_PLAN.md`` batch 1.

Every function here is a pure calculation over a per-symbol daily OHLCV frame or
a cross-section of already-computed rows.  Nothing in this module reads the
cache, holds portfolio state, or places orders; the native LumiBot strategy in
:mod:`strategy_lab.native_experiments` owns execution.

Causality contract
------------------
A feature at session ``S`` uses only bars that have completed by ``S``'s close.
The strategy always reads the *last completed session before* the decision
session, so appending future rows can never change an earlier decision.  This
module never shifts a series forward to reach future data; it only reads
history.
"""
from __future__ import annotations

import math
from typing import Mapping, Sequence

import numpy as np
import pandas as pd

TRADING_DAYS = 252.0
RESIDUAL_WINDOW = 20
REGRESSION_WINDOW = 60
EFFICIENCY_WINDOW = 20
CORRELATION_WINDOW = 60


def _empty(index: pd.Index) -> pd.Series:
    return pd.Series(np.nan, index=index, dtype="float64")


def rolling_return(close: pd.Series, sessions: int) -> pd.Series:
    """Simple return from ``sessions`` completed sessions earlier."""
    return close / close.shift(int(sessions)) - 1.0


def annualized_volatility(returns: pd.Series, sessions: int) -> pd.Series:
    return returns.rolling(int(sessions), min_periods=int(sessions)).std(ddof=1) * math.sqrt(TRADING_DAYS)


def downside_deviation(returns: pd.Series, sessions: int) -> pd.Series:
    """Annualized root-mean-square of negative returns over the window."""
    negative = returns.clip(upper=0.0)
    mean_square = (negative ** 2).rolling(int(sessions), min_periods=int(sessions)).mean()
    return np.sqrt(mean_square) * math.sqrt(TRADING_DAYS)


def median_dollar_volume(frame: pd.DataFrame, sessions: int) -> pd.Series:
    return (frame["close"] * frame["volume"]).rolling(int(sessions), min_periods=int(sessions)).median()


def efficiency_ratio(close: pd.Series, sessions: int) -> pd.Series:
    """Signed net move divided by the total distance travelled over the window."""
    net = (close - close.shift(int(sessions))).abs()
    path = close.diff().abs().rolling(int(sessions), min_periods=int(sessions)).sum()
    return net / path.replace(0.0, np.nan)


def regression_slope_r2(close: pd.Series, sessions: int) -> tuple[pd.Series, pd.Series]:
    """Return the rolling OLS slope and R-squared of log price on time.

    Vectorized with a convolution over fixed centered time offsets, so the
    expensive path is O(n) rather than a Python ``rolling.apply`` per window.
    Windows containing a non-finite log price come back NaN, which the strategy
    treats as ineligible.
    """
    window = int(sessions)
    index = close.index
    values = np.log(close.to_numpy(dtype="float64")) if len(close) else np.array([], dtype="float64")
    if len(values) < window:
        return _empty(index), _empty(index)
    offsets = np.arange(window, dtype="float64") - (window - 1) / 2.0
    sxx = float((offsets ** 2).sum())
    numerator = np.convolve(values, offsets[::-1], mode="valid")
    target_index = index[window - 1:]
    numerator_series = pd.Series(numerator, index=target_index, dtype="float64")
    value_series = pd.Series(values, index=index, dtype="float64")
    mean = value_series.rolling(window, min_periods=window).mean()
    mean_square = (value_series ** 2).rolling(window, min_periods=window).mean()
    variance = (mean_square - mean ** 2).clip(lower=0.0)
    slope = numerator_series / sxx
    r_squared = (numerator_series ** 2) / (sxx * window * variance.replace(0.0, np.nan))
    return slope.reindex(index), r_squared.reindex(index)


def residual_momentum(close: pd.Series, benchmark: pd.Series, sessions: int) -> pd.Series:
    """Summed residual returns of ``close`` against ``benchmark`` over the window.

    Beta is estimated over the same window with an intercept; the residual return
    each session is ``r_i - beta * r_market`` (the intercept is not subtracted,
    otherwise the summed residual would be identically zero by construction).
    """
    window = int(sessions)
    benchmark = benchmark.reindex(close.index)
    symbol_returns = close.pct_change()
    benchmark_returns = benchmark.pct_change()
    mean_symbol = symbol_returns.rolling(window, min_periods=window).mean()
    mean_benchmark = benchmark_returns.rolling(window, min_periods=window).mean()
    mean_product = (symbol_returns * benchmark_returns).rolling(window, min_periods=window).mean()
    covariance = mean_product - mean_symbol * mean_benchmark
    variance = (benchmark_returns ** 2).rolling(window, min_periods=window).mean() - mean_benchmark ** 2
    beta = covariance / variance.replace(0.0, np.nan)
    symbol_sum = symbol_returns.rolling(window, min_periods=window).sum()
    benchmark_sum = benchmark_returns.rolling(window, min_periods=window).sum()
    return symbol_sum - beta * benchmark_sum


def daily_feature_frame(
    frame: pd.DataFrame,
    *,
    trend_sma: int,
    return_period: int,
    liquidity_period: int,
    benchmark_close: pd.Series | None = None,
) -> pd.DataFrame:
    """Attach every daily feature the catalog can ask for to ``frame``.

    ``benchmark_close`` is the QQQ close used by the residual-momentum score.
    It may be ``None`` for candidates that never use that score, in which case
    the residual column is NaN.
    """
    close = frame["close"].astype("float64")
    returns = close.pct_change()
    result = pd.DataFrame(index=frame.index)
    result["close"] = close
    result["ret1"] = returns
    result["high"] = frame["high"].astype("float64")
    result["low"] = frame["low"].astype("float64")
    result["open"] = frame["open"].astype("float64")
    result["volume"] = frame["volume"].astype("float64")
    result["sma"] = close.rolling(int(trend_sma), min_periods=int(trend_sma)).mean()
    result["ret"] = rolling_return(close, int(return_period))
    result["mdv"] = median_dollar_volume(frame, int(liquidity_period))
    result["r10"] = rolling_return(close, 10)
    result["r20"] = rolling_return(close, 20)
    result["r60"] = rolling_return(close, 60)
    result["r120"] = rolling_return(close, 120)
    result["r252"] = rolling_return(close, 252)
    result["vol20"] = annualized_volatility(returns, 20)
    result["vol60"] = annualized_volatility(returns, 60)
    result["vol120"] = annualized_volatility(returns, 120)
    result["ddvol20"] = downside_deviation(returns, 20)
    slope, r_squared = regression_slope_r2(close, REGRESSION_WINDOW)
    result["reg_slope"] = slope
    result["reg_r2"] = r_squared
    result["reg60"] = slope * TRADING_DAYS * r_squared
    result["eff20"] = efficiency_ratio(close, EFFICIENCY_WINDOW)
    result["skip5"] = close.shift(5) / close.shift(65) - 1.0
    if benchmark_close is not None:
        result["res20"] = residual_momentum(close, benchmark_close, RESIDUAL_WINDOW)
    else:
        result["res20"] = np.nan
    return result


def percentile_ranks(values: Mapping[str, float]) -> dict[str, float]:
    """Average-tie percentile ranks in ``(0, 1]`` over the supplied cross-section."""
    if not values:
        return {}
    series = pd.Series(dict(values), dtype="float64")
    return {str(key): float(rank) for key, rank in series.rank(pct=True).items()}


def sample_correlation(a: pd.Series, b: pd.Series, sessions: int = CORRELATION_WINDOW) -> float:
    """Pearson correlation of the last ``sessions`` aligned, complete returns.

    Returns NaN when either leg lacks complete history, which the correlation
    screen treats as "cannot verify" and rejects conservatively.
    """
    joined = pd.concat([a, b], axis=1, join="inner").tail(int(sessions)).dropna()
    if len(joined) < int(sessions):
        return float("nan")
    first, second = joined.iloc[:, 0], joined.iloc[:, 1]
    if first.std(ddof=1) == 0.0 or second.std(ddof=1) == 0.0:
        return float("nan")
    return float(first.corr(second))


def covariance_matrix(frames: Sequence[pd.Series], sessions: int) -> np.ndarray | None:
    """Annualized covariance of aligned completed returns, or ``None`` if short."""
    if not frames:
        return None
    joined = pd.concat(frames, axis=1, join="inner").tail(int(sessions)).dropna()
    if len(joined) < int(sessions) or joined.shape[1] != len(frames):
        return None
    covariance = np.cov(joined.to_numpy(dtype="float64"), rowvar=False, ddof=1)
    # A single instrument collapses to a scalar/1-D array; callers always expect a
    # square matrix, so normalise the shape here rather than at every call site.
    covariance = np.atleast_2d(covariance)
    if covariance.shape != (len(frames), len(frames)):
        return None
    if np.any(~np.isfinite(covariance)):
        return None
    return covariance * TRADING_DAYS


def finite(value: object) -> bool:
    """True when ``value`` is a finite real number (``None``/NaN are not)."""
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False
    return math.isfinite(number)
