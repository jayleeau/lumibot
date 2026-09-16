# HTS v2 Robustness Surface and 100-Variation Matrix

Status: plan only; no source implementation or backtest is authorized by this document.

## Objective

Build a second-generation HTS catalogue that directly targets the three observed
v1 failures: unstable fold performance and winner concentration, excessive
turnover/cost drag, and unacceptable drawdown/concentration. Every v2 candidate
must execute through native LumiBot only:

`lumibot.strategies.Strategy.run_backtest + BacktestingBroker`

The North Star is not full-period Sharpe. It is the predeclared, held-out
walk-forward selection track: positive net returns in most outer folds, positive
independently recomputed daily-return Sharpe, materially lower drawdown, broader
position-level PnL, and survival at higher costs.

The implementation must not touch LumiBot core. Strategy and evaluation logic
belongs in `strategy_lab/`; orchestration belongs in `scripts/`; tests belong in
`tests/strategy_lab/`.

## Constraints honored

- Native LumiBot is the only execution and qualification path. The retired custom
  replay and `strategy_lab/hts_v1_core.py` are not execution targets and need no
  v2 changes.
- `EXECUTION_ENGINE` remains `native-lumibot-backtesting`, and the artifact
  `engine` remains
  `lumibot.strategies.Strategy.run_backtest + BacktestingBroker`.
- Set `IMPLEMENTATION_REVISION` to
  `hts-native-v2-2026-09-16-1` when the behavior is implemented. Every run,
  manifest, resume check, and audit record must carry it.
- Primary cost remains 3.5 bps per side. Seven and 15 bps per side are stress
  tests, not substitutes for the primary convention.
- Hourly data remains exact clock-hour 09:00-15:00 ET. A 15:00 order may use the
  completed 14:00 bar and fill at the 15:00 open; it must not use the 15:00 bar,
  which is unavailable until 16:00.
- Daily-return Sharpe is recomputed from the saved daily equity curve. LumiBot's
  `sharpe` analysis field remains labelled `cagr_over_volatility`.
- The original `HTS_CONTROL_1`, H001-H100, A01-A10, their resolved parameters,
  and their fingerprints remain unchanged.
- Every v2 override is declared by one v2 `RuleFamily`; unknown overrides fail
  closed. IDs, slugs, fingerprints, and resolved parameter maps are unique.
- Missing benchmark, breadth, ATR, price, or edge inputs fail closed; there is no
  fallback download or substitute provider.

## Candidate namespace and registry shape

Use `V001`-`V100` with a new kind, `hts-v2`.

This costs a small amount of registry and CLI work: add `KIND_HTS_V2`, a
`hts_v2_variations` tuple on `ExperimentRegistry`, `--kind hts-v2`, and v2 count
validation. The benefit is worth it: the frozen H001-H100 discovery catalogue
remains visibly immutable, v1 and v2 cannot be accidentally mixed by an `hts`
filter, and artifacts reveal their research generation without reading their
parameters. H101-H200 would require less plumbing but would blur that boundary
and make cumulative trial accounting easier to misread.

Add optional `parent_candidate_id` metadata to `CandidateSpec`. Include it in
`describe()` and in a v2 fingerprint payload, but conditionally omit it for
legacy candidates so all 111 existing fingerprints remain unchanged. It is
lineage metadata, not a strategy knob and therefore is not a `ParameterSpec`.

Define a separate `HTS_V2_BASELINE = {**HTS_BASELINE, **HTS_V2_DEFAULTS}`. Do not
mutate `HTS_BASELINE`. A v2 candidate's exact override dictionary is the merge of
its parent seed and one overlay recipe. Resolve that dictionary against
`HTS_V2_BASELINE` through `family-v2-robust-overlay`.

The v2 family must declare the union of:

- inherited parent-seed keys: `atr_period`, `atr_k`, `exit_mode`,
  `leveraged_cap`, `vol_covariance_sessions`, `vol_target`, and `weight_mode`;
- reused v1 controls: `rebalance_hour`, `reentry_cooldown_bars`, `top_n`, and
  `exposure_group_limit`;
- the five new v2 keys in the next section.

In addition to the current fingerprint uniqueness check, reject duplicate v2
resolved parameter maps after removing only identity metadata. The current
fingerprint includes `candidate_id`, so fingerprint uniqueness alone cannot
detect two IDs that accidentally execute the same configuration.

## Parameter surface

Defaults below belong in `HTS_V2_DEFAULTS`; the existing `ParameterSpec` design
does not need a new `default` field.

### New ParameterSpecs

| Name | Kind | Range / allowed values | Default | Description |
|---|---|---|---|---|
| `risk_off_gate` | `str` | `none`, `spy-sma100`, `spy-sma200`, `qqq-sma100`, `breadth-50`, `breadth-60` | `none` | A portfolio-level cash switch, evaluated from the prior completed daily session. Closed means cancel pending buys, suppress new risk, and flatten every holding; it is not a per-symbol eligibility screen. |
| `risk_off_cooldown_bars` | `int` | 0..20 | 0 | Number of complete exchange-session bars that must pass after the last closed global-gate observation before risk may be added again. It has no effect when `risk_off_gate=none`. |
| `risk_contribution_cap` | `nullable-float` | 0.0..1.0 | `None` | Maximum ex-ante stop-distance loss from one position as a fraction of NAV. `None` disables the cap; a numeric zero means no position risk, not “off.” |
| `min_trade_edge_bps` | `float` | 0.0..1000.0 bps | 0.0 | Minimum causal trend/stop-room move proxy required for a new entry. Zero disables the entry filter. It never forces an exit. |
| `min_position_holding_bars` | `int` | 0..60 | 0 | Minimum complete exchange-session bars before an ordinary rank/selection exit. It never blocks a stop, time exit, broker protective order, or global risk-off liquidation. |

### Reused ParameterSpecs

| Name | Existing kind/range | v2 use |
|---|---|---|
| `atr_k` | `float`, 0.25..8.0 | This is the ATR stop multiplier; do not create a synonymous `atr_stop_multiplier`. Parent value, 3.0, and 4.0 are tested. |
| `reentry_cooldown_bars` | `int`, -1..20 | Existing per-symbol post-stop cooldown; v2 uses 0, 3, and 5 complete sessions. |
| `rebalance_hour` | `int`, 0..23 | Existing execution hour; v2 uses 15 for last-same-day-actionable entry/rebalance variants. |
| `top_n` | `int`, 1..12 | Existing slot count; the two combined stacks use four slots so breadth is created rather than merely shrinking two concentrated positions. |
| `exposure_group_limit` | `nullable-int`, 1..12 | Existing economic-exposure cap; the two combined stacks allow at most one holding per exposure group. |
| `exit_mode` | `str` enum | Add `resting-stop-atr` as a semantic alias implemented with `atr_k`. Use it for every H100-derived v2 candidate so a 3-ATR or 4-ATR order is never mislabeled `resting-stop-2atr`. Existing v1 values and behavior stay unchanged. |

Do not add `weight_mode_v2`. `risk_contribution_cap` is deliberately a cap that
composes after any existing base sizing mode, including H100's volatility target.
Unused capacity stays cash and is never redistributed.

### Exact formulas and state semantics

For a target weight `w_i`, current executable price `P_i`, completed hourly ATR
`ATR_i`, and stop multiple `k=atr_k`:

```text
relative_stop_distance_i = k * ATR_i / P_i
risk_contribution_i      = w_i * relative_stop_distance_i
capped_weight_i          = min(w_i, risk_contribution_cap / relative_stop_distance_i)
```

The cap runs after the parent's base weights and gross/volatility scaling, and
before the leveraged-product cap. Missing or non-positive price/ATR while the cap
is enabled makes that symbol ineligible for an order. No capped amount is
reallocated.

The entry-edge proxy is deterministic and causal. At rebalance, take the prior
completed daily return `R_p` over `p=return_period`, let
`h=max(1, min_position_holding_bars)`, and use the prior completed hourly ATR:

```text
trend_move = max(0, exp(log1p(R_p) * h / p) - 1)
stop_room  = atr_k * ATR / executable_price
expected_move_bps = 10_000 * min(trend_move, stop_room)
```

Enter only when `expected_move_bps >= min_trade_edge_bps`. This is explicitly a
trend-implied screening proxy, not a claim that future profit is known. At the
fixed 3.5 bps/side convention, 14 and 28 bps are two and four times the 7 bps
round-trip cost.

The global gate uses strict comparisons on prior completed daily data:

- `spy-sma100`, `spy-sma200`, `qqq-sma100`: risk is on only when close is
  strictly above the named SMA.
- `breadth-50`, `breadth-60`: risk is on only when strictly more than 50% or 60%
  of the fixed `BREADTH_BASKET` is above its own SMA50. Require valid rows for the
  full fixed basket; incomplete coverage closes the gate.
- A closed observation extends the re-risk deadline. With cooldown `N`, risk may
  return only after `N` complete sessions following the most recent closed
  observation and only if the gate is open then.
- Risk-off overrides the minimum hold and ordinary rebalance schedule. Pending
  buys are canceled; any buy fill that races the cancellation is liquidated at
  the next executable bar.

Minimum-hold positions occupy their existing slots. They are carried into
selection before new ranked symbols, so a protected old holding cannot coexist
with a replacement that silently exceeds `top_n` or the gross budget.

## Parent performers and combination rule

The 100 candidates use five native hourly parents:

| Parent | Frozen v1 seed | Why it is included |
|---|---|---|
| H100 | `exit_mode=resting-stop-2atr`, `weight_mode=vol-target`, `vol_target=0.20`, `vol_covariance_sessions=60` | Best full-period v1 Sharpe; combines protective orders and risk scaling. V2 uses the behavior-equivalent `resting-stop-atr` name. |
| H027 | `atr_period=28`, `atr_k=1.5` | Best high-return virtual-stop lineage; slow ATR estimator tests whether wider stops cure churn without relying on fast volatility. |
| H022 | `atr_period=7`, `atr_k=1.5` | Same stop idea with a fast ATR estimator; retained as a robustness contrast to H027 rather than assuming the winning ATR lookback is stable. |
| H095 | `leveraged_cap=0.25` | Direct v1 concentration control and a useful test of composing notional and risk-contribution caps. |
| HTS_CONTROL_1 | no overrides | Attribution anchor; it proves v2 behavior is real and catches accidental changes to the frozen control. |

A09 remains a required benchmark on every evaluation window, but it is not a v2
parent. Its native implementation is a monthly daily-bar alternative with no
hourly ATR stop, stop re-entry lifecycle, or last-actionable hourly entry. Adding
those mechanisms would redefine A09 and make several common overlays dead or
incommensurate. Report every selected v2 track against unchanged A09, especially
its low 10% six-year drawdown.

The matrix is exactly five parents times 20 predeclared overlays. This is the only
deliberate product. It is not a Cartesian expansion of the parameter value lists.

### The 20 overlay recipes

An omitted key inherits `HTS_V2_BASELINE` or the parent seed.

| Recipe | Exact overlay | Purpose |
|---|---|---|
| P01 | `{}` | Lineage parity anchor. |
| P02 | `{"rebalance_hour":15}` | Last-same-day-actionable entries/rebalances. |
| P03 | `{"atr_k":3.0}` | One-factor wider stop. |
| P04 | `{"atr_k":4.0}` | One-factor coarser stop. |
| P05 | `{"reentry_cooldown_bars":3}` | One-factor stop re-entry delay. |
| P06 | `{"reentry_cooldown_bars":5}` | Stronger stop re-entry delay. |
| P07 | `{"min_position_holding_bars":3}` | One-factor selection-churn floor. |
| P08 | `{"min_position_holding_bars":5}` | Stronger selection-churn floor. |
| P09 | `{"min_trade_edge_bps":14.0}` | Require a 2x round-trip-cost move proxy. |
| P10 | `{"min_trade_edge_bps":28.0}` | Require a 4x round-trip-cost move proxy. |
| P11 | `{"risk_contribution_cap":0.01}` | Cap single-position stop risk at 1.00% NAV. |
| P12 | `{"risk_contribution_cap":0.005}` | Cap single-position stop risk at 0.50% NAV. |
| P13 | `{"risk_off_gate":"spy-sma100"}` | Faster absolute-momentum cash switch. |
| P14 | `{"risk_off_gate":"spy-sma200"}` | Slower absolute-momentum cash switch. |
| P15 | `{"risk_off_gate":"qqq-sma100"}` | Tech-led absolute-momentum cash switch. |
| P16 | `{"risk_off_gate":"breadth-50"}` | Broad participation cash switch. |
| P17 | `{"risk_off_gate":"breadth-60"}` | Stricter broad participation cash switch. |
| P18 | `{"risk_off_cooldown_bars":3,"risk_off_gate":"spy-sma200"}` | Isolate global gate hysteresis. |
| P19 | `{"atr_k":3.0,"exposure_group_limit":1,"min_position_holding_bars":3,"min_trade_edge_bps":14.0,"rebalance_hour":15,"reentry_cooldown_bars":3,"risk_contribution_cap":0.01,"risk_off_cooldown_bars":3,"risk_off_gate":"breadth-50","top_n":4}` | Balanced, moderate all-failure stack with four economically distinct slots. |
| P20 | `{"atr_k":4.0,"exposure_group_limit":1,"min_position_holding_bars":5,"min_trade_edge_bps":28.0,"rebalance_hour":15,"reentry_cooldown_bars":5,"risk_contribution_cap":0.005,"risk_off_cooldown_bars":5,"risk_off_gate":"breadth-60","top_n":4}` | Strict all-failure stack with four economically distinct slots. |

## Exact 100-variation matrix

Every dictionary below is the complete actual override dictionary relative to
`HTS_V2_BASELINE`; there are no hidden per-row deltas. `parent_candidate_id` is
the parent shown in the name and is stored separately as lineage metadata.
The SHA-256 of the 100 newline-joined strings
`candidate_id|canonical_json(override)` in row order is
`ce1a69e1ba5d057d0773839a0be2155a80f572ff3fe8c2786d203320d02a70ef`.

| ID | Name | Family | Full override dictionary |
|---|---|---|---|
| V001 | H100 + lineage parity anchor | `family-v2-robust-overlay` | `{"exit_mode":"resting-stop-atr","vol_covariance_sessions":60,"vol_target":0.2,"weight_mode":"vol-target"}` |
| V002 | H100 + last actionable entry | `family-v2-robust-overlay` | `{"exit_mode":"resting-stop-atr","rebalance_hour":15,"vol_covariance_sessions":60,"vol_target":0.2,"weight_mode":"vol-target"}` |
| V003 | H100 + 3 ATR stop | `family-v2-robust-overlay` | `{"atr_k":3.0,"exit_mode":"resting-stop-atr","vol_covariance_sessions":60,"vol_target":0.2,"weight_mode":"vol-target"}` |
| V004 | H100 + 4 ATR stop | `family-v2-robust-overlay` | `{"atr_k":4.0,"exit_mode":"resting-stop-atr","vol_covariance_sessions":60,"vol_target":0.2,"weight_mode":"vol-target"}` |
| V005 | H100 + 3-session stop re-entry cooldown | `family-v2-robust-overlay` | `{"exit_mode":"resting-stop-atr","reentry_cooldown_bars":3,"vol_covariance_sessions":60,"vol_target":0.2,"weight_mode":"vol-target"}` |
| V006 | H100 + 5-session stop re-entry cooldown | `family-v2-robust-overlay` | `{"exit_mode":"resting-stop-atr","reentry_cooldown_bars":5,"vol_covariance_sessions":60,"vol_target":0.2,"weight_mode":"vol-target"}` |
| V007 | H100 + 3-session minimum hold | `family-v2-robust-overlay` | `{"exit_mode":"resting-stop-atr","min_position_holding_bars":3,"vol_covariance_sessions":60,"vol_target":0.2,"weight_mode":"vol-target"}` |
| V008 | H100 + 5-session minimum hold | `family-v2-robust-overlay` | `{"exit_mode":"resting-stop-atr","min_position_holding_bars":5,"vol_covariance_sessions":60,"vol_target":0.2,"weight_mode":"vol-target"}` |
| V009 | H100 + 14 bps edge floor | `family-v2-robust-overlay` | `{"exit_mode":"resting-stop-atr","min_trade_edge_bps":14.0,"vol_covariance_sessions":60,"vol_target":0.2,"weight_mode":"vol-target"}` |
| V010 | H100 + 28 bps edge floor | `family-v2-robust-overlay` | `{"exit_mode":"resting-stop-atr","min_trade_edge_bps":28.0,"vol_covariance_sessions":60,"vol_target":0.2,"weight_mode":"vol-target"}` |
| V011 | H100 + 1.00% NAV risk-contribution cap | `family-v2-robust-overlay` | `{"exit_mode":"resting-stop-atr","risk_contribution_cap":0.01,"vol_covariance_sessions":60,"vol_target":0.2,"weight_mode":"vol-target"}` |
| V012 | H100 + 0.50% NAV risk-contribution cap | `family-v2-robust-overlay` | `{"exit_mode":"resting-stop-atr","risk_contribution_cap":0.005,"vol_covariance_sessions":60,"vol_target":0.2,"weight_mode":"vol-target"}` |
| V013 | H100 + SPY SMA100 global risk-off | `family-v2-robust-overlay` | `{"exit_mode":"resting-stop-atr","risk_off_gate":"spy-sma100","vol_covariance_sessions":60,"vol_target":0.2,"weight_mode":"vol-target"}` |
| V014 | H100 + SPY SMA200 global risk-off | `family-v2-robust-overlay` | `{"exit_mode":"resting-stop-atr","risk_off_gate":"spy-sma200","vol_covariance_sessions":60,"vol_target":0.2,"weight_mode":"vol-target"}` |
| V015 | H100 + QQQ SMA100 global risk-off | `family-v2-robust-overlay` | `{"exit_mode":"resting-stop-atr","risk_off_gate":"qqq-sma100","vol_covariance_sessions":60,"vol_target":0.2,"weight_mode":"vol-target"}` |
| V016 | H100 + 50% breadth global risk-off | `family-v2-robust-overlay` | `{"exit_mode":"resting-stop-atr","risk_off_gate":"breadth-50","vol_covariance_sessions":60,"vol_target":0.2,"weight_mode":"vol-target"}` |
| V017 | H100 + 60% breadth global risk-off | `family-v2-robust-overlay` | `{"exit_mode":"resting-stop-atr","risk_off_gate":"breadth-60","vol_covariance_sessions":60,"vol_target":0.2,"weight_mode":"vol-target"}` |
| V018 | H100 + SPY SMA200 plus 3-session re-risk delay | `family-v2-robust-overlay` | `{"exit_mode":"resting-stop-atr","risk_off_cooldown_bars":3,"risk_off_gate":"spy-sma200","vol_covariance_sessions":60,"vol_target":0.2,"weight_mode":"vol-target"}` |
| V019 | H100 + balanced robustness stack | `family-v2-robust-overlay` | `{"atr_k":3.0,"exit_mode":"resting-stop-atr","exposure_group_limit":1,"min_position_holding_bars":3,"min_trade_edge_bps":14.0,"rebalance_hour":15,"reentry_cooldown_bars":3,"risk_contribution_cap":0.01,"risk_off_cooldown_bars":3,"risk_off_gate":"breadth-50","top_n":4,"vol_covariance_sessions":60,"vol_target":0.2,"weight_mode":"vol-target"}` |
| V020 | H100 + strict robustness stack | `family-v2-robust-overlay` | `{"atr_k":4.0,"exit_mode":"resting-stop-atr","exposure_group_limit":1,"min_position_holding_bars":5,"min_trade_edge_bps":28.0,"rebalance_hour":15,"reentry_cooldown_bars":5,"risk_contribution_cap":0.005,"risk_off_cooldown_bars":5,"risk_off_gate":"breadth-60","top_n":4,"vol_covariance_sessions":60,"vol_target":0.2,"weight_mode":"vol-target"}` |
| V021 | H027 + lineage parity anchor | `family-v2-robust-overlay` | `{"atr_k":1.5,"atr_period":28}` |
| V022 | H027 + last actionable entry | `family-v2-robust-overlay` | `{"atr_k":1.5,"atr_period":28,"rebalance_hour":15}` |
| V023 | H027 + 3 ATR stop | `family-v2-robust-overlay` | `{"atr_k":3.0,"atr_period":28}` |
| V024 | H027 + 4 ATR stop | `family-v2-robust-overlay` | `{"atr_k":4.0,"atr_period":28}` |
| V025 | H027 + 3-session stop re-entry cooldown | `family-v2-robust-overlay` | `{"atr_k":1.5,"atr_period":28,"reentry_cooldown_bars":3}` |
| V026 | H027 + 5-session stop re-entry cooldown | `family-v2-robust-overlay` | `{"atr_k":1.5,"atr_period":28,"reentry_cooldown_bars":5}` |
| V027 | H027 + 3-session minimum hold | `family-v2-robust-overlay` | `{"atr_k":1.5,"atr_period":28,"min_position_holding_bars":3}` |
| V028 | H027 + 5-session minimum hold | `family-v2-robust-overlay` | `{"atr_k":1.5,"atr_period":28,"min_position_holding_bars":5}` |
| V029 | H027 + 14 bps edge floor | `family-v2-robust-overlay` | `{"atr_k":1.5,"atr_period":28,"min_trade_edge_bps":14.0}` |
| V030 | H027 + 28 bps edge floor | `family-v2-robust-overlay` | `{"atr_k":1.5,"atr_period":28,"min_trade_edge_bps":28.0}` |
| V031 | H027 + 1.00% NAV risk-contribution cap | `family-v2-robust-overlay` | `{"atr_k":1.5,"atr_period":28,"risk_contribution_cap":0.01}` |
| V032 | H027 + 0.50% NAV risk-contribution cap | `family-v2-robust-overlay` | `{"atr_k":1.5,"atr_period":28,"risk_contribution_cap":0.005}` |
| V033 | H027 + SPY SMA100 global risk-off | `family-v2-robust-overlay` | `{"atr_k":1.5,"atr_period":28,"risk_off_gate":"spy-sma100"}` |
| V034 | H027 + SPY SMA200 global risk-off | `family-v2-robust-overlay` | `{"atr_k":1.5,"atr_period":28,"risk_off_gate":"spy-sma200"}` |
| V035 | H027 + QQQ SMA100 global risk-off | `family-v2-robust-overlay` | `{"atr_k":1.5,"atr_period":28,"risk_off_gate":"qqq-sma100"}` |
| V036 | H027 + 50% breadth global risk-off | `family-v2-robust-overlay` | `{"atr_k":1.5,"atr_period":28,"risk_off_gate":"breadth-50"}` |
| V037 | H027 + 60% breadth global risk-off | `family-v2-robust-overlay` | `{"atr_k":1.5,"atr_period":28,"risk_off_gate":"breadth-60"}` |
| V038 | H027 + SPY SMA200 plus 3-session re-risk delay | `family-v2-robust-overlay` | `{"atr_k":1.5,"atr_period":28,"risk_off_cooldown_bars":3,"risk_off_gate":"spy-sma200"}` |
| V039 | H027 + balanced robustness stack | `family-v2-robust-overlay` | `{"atr_k":3.0,"atr_period":28,"exposure_group_limit":1,"min_position_holding_bars":3,"min_trade_edge_bps":14.0,"rebalance_hour":15,"reentry_cooldown_bars":3,"risk_contribution_cap":0.01,"risk_off_cooldown_bars":3,"risk_off_gate":"breadth-50","top_n":4}` |
| V040 | H027 + strict robustness stack | `family-v2-robust-overlay` | `{"atr_k":4.0,"atr_period":28,"exposure_group_limit":1,"min_position_holding_bars":5,"min_trade_edge_bps":28.0,"rebalance_hour":15,"reentry_cooldown_bars":5,"risk_contribution_cap":0.005,"risk_off_cooldown_bars":5,"risk_off_gate":"breadth-60","top_n":4}` |
| V041 | H022 + lineage parity anchor | `family-v2-robust-overlay` | `{"atr_k":1.5,"atr_period":7}` |
| V042 | H022 + last actionable entry | `family-v2-robust-overlay` | `{"atr_k":1.5,"atr_period":7,"rebalance_hour":15}` |
| V043 | H022 + 3 ATR stop | `family-v2-robust-overlay` | `{"atr_k":3.0,"atr_period":7}` |
| V044 | H022 + 4 ATR stop | `family-v2-robust-overlay` | `{"atr_k":4.0,"atr_period":7}` |
| V045 | H022 + 3-session stop re-entry cooldown | `family-v2-robust-overlay` | `{"atr_k":1.5,"atr_period":7,"reentry_cooldown_bars":3}` |
| V046 | H022 + 5-session stop re-entry cooldown | `family-v2-robust-overlay` | `{"atr_k":1.5,"atr_period":7,"reentry_cooldown_bars":5}` |
| V047 | H022 + 3-session minimum hold | `family-v2-robust-overlay` | `{"atr_k":1.5,"atr_period":7,"min_position_holding_bars":3}` |
| V048 | H022 + 5-session minimum hold | `family-v2-robust-overlay` | `{"atr_k":1.5,"atr_period":7,"min_position_holding_bars":5}` |
| V049 | H022 + 14 bps edge floor | `family-v2-robust-overlay` | `{"atr_k":1.5,"atr_period":7,"min_trade_edge_bps":14.0}` |
| V050 | H022 + 28 bps edge floor | `family-v2-robust-overlay` | `{"atr_k":1.5,"atr_period":7,"min_trade_edge_bps":28.0}` |
| V051 | H022 + 1.00% NAV risk-contribution cap | `family-v2-robust-overlay` | `{"atr_k":1.5,"atr_period":7,"risk_contribution_cap":0.01}` |
| V052 | H022 + 0.50% NAV risk-contribution cap | `family-v2-robust-overlay` | `{"atr_k":1.5,"atr_period":7,"risk_contribution_cap":0.005}` |
| V053 | H022 + SPY SMA100 global risk-off | `family-v2-robust-overlay` | `{"atr_k":1.5,"atr_period":7,"risk_off_gate":"spy-sma100"}` |
| V054 | H022 + SPY SMA200 global risk-off | `family-v2-robust-overlay` | `{"atr_k":1.5,"atr_period":7,"risk_off_gate":"spy-sma200"}` |
| V055 | H022 + QQQ SMA100 global risk-off | `family-v2-robust-overlay` | `{"atr_k":1.5,"atr_period":7,"risk_off_gate":"qqq-sma100"}` |
| V056 | H022 + 50% breadth global risk-off | `family-v2-robust-overlay` | `{"atr_k":1.5,"atr_period":7,"risk_off_gate":"breadth-50"}` |
| V057 | H022 + 60% breadth global risk-off | `family-v2-robust-overlay` | `{"atr_k":1.5,"atr_period":7,"risk_off_gate":"breadth-60"}` |
| V058 | H022 + SPY SMA200 plus 3-session re-risk delay | `family-v2-robust-overlay` | `{"atr_k":1.5,"atr_period":7,"risk_off_cooldown_bars":3,"risk_off_gate":"spy-sma200"}` |
| V059 | H022 + balanced robustness stack | `family-v2-robust-overlay` | `{"atr_k":3.0,"atr_period":7,"exposure_group_limit":1,"min_position_holding_bars":3,"min_trade_edge_bps":14.0,"rebalance_hour":15,"reentry_cooldown_bars":3,"risk_contribution_cap":0.01,"risk_off_cooldown_bars":3,"risk_off_gate":"breadth-50","top_n":4}` |
| V060 | H022 + strict robustness stack | `family-v2-robust-overlay` | `{"atr_k":4.0,"atr_period":7,"exposure_group_limit":1,"min_position_holding_bars":5,"min_trade_edge_bps":28.0,"rebalance_hour":15,"reentry_cooldown_bars":5,"risk_contribution_cap":0.005,"risk_off_cooldown_bars":5,"risk_off_gate":"breadth-60","top_n":4}` |
| V061 | H095 + lineage parity anchor | `family-v2-robust-overlay` | `{"leveraged_cap":0.25}` |
| V062 | H095 + last actionable entry | `family-v2-robust-overlay` | `{"leveraged_cap":0.25,"rebalance_hour":15}` |
| V063 | H095 + 3 ATR stop | `family-v2-robust-overlay` | `{"atr_k":3.0,"leveraged_cap":0.25}` |
| V064 | H095 + 4 ATR stop | `family-v2-robust-overlay` | `{"atr_k":4.0,"leveraged_cap":0.25}` |
| V065 | H095 + 3-session stop re-entry cooldown | `family-v2-robust-overlay` | `{"leveraged_cap":0.25,"reentry_cooldown_bars":3}` |
| V066 | H095 + 5-session stop re-entry cooldown | `family-v2-robust-overlay` | `{"leveraged_cap":0.25,"reentry_cooldown_bars":5}` |
| V067 | H095 + 3-session minimum hold | `family-v2-robust-overlay` | `{"leveraged_cap":0.25,"min_position_holding_bars":3}` |
| V068 | H095 + 5-session minimum hold | `family-v2-robust-overlay` | `{"leveraged_cap":0.25,"min_position_holding_bars":5}` |
| V069 | H095 + 14 bps edge floor | `family-v2-robust-overlay` | `{"leveraged_cap":0.25,"min_trade_edge_bps":14.0}` |
| V070 | H095 + 28 bps edge floor | `family-v2-robust-overlay` | `{"leveraged_cap":0.25,"min_trade_edge_bps":28.0}` |
| V071 | H095 + 1.00% NAV risk-contribution cap | `family-v2-robust-overlay` | `{"leveraged_cap":0.25,"risk_contribution_cap":0.01}` |
| V072 | H095 + 0.50% NAV risk-contribution cap | `family-v2-robust-overlay` | `{"leveraged_cap":0.25,"risk_contribution_cap":0.005}` |
| V073 | H095 + SPY SMA100 global risk-off | `family-v2-robust-overlay` | `{"leveraged_cap":0.25,"risk_off_gate":"spy-sma100"}` |
| V074 | H095 + SPY SMA200 global risk-off | `family-v2-robust-overlay` | `{"leveraged_cap":0.25,"risk_off_gate":"spy-sma200"}` |
| V075 | H095 + QQQ SMA100 global risk-off | `family-v2-robust-overlay` | `{"leveraged_cap":0.25,"risk_off_gate":"qqq-sma100"}` |
| V076 | H095 + 50% breadth global risk-off | `family-v2-robust-overlay` | `{"leveraged_cap":0.25,"risk_off_gate":"breadth-50"}` |
| V077 | H095 + 60% breadth global risk-off | `family-v2-robust-overlay` | `{"leveraged_cap":0.25,"risk_off_gate":"breadth-60"}` |
| V078 | H095 + SPY SMA200 plus 3-session re-risk delay | `family-v2-robust-overlay` | `{"leveraged_cap":0.25,"risk_off_cooldown_bars":3,"risk_off_gate":"spy-sma200"}` |
| V079 | H095 + balanced robustness stack | `family-v2-robust-overlay` | `{"atr_k":3.0,"exposure_group_limit":1,"leveraged_cap":0.25,"min_position_holding_bars":3,"min_trade_edge_bps":14.0,"rebalance_hour":15,"reentry_cooldown_bars":3,"risk_contribution_cap":0.01,"risk_off_cooldown_bars":3,"risk_off_gate":"breadth-50","top_n":4}` |
| V080 | H095 + strict robustness stack | `family-v2-robust-overlay` | `{"atr_k":4.0,"exposure_group_limit":1,"leveraged_cap":0.25,"min_position_holding_bars":5,"min_trade_edge_bps":28.0,"rebalance_hour":15,"reentry_cooldown_bars":5,"risk_contribution_cap":0.005,"risk_off_cooldown_bars":5,"risk_off_gate":"breadth-60","top_n":4}` |
| V081 | HTS_CONTROL_1 + lineage parity anchor | `family-v2-robust-overlay` | `{}` |
| V082 | HTS_CONTROL_1 + last actionable entry | `family-v2-robust-overlay` | `{"rebalance_hour":15}` |
| V083 | HTS_CONTROL_1 + 3 ATR stop | `family-v2-robust-overlay` | `{"atr_k":3.0}` |
| V084 | HTS_CONTROL_1 + 4 ATR stop | `family-v2-robust-overlay` | `{"atr_k":4.0}` |
| V085 | HTS_CONTROL_1 + 3-session stop re-entry cooldown | `family-v2-robust-overlay` | `{"reentry_cooldown_bars":3}` |
| V086 | HTS_CONTROL_1 + 5-session stop re-entry cooldown | `family-v2-robust-overlay` | `{"reentry_cooldown_bars":5}` |
| V087 | HTS_CONTROL_1 + 3-session minimum hold | `family-v2-robust-overlay` | `{"min_position_holding_bars":3}` |
| V088 | HTS_CONTROL_1 + 5-session minimum hold | `family-v2-robust-overlay` | `{"min_position_holding_bars":5}` |
| V089 | HTS_CONTROL_1 + 14 bps edge floor | `family-v2-robust-overlay` | `{"min_trade_edge_bps":14.0}` |
| V090 | HTS_CONTROL_1 + 28 bps edge floor | `family-v2-robust-overlay` | `{"min_trade_edge_bps":28.0}` |
| V091 | HTS_CONTROL_1 + 1.00% NAV risk-contribution cap | `family-v2-robust-overlay` | `{"risk_contribution_cap":0.01}` |
| V092 | HTS_CONTROL_1 + 0.50% NAV risk-contribution cap | `family-v2-robust-overlay` | `{"risk_contribution_cap":0.005}` |
| V093 | HTS_CONTROL_1 + SPY SMA100 global risk-off | `family-v2-robust-overlay` | `{"risk_off_gate":"spy-sma100"}` |
| V094 | HTS_CONTROL_1 + SPY SMA200 global risk-off | `family-v2-robust-overlay` | `{"risk_off_gate":"spy-sma200"}` |
| V095 | HTS_CONTROL_1 + QQQ SMA100 global risk-off | `family-v2-robust-overlay` | `{"risk_off_gate":"qqq-sma100"}` |
| V096 | HTS_CONTROL_1 + 50% breadth global risk-off | `family-v2-robust-overlay` | `{"risk_off_gate":"breadth-50"}` |
| V097 | HTS_CONTROL_1 + 60% breadth global risk-off | `family-v2-robust-overlay` | `{"risk_off_gate":"breadth-60"}` |
| V098 | HTS_CONTROL_1 + SPY SMA200 plus 3-session re-risk delay | `family-v2-robust-overlay` | `{"risk_off_cooldown_bars":3,"risk_off_gate":"spy-sma200"}` |
| V099 | HTS_CONTROL_1 + balanced robustness stack | `family-v2-robust-overlay` | `{"atr_k":3.0,"exposure_group_limit":1,"min_position_holding_bars":3,"min_trade_edge_bps":14.0,"rebalance_hour":15,"reentry_cooldown_bars":3,"risk_contribution_cap":0.01,"risk_off_cooldown_bars":3,"risk_off_gate":"breadth-50","top_n":4}` |
| V100 | HTS_CONTROL_1 + strict robustness stack | `family-v2-robust-overlay` | `{"atr_k":4.0,"exposure_group_limit":1,"min_position_holding_bars":5,"min_trade_edge_bps":28.0,"rebalance_hour":15,"reentry_cooldown_bars":5,"risk_contribution_cap":0.005,"risk_off_cooldown_bars":5,"risk_off_gate":"breadth-60","top_n":4}` |

## Native wiring map

| File and function | Required work |
|---|---|
| `strategy_lab/experiment_config.py`: kind constants, `CandidateSpec` | Add `KIND_HTS_V2`; add optional `parent_candidate_id`; preserve legacy fingerprint payloads exactly and include lineage for v2. Keep `EXECUTION_ENGINE` unchanged. |
| `strategy_lab/hts_variants.py`: specs, baselines, families, builders | Add the five specs, `HTS_V2_DEFAULTS`, `HTS_V2_BASELINE`, the single v2 family, five parent seeds, 20 frozen recipes, and V001-V100. Extend `EXIT_MODES` with `resting-stop-atr`. Do not mutate the v1 control or H/A tables. |
| `strategy_lab/experiment_registry.py`: `ExperimentRegistry`, `validate_registry`, render/search/statistics | Store v2 separately; require exact ordered IDs V001-V100, kind `hts-v2`, valid parents, unique IDs/slugs/fingerprints/resolved maps, declared overrides, and exact total 211 = 1 control + 100 hts + 100 hts-v2 + 10 alternatives. Render parent lineage. |
| `strategy_lab/hts_policies.py`: new pure helpers | Add `risk_off_gate_open`, `cap_risk_contributions`, and `expected_trade_move_bps`. Extend `select_holdings` with explicit mandatory minimum-hold symbols or add an equally pure wrapper. Hand-calculate these functions in tests. |
| `strategy_lab/native_experiments.py`: `prepare_inputs` | Reuse the already causal benchmark SMA and breadth inputs plus hourly ATR. Do not add a provider or data path. Make v2 breadth require complete fixed-basket evidence. |
| `RegistryHtsStrategy.initialize` | Add risk-off state, last evaluated gate session, re-risk deadline, pending-buy order handles, and diagnostic counters for gate transitions, capped sizes, edge rejections, and deferred selection exits. |
| `RegistryHtsStrategy._market_gate` and new `_risk_off_gate` / `_refresh_risk_off_state` | Keep legacy `market_gate` behavior intact. Implement the new global gate separately, fail closed on missing inputs, and update the state once per completed session. |
| `RegistryHtsStrategy._select` | If globally risk-off, return no risk. Otherwise retain positions still inside `min_position_holding_bars` before filling spare ranked slots. These mandatory holds count toward `top_n`. |
| `RegistryHtsStrategy._target_weights` | Call existing `target_weights`, then `cap_risk_contributions`, then `apply_leveraged_cap`. Validate each final weight and leave trimmed weight in cash. This is the requested sizing hook. |
| `RegistryHtsStrategy._rebalance` | When risk-off, cancel buys and flatten the entire book regardless of minimum hold. When risk-on, evaluate `expected_trade_move_bps` immediately before each new buy and journal both accepted and rejected values. Prevent replacement entry while a mandatory hold or pending exit occupies the slot. |
| `RegistryHtsStrategy._place_protective_stop` | Treat `resting-stop-atr` exactly like the existing resting stop except the level is explicitly `entry - atr_k*entry_atr`. Preserve `resting-stop-2atr` for frozen v1. |
| `RegistryHtsStrategy.on_trading_iteration` | Fixed order: refresh global gate; update/submit risk exits; if risk-off, perform global flatten at the configured rebalance hour even when ordinary schedule is not due; otherwise select and rebalance normally. Stops remain hourly and are never delayed to 15:00. |
| `RegistryHtsStrategy.on_filled_order` | Store entry session indices, preserve existing per-symbol cooldown behavior, and immediately mark any risk-off race fill for liquidation. |
| `strategy_lab/native_experiments.py`: `check_supported`, `build_payload` | Recognize `hts-v2`; fail on any unimplemented enum; emit parent, resolved parameters, execution engine, v2 revision, gate transitions, binding counts, edge-filter counts, and minimum-hold deferrals. Include the clock-hour convention for v2. |
| `strategy_lab/experiment_validation.py` | Independently verify v2 artifact identity, 3.5 bps cost metadata, risk-cap arithmetic from recorded entry inputs, global-gate flatten events, and daily-return metrics. Add position-level PnL breadth metrics without importing strategy policy code. |
| `scripts/run_hts_experiments.py` | Add `hts-v2` filtering/classification, v2 counts and lineage in the manifest, and the new block labels. Resume remains fingerprint plus implementation-revision gated. Keep three workers, numerical threads at one, and one job per child. |
| `scripts/list_strategy_experiments.py` | Accept/show/search `hts-v2`, parent, full resolved params, and exact overrides. `--verify` must report the new counts. |
| `scripts/evaluate_walk_forward.py` | Replace peak-like two-window selection with the six-block robust rule below; write and verify a selection lock before reading each outer block. Report lineage champions, position breadth, cost drag, and outer positive-fold count. |
| `scripts/stress_walk_forward_costs.py` | Rerun only the causally selected outer candidates at 7 and 15 bps/side; do not reselect at each cost. |

`strategy_lab/native_alternatives.py` is not part of the v2 execution path. A09
is rerun unchanged as a benchmark.

## Artifact contract

Each v2 `run_result.json` and suite manifest must add:

- `execution_engine`, `engine`, and `implementation_revision`;
- `candidate_id`, `kind`, `family_id`, `parent_candidate_id`, fingerprint, and a
  fingerprint/hash of the resolved parameter map that excludes candidate ID;
- the full resolved parameter map and exact override map;
- clock-hour and cost conventions;
- global risk-off open/close/re-risk transitions and full-book flatten counts;
- per-entry expected-move bps, threshold, and accept/reject decision;
- pre-cap and post-cap weights, relative stop distance, risk contribution, and
  cap-binding count;
- minimum-hold deferral count and stop/risk-off override count;
- position-level net PnL records, including terminal marked positions, so median
  PnL and top-three contribution can be independently reproduced.

Do not use truncated diagnostics as the sole audit source. Large event records may
live in a separate JSONL/Parquet artifact referenced by hash from `run_result`.

## Test plan for terra

### Registry and identity tests

Extend `tests/strategy_lab/test_experiment_registry.py` with:

1. Exact counts and order: V001-V100, 100 `hts-v2`, total 211.
2. Exact parent blocks and exact recipe mapping from this plan.
3. Unique IDs, slugs, fingerprints, and semantic resolved-parameter maps.
4. Rejection of an undeclared v2 override, invalid gate enum, invalid cap, and
   wrong type.
5. All v2 override names declared by `family-v2-robust-overlay`.
6. Original 111 IDs, parameter maps, and fingerprints unchanged; specifically
   keep `HTS_CONTROL_1.overrides == ()` and its current fingerprint.
7. Every V id resolves to its parent seed plus exactly one declared recipe.
8. Catalogue rendering/search/`--kind hts-v2` includes lineage and does not count
   v2 as v1 HTS.

### Pure unit tests

Extend `tests/strategy_lab/test_hts_policies.py` with hand-calculated cases:

- every global-gate enum, exact equality boundaries, and fail-closed missing or
  incomplete inputs;
- risk contribution `w*k*ATR/P`, cap binding, no renormalization, and invalid
  price/ATR;
- edge proxy values, threshold equality, zero-disabled behavior, and invalid
  return inputs;
- mandatory minimum-hold selection occupies slots and becomes replaceable on the
  exact expiry session.

### Before/after wiring controls

Extend `tests/strategy_lab/test_native_experiments.py`. Each new parameter needs a
paired test on the same synthetic bars so it cannot be accepted but dead:

1. `risk_off_gate=none` versus `spy-sma200`: only the gated run cancels entries
   and liquidates all holdings when SPY closes below SMA200; both are identical
   while the gate is open.
2. Risk-off cooldown 0 versus 3: after a one-session gate closure, the second run
   stays cash for exactly three complete sessions longer.
3. Risk cap `None` versus 0.005: the capped order quantity is smaller and its
   recomputed stop-distance loss is at most 0.5% NAV after share rounding.
4. Edge floor 0 versus 14: a low-edge entry appears only in the zero-floor run;
   an entry exactly at 14 bps appears in both.
5. Minimum hold 0 versus 3: a rank change exits immediately only at zero, while
   a stop breach exits both runs immediately.
6. `atr_k=2` versus 4: initial/trailing stop levels and capped risk quantities
   differ by the hand-calculated factor.
7. Existing re-entry cooldown 0 versus 3 remains exact after the v2 changes.
8. Rebalance hour 10 versus 15 proves the 15:00 order uses the 14:00 completed
   source bar and 15:00 open, never the unavailable 15:00 close.

Also add:

- parent parity integrations: V001/H100, V021/H027, V041/H022, V061/H095, and
  V081/control must have identical normalized order/fill/equity hashes to their
  parents when all new behavior is at its default. `resting-stop-atr` must be
  event-identical to H100's 2-ATR mode at `atr_k=2`;
- a native `Strategy.run_backtest` integration where a global gate closes with
  two holdings and confirms both broker exits, cash-only state, and no re-entry
  before expiry;
- artifact tests for parent, resolved parameters, revision, engine, diagnostics,
  and resume rejection under a stale revision;
- independent audit tests that recompute daily Sharpe, risk contribution,
  position PnL, top-three share, and median position PnL from emitted artifacts.

Run targeted tests first, then all `tests/strategy_lab`. Inspect test age before
editing existing assertions, and do not change old expectations merely to accept
new output.

## Honest v2 walk-forward plan

The existing two-inner-window outer folds are retained for outer-test date
comparability, but selection is strengthened to use six non-overlapping
six-month discovery blocks. Run each unique block once from cash:

| Block | Start | End |
|---|---|---|
| b01 | 2020-09-09 | 2021-03-09 |
| b02 | 2021-03-09 | 2021-09-09 |
| b03 | 2021-09-09 | 2022-03-09 |
| b04 | 2022-03-09 | 2022-09-09 |
| b05 | 2022-09-09 | 2023-03-09 |
| b06 | 2023-03-09 | 2023-09-09 |
| b07 | 2023-09-09 | 2024-03-09 |
| b08 | 2024-03-09 | 2024-09-09 |
| b09 | 2024-09-09 | 2025-03-09 |
| b10 | 2025-03-09 | 2025-09-09 |
| b11 | 2025-09-09 | 2026-03-09 |
| b12 | 2026-03-09 | 2026-09-09 |

Fold mapping is:

| Fold | Discovery blocks used for selection | Held-out outer block |
|---|---|---|
| f1 | b01-b06 | b07 |
| f2 | b02-b07 | b08 |
| f3 | b03-b08 | b09 |
| f4 | b04-b09 | b10 |
| f5 | b05-b10 | b11 |
| f6 | b06-b11 | b12 |

Run and reveal sequentially. For f1, run b01-b06, write a
`selection_lock.json` containing the registry hash, implementation revision,
input hashes, eligible set, ranking values, and selected ID, then run/reveal b07.
For f2, use b02-b07, lock, then reveal b08, and so on. The evaluator must refuse
to score an outer block if its lock is missing or does not hash-match.

For each fold and candidate, require all six discovery jobs to pass independent
audit. A candidate is eligible only if all predeclared gates pass:

1. daily-return Sharpe and net total return are both positive in at least four of
   six discovery blocks;
2. aggregate discovery median per-position net PnL is strictly positive;
3. aggregate top-three position net PnL is no more than 50% of aggregate net PnL;
4. chained discovery max drawdown is at most 35%; chain the six from-cash daily
   return series in calendar order and charge liquidation plus re-entry at each
   boundary, using the same explicitly labelled approximation as the outer
   track;
5. transaction cost at 3.5 bps/side is no more than 10% of gross strategy profit
   before costs, where gross profit is `net PnL + charged transaction costs` and
   must be positive.

If no candidate passes, select cash for that outer fold. Do not relax a threshold
after seeing results.

To prevent 20 near-identical siblings from dominating, first choose one champion
within each `parent_candidate_id`, then compare at most five parent champions.
Use this fixed lexicographic order, not peak Sharpe:

1. number of positive discovery blocks, descending;
2. worst discovery-block Sharpe, descending;
3. `median(block Sharpe) - 0.5 * IQR(block Sharpe)`, descending;
4. top-three PnL share, ascending;
5. chained maximum drawdown, ascending;
6. fills per session, ascending;
7. candidate ID, ascending.

Freeze the winner before the outer run. The primary reported result is only the
six causally selected outer rows and their stitched track with charged boundary
turnover. All-candidate outer leaderboards are diagnostic and must be labelled
post-selection, never qualification evidence.

After all six folds, a v2 track qualifies only if every condition holds:

- positive outer total return in at least four of six folds;
- stitched selected-track total return > 0 and daily-return Sharpe > 0;
- stitched maximum drawdown <= 30%;
- stitched/aggregate top-three position PnL share <= 50% and median position PnL
  > 0;
- the same frozen selections remain positive in total return and daily Sharpe at
  both 7 and 15 bps per side.

Failure means “no v2 candidate qualified”; it is not permission to alter the
matrix or thresholds. Run unchanged A09 and the five parents as benchmarks on the
same blocks. The v2 native workload is 100 candidates x 12 unique blocks = 1,200
jobs. With six unchanged benchmarks it is 1,272 jobs. Batch the run into blocks
that fit the repository's 20-minute safe-timeout policy, keep three workers, and
use revisioned resume.

The retrospective folds are still contaminated by the fact that v2 was designed
after observing v1's 2020-2026 results. They test robustness and selection
discipline, not untouched out-of-sample edge. A truly new prospective period is
still required before any live claim.

## Multiple-testing disclosure and combination count

- New ParameterSpecs: 5.
- Reused execution/risk controls: 5 (`atr_k`, `reentry_cooldown_bars`,
  `rebalance_hour`, `top_n`, and `exposure_group_limit`), plus the semantic
  resting-stop alias.
- Predeclared overlay recipes: 20.
- Parent lineages: 5.
- Exact v2 configurations: 20 x 5 = 100.
- v2 tests in the selection pool per fold: 100, reduced to at most five lineage
  champions before final selection.
- Cumulative research ledger: 100 v1 H variants + 10 alternatives + 100 v2
  variants = 210 non-control research candidates, plus the unchanged control.

This is not “only 20 tests” merely because recipes repeat across parents. The
selection multiplicity is 100 correlated v2 configurations, and the historical
research multiplicity includes the 110 v1 alternatives used to choose these
parents and knobs. Report all trials and failures. For the frozen selected track,
add a stationary/block-bootstrap confidence interval using 20-session blocks and
10,000 resamples, plus a deflated-Sharpe or equivalent multiple-testing statistic
that states both raw `m=100` and a correlation-based effective trial count. These
statistics are diagnostics; the held-out gates above remain the decision rule.

## Implementation acceptance checklist

1. Registry verifies 211 total configurations with the original 111 unchanged.
2. V001-V100 match this matrix exactly and have unique semantic configurations.
3. All targeted before/after tests and the complete `tests/strategy_lab` suite
   pass.
4. Parent parity hashes pass before running the matrix.
5. A short native pilot for V001, V019, V020, V039, V079, V081, and V100 passes
   independent audit and demonstrates every new diagnostic field.
6. The sequential 1,272-job walk-forward/benchmark program completes with no
   mixed revisions and every job independently audited.
7. Selection locks predate and hash-match their outer results.
8. Primary and 7/15 bps stress reports use frozen selections and independently
   recomputed daily-return Sharpe.
9. Results are labelled discovery/retrospective evidence. No live, paper,
   deployment, release, or LumiBot-core change is implied.
