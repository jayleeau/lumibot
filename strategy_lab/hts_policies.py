"""Pure HTS ranking, gating, selection, schedule, and weighting policies.

Plan of record: ``docs/HTS_REMAINING_IMPLEMENTATION_PLAN.md`` sections 3-5.

These functions are deliberately free of portfolio state and of LumiBot.  The
native strategy calls them with already-computed, already-causal inputs and then
turns their output into orders.  That split is what makes the formulas testable
without running a backtest.

Decision order (fixed by the plan):

1. Read only completed, valid inputs.
2. Apply eligibility and market gates.
3. Calculate rank scores.
4. Apply retention, cooldown, correlation, and exposure-group selection rules.
5. Calculate target weights and portfolio caps.
6. Apply protective exits before allocation trades.
7. Construct causal orders from current cash and confirmed fills.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Callable, Mapping, Sequence

import numpy as np
import pandas as pd

from strategy_lab.feature_store import finite, percentile_ranks

WEEKDAY_INDEX: Mapping[str, int] = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3, "friday": 4}
EVERY_N_ANCHOR = date(2020, 9, 8)

# ``r20`` is the control's parameterized ranking return: the catalog's family 2
# varies ``return_period`` while keeping ``rank_score="r20"``, and the audited
# control ranked on that parameter, not on a hard-coded 20-session window.
SIMPLE_RANK_COLUMNS: Mapping[str, str] = {
    "r20": "ret",
    "r20-over-vol20": "r20",
    "r20-skip-5": "skip5",
    "regression-momentum-60": "reg60",
    "qqq-residual-momentum-20": "res20",
}


class PolicyError(ValueError):
    """Raised when a policy is asked for an unimplemented mode."""


def _ratio(rows: Mapping[str, pd.Series], numerator: str, denominator: str) -> dict[str, float]:
    scores: dict[str, float] = {}
    for symbol, row in rows.items():
        top, bottom = row.get(numerator), row.get(denominator)
        if not finite(top) or not finite(bottom) or float(bottom) <= 0.0:
            continue
        scores[symbol] = float(top) / float(bottom)
    return scores


def rank_scores(mode: str, rows: Mapping[str, pd.Series]) -> dict[str, float]:
    """Return a finite score per eligible symbol for the requested ranking mode."""
    if mode in ("r20", "r20-skip-5", "regression-momentum-60", "qqq-residual-momentum-20"):
        column = SIMPLE_RANK_COLUMNS[mode]
        return {symbol: float(row[column]) for symbol, row in rows.items() if finite(row.get(column))}
    if mode == "r20-over-vol20":
        return _ratio(rows, "r20", "vol20")
    if mode == "r60-over-vol60":
        return _ratio(rows, "r60", "vol60")
    if mode == "r20-over-ddvol20":
        return _ratio(rows, "r20", "ddvol20")
    if mode == "efficiency-ratio-20":
        score: dict[str, float] = {}
        for symbol, row in rows.items():
            if finite(row.get("r20")) and finite(row.get("eff20")):
                score[symbol] = float(row["r20"]) * float(row["eff20"])
        return score
    if mode in ("pctrank-10-20-60", "pctrank-20-60-120"):
        windows = ("r10", "r20", "r60") if mode == "pctrank-10-20-60" else ("r20", "r60", "r120")
        complete = {
            symbol: row for symbol, row in rows.items()
            if all(finite(row.get(column)) for column in windows)
        }
        if not complete:
            return {}
        ranks = [percentile_ranks({s: float(r[column]) for s, r in complete.items()}) for column in windows]
        return {
            symbol: float(np.mean([rank[symbol] for rank in ranks]))
            for symbol in complete
        }
    raise PolicyError(f"unknown rank score mode: {mode!r}")


def market_gate_open(
    gate: str,
    *,
    benchmark_rows: Mapping[str, pd.Series],
    breadth_rows: Sequence[pd.Series],
    breadth_sma: int,
    breadth_threshold: float | None,
) -> bool:
    """Return True when the market gate permits holding risk.

    A missing benchmark or an incomplete breadth reading makes the gate closed,
    never silently open.
    """
    if gate == "none":
        return True
    if gate == "spy-vol-ratio-1.5":
        row = benchmark_rows.get("SPY")
        if row is None or not finite(row.get("vol20")) or not finite(row.get("vol120")) or float(row["vol120"]) <= 0.0:
            return False
        return float(row["vol20"]) / float(row["vol120"]) <= 1.5
    if gate == "breadth-sma50":
        if breadth_threshold is None or not breadth_rows:
            return False
        above = 0
        counted = 0
        for row in breadth_rows:
            if row is None or not finite(row.get("sma")) or not finite(row.get("close")):
                continue
            counted += 1
            if float(row["close"]) > float(row["sma"]):
                above += 1
        if counted == 0:
            return False
        return (above / counted) > float(breadth_threshold)
    symbol, _, sessions = gate.partition("-sma")
    if not sessions.isdigit():
        raise PolicyError(f"unknown market gate: {gate!r}")
    window = int(sessions)
    symbols = symbol.split("-and-")
    for name in symbols:
        row = benchmark_rows.get(name.upper())
        column = f"sma{window}"
        if row is None or not finite(row.get("close")) or not finite(row.get(column)):
            return False
        if float(row[column]) <= 0.0 or float(row["close"]) <= float(row[column]):
            return False
    return True


def benchmark_sma(close: pd.Series, window: int) -> pd.Series:
    return close.rolling(int(window), min_periods=int(window)).mean()


def schedule_due(schedule: str, session: date, *, session_index: int | None = None) -> bool:
    """Whether a rebalance/selection decision happens on ``session``.

    ``weekly-*`` uses the first session on or after the named weekday, carrying a
    holiday week's decision forward to the next session.  ``every-Nth`` uses the
    frozen :data:`EVERY_N_ANCHOR` so every fold counts from the same origin.
    """
    if schedule == "daily":
        return True
    if schedule in ("every-2nd-session", "every-3rd-session"):
        if session_index is None:
            return False
        step = 2 if schedule == "every-2nd-session" else 3
        anchor_index = (EVERY_N_ANCHOR - date(2020, 1, 6)).days
        return (session_index - anchor_index) % step == 0
    if schedule.startswith("weekly-"):
        weekday = WEEKDAY_INDEX.get(schedule.split("-", 1)[1])
        if weekday is None:
            raise PolicyError(f"unknown weekly schedule: {schedule!r}")
        return session.weekday() >= weekday
    raise PolicyError(f"unknown rebalance schedule: {schedule!r}")


def weekly_due_sessions(sessions: Sequence[date], target_weekday: int) -> set[date]:
    """Greedy carry-forward weekly schedule, used by tests and the runner.

    Walks the ordered session list and, after each decision, advances to the
    first session on or after the next week's target weekday.  A holiday target
    therefore carries to the next trading session.
    """
    chosen: set[date] = set()
    previous: date | None = None
    ordered = sorted(sessions)
    for session in ordered:
        if previous is None:
            chosen.add(session)
            previous = session
            continue
        week_start = previous - timedelta(days=previous.weekday())
        boundary = week_start + timedelta(days=target_weekday)
        while boundary <= previous:
            boundary += timedelta(days=7)
        if session >= boundary:
            chosen.add(session)
            previous = session
    return chosen


@dataclass(frozen=True)
class Selection:
    """The chosen holdings and the rank of every eligible symbol."""

    holdings: tuple[str, ...]
    ranks: Mapping[str, int]


def select_holdings(
    *,
    ranked: Sequence[tuple[str, float]],
    held: Sequence[str],
    top_n: int,
    rank_buffer: int | None,
    exposure_limit: int | None,
    exposure_of: Callable[[str], str] | None,
    correlation_of: Callable[[str, str], float] | None,
    correlation_cap: float | None,
) -> Selection:
    """Apply retention, exposure-group, and correlation rules in rank order."""
    ranks = {symbol: index + 1 for index, (symbol, _score) in enumerate(ranked)}
    selected: list[str] = []
    groups: dict[str, int] = {}

    def note(symbol: str) -> None:
        if exposure_of is not None:
            group = exposure_of(symbol)
            groups[group] = groups.get(group, 0) + 1

    if rank_buffer is not None:
        for symbol in held:
            rank = ranks.get(symbol)
            if rank is not None and rank <= int(rank_buffer) and len(selected) < int(top_n):
                selected.append(symbol)
                note(symbol)

    for symbol, _score in ranked:
        if len(selected) >= int(top_n):
            break
        if symbol in selected:
            continue
        if exposure_limit is not None and exposure_of is not None:
            if groups.get(exposure_of(symbol), 0) >= int(exposure_limit):
                continue
        if correlation_cap is not None and correlation_of is not None and selected:
            blocked = False
            for existing in selected:
                correlation = correlation_of(symbol, existing)
                if not finite(correlation) or float(correlation) > float(correlation_cap):
                    blocked = True
                    break
            if blocked:
                continue
        selected.append(symbol)
        note(symbol)
    return Selection(holdings=tuple(selected), ranks=ranks)


def equal_weights(selected: Sequence[str]) -> dict[str, float]:
    if not selected:
        return {}
    share = 1.0 / len(selected)
    return {symbol: share for symbol in selected}


def inverse_volatility_weights(selected: Sequence[str], volatility: Mapping[str, float]) -> dict[str, float]:
    """Normalized inverse-volatility weights; incomplete vols make a symbol ineligible."""
    inverse: dict[str, float] = {}
    for symbol in selected:
        value = volatility.get(symbol)
        if not finite(value) or float(value) <= 0.0:
            return {}
        inverse[symbol] = 1.0 / float(value)
    total = sum(inverse.values())
    if total <= 0.0:
        return {}
    return {symbol: value / total for symbol, value in inverse.items()}


def stop_distance_weights(
    selected: Sequence[str],
    *,
    budget: float,
    atr_k: float,
    atr: Mapping[str, float],
    price: Mapping[str, float],
) -> dict[str, float]:
    """Notional weight per entry so ``atr_k * ATR`` distance risks ``budget`` of NAV."""
    weights: dict[str, float] = {}
    for symbol in selected:
        distance = atr.get(symbol)
        last = price.get(symbol)
        if not finite(distance) or float(distance) <= 0.0 or not finite(last) or float(last) <= 0.0:
            return {}
        weights[symbol] = float(budget) * float(last) / (float(atr_k) * float(distance))
    return weights


def forecast_volatility(weights: Mapping[str, float], covariance: np.ndarray | None) -> float | None:
    """Annualized ex-ante portfolio volatility from the annualized covariance."""
    if not weights:
        return None
    if covariance is None:
        return None
    vector = np.array([weights[symbol] for symbol in weights], dtype="float64")
    if covariance.shape != (len(vector), len(vector)):
        return None
    variance = float(vector @ covariance @ vector)
    if not math.isfinite(variance) or variance <= 0.0:
        return None
    return math.sqrt(variance)


def cap_weights(weights: Mapping[str, float], *, per_symbol_cap: float | None) -> dict[str, float]:
    """Clip per-symbol weights; leftover capacity stays cash rather than being reallocated."""
    if per_symbol_cap is None:
        return dict(weights)
    cap = float(per_symbol_cap)
    return {symbol: min(float(weight), cap) for symbol, weight in weights.items()}


def cap_aggregate(weights: Mapping[str, float], *, gross_target: float) -> dict[str, float]:
    """Trim every weight proportionally when the aggregate exceeds ``gross_target``."""
    total = sum(float(weight) for weight in weights.values())
    if total <= float(gross_target) or total <= 0.0:
        return {symbol: float(weight) for symbol, weight in weights.items()}
    scale = float(gross_target) / total
    return {symbol: float(weight) * scale for symbol, weight in weights.items()}


def target_weights(
    mode: str,
    *,
    selected: Sequence[str],
    gross_target: float,
    per_symbol_cap: float | None,
    atr_k: float,
    volatility: Mapping[str, float],
    atr: Mapping[str, float],
    price: Mapping[str, float],
    stop_distance_budget: float | None,
    vol_target: float | None,
    covariance: np.ndarray | None,
) -> dict[str, float]:
    """Return NAV-fraction target weights after gross, volatility, and per-symbol caps.

    Applies the plan's order: base weights, gross scale, volatility target, then
    the per-symbol cap.  Unused capacity is left in cash.  ``stop-distance-budget``
    derives its size from the declared risk budget, so its aggregate only binds as
    a cap and is never used to scale the per-position risk below the stated budget.
    """
    if not selected:
        return {}
    if mode == "equal-slots":
        base = equal_weights(selected)
    elif mode == "inverse-vol-20":
        base = inverse_volatility_weights(selected, volatility)
    elif mode == "stop-distance-budget":
        if stop_distance_budget is None:
            raise PolicyError("stop-distance-budget requires stop_distance_budget")
        base = stop_distance_weights(selected, budget=stop_distance_budget, atr_k=atr_k, atr=atr, price=price)
    elif mode == "vol-target":
        base = equal_weights(selected)
    else:
        raise PolicyError(f"unknown weight mode: {mode!r}")
    if not base:
        return {}

    if mode == "stop-distance-budget":
        weights = cap_aggregate(base, gross_target=float(gross_target))
    else:
        gross = float(gross_target)
        if mode == "vol-target":
            if vol_target is None:
                raise PolicyError("vol-target requires vol_target")
            candidate = equal_weights(selected)
            forecast = forecast_volatility(candidate, covariance)
            if forecast is None:
                return {}
            gross = min(gross, float(vol_target) / forecast)
        weights = {symbol: weight * gross for symbol, weight in base.items()}
    return cap_weights(weights, per_symbol_cap=per_symbol_cap)


def apply_leveraged_cap(
    weights: Mapping[str, float],
    *,
    leveraged_symbols: Sequence[str],
    cap: float | None,
) -> dict[str, float]:
    """Trim leveraged holdings so their aggregate weight does not exceed ``cap``."""
    if cap is None or not leveraged_symbols:
        return dict(weights)
    leveraged = [symbol for symbol in leveraged_symbols if symbol in weights]
    if not leveraged:
        return dict(weights)
    total = sum(weights[symbol] for symbol in leveraged)
    if total <= float(cap) or total <= 0.0:
        return dict(weights)
    scale = float(cap) / total
    trimmed = dict(weights)
    for symbol in leveraged:
        trimmed[symbol] = weights[symbol] * scale
    return trimmed
