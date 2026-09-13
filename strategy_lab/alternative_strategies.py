"""The ten registered alternative strategies A01-A10.

Plan of record: ``docs/HTS_100_VARIATIONS_RESEARCH_PLAN.md`` section 6.  Each
alternative is a single predeclared seed, not a parameter-search space.  They
share the audited accounting/execution contract but not the HTS signal or exit
rules, and none of them silently inherits an HTS stop.

The catalog is metadata.  Importing it does not run a backtest, fetch
distributions, or authorize a paper session.
"""
from __future__ import annotations

import re
from typing import Any, Iterable, Mapping

from strategy_lab.experiment_config import (
    KIND_ALTERNATIVE,
    PRIORITY_STANDARD,
    PRIORITY_STARTING,
    STATUS_BLOCKED_DATA,
    STATUS_REGISTERED,
    CandidateSpec,
    ParameterSpec,
    RuleFamily,
    frozen_pairs,
    resolve_parameters,
)

ALTERNATIVE_BASELINE: dict[str, Any] = {
    "cost_bps_per_side": 3.5,
    "gross_target": 0.995,
    "risk_free_rate": 0.0,
    "fill_convention": "next-executable-open",
}


def _int(name: str, description: str, minimum: int, maximum: int) -> ParameterSpec:
    return ParameterSpec(name=name, kind="int", description=description, minimum=minimum, maximum=maximum)


def _float(name: str, description: str, minimum: float, maximum: float) -> ParameterSpec:
    return ParameterSpec(name=name, kind="float", description=description, minimum=minimum, maximum=maximum)


def _str(name: str, description: str, allowed: tuple[str, ...]) -> ParameterSpec:
    return ParameterSpec(name=name, kind="str", description=description, allowed_values=allowed)


def _symbols(name: str, description: str) -> ParameterSpec:
    return ParameterSpec(name=name, kind="symbols", description=description)


def _int_list(name: str, description: str) -> ParameterSpec:
    return ParameterSpec(name=name, kind="int-list", description=description)


# Shared knobs keep an identical spec across every family that uses them.
SPEC_UNIVERSE_SYMBOLS = _symbols("universe_symbols", "Explicit ordered instrument list")
SPEC_SCHEDULE = _str("schedule", "Decision cadence", ("month-end", "daily"))
SPEC_MAX_POSITIONS = _int("max_positions", "Maximum simultaneous positions", 1, 20)
SPEC_SLOT_WEIGHT = _float("slot_weight", "Target NAV fraction per entry slot", 0.01, 1.0)


def _alternative(
    candidate_id: str,
    name: str,
    rule: str,
    hypothesis: str,
    parameters: tuple[ParameterSpec, ...],
    seed: Mapping[str, Any],
    *,
    priority: str = PRIORITY_STANDARD,
    status: str = STATUS_REGISTERED,
    blocked_reasons: Iterable[str] = (),
    data_requirements: Iterable[str] = (),
    research_refs: Iterable[str] = (),
    notes: str = "",
) -> tuple[RuleFamily, CandidateSpec]:
    family = RuleFamily(
        family_id=candidate_id,
        title=name,
        hypothesis=hypothesis,
        parameters=parameters,
        notes=notes,
    )
    candidate = CandidateSpec(
        candidate_id=candidate_id,
        name=name,
        slug=f"{candidate_id.lower()}-{re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-')}",
        kind=KIND_ALTERNATIVE,
        family_id=family.family_id,
        rule=rule,
        hypothesis=hypothesis,
        parameters=resolve_parameters(ALTERNATIVE_BASELINE, seed, family),
        overrides=frozen_pairs(seed),
        tags=("alternative",),
        data_requirements=tuple(data_requirements),
        status=status,
        blocked_reasons=tuple(blocked_reasons),
        priority=priority,
        research_refs=tuple(research_refs),
    )
    return family, candidate


_SEEDS = (
    _alternative(
        "A01",
        "Multi-horizon time-series momentum",
        "At each completed month end, size each fixed 1/10 sleeve by the fraction of positive "
        "R(63), R(126), and R(252) votes; rebalance at the next session open.",
        "Slower independent trends offer an alternative to selecting only recent top performers.",
        (SPEC_UNIVERSE_SYMBOLS, _int_list("horizons", "Return lookbacks in sessions"), SPEC_SCHEDULE),
        {
            "universe_symbols": ("SPY", "QQQ", "IWM", "EFA", "EEM", "GLD", "TLT", "IEF", "DBC", "VNQ"),
            "horizons": (63, 126, 252),
            "schedule": "month-end",
        },
        priority=PRIORITY_STARTING,
        research_refs=("Time Series Momentum (Moskowitz, Ooi & Pedersen)",),
    ),
    _alternative(
        "A02",
        "Dual-momentum rotation",
        "At month end hold the stronger of SPY and EFA on trailing 252-session total return, "
        "but only if it beats BIL's total return; otherwise hold BIL.",
        "Require both relative strength and an absolute hurdle before holding risk.",
        (
            _symbols("candidates", "Competing absolute-momentum instruments"),
            _str("hurdle_symbol", "Cash-equivalent hurdle instrument", ("BIL",)),
            _int("lookback", "Total-return lookback in sessions", 20, 300),
            SPEC_SCHEDULE,
        ),
        {
            "candidates": ("SPY", "EFA"),
            "hurdle_symbol": "BIL",
            "lookback": 252,
            "schedule": "month-end",
        },
        status=STATUS_BLOCKED_DATA,
        blocked_reasons=("Requires verified BIL total-return distributions before qualification.",),
        data_requirements=("bil-distributions",),
        research_refs=("Dual Momentum (Antonacci)",),
    ),
    _alternative(
        "A03",
        "Slow trend asset allocation",
        "Five equal sleeves hold their asset only while the month-end close exceeds its "
        "10-month SMA; each active sleeve targets 19.9% NAV, otherwise the sleeve is cash.",
        "Slow decisions and broad allocation reduce churn relative to intraday selection.",
        (
            SPEC_UNIVERSE_SYMBOLS,
            _int("sma_months", "Monthly closes in the sleeve SMA", 2, 24),
            SPEC_SLOT_WEIGHT,
            SPEC_SCHEDULE,
        ),
        {
            "universe_symbols": ("SPY", "EFA", "IEF", "VNQ", "DBC"),
            "sma_months": 10,
            "slot_weight": 0.199,
            "schedule": "month-end",
        },
        priority=PRIORITY_STARTING,
        research_refs=("Faber tactical allocation",),
    ),
    _alternative(
        "A04",
        "Daily channel breakout",
        "Enter after a completed close exceeds the highest high of the preceding 55 sessions; "
        "exit below the prior 20-session low or at a virtual stop of entry minus 3 x ATR20.",
        "Require a genuine range breakout rather than just an SMA test.",
        (
            SPEC_UNIVERSE_SYMBOLS,
            _int("entry_lookback", "Sessions in the breakout channel", 5, 200),
            _int("exit_lookback", "Sessions in the exit channel", 5, 200),
            _int("daily_atr_period", "Daily ATR bars known at the decision", 2, 60),
            _float("daily_atr_k", "ATR multiple for the daily virtual stop", 0.5, 8.0),
            SPEC_MAX_POSITIONS,
            SPEC_SLOT_WEIGHT,
            _int("rank_lookback", "Return lookback used to rank simultaneous entries", 1, 300),
        ),
        {
            "universe_symbols": ("SPY", "QQQ", "IWM", "EFA", "EEM", "GLD", "TLT", "DBC", "VNQ"),
            "entry_lookback": 55,
            "exit_lookback": 20,
            "daily_atr_period": 20,
            "daily_atr_k": 3.0,
            "max_positions": 5,
            "slot_weight": 0.199,
            "rank_lookback": 126,
        },
        research_refs=("Zarattini, Antonacci & Barbon, trend following on stocks",),
    ),
    _alternative(
        "A05",
        "RSI(2) pullback in an uptrend",
        "Enter at the next open when the daily close exceeds SMA200 and Wilder RSI(2) is below 5; "
        "exit at the next open above SMA5 or after five held sessions.",
        "Buy temporary weakness instead of recent winners.",
        (
            SPEC_UNIVERSE_SYMBOLS,
            _int("uptrend_sma_sessions", "Sessions in the uptrend filter", 10, 400),
            _int("rsi_period", "Wilder RSI lookback", 2, 30),
            _float("rsi_entry", "RSI entry threshold", 0.0, 50.0),
            _int("exit_sma", "Sessions in the exit SMA", 2, 60),
            _int("max_hold_sessions", "Sessions before a time exit", 1, 60),
            SPEC_MAX_POSITIONS,
            SPEC_SLOT_WEIGHT,
        ),
        {
            "universe_symbols": ("SPY", "QQQ", "IWM", "XLK", "XLF", "XLE", "XLV", "XLI", "XLP", "XLU"),
            "uptrend_sma_sessions": 200,
            "rsi_period": 2,
            "rsi_entry": 5.0,
            "exit_sma": 5,
            "max_hold_sessions": 5,
            "max_positions": 4,
            "slot_weight": 0.24875,
        },
        priority=PRIORITY_STARTING,
        research_refs=("Alvarez, mean-reversion construction",),
    ),
    _alternative(
        "A06",
        "Internal-bar-strength rebound",
        "Enter at the next open when IBS below 0.2 and close below SMA5; exit at the next open "
        "when IBS exceeds 0.8 or after three held sessions.",
        "An unusually weak daily finish may partially reverse.",
        (
            SPEC_UNIVERSE_SYMBOLS,
            _float("ibs_entry", "Internal-bar-strength entry threshold", 0.0, 1.0),
            _float("ibs_exit", "Internal-bar-strength exit threshold", 0.0, 1.0),
            _int("max_hold_sessions", "Sessions before a time exit", 1, 60),
            SPEC_MAX_POSITIONS,
            SPEC_SLOT_WEIGHT,
        ),
        {
            "universe_symbols": ("SPY", "QQQ", "IWM", "EFA", "EEM"),
            "ibs_entry": 0.2,
            "ibs_exit": 0.8,
            "max_hold_sessions": 3,
            "max_positions": 3,
            "slot_weight": 0.331666,
        },
        priority=PRIORITY_STARTING,
        research_refs=("Pagonidis, The IBS Effect",),
    ),
    _alternative(
        "A07",
        "Gap-down recovery after confirmation",
        "After a prior close above SMA200 and a regular-session open gap of at least 0.5 x ATR20, "
        "buy only once the first verified opening hourly bar closes above its own open.",
        "Some overnight pressure reverses after opening price discovery.",
        (
            SPEC_UNIVERSE_SYMBOLS,
            _float("gap_atr_mult", "Opening-gap threshold in prior ATR20 units", 0.1, 5.0),
            _int("daily_atr_period", "Daily ATR bars known at the decision", 2, 60),
            SPEC_SLOT_WEIGHT,
            _int("latest_entry_hour", "Latest exchange-clock hour for a confirmed entry", 0, 23),
        ),
        {
            "universe_symbols": ("SPY", "QQQ", "IWM"),
            "gap_atr_mult": 0.5,
            "daily_atr_period": 20,
            "slot_weight": 0.5,
            "latest_entry_hour": 14,
        },
        status=STATUS_BLOCKED_DATA,
        blocked_reasons=("Requires validated session-open bars and a session-integrity audit.",),
        data_requirements=("validated-session-open-bars",),
        research_refs=("New York Fed, The Overnight Drift",),
    ),
    _alternative(
        "A08",
        "ETF pairs mean reversion",
        "Fit a log-price hedge ratio on 252 sessions at each month end, require an "
        "Engle-Granger p-value below 0.05, then trade the spread z-score at |z| > 2 and exit at |z| < 0.5.",
        "A stationary relative price may mean-revert even when the legs are volatile.",
        (
            _symbols("pairs", "Predeclared candidate pairs as LEG1/LEG2"),
            _int("formation_sessions", "Sessions in the formation window", 20, 500),
            _float("coint_pvalue", "Engle-Granger screen p-value", 0.001, 0.5),
            _float("entry_z", "Entry z-score magnitude", 0.5, 5.0),
            _float("exit_z", "Exit z-score magnitude", 0.0, 5.0),
            _int("max_hold_sessions", "Sessions before a time exit", 1, 60),
        ),
        {
            "pairs": ("SMH/SOXX", "XBI/IBB", "GLD/SLV", "EFA/EEM"),
            "formation_sessions": 252,
            "coint_pvalue": 0.05,
            "entry_z": 2.0,
            "exit_z": 0.5,
            "max_hold_sessions": 10,
        },
        status=STATUS_BLOCKED_DATA,
        blocked_reasons=(
            "Prices alone are insufficient: borrow availability, borrow rates, and a two-leg "
            "execution model are required before this can qualify.",
        ),
        data_requirements=("short-borrow-data", "two-leg-execution-model"),
        research_refs=("Gatev, Goetzmann & Rouwenhorst, Pairs Trading",),
    ),
    _alternative(
        "A09",
        "Trend-filtered risk allocation",
        "At month end exclude assets below SMA200, shrink the 126-session covariance, solve "
        "long-only equal risk contributions, cap each asset at 35% NAV, and hold the remainder in cash.",
        "Diversify sources of portfolio risk instead of dollars.",
        (
            SPEC_UNIVERSE_SYMBOLS,
            _int("trend_gate_sma", "Sessions in the asset trend gate", 10, 400),
            _int("covariance_sessions", "Aligned daily returns in the covariance estimate", 20, 500),
            _float("shrinkage", "Weight on the diagonal shrinkage target", 0.0, 1.0),
            _float("max_weight", "Per-asset notional cap", 0.01, 1.0),
        ),
        {
            "universe_symbols": ("SPY", "EFA", "IEF", "TLT", "GLD", "DBC"),
            "trend_gate_sma": 200,
            "covariance_sessions": 126,
            "shrinkage": 0.1,
            "max_weight": 0.35,
        },
        research_refs=("Maillard, Roncalli & Teiletche, equal risk contributions",),
    ),
    _alternative(
        "A10",
        "Opening-range breakout",
        "Define each day's range from 09:30-10:00 ET one-minute bars, enter on a completed "
        "one-minute close above the range high, and exit at 15:55 ET or the protective range low.",
        "The first thirty minutes may establish a level that later momentum continues through.",
        (
            SPEC_UNIVERSE_SYMBOLS,
            _int("opening_range_minutes", "Minutes in the opening range", 5, 60),
            _float("entry_stop_distance_budget", "Opening-range stop-distance risk budget as a NAV fraction", 0.0, 0.10),
            SPEC_SLOT_WEIGHT,
            _int("entry_cutoff_hour", "Latest exchange-clock hour for a new entry", 0, 23),
            _int("exit_hour", "Exchange-clock hour of the scheduled exit", 0, 23),
        ),
        {
            "universe_symbols": ("QQQ", "SPY"),
            "opening_range_minutes": 30,
            "entry_stop_distance_budget": 0.0025,
            "slot_weight": 0.4975,
            "entry_cutoff_hour": 14,
            "exit_hour": 15,
        },
        status=STATUS_BLOCKED_DATA,
        blocked_reasons=(
            "Requires one-minute bars and an exchange calendar; it is not implementable from the "
            "retained hourly cache alone.",
        ),
        data_requirements=("one-minute-bars", "exchange-calendar"),
        research_refs=("Zarattini & Aziz, Can Day Trading Really Be Profitable?",),
    ),
)

ALTERNATIVE_FAMILIES: tuple[RuleFamily, ...] = tuple(family for family, _ in _SEEDS)
ALTERNATIVE_CANDIDATES: tuple[CandidateSpec, ...] = tuple(candidate for _, candidate in _SEEDS)
ALTERNATIVE_IDS: tuple[str, ...] = tuple(candidate.candidate_id for candidate in ALTERNATIVE_CANDIDATES)


def build_alternative_candidates() -> tuple[CandidateSpec, ...]:
    """Return A01-A10 in ID order."""
    return ALTERNATIVE_CANDIDATES
