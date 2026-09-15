# Title: HTS Variation Catalog (Control, H001-H100, A01-A10)

Description: The named, resolved, fingerprinted lookup table for every registered HTS research configuration.

Last Updated: 2026-09-16

Status: Implemented on the native engine; descriptive results in `docs/HTS_NATIVE_RESULTS.md`

Audience: Strategy developers and the strategy owner

## Overview

This catalog is generated from `strategy_lab/experiment_registry.py`; it is the authoritative index of the research plan's candidates. Each row has a stable ID, a human-readable name, a family, and a fingerprint over the fully resolved parameter set. Address a candidate by ID in any runner, artifact path, or conversation.

Execution engine of record: `native-lumibot-backtesting` (`PandasDataBacktesting` plus `BacktestingBroker`). The retired custom local replay qualifies nothing and is not part of this catalog's execution path.

The audited control `HTS_CONTROL_1` is listed separately and is **not** counted as a variation. Totals: 111 configurations = 1 control + 100 HTS variations + 10 alternative strategies.

Planning source: `docs/HTS_100_VARIATIONS_RESEARCH_PLAN.md`; implementation and validation plan: `docs/HTS_REMAINING_IMPLEMENTATION_PLAN.md`; completed descriptive run: `docs/HTS_NATIVE_RESULTS.md`. Every HTS variation and the six daily-data alternatives are implemented on `native-lumibot-backtesting`; A02, A07, A08, and A10 remain `blocked-data` because their inputs are not in the retained archives. Registration is not authorization to paper trade: no live session or broker order is authorized by this catalog.

## How to look these up

```bash
# Full index (this document)
python scripts/list_strategy_experiments.py --list

# One candidate, with resolved parameters
python scripts/list_strategy_experiments.py --show H052

# Filter or search
python scripts/list_strategy_experiments.py --family family-6-concentration
python scripts/list_strategy_experiments.py --kind alternative
python scripts/list_strategy_experiments.py --search correlation

# Rebuild this document or verify the acceptance rules
python scripts/list_strategy_experiments.py --write-catalog docs/HTS_VARIATIONS_CATALOG.md
python scripts/list_strategy_experiments.py --verify
```

## Summary

- Registered configurations: 111
- Audited control: 1
- HTS variations: 100
- Alternative strategies: 10
- Families: 21
- Blocked on required data: A02, A07, A08, A10

## Master index

| ID | Name | Kind | Family | Priority | Status | Fingerprint |
|---|---|---|---|---|---|---|
| HTS_CONTROL_1 | Audited HTS control (HTS_CONTROL_1) | control | Audited HTS control | starting | registered | `40a366fa3530` |
| H001 | Trend filter SMA 10 | hts | Trend-filter speed | standard | registered | `ebb421f426a2` |
| H002 | Trend filter SMA 15 | hts | Trend-filter speed | standard | registered | `80020d1c34d4` |
| H003 | Trend filter SMA 30 | hts | Trend-filter speed | standard | registered | `94ba0b99d3ad` |
| H004 | Trend filter SMA 40 | hts | Trend-filter speed | standard | registered | `8ca2bf8fa295` |
| H005 | Trend filter SMA 50 | hts | Trend-filter speed | standard | registered | `65d6e721d6bd` |
| H006 | Trend filter SMA 60 | hts | Trend-filter speed | standard | registered | `988d36cbf9d1` |
| H007 | Trend filter SMA 80 | hts | Trend-filter speed | standard | registered | `a42c6f79d271` |
| H008 | Trend filter SMA 100 | hts | Trend-filter speed | standard | registered | `2309f31362e1` |
| H009 | Trend filter SMA 150 | hts | Trend-filter speed | standard | registered | `f8cf0ef20473` |
| H010 | Trend filter SMA 200 | hts | Trend-filter speed | standard | registered | `2d61377842ae` |
| H011 | Momentum ranking R(5) | hts | Momentum-ranking horizon | standard | registered | `4b63b3c10453` |
| H012 | Momentum ranking R(10) | hts | Momentum-ranking horizon | standard | registered | `bf34b4b35e27` |
| H013 | Momentum ranking R(15) | hts | Momentum-ranking horizon | standard | registered | `682321545f10` |
| H014 | Momentum ranking R(30) | hts | Momentum-ranking horizon | standard | registered | `d8aee1c7dc6f` |
| H015 | Momentum ranking R(40) | hts | Momentum-ranking horizon | standard | registered | `5426db439b51` |
| H016 | Momentum ranking R(60) | hts | Momentum-ranking horizon | standard | registered | `86e512e08d3f` |
| H017 | Momentum ranking R(90) | hts | Momentum-ranking horizon | standard | registered | `4bebd47b7fbb` |
| H018 | Momentum ranking R(120) | hts | Momentum-ranking horizon | standard | registered | `6aadef0aac04` |
| H019 | Momentum ranking R(180) | hts | Momentum-ranking horizon | standard | registered | `0e5f5a2aa975` |
| H020 | Momentum ranking R(252) | hts | Momentum-ranking horizon | standard | registered | `ad957ff6a587` |
| H021 | ATR(7) x 1.0 stop | hts | ATR responsiveness and distance | standard | registered | `a9358d2f9196` |
| H022 | ATR(7) x 1.5 stop | hts | ATR responsiveness and distance | standard | registered | `3ac49438f09b` |
| H023 | ATR(7) x 2.0 stop | hts | ATR responsiveness and distance | standard | registered | `aeac0968ace3` |
| H024 | ATR(7) x 3.0 stop | hts | ATR responsiveness and distance | standard | registered | `90b506757e95` |
| H025 | ATR(7) x 4.0 stop | hts | ATR responsiveness and distance | standard | registered | `29b46337b695` |
| H026 | ATR(28) x 1.0 stop | hts | ATR responsiveness and distance | standard | registered | `9e285517b6e4` |
| H027 | ATR(28) x 1.5 stop | hts | ATR responsiveness and distance | standard | registered | `e0ef3d52c0c2` |
| H028 | ATR(28) x 2.0 stop | hts | ATR responsiveness and distance | standard | registered | `34ba36afdbcf` |
| H029 | ATR(28) x 3.0 stop | hts | ATR responsiveness and distance | standard | registered | `87a64ea86c75` |
| H030 | ATR(28) x 4.0 stop | hts | ATR responsiveness and distance | standard | registered | `b63bcecd5011` |
| H031 | Two-close stop confirmation | hts | Exit behavior | standard | registered | `72b9037df7e6` |
| H032 | Stop breach buffer 0.25 ATR | hts | Exit behavior | standard | registered | `d8ffa15e496e` |
| H033 | Stop breach buffer 0.50 ATR | hts | Exit behavior | standard | registered | `92669f2c0946` |
| H034 | Chandelier trail from entry high | hts | Exit behavior | standard | registered | `bf85fe15e9b1` |
| H035 | Chandelier trail over 14 bars | hts | Exit behavior | standard | registered | `4503b0322915` |
| H036 | Fixed entry ATR stop | hts | Exit behavior | standard | registered | `012fa7eb1bf1` |
| H037 | Trail plus 10-session time exit | hts | Exit behavior | standard | registered | `5c1fe7d6fcfe` |
| H038 | Break-even ratchet at +2R | hts | Exit behavior | standard | registered | `f22038b45fb2` |
| H039 | Resting broker stop 2 ATR | hts | Exit behavior | deferred | registered | `d4038ae6de5a` |
| H040 | Trail plus emergency 4 ATR stop | hts | Exit behavior | deferred | registered | `85e1ccb24771` |
| H041 | Risk-adjusted R(20) | hts | Quality of the ranking signal | standard | registered | `23caf73a6d04` |
| H042 | Risk-adjusted R(60) | hts | Quality of the ranking signal | standard | registered | `3fdf315a9c83` |
| H043 | Return over downside deviation | hts | Quality of the ranking signal | standard | registered | `06e5e21719e5` |
| H044 | Multi-horizon percentile rank (10/20/60) | hts | Quality of the ranking signal | standard | registered | `e84ab224c4de` |
| H045 | Multi-horizon percentile rank (20/60/120) | hts | Quality of the ranking signal | standard | registered | `70ee579fa2fb` |
| H046 | Regression persistence | hts | Quality of the ranking signal | standard | registered | `8ed97af1d0a9` |
| H047 | Efficiency-ratio momentum | hts | Quality of the ranking signal | standard | registered | `47704a30b11c` |
| H048 | Residual momentum versus QQQ | hts | Quality of the ranking signal | standard | registered | `4ad551fd9340` |
| H049 | Momentum skipping the last five sessions | hts | Quality of the ranking signal | standard | registered | `c806cdea3295` |
| H050 | R(20) with a positive-return requirement | hts | Quality of the ranking signal | standard | registered | `2f9e634a2994` |
| H051 | Top-3 equal-weight holdings | hts | Concentration and allocation | standard | registered | `45aee96954c9` |
| H052 | Top-4 equal-weight holdings | hts | Concentration and allocation | starting | registered | `04c753378c7c` |
| H053 | Top-5 equal-weight holdings | hts | Concentration and allocation | starting | registered | `6fff67e2ea9f` |
| H054 | Top-8 equal-weight holdings | hts | Concentration and allocation | standard | registered | `822fe6312797` |
| H055 | Two inverse-volatility holdings, 60% cap | hts | Concentration and allocation | standard | registered | `c2f06ccc7194` |
| H056 | Four inverse-volatility holdings, 35% cap | hts | Concentration and allocation | standard | registered | `6832cef9c0a6` |
| H057 | Two holdings, 0.25% NAV stop-distance budget | hts | Concentration and allocation | standard | registered | `a17dad37c00e` |
| H058 | Two holdings, 0.50% NAV stop-distance budget | hts | Concentration and allocation | standard | registered | `10f397b9844b` |
| H059 | Four correlation-screened holdings | hts | Concentration and allocation | starting | registered | `fbdd57d21f3f` |
| H060 | Four holdings, one per exposure group | hts | Concentration and allocation | starting | registered | `53f5ff66d587` |
| H061 | Volatility target 10% over 20 sessions | hts | Portfolio volatility target | standard | registered | `6942d20eb3cd` |
| H062 | Volatility target 15% over 20 sessions | hts | Portfolio volatility target | standard | registered | `355164ad5ba6` |
| H063 | Volatility target 20% over 20 sessions | hts | Portfolio volatility target | starting | registered | `f56951f190e5` |
| H064 | Volatility target 25% over 20 sessions | hts | Portfolio volatility target | standard | registered | `34861428b210` |
| H065 | Volatility target 30% over 20 sessions | hts | Portfolio volatility target | standard | registered | `b3620f72be8c` |
| H066 | Volatility target 10% over 60 sessions | hts | Portfolio volatility target | standard | registered | `318ab31a5628` |
| H067 | Volatility target 15% over 60 sessions | hts | Portfolio volatility target | standard | registered | `7a6b59088345` |
| H068 | Volatility target 20% over 60 sessions | hts | Portfolio volatility target | starting | registered | `d97c1cf09b32` |
| H069 | Volatility target 25% over 60 sessions | hts | Portfolio volatility target | standard | registered | `d479ce85eab3` |
| H070 | Volatility target 30% over 60 sessions | hts | Portfolio volatility target | standard | registered | `136fdc595f64` |
| H071 | SPY above SMA50 | hts | Market-condition filters | standard | registered | `a87c719ac77d` |
| H072 | SPY above SMA100 | hts | Market-condition filters | standard | registered | `7c2035ae4bf4` |
| H073 | SPY above SMA200 | hts | Market-condition filters | standard | registered | `3cc7f4ec03d8` |
| H074 | QQQ above SMA100 | hts | Market-condition filters | standard | registered | `080af913711b` |
| H075 | QQQ above SMA200 | hts | Market-condition filters | standard | registered | `f9de520887fe` |
| H076 | SPY and QQQ above SMA200 | hts | Market-condition filters | standard | registered | `58cf923eded3` |
| H077 | Breadth above 50% (SMA50) | hts | Market-condition filters | standard | registered | `d84ad28ee8a3` |
| H078 | Breadth above 60% (SMA50) | hts | Market-condition filters | standard | registered | `dfe852f89e82` |
| H079 | Breadth above 70% (SMA50) | hts | Market-condition filters | standard | registered | `b547a72fb34a` |
| H080 | SPY volatility ratio at most 1.5 | hts | Market-condition filters | standard | registered | `3692f0ce456a` |
| H081 | Weekly rebalance on Monday | hts | Turnover and re-entry discipline | standard | registered | `3ca0d5ce6cf9` |
| H082 | Weekly rebalance on Wednesday | hts | Turnover and re-entry discipline | standard | registered | `ce75cbbcb455` |
| H083 | Weekly rebalance on Friday | hts | Turnover and re-entry discipline | standard | registered | `d79ed7d1e8ef` |
| H084 | Rebalance every second session | hts | Turnover and re-entry discipline | standard | registered | `a5f5c6ed6bf9` |
| H085 | Rebalance every third session | hts | Turnover and re-entry discipline | standard | registered | `55a06bbdbdbc` |
| H086 | Retention buffer to rank 3 | hts | Turnover and re-entry discipline | starting | registered | `db6616839fac` |
| H087 | Retention buffer to rank 4 | hts | Turnover and re-entry discipline | standard | registered | `f302d95586e5` |
| H088 | One-session stop cooldown | hts | Turnover and re-entry discipline | standard | registered | `271d522e6d64` |
| H089 | Three-session stop cooldown | hts | Turnover and re-entry discipline | starting | registered | `db67e4fe8274` |
| H090 | Five-session stop cooldown | hts | Turnover and re-entry discipline | standard | registered | `4be3980e1088` |
| H091 | Unleveraged ETF universe | hts | Universe and predeclared combinations | standard | registered | `0ff42a7d01fd` |
| H092 | Diversified core universe | hts | Universe and predeclared combinations | standard | registered | `3bfd4796c71f` |
| H093 | Sector-only universe | hts | Universe and predeclared combinations | standard | registered | `464a61a3febf` |
| H094 | U0 excluding crypto-linked exposure | hts | Universe and predeclared combinations | standard | registered | `a1b4d70fb84c` |
| H095 | Leveraged exposure capped at 25% NAV | hts | Universe and predeclared combinations | standard | registered | `355d04c04173` |
| H096 | Five holdings with a 20% volatility target | hts | Universe and predeclared combinations | standard | registered | `e44f526666c0` |
| H097 | Regression rank with a 15% volatility target | hts | Universe and predeclared combinations | standard | registered | `4d6ebeceabb4` |
| H098 | Screened four holdings with a 20% volatility target | hts | Universe and predeclared combinations | starting | registered | `1bf59acfb7fa` |
| H099 | QQQ SMA100 gate with a five-session cooldown | hts | Universe and predeclared combinations | standard | registered | `7ebe993f9499` |
| H100 | Resting protective stops with a 20% volatility target | hts | Universe and predeclared combinations | deferred | registered | `ba4b24ee4571` |
| A01 | Multi-horizon time-series momentum | alternative | Multi-horizon time-series momentum | starting | registered | `ca1bb7061d81` |
| A02 | Dual-momentum rotation | alternative | Dual-momentum rotation | standard | blocked-data | `b7b003d910bc` |
| A03 | Slow trend asset allocation | alternative | Slow trend asset allocation | starting | registered | `71604523d1d4` |
| A04 | Daily channel breakout | alternative | Daily channel breakout | standard | registered | `4019c601fc3d` |
| A05 | RSI(2) pullback in an uptrend | alternative | RSI(2) pullback in an uptrend | starting | registered | `593b1d863dd1` |
| A06 | Internal-bar-strength rebound | alternative | Internal-bar-strength rebound | starting | registered | `e37d6e651604` |
| A07 | Gap-down recovery after confirmation | alternative | Gap-down recovery after confirmation | standard | blocked-data | `eb36568176ca` |
| A08 | ETF pairs mean reversion | alternative | ETF pairs mean reversion | standard | blocked-data | `0d54e9f73439` |
| A09 | Trend-filtered risk allocation | alternative | Trend-filtered risk allocation | standard | registered | `639b4a8376fe` |
| A10 | Opening-range breakout | alternative | Opening-range breakout | standard | blocked-data | `8f9ca75f5379` |

## Families

### Audited HTS control (`HTS_CONTROL_1`)

The retained signal and virtual-stop rules, replayed with corrected accounting, calendar handling, and bounded allocation, are the reference everything else is compared against.

Separately versioned; it is not a silent substitution for the previously locked hts_v1.

| ID | Name | Rule |
|---|---|---|
| HTS_CONTROL_1 | Audited HTS control (HTS_CONTROL_1) | Last completed session selection on SMA20, R(20), and 63-session median dollar volume; top two equal slots; hourly ATR14 virtual 2x trail; next-executable exits. |

### Trend-filter speed (`family-1-trend-speed`)

A 20-day filter may respond too readily to short rallies; slower filters might avoid more false starts but enter later.

| ID | Name | Rule |
|---|---|---|
| H001 | Trend filter SMA 10 | Require the completed close to exceed its 10-session SMA. |
| H002 | Trend filter SMA 15 | Require the completed close to exceed its 15-session SMA. |
| H003 | Trend filter SMA 30 | Require the completed close to exceed its 30-session SMA. |
| H004 | Trend filter SMA 40 | Require the completed close to exceed its 40-session SMA. |
| H005 | Trend filter SMA 50 | Require the completed close to exceed its 50-session SMA. |
| H006 | Trend filter SMA 60 | Require the completed close to exceed its 60-session SMA. |
| H007 | Trend filter SMA 80 | Require the completed close to exceed its 80-session SMA. |
| H008 | Trend filter SMA 100 | Require the completed close to exceed its 100-session SMA. |
| H009 | Trend filter SMA 150 | Require the completed close to exceed its 150-session SMA. |
| H010 | Trend filter SMA 200 | Require the completed close to exceed its 200-session SMA. |

### Momentum-ranking horizon (`family-2-rank-horizon`)

Ranking a volatile universe on only one month's gain may select recent spikes; longer lookbacks may rank persistence.

| ID | Name | Rule |
|---|---|---|
| H011 | Momentum ranking R(5) | Rank eligible symbols on R(5); everything else inherits the control. |
| H012 | Momentum ranking R(10) | Rank eligible symbols on R(10); everything else inherits the control. |
| H013 | Momentum ranking R(15) | Rank eligible symbols on R(15); everything else inherits the control. |
| H014 | Momentum ranking R(30) | Rank eligible symbols on R(30); everything else inherits the control. |
| H015 | Momentum ranking R(40) | Rank eligible symbols on R(40); everything else inherits the control. |
| H016 | Momentum ranking R(60) | Rank eligible symbols on R(60); everything else inherits the control. |
| H017 | Momentum ranking R(90) | Rank eligible symbols on R(90); everything else inherits the control. |
| H018 | Momentum ranking R(120) | Rank eligible symbols on R(120); everything else inherits the control. |
| H019 | Momentum ranking R(180) | Rank eligible symbols on R(180); everything else inherits the control. |
| H020 | Momentum ranking R(252) | Rank eligible symbols on R(252); everything else inherits the control. |

### ATR responsiveness and distance (`family-3-atr`)

Stop behavior depends on both the ATR estimator's speed and its multiplier.

| ID | Name | Rule |
|---|---|---|
| H021 | ATR(7) x 1.0 stop | Trail from close minus 1.0 x ATR(7); hourly-close trigger, next-executable exit. |
| H022 | ATR(7) x 1.5 stop | Trail from close minus 1.5 x ATR(7); hourly-close trigger, next-executable exit. |
| H023 | ATR(7) x 2.0 stop | Trail from close minus 2.0 x ATR(7); hourly-close trigger, next-executable exit. |
| H024 | ATR(7) x 3.0 stop | Trail from close minus 3.0 x ATR(7); hourly-close trigger, next-executable exit. |
| H025 | ATR(7) x 4.0 stop | Trail from close minus 4.0 x ATR(7); hourly-close trigger, next-executable exit. |
| H026 | ATR(28) x 1.0 stop | Trail from close minus 1.0 x ATR(28); hourly-close trigger, next-executable exit. |
| H027 | ATR(28) x 1.5 stop | Trail from close minus 1.5 x ATR(28); hourly-close trigger, next-executable exit. |
| H028 | ATR(28) x 2.0 stop | Trail from close minus 2.0 x ATR(28); hourly-close trigger, next-executable exit. |
| H029 | ATR(28) x 3.0 stop | Trail from close minus 3.0 x ATR(28); hourly-close trigger, next-executable exit. |
| H030 | ATR(28) x 4.0 stop | Trail from close minus 4.0 x ATR(28); hourly-close trigger, next-executable exit. |

### Exit behavior (`family-4-exit`)

Confirmation can avoid temporary dips, while an actual protective order can limit exposure between observations.

Fill models for virtual exits and resting protective orders are never interchanged.

| ID | Name | Rule |
|---|---|---|
| H031 | Two-close stop confirmation | Require two consecutive completed hourly closes at or below the active virtual stop. |
| H032 | Stop breach buffer 0.25 ATR | Breach only when close <= stop minus 0.25 x prior completed hourly ATR14. |
| H033 | Stop breach buffer 0.50 ATR | Breach only when close <= stop minus 0.50 x prior completed hourly ATR14. |
| H034 | Chandelier trail from entry high | Trail at the highest completed hourly high since entry minus 3 x current ATR14. |
| H035 | Chandelier trail over 14 bars | Trail at the highest high of the last 14 completed post-entry hourly bars minus 3 x ATR14. |
| H036 | Fixed entry ATR stop | Hold a fixed virtual stop at fill minus 2 x entry-known ATR14; selection exits remain. |
| H037 | Trail plus 10-session time exit | Baseline virtual trail plus a scheduled exit on the tenth exchange session after entry. |
| H038 | Break-even ratchet at +2R | Ratchet the stop to entry once a completed close reaches entry + 2R. |
| H039 | Resting broker stop 2 ATR | Replace the virtual stop with a resting stop-market order at fill minus 2 x entry ATR14. |
| H040 | Trail plus emergency 4 ATR stop | Baseline virtual trail plus a separate emergency resting stop at 4 x ATR14. |

### Quality of the ranking signal (`family-5-rank-quality`)

Reward persistence rather than just the biggest raw price rise.

| ID | Name | Rule |
|---|---|---|
| H041 | Risk-adjusted R(20) | Rank on R(20) divided by 20-session annualized return volatility. |
| H042 | Risk-adjusted R(60) | Rank on R(60) divided by 60-session annualized return volatility. |
| H043 | Return over downside deviation | Rank on R(20) divided by 20-session downside deviation. |
| H044 | Multi-horizon percentile rank (10/20/60) | Rank on the mean cross-sectional percentile of R(10), R(20), and R(60). |
| H045 | Multi-horizon percentile rank (20/60/120) | Rank on the mean cross-sectional percentile of R(20), R(60), and R(120). |
| H046 | Regression persistence | Rank on the 60-session OLS log-price slope times 252 times regression R-squared. |
| H047 | Efficiency-ratio momentum | Rank on R(20) times the 20-session efficiency ratio. |
| H048 | Residual momentum versus QQQ | Rank on the summed 20-session residuals of daily returns regressed on QQQ. |
| H049 | Momentum skipping the last five sessions | Rank on C[t-5]/C[t-65]-1 to skip the most recent week. |
| H050 | R(20) with a positive-return requirement | Keep the R(20) ranking but require R(20) > 0 in addition to trend and liquidity. |

### Concentration and allocation (`family-6-concentration`)

Two highly correlated holdings may be one large economic bet; empty slots stay cash.

| ID | Name | Rule |
|---|---|---|
| H051 | Top-3 equal-weight holdings | Hold the three strongest eligible symbols in equal initial slots. |
| H052 | Top-4 equal-weight holdings | Hold the four strongest eligible symbols in equal initial slots. |
| H053 | Top-5 equal-weight holdings | Hold the five strongest eligible symbols in equal initial slots. |
| H054 | Top-8 equal-weight holdings | Hold the eight strongest eligible symbols in equal initial slots. |
| H055 | Two inverse-volatility holdings, 60% cap | Hold two symbols at normalized inverse vol(20) weights with a 60% per-symbol cap. |
| H056 | Four inverse-volatility holdings, 35% cap | Hold four symbols at normalized inverse vol(20) weights with a 35% per-symbol cap. |
| H057 | Two holdings, 0.25% NAV stop-distance budget | Size each of two entries so the 2 x ATR14 stop distance is 0.25% of NAV. |
| H058 | Two holdings, 0.50% NAV stop-distance budget | Size each of two entries so the 2 x ATR14 stop distance is 0.50% of NAV. |
| H059 | Four correlation-screened holdings | Fill four slots, rejecting any symbol correlated above 0.80 with an accepted holding. |
| H060 | Four holdings, one per exposure group | Fill four slots with at most one instrument from each economic-exposure group. |

### Portfolio volatility target (`family-7-vol-target`)

Scaling gross exposure to a covariance-based volatility target may reduce drawdown, but the leverage and the estimator both change the result.

| ID | Name | Rule |
|---|---|---|
| H061 | Volatility target 10% over 20 sessions | Scale equal-slot weights to a 10% annual target using a 20-session covariance; never scale above the 99.5% cap. |
| H062 | Volatility target 15% over 20 sessions | Scale equal-slot weights to a 15% annual target using a 20-session covariance; never scale above the 99.5% cap. |
| H063 | Volatility target 20% over 20 sessions | Scale equal-slot weights to a 20% annual target using a 20-session covariance; never scale above the 99.5% cap. |
| H064 | Volatility target 25% over 20 sessions | Scale equal-slot weights to a 25% annual target using a 20-session covariance; never scale above the 99.5% cap. |
| H065 | Volatility target 30% over 20 sessions | Scale equal-slot weights to a 30% annual target using a 20-session covariance; never scale above the 99.5% cap. |
| H066 | Volatility target 10% over 60 sessions | Scale equal-slot weights to a 10% annual target using a 60-session covariance; never scale above the 99.5% cap. |
| H067 | Volatility target 15% over 60 sessions | Scale equal-slot weights to a 15% annual target using a 60-session covariance; never scale above the 99.5% cap. |
| H068 | Volatility target 20% over 60 sessions | Scale equal-slot weights to a 20% annual target using a 60-session covariance; never scale above the 99.5% cap. |
| H069 | Volatility target 25% over 60 sessions | Scale equal-slot weights to a 25% annual target using a 60-session covariance; never scale above the 99.5% cap. |
| H070 | Volatility target 30% over 60 sessions | Scale equal-slot weights to a 30% annual target using a 60-session covariance; never scale above the 99.5% cap. |

### Market-condition filters (`family-8-market-gate`)

A broad-market trend gate may keep risk off during sustained downtrends.

| ID | Name | Rule |
|---|---|---|
| H071 | SPY above SMA50 | Hold risk only while SPY's prior close exceeds its 50-session SMA. |
| H072 | SPY above SMA100 | Hold risk only while SPY's prior close exceeds its 100-session SMA. |
| H073 | SPY above SMA200 | Hold risk only while SPY's prior close exceeds its 200-session SMA. |
| H074 | QQQ above SMA100 | Hold risk only while QQQ's prior close exceeds its 100-session SMA. |
| H075 | QQQ above SMA200 | Hold risk only while QQQ's prior close exceeds its 200-session SMA. |
| H076 | SPY and QQQ above SMA200 | Hold risk only while both SPY and QQQ exceed their own 200-session SMA. |
| H077 | Breadth above 50% (SMA50) | Hold risk only while more than 50% of the equity-ETF breadth basket exceeds its SMA50. |
| H078 | Breadth above 60% (SMA50) | Hold risk only while more than 60% of the breadth basket exceeds its SMA50. |
| H079 | Breadth above 70% (SMA50) | Hold risk only while more than 70% of the breadth basket exceeds its SMA50. |
| H080 | SPY volatility ratio at most 1.5 | Hold risk only while SPY vol(20)/vol(120) is at most 1.5. |

### Turnover and re-entry discipline (`family-9-turnover`)

Slower rebalancing and cooldowns may cut churn without abandoning risk exits.

| ID | Name | Rule |
|---|---|---|
| H081 | Weekly rebalance on Monday | Select and rebalance at the first exchange session on or after Monday. |
| H082 | Weekly rebalance on Wednesday | Select and rebalance at the first exchange session on or after Wednesday. |
| H083 | Weekly rebalance on Friday | Select and rebalance at the first exchange session on or after Friday. |
| H084 | Rebalance every second session | Select and rebalance every second exchange session from the frozen anchor date. |
| H085 | Rebalance every third session | Select and rebalance every third exchange session from the frozen anchor date. |
| H086 | Retention buffer to rank 3 | Retain an eligible holding while its rank is 3 or better, then fill spare capacity. |
| H087 | Retention buffer to rank 4 | Retain an eligible holding while its rank is 4 or better, then fill spare capacity. |
| H088 | One-session stop cooldown | Bar a stopped symbol from new entry for one complete subsequent exchange session. |
| H089 | Three-session stop cooldown | Bar a stopped symbol from new entry for three complete subsequent exchange sessions. |
| H090 | Five-session stop cooldown | Bar a stopped symbol from new entry for five complete subsequent exchange sessions. |

### Universe and predeclared combinations (`family-10-universe-and-combinations`)

Exposure restrictions and five combinations frozen before results test whether the simplest, most diversified readings survive.

| ID | Name | Rule |
|---|---|---|
| H091 | Unleveraged ETF universe | Restrict U0 to unleveraged ETFs and trusts, dropping leveraged products and MSTR/COIN. |
| H092 | Diversified core universe | Trade a fixed diversified basket: SPY, QQQ, IWM, EFA, EEM, GLD, IEF, TLT, DBC, VNQ, BIL. |
| H093 | Sector-only universe | Trade the eleven sector SPDRs only. |
| H094 | U0 excluding crypto-linked exposure | Drop MSTR, COIN, BITX, and IBIT to test reliance on crypto-linked moves. |
| H095 | Leveraged exposure capped at 25% NAV | Cap aggregate leveraged-product market value at 25% of NAV; overflow stays cash. |
| H096 | Five holdings with a 20% volatility target | Combine top-5 equal holdings with a 20-session, 20% portfolio volatility target. |
| H097 | Regression rank with a 15% volatility target | Combine regression-persistence ranking with a 60-session, 15% volatility target. |
| H098 | Screened four holdings with a 20% volatility target | Combine the 0.80 correlation screen with a 20-session, 20% volatility target. |
| H099 | QQQ SMA100 gate with a five-session cooldown | Combine the QQQ SMA100 gate with the five-session post-stop cooldown. |
| H100 | Resting protective stops with a 20% volatility target | Combine resting 2-ATR protective stops with a 60-session, 20% volatility target. |

### Multi-horizon time-series momentum (`A01`)

Slower independent trends offer an alternative to selecting only recent top performers.

| ID | Name | Rule |
|---|---|---|
| A01 | Multi-horizon time-series momentum | At each completed month end, size each fixed 1/10 sleeve by the fraction of positive R(63), R(126), and R(252) votes; rebalance at the next session open. |

### Dual-momentum rotation (`A02`)

Require both relative strength and an absolute hurdle before holding risk.

| ID | Name | Rule |
|---|---|---|
| A02 | Dual-momentum rotation | At month end hold the stronger of SPY and EFA on trailing 252-session total return, but only if it beats BIL's total return; otherwise hold BIL. |

### Slow trend asset allocation (`A03`)

Slow decisions and broad allocation reduce churn relative to intraday selection.

| ID | Name | Rule |
|---|---|---|
| A03 | Slow trend asset allocation | Five equal sleeves hold their asset only while the month-end close exceeds its 10-month SMA; each active sleeve targets 19.9% NAV, otherwise the sleeve is cash. |

### Daily channel breakout (`A04`)

Require a genuine range breakout rather than just an SMA test.

| ID | Name | Rule |
|---|---|---|
| A04 | Daily channel breakout | Enter after a completed close exceeds the highest high of the preceding 55 sessions; exit below the prior 20-session low or at a virtual stop of entry minus 3 x ATR20. |

### RSI(2) pullback in an uptrend (`A05`)

Buy temporary weakness instead of recent winners.

| ID | Name | Rule |
|---|---|---|
| A05 | RSI(2) pullback in an uptrend | Enter at the next open when the daily close exceeds SMA200 and Wilder RSI(2) is below 5; exit at the next open above SMA5 or after five held sessions. |

### Internal-bar-strength rebound (`A06`)

An unusually weak daily finish may partially reverse.

| ID | Name | Rule |
|---|---|---|
| A06 | Internal-bar-strength rebound | Enter at the next open when IBS below 0.2 and close below SMA5; exit at the next open when IBS exceeds 0.8 or after three held sessions. |

### Gap-down recovery after confirmation (`A07`)

Some overnight pressure reverses after opening price discovery.

| ID | Name | Rule |
|---|---|---|
| A07 | Gap-down recovery after confirmation | After a prior close above SMA200 and a regular-session open gap of at least 0.5 x ATR20, buy only once the first verified opening hourly bar closes above its own open. |

### ETF pairs mean reversion (`A08`)

A stationary relative price may mean-revert even when the legs are volatile.

| ID | Name | Rule |
|---|---|---|
| A08 | ETF pairs mean reversion | Fit a log-price hedge ratio on 252 sessions at each month end, require an Engle-Granger p-value below 0.05, then trade the spread z-score at |z| > 2 and exit at |z| < 0.5. |

### Trend-filtered risk allocation (`A09`)

Diversify sources of portfolio risk instead of dollars.

| ID | Name | Rule |
|---|---|---|
| A09 | Trend-filtered risk allocation | At month end exclude assets below SMA200, shrink the 126-session covariance, solve long-only equal risk contributions, cap each asset at 35% NAV, and hold the remainder in cash. |

### Opening-range breakout (`A10`)

The first thirty minutes may establish a level that later momentum continues through.

| ID | Name | Rule |
|---|---|---|
| A10 | Opening-range breakout | Define each day's range from 09:30-10:00 ET one-minute bars, enter on a completed one-minute close above the range high, and exit at 15:55 ET or the protective range low. |

## Data prerequisites

| ID | Requirement | Reason |
|---|---|---|
| A02 | bil-distributions | Requires verified BIL total-return distributions before qualification. |
| A07 | validated-session-open-bars | Requires validated session-open bars and a session-integrity audit. |
| A08 | short-borrow-data, two-leg-execution-model | Prices alone are insufficient: borrow availability, borrow rates, and a two-leg execution model are required before this can qualify. |
| A10 | one-minute-bars, exchange-calendar | Requires one-minute bars and an exchange calendar; it is not implementable from the retained hourly cache alone. |

## Previously deferred candidates

These candidates were deferred pending resting protective-stop support in the native strategy. That support is implemented and their descriptive runs are complete; see `docs/HTS_NATIVE_RESULTS.md` for the fill-fidelity caveat on simulated stop prices: H039, H040, H100.
