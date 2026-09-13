"""The audited HTS control and the frozen H001-H100 variation catalog.

Plan of record: ``docs/HTS_100_VARIATIONS_RESEARCH_PLAN.md`` sections 3 and 5.
Every candidate below is fully resolved against :data:`HTS_BASELINE`, carries a
human-readable name, and is enumerated from an explicit table.  There is no
implicit Cartesian expansion: adding a value to a table adds a named candidate,
and nothing else.

The catalog is metadata.  Importing it does not run a backtest, read the cache,
or authorize a paper session.
"""
from __future__ import annotations

import re
from dataclasses import replace
from typing import Any, Iterable, Mapping

from strategy_lab.experiment_config import (
    CONTROL_FAMILY_ID,
    KIND_HTS,
    PRIORITY_DEFERRED,
    PRIORITY_STANDARD,
    PRIORITY_STARTING,
    CandidateSpec,
    ParameterSpec,
    RuleFamily,
    frozen_pairs,
    resolve_parameters,
)
from strategy_lab.experiment_universes import UNIVERSE_KEYWORDS

HTS_CONTROL_ID = CONTROL_FAMILY_ID

RANK_SCORES: tuple[str, ...] = (
    "r20",
    "r20-over-vol20",
    "r60-over-vol60",
    "r20-over-ddvol20",
    "pctrank-10-20-60",
    "pctrank-20-60-120",
    "regression-momentum-60",
    "efficiency-ratio-20",
    "qqq-residual-momentum-20",
    "r20-skip-5",
)
WEIGHT_MODES: tuple[str, ...] = (
    "equal-slots", "inverse-vol-20", "stop-distance-budget", "vol-target",
)
EXIT_MODES: tuple[str, ...] = (
    "virtual-trail-baseline",
    "confirm-two-closes",
    "breach-buffer-0.25-atr",
    "breach-buffer-0.50-atr",
    "chandelier-since-entry",
    "chandelier-14-bar",
    "fixed-entry-atr",
    "virtual-trail-breakeven-2r",
    "resting-stop-2atr",
    "virtual-trail-plus-emergency-4atr",
)
MARKET_GATES: tuple[str, ...] = (
    "none",
    "spy-sma50",
    "spy-sma100",
    "spy-sma200",
    "qqq-sma100",
    "qqq-sma200",
    "spy-and-qqq-sma200",
    "breadth-sma50",
    "spy-vol-ratio-1.5",
)
REBALANCE_SCHEDULES: tuple[str, ...] = (
    "daily",
    "weekly-monday",
    "weekly-wednesday",
    "weekly-friday",
    "every-2nd-session",
    "every-3rd-session",
)

# Priority from plan section 9, plus implementation order from section 7.
STARTING_IDS: frozenset[str] = frozenset(
    {"H052", "H053", "H059", "H060", "H063", "H068", "H086", "H089", "H098"}
)
DEFERRED_IDS: frozenset[str] = frozenset({"H039", "H040", "H100"})
PROTECTIVE_ORDER_REQUIREMENT = "native-protective-order-lifecycle"


def _int(name: str, description: str, minimum: int, maximum: int, unit: str | None = None) -> ParameterSpec:
    return ParameterSpec(name=name, kind="int", description=description, minimum=minimum, maximum=maximum, unit=unit)


def _float(name: str, description: str, minimum: float, maximum: float, unit: str | None = None) -> ParameterSpec:
    return ParameterSpec(name=name, kind="float", description=description, minimum=minimum, maximum=maximum, unit=unit)


def _nullable_int(name: str, description: str, minimum: int, maximum: int) -> ParameterSpec:
    return ParameterSpec(name=name, kind="nullable-int", description=description, minimum=minimum, maximum=maximum)


def _nullable_float(name: str, description: str, minimum: float, maximum: float) -> ParameterSpec:
    return ParameterSpec(name=name, kind="nullable-float", description=description, minimum=minimum, maximum=maximum)


def _str(name: str, description: str, allowed: tuple[str, ...]) -> ParameterSpec:
    return ParameterSpec(name=name, kind="str", description=description, allowed_values=allowed)


CONTROL_PARAMETER_SPECS: tuple[ParameterSpec, ...] = (
    _str("universe", "Named research universe", UNIVERSE_KEYWORDS),
    _int("top_n", "Maximum active or pending entry slots", 1, 12, "positions"),
    _int("trend_sma", "Sessions in the price-versus-SMA eligibility filter", 2, 400, "sessions"),
    _int("return_period", "Sessions in the momentum ranking return", 1, 300, "sessions"),
    _int("atr_period", "Completed hourly bars in the ATR estimate", 2, 60, "bars"),
    _float("atr_k", "ATR multiple for the trailing stop", 0.25, 8.0, "x ATR"),
    _int("liquidity_period", "Sessions in the median dollar-volume filter", 1, 300, "sessions"),
    _float("min_median_dollar_volume", "Median dollar-volume floor", 0.0, 1e12, "USD"),
    ParameterSpec(name="require_positive_return", kind="bool", description="Require a positive ranking return"),
    _str("rank_score", "Cross-sectional ranking score", RANK_SCORES),
    _str("weight_mode", "Position sizing mode", WEIGHT_MODES),
    _nullable_float("per_symbol_cap", "Per-symbol notional cap as a NAV fraction", 0.0, 1.0),
    _nullable_float("stop_distance_budget", "Per-position stop-distance risk budget as a NAV fraction", 0.0, 0.10),
    _nullable_float("correlation_screen", "Maximum pairwise 60-session return correlation", 0.0, 1.0),
    _nullable_int("exposure_group_limit", "Maximum instruments per economic-exposure group", 1, 12),
    _nullable_float("leveraged_cap", "Aggregate leveraged-product cap as a NAV fraction", 0.0, 1.0),
    _nullable_float("vol_target", "Annualized portfolio volatility target", 0.02, 0.60),
    _int("vol_covariance_sessions", "Aligned daily returns in the covariance estimate", 10, 252, "sessions"),
    _str("market_gate", "Market-condition gate required to hold risk", MARKET_GATES),
    _int("breadth_sma", "Sessions in the breadth-basket member SMA", 2, 400, "sessions"),
    _nullable_float("breadth_basket_threshold", "Breadth fraction required above its SMA", 0.0, 1.0),
    _str("rebalance_schedule", "Selection/rebalance cadence", REBALANCE_SCHEDULES),
    _nullable_int("rank_buffer", "Holding rank that survives a rebalance", 1, 12),
    _int("stop_cooldown_sessions", "Complete sessions a stopped symbol is barred", 0, 20, "sessions"),
    _str("exit_mode", "Exit and protective-stop behavior", EXIT_MODES),
    _nullable_int("time_exit_sessions", "Scheduled exit after entry", 1, 60),
    _int("signal_hour", "Exchange-clock hour of the completed signal bar", 0, 23, "hour"),
    _int("rebalance_hour", "Exchange-clock hour of the rebalance decision", 0, 23, "hour"),
    _float("cost_bps_per_side", "Effective friction per side", 0.0, 100.0, "bps"),
    _float("gross_target", "Maximum gross target as a NAV fraction", 0.0, 1.0),
    _str("fill_convention", "Approximate fill convention for the replay", ("next-executable-open",)),
    _float("risk_free_rate", "Explicit risk-free convention for excess returns", 0.0, 0.20),
)
CONTROL_SPEC_BY_NAME: dict[str, ParameterSpec] = {spec.name: spec for spec in CONTROL_PARAMETER_SPECS}


def _params(*names: str) -> tuple[ParameterSpec, ...]:
    return tuple(CONTROL_SPEC_BY_NAME[name] for name in names)


HTS_BASELINE: dict[str, Any] = {
    "universe": "U0",
    "top_n": 2,
    "trend_sma": 20,
    "return_period": 20,
    "atr_period": 14,
    "atr_k": 2.0,
    "liquidity_period": 63,
    "min_median_dollar_volume": 5_000_000.0,
    "require_positive_return": False,
    "rank_score": "r20",
    "weight_mode": "equal-slots",
    "per_symbol_cap": None,
    "stop_distance_budget": None,
    "correlation_screen": None,
    "exposure_group_limit": None,
    "leveraged_cap": None,
    "vol_target": None,
    "vol_covariance_sessions": 60,
    "market_gate": "none",
    "breadth_sma": 50,
    "breadth_basket_threshold": None,
    "rebalance_schedule": "daily",
    "rank_buffer": None,
    "stop_cooldown_sessions": 0,
    "exit_mode": "virtual-trail-baseline",
    "time_exit_sessions": None,
    "signal_hour": 9,
    "rebalance_hour": 10,
    "cost_bps_per_side": 3.5,
    "gross_target": 0.995,
    "fill_convention": "next-executable-open",
    "risk_free_rate": 0.0,
}

CONTROL_FAMILY = RuleFamily(
    family_id=HTS_CONTROL_ID,
    title="Audited HTS control",
    hypothesis=(
        "The retained signal and virtual-stop rules, replayed with corrected accounting, "
        "calendar handling, and bounded allocation, are the reference everything else is "
        "compared against."
    ),
    parameters=CONTROL_PARAMETER_SPECS,
    notes="Separately versioned; it is not a silent substitution for the previously locked hts_v1.",
)

FAMILY_1_TREND_SPEED = RuleFamily(
    family_id="family-1-trend-speed",
    title="Trend-filter speed",
    hypothesis=(
        "A 20-day filter may respond too readily to short rallies; slower filters might avoid "
        "more false starts but enter later."
    ),
    parameters=_params("trend_sma"),
)
FAMILY_2_RANK_HORIZON = RuleFamily(
    family_id="family-2-rank-horizon",
    title="Momentum-ranking horizon",
    hypothesis=(
        "Ranking a volatile universe on only one month's gain may select recent spikes; "
        "longer lookbacks may rank persistence."
    ),
    parameters=_params("return_period"),
)
FAMILY_3_ATR = RuleFamily(
    family_id="family-3-atr",
    title="ATR responsiveness and distance",
    hypothesis="Stop behavior depends on both the ATR estimator's speed and its multiplier.",
    parameters=_params("atr_period", "atr_k"),
)
FAMILY_4_EXIT = RuleFamily(
    family_id="family-4-exit",
    title="Exit behavior",
    hypothesis=(
        "Confirmation can avoid temporary dips, while an actual protective order can limit "
        "exposure between observations."
    ),
    parameters=_params("exit_mode", "time_exit_sessions"),
    notes="Fill models for virtual exits and resting protective orders are never interchanged.",
)
FAMILY_5_RANK_QUALITY = RuleFamily(
    family_id="family-5-rank-quality",
    title="Quality of the ranking signal",
    hypothesis="Reward persistence rather than just the biggest raw price rise.",
    parameters=_params("rank_score", "require_positive_return"),
)
FAMILY_6_CONCENTRATION = RuleFamily(
    family_id="family-6-concentration",
    title="Concentration and allocation",
    hypothesis="Two highly correlated holdings may be one large economic bet; empty slots stay cash.",
    parameters=_params(
        "top_n", "weight_mode", "per_symbol_cap", "stop_distance_budget",
        "correlation_screen", "exposure_group_limit",
    ),
)
FAMILY_7_VOL_TARGET = RuleFamily(
    family_id="family-7-vol-target",
    title="Portfolio volatility target",
    hypothesis=(
        "Scaling gross exposure to a covariance-based volatility target may reduce drawdown, "
        "but the leverage and the estimator both change the result."
    ),
    parameters=_params("vol_target", "vol_covariance_sessions", "weight_mode"),
)
FAMILY_8_MARKET_GATE = RuleFamily(
    family_id="family-8-market-gate",
    title="Market-condition filters",
    hypothesis="A broad-market trend gate may keep risk off during sustained downtrends.",
    parameters=_params("market_gate", "breadth_sma", "breadth_basket_threshold"),
)
FAMILY_9_TURNOVER = RuleFamily(
    family_id="family-9-turnover",
    title="Turnover and re-entry discipline",
    hypothesis="Slower rebalancing and cooldowns may cut churn without abandoning risk exits.",
    parameters=_params("rebalance_schedule", "rank_buffer", "stop_cooldown_sessions"),
)
FAMILY_10_UNIVERSE = RuleFamily(
    family_id="family-10-universe-and-combinations",
    title="Universe and predeclared combinations",
    hypothesis=(
        "Exposure restrictions and five combinations frozen before results test whether the "
        "simplest, most diversified readings survive."
    ),
    parameters=_params(
        "universe", "top_n", "weight_mode", "vol_target", "vol_covariance_sessions",
        "rank_score", "correlation_screen", "market_gate", "stop_cooldown_sessions",
        "exit_mode", "leveraged_cap",
    ),
)

HTS_FAMILIES: tuple[RuleFamily, ...] = (
    FAMILY_1_TREND_SPEED,
    FAMILY_2_RANK_HORIZON,
    FAMILY_3_ATR,
    FAMILY_4_EXIT,
    FAMILY_5_RANK_QUALITY,
    FAMILY_6_CONCENTRATION,
    FAMILY_7_VOL_TARGET,
    FAMILY_8_MARKET_GATE,
    FAMILY_9_TURNOVER,
    FAMILY_10_UNIVERSE,
)
FAMILY_BY_ID: dict[str, RuleFamily] = {family.family_id: family for family in HTS_FAMILIES}


def _slug(candidate_id: str, name: str) -> str:
    token = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return f"{candidate_id.lower()}-{token}"


def _hts(
    number: int,
    family: RuleFamily,
    name: str,
    rule: str,
    overrides: Mapping[str, Any],
    *,
    tags: Iterable[str] = (),
    data_requirements: Iterable[str] = (),
    research_refs: Iterable[str] = (),
) -> CandidateSpec:
    candidate_id = f"H{number:03d}"
    priority = PRIORITY_STANDARD
    if candidate_id in STARTING_IDS:
        priority = PRIORITY_STARTING
    elif candidate_id in DEFERRED_IDS:
        priority = PRIORITY_DEFERRED
    return CandidateSpec(
        candidate_id=candidate_id,
        name=name,
        slug=_slug(candidate_id, name),
        kind=KIND_HTS,
        family_id=family.family_id,
        rule=rule,
        hypothesis=family.hypothesis,
        parameters=resolve_parameters(HTS_BASELINE, overrides, family),
        overrides=frozen_pairs(overrides),
        tags=tuple(tags),
        data_requirements=tuple(data_requirements),
        priority=priority,
        research_refs=tuple(research_refs),
    )


# --- Family 1: trend-filter speed (H001-H010) ---------------------------------
_TREND_SESSIONS: tuple[int, ...] = (10, 15, 30, 40, 50, 60, 80, 100, 150, 200)
_F1 = tuple(
    _hts(
        number, FAMILY_1_TREND_SPEED,
        f"Trend filter SMA {sessions}",
        f"Require the completed close to exceed its {sessions}-session SMA.",
        {"trend_sma": sessions},
        tags=("family-1", "trend-filter"),
    )
    for number, sessions in enumerate(_TREND_SESSIONS, start=1)
)

# --- Family 2: momentum-ranking horizon (H011-H020) --------------------------
_RANK_HORIZONS: tuple[int, ...] = (5, 10, 15, 30, 40, 60, 90, 120, 180, 252)
_F2 = tuple(
    _hts(
        10 + number, FAMILY_2_RANK_HORIZON,
        f"Momentum ranking R({sessions})",
        f"Rank eligible symbols on R({sessions}); everything else inherits the control.",
        {"return_period": sessions},
        tags=("family-2", "ranking"),
    )
    for number, sessions in enumerate(_RANK_HORIZONS, start=1)
)

# --- Family 3: ATR responsiveness and distance (H021-H030) -------------------
_ATR_COMBINATIONS: tuple[tuple[int, float], ...] = (
    (7, 1.0), (7, 1.5), (7, 2.0), (7, 3.0), (7, 4.0),
    (28, 1.0), (28, 1.5), (28, 2.0), (28, 3.0), (28, 4.0),
)
_F3 = tuple(
    _hts(
        20 + number, FAMILY_3_ATR,
        f"ATR({bars}) x {multiple} stop",
        f"Trail from close minus {multiple} x ATR({bars}); hourly-close trigger, next-executable exit.",
        {"atr_period": bars, "atr_k": multiple},
        tags=("family-3", "stop"),
    )
    for number, (bars, multiple) in enumerate(_ATR_COMBINATIONS, start=1)
)

# --- Family 4: exit behavior (H031-H040) -------------------------------------
_EXIT_SPECS: tuple[tuple[str, dict[str, Any], str, str], ...] = (
    ("confirm-two-closes", {}, "Two-close stop confirmation",
     "Require two consecutive completed hourly closes at or below the active virtual stop."),
    ("breach-buffer-0.25-atr", {}, "Stop breach buffer 0.25 ATR",
     "Breach only when close <= stop minus 0.25 x prior completed hourly ATR14."),
    ("breach-buffer-0.50-atr", {}, "Stop breach buffer 0.50 ATR",
     "Breach only when close <= stop minus 0.50 x prior completed hourly ATR14."),
    ("chandelier-since-entry", {}, "Chandelier trail from entry high",
     "Trail at the highest completed hourly high since entry minus 3 x current ATR14."),
    ("chandelier-14-bar", {}, "Chandelier trail over 14 bars",
     "Trail at the highest high of the last 14 completed post-entry hourly bars minus 3 x ATR14."),
    ("fixed-entry-atr", {}, "Fixed entry ATR stop",
     "Hold a fixed virtual stop at fill minus 2 x entry-known ATR14; selection exits remain."),
    ("virtual-trail-baseline", {"time_exit_sessions": 10}, "Trail plus 10-session time exit",
     "Baseline virtual trail plus a scheduled exit on the tenth exchange session after entry."),
    ("virtual-trail-breakeven-2r", {}, "Break-even ratchet at +2R",
     "Ratchet the stop to entry once a completed close reaches entry + 2R."),
    ("resting-stop-2atr", {}, "Resting broker stop 2 ATR",
     "Replace the virtual stop with a resting stop-market order at fill minus 2 x entry ATR14."),
    ("virtual-trail-plus-emergency-4atr", {}, "Trail plus emergency 4 ATR stop",
     "Baseline virtual trail plus a separate emergency resting stop at 4 x ATR14."),
)
_F4 = tuple(
    _hts(
        30 + number, FAMILY_4_EXIT, name, rule,
        {"exit_mode": mode, **extra},
        tags=("family-4", "stop" if "stop" in mode else "exit"),
        data_requirements=(PROTECTIVE_ORDER_REQUIREMENT,) if mode.startswith("resting-stop") else (),
    )
    for number, (mode, extra, name, rule) in enumerate(_EXIT_SPECS, start=1)
)
# H040 also depends on resting protective-order support.
_F4 = tuple(
    replace(candidate, data_requirements=(PROTECTIVE_ORDER_REQUIREMENT,))
    if candidate.candidate_id == "H040" else candidate
    for candidate in _F4
)

# --- Family 5: quality of the ranking signal (H041-H050) ---------------------
_RANK_QUALITY: tuple[tuple[str, bool, str, str], ...] = (
    ("r20-over-vol20", False, "Risk-adjusted R(20)",
     "Rank on R(20) divided by 20-session annualized return volatility."),
    ("r60-over-vol60", False, "Risk-adjusted R(60)",
     "Rank on R(60) divided by 60-session annualized return volatility."),
    ("r20-over-ddvol20", False, "Return over downside deviation",
     "Rank on R(20) divided by 20-session downside deviation."),
    ("pctrank-10-20-60", False, "Multi-horizon percentile rank (10/20/60)",
     "Rank on the mean cross-sectional percentile of R(10), R(20), and R(60)."),
    ("pctrank-20-60-120", False, "Multi-horizon percentile rank (20/60/120)",
     "Rank on the mean cross-sectional percentile of R(20), R(60), and R(120)."),
    ("regression-momentum-60", False, "Regression persistence",
     "Rank on the 60-session OLS log-price slope times 252 times regression R-squared."),
    ("efficiency-ratio-20", False, "Efficiency-ratio momentum",
     "Rank on R(20) times the 20-session efficiency ratio."),
    ("qqq-residual-momentum-20", False, "Residual momentum versus QQQ",
     "Rank on the summed 20-session residuals of daily returns regressed on QQQ."),
    ("r20-skip-5", False, "Momentum skipping the last five sessions",
     "Rank on C[t-5]/C[t-65]-1 to skip the most recent week."),
    ("r20", True, "R(20) with a positive-return requirement",
     "Keep the R(20) ranking but require R(20) > 0 in addition to trend and liquidity."),
)
_F5 = tuple(
    _hts(
        40 + number, FAMILY_5_RANK_QUALITY, name, rule,
        {"rank_score": score, "require_positive_return": require_positive},
        tags=("family-5", "ranking"),
        data_requirements=("qqq-aligned-history",) if score == "qqq-residual-momentum-20" else (),
    )
    for number, (score, require_positive, name, rule) in enumerate(_RANK_QUALITY, start=1)
)

# --- Family 6: concentration and allocation (H051-H060) ----------------------
_CONCENTRATION: tuple[tuple[dict[str, Any], str, str], ...] = (
    ({"top_n": 3}, "Top-3 equal-weight holdings",
     "Hold the three strongest eligible symbols in equal initial slots."),
    ({"top_n": 4}, "Top-4 equal-weight holdings",
     "Hold the four strongest eligible symbols in equal initial slots."),
    ({"top_n": 5}, "Top-5 equal-weight holdings",
     "Hold the five strongest eligible symbols in equal initial slots."),
    ({"top_n": 8}, "Top-8 equal-weight holdings",
     "Hold the eight strongest eligible symbols in equal initial slots."),
    ({"top_n": 2, "weight_mode": "inverse-vol-20", "per_symbol_cap": 0.60},
     "Two inverse-volatility holdings, 60% cap",
     "Hold two symbols at normalized inverse vol(20) weights with a 60% per-symbol cap."),
    ({"top_n": 4, "weight_mode": "inverse-vol-20", "per_symbol_cap": 0.35},
     "Four inverse-volatility holdings, 35% cap",
     "Hold four symbols at normalized inverse vol(20) weights with a 35% per-symbol cap."),
    ({"top_n": 2, "weight_mode": "stop-distance-budget", "stop_distance_budget": 0.0025},
     "Two holdings, 0.25% NAV stop-distance budget",
     "Size each of two entries so the 2 x ATR14 stop distance is 0.25% of NAV."),
    ({"top_n": 2, "weight_mode": "stop-distance-budget", "stop_distance_budget": 0.0050},
     "Two holdings, 0.50% NAV stop-distance budget",
     "Size each of two entries so the 2 x ATR14 stop distance is 0.50% of NAV."),
    ({"top_n": 4, "correlation_screen": 0.80}, "Four correlation-screened holdings",
     "Fill four slots, rejecting any symbol correlated above 0.80 with an accepted holding."),
    ({"top_n": 4, "exposure_group_limit": 1}, "Four holdings, one per exposure group",
     "Fill four slots with at most one instrument from each economic-exposure group."),
)
_F6 = tuple(
    _hts(
        50 + number, FAMILY_6_CONCENTRATION, name, rule, overrides,
        tags=("family-6", "allocation"),
        data_requirements=("complete-aligned-correlation-history",)
        if overrides.get("correlation_screen") else (),
    )
    for number, (overrides, name, rule) in enumerate(_CONCENTRATION, start=1)
)

# --- Family 7: portfolio volatility target (H061-H070) -----------------------
_VOL_TARGET_COMBINATIONS: tuple[tuple[int, float], ...] = (
    (20, 0.10), (20, 0.15), (20, 0.20), (20, 0.25), (20, 0.30),
    (60, 0.10), (60, 0.15), (60, 0.20), (60, 0.25), (60, 0.30),
)
_F7 = tuple(
    _hts(
        60 + number, FAMILY_7_VOL_TARGET,
        f"Volatility target {int(target * 100)}% over {sessions} sessions",
        f"Scale equal-slot weights to a {int(target * 100)}% annual target using a "
        f"{sessions}-session covariance; never scale above the 99.5% cap.",
        {"vol_target": target, "vol_covariance_sessions": sessions, "weight_mode": "vol-target"},
        tags=("family-7", "volatility-target"),
    )
    for number, (sessions, target) in enumerate(_VOL_TARGET_COMBINATIONS, start=1)
)

# --- Family 8: market-condition filters (H071-H080) --------------------------
_MARKET_GATE_SPECS: tuple[tuple[dict[str, Any], str, str], ...] = (
    ({"market_gate": "spy-sma50"}, "SPY above SMA50",
     "Hold risk only while SPY's prior close exceeds its 50-session SMA."),
    ({"market_gate": "spy-sma100"}, "SPY above SMA100",
     "Hold risk only while SPY's prior close exceeds its 100-session SMA."),
    ({"market_gate": "spy-sma200"}, "SPY above SMA200",
     "Hold risk only while SPY's prior close exceeds its 200-session SMA."),
    ({"market_gate": "qqq-sma100"}, "QQQ above SMA100",
     "Hold risk only while QQQ's prior close exceeds its 100-session SMA."),
    ({"market_gate": "qqq-sma200"}, "QQQ above SMA200",
     "Hold risk only while QQQ's prior close exceeds its 200-session SMA."),
    ({"market_gate": "spy-and-qqq-sma200"}, "SPY and QQQ above SMA200",
     "Hold risk only while both SPY and QQQ exceed their own 200-session SMA."),
    ({"market_gate": "breadth-sma50", "breadth_sma": 50, "breadth_basket_threshold": 0.50},
     "Breadth above 50% (SMA50)",
     "Hold risk only while more than 50% of the equity-ETF breadth basket exceeds its SMA50."),
    ({"market_gate": "breadth-sma50", "breadth_sma": 50, "breadth_basket_threshold": 0.60},
     "Breadth above 60% (SMA50)",
     "Hold risk only while more than 60% of the breadth basket exceeds its SMA50."),
    ({"market_gate": "breadth-sma50", "breadth_sma": 50, "breadth_basket_threshold": 0.70},
     "Breadth above 70% (SMA50)",
     "Hold risk only while more than 70% of the breadth basket exceeds its SMA50."),
    ({"market_gate": "spy-vol-ratio-1.5"}, "SPY volatility ratio at most 1.5",
     "Hold risk only while SPY vol(20)/vol(120) is at most 1.5."),
)
_F8 = tuple(
    _hts(
        70 + number, FAMILY_8_MARKET_GATE, name, rule, overrides,
        tags=("family-8", "market-filter"),
    )
    for number, (overrides, name, rule) in enumerate(_MARKET_GATE_SPECS, start=1)
)

# --- Family 9: turnover and re-entry discipline (H081-H090) ------------------
_TURNOVER_SPECS: tuple[tuple[dict[str, Any], str, str], ...] = (
    ({"rebalance_schedule": "weekly-monday"}, "Weekly rebalance on Monday",
     "Select and rebalance at the first exchange session on or after Monday."),
    ({"rebalance_schedule": "weekly-wednesday"}, "Weekly rebalance on Wednesday",
     "Select and rebalance at the first exchange session on or after Wednesday."),
    ({"rebalance_schedule": "weekly-friday"}, "Weekly rebalance on Friday",
     "Select and rebalance at the first exchange session on or after Friday."),
    ({"rebalance_schedule": "every-2nd-session"}, "Rebalance every second session",
     "Select and rebalance every second exchange session from the frozen anchor date."),
    ({"rebalance_schedule": "every-3rd-session"}, "Rebalance every third session",
     "Select and rebalance every third exchange session from the frozen anchor date."),
    ({"rank_buffer": 3}, "Retention buffer to rank 3",
     "Retain an eligible holding while its rank is 3 or better, then fill spare capacity."),
    ({"rank_buffer": 4}, "Retention buffer to rank 4",
     "Retain an eligible holding while its rank is 4 or better, then fill spare capacity."),
    ({"stop_cooldown_sessions": 1}, "One-session stop cooldown",
     "Bar a stopped symbol from new entry for one complete subsequent exchange session."),
    ({"stop_cooldown_sessions": 3}, "Three-session stop cooldown",
     "Bar a stopped symbol from new entry for three complete subsequent exchange sessions."),
    ({"stop_cooldown_sessions": 5}, "Five-session stop cooldown",
     "Bar a stopped symbol from new entry for five complete subsequent exchange sessions."),
)
_F9 = tuple(
    _hts(
        80 + number, FAMILY_9_TURNOVER, name, rule, overrides,
        tags=("family-9", "turnover"),
    )
    for number, (overrides, name, rule) in enumerate(_TURNOVER_SPECS, start=1)
)

# --- Family 10: universe restrictions and predeclared combinations (H091-H100) -
_UNIVERSE_AND_COMBINATIONS: tuple[tuple[dict[str, Any], str, str, tuple[str, ...]], ...] = (
    ({"universe": "U0_UNLEVERAGED"}, "Unleveraged ETF universe",
     "Restrict U0 to unleveraged ETFs and trusts, dropping leveraged products and MSTR/COIN.", ()),
    ({"universe": "DIVERSIFIED_CORE"}, "Diversified core universe",
     "Trade a fixed diversified basket: SPY, QQQ, IWM, EFA, EEM, GLD, IEF, TLT, DBC, VNQ, BIL.", ()),
    ({"universe": "SECTOR_ONLY"}, "Sector-only universe",
     "Trade the eleven sector SPDRs only.", ()),
    ({"universe": "U0_EX_CRYPTO"}, "U0 excluding crypto-linked exposure",
     "Drop MSTR, COIN, BITX, and IBIT to test reliance on crypto-linked moves.", ()),
    ({"leveraged_cap": 0.25}, "Leveraged exposure capped at 25% NAV",
     "Cap aggregate leveraged-product market value at 25% of NAV; overflow stays cash.", ()),
    ({"top_n": 5, "vol_target": 0.20, "vol_covariance_sessions": 20, "weight_mode": "vol-target"},
     "Five holdings with a 20% volatility target",
     "Combine top-5 equal holdings with a 20-session, 20% portfolio volatility target.", ()),
    ({"rank_score": "regression-momentum-60", "vol_target": 0.15,
      "vol_covariance_sessions": 60, "weight_mode": "vol-target"},
     "Regression rank with a 15% volatility target",
     "Combine regression-persistence ranking with a 60-session, 15% volatility target.", ()),
    ({"top_n": 4, "correlation_screen": 0.80, "vol_target": 0.20,
      "vol_covariance_sessions": 20, "weight_mode": "vol-target"},
     "Screened four holdings with a 20% volatility target",
     "Combine the 0.80 correlation screen with a 20-session, 20% volatility target.", ()),
    ({"market_gate": "qqq-sma100", "stop_cooldown_sessions": 5},
     "QQQ SMA100 gate with a five-session cooldown",
     "Combine the QQQ SMA100 gate with the five-session post-stop cooldown.", ()),
    ({"exit_mode": "resting-stop-2atr", "vol_target": 0.20,
      "vol_covariance_sessions": 60, "weight_mode": "vol-target"},
     "Resting protective stops with a 20% volatility target",
     "Combine resting 2-ATR protective stops with a 60-session, 20% volatility target.",
     (PROTECTIVE_ORDER_REQUIREMENT,)),
)
_F10 = tuple(
    _hts(
        90 + number, FAMILY_10_UNIVERSE, name, rule, overrides,
        tags=("family-10", "universe"),
        data_requirements=requirements,
    )
    for number, (overrides, name, rule, requirements) in enumerate(_UNIVERSE_AND_COMBINATIONS, start=1)
)

HTS_CONTROL = CandidateSpec(
    candidate_id=HTS_CONTROL_ID,
    name="Audited HTS control (HTS_CONTROL_1)",
    slug="hts-control-1",
    kind="control",
    family_id=CONTROL_FAMILY.family_id,
    rule=(
        "Last completed session selection on SMA20, R(20), and 63-session median dollar "
        "volume; top two equal slots; hourly ATR14 virtual 2x trail; next-executable exits."
    ),
    hypothesis=CONTROL_FAMILY.hypothesis,
    parameters=resolve_parameters(HTS_BASELINE, {}, CONTROL_FAMILY),
    overrides=(),
    tags=("control",),
    priority=PRIORITY_STARTING,
)


def build_hts_candidates() -> tuple[CandidateSpec, ...]:
    """Return the control followed by H001-H100 in ID order."""
    variations = (*_F1, *_F2, *_F3, *_F4, *_F5, *_F6, *_F7, *_F8, *_F9, *_F10)
    return (HTS_CONTROL, *variations)
