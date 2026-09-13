# Title: HTS Research Plan — 100 Variations and 10 Alternative Strategies

Description: A fixed candidate catalog and implementation plan for causal, reproducible strategy experiments using the retained local cache.

Last Updated: 2026-09-13
Status: Proposed; research and read-only inspection completed; strategies not implemented or run
Audience: Strategy developers and the strategy owner

## Overview

Build exactly **100 HTS candidates (H001–H100)** and **10 alternative strategies (A01–A10)**. Keep a separately versioned, audited HTS control outside those counts. Preserve the existing `hts_v1` artifacts as historical diagnostics. Do not revive the retired oracle engine.

The objective is to find repeatable returns after costs with tolerable losses, rather than the largest Sharpe obtainable from a search. Proposed North Star: standard daily-return Sharpe of the predeclared walk-forward selection procedure, considered alongside worst drawdown, cost sensitivity, and uncertainty. Acceptance objectives: 100% accounting/causality/parity gates passing; every attempted candidate recorded; no finalist supported solely by one favorable window. A Sharpe above 2 is a hypothesis to test, not an implementation requirement.

The first 90 HTS candidates isolate particular rule families. H091–H095 test explicit universes/exposure restrictions; H096–H100 are five combinations chosen before looking at their results. They are not 100 copies with randomly changed numbers. Parameter interactions beyond these five belong to a separately counted later experiment.

This document is a plan only. No backtest, paper session, broker order, package change, or deployment is authorized by execution of the research step itself.

## 1. What the current code and cache actually contain

Inspected `strategy_lab/hts_v1_core.py`, `strategy_lab/hts_v1_strategy.py` through its documented contract, `scripts/backtest_hts_v1_local.py`, `strategy_lab/hts_backtest.py`, and `docs/HTS_V1_PAPER_PARITY.md`.

The shared core currently selects up to two eligible symbols by 20-session return, requires price above its 20-session SMA and 63-session median dollar volume of at least $5 million, and uses a simple rolling 14-hour ATR with a 2× ATR trailing level. Intraday selection uses prior completed daily sessions. Stops are virtual: a completed hourly close at/below the previously active level creates a later market-exit intent. The local executor approximates that fill using the following hourly open. Configuration currently uses signal-hour label 9 and rebalance-hour label 10 in New York time; those labels are not yet a verified statement of bar completion times.

The existing shared-core replay is a custom local ledger. It must not be described as a full native LumiBot backtest. Native LumiBot qualification remains a separate required step.

Read-only cache inventory on the date above:

| Cache | Table | Rows | Symbols | Observed range |
|---|---|---:|---:|---|
| `short/suite_monitored_xnas_itch_daily_adjusted.duckdb` | `bars_daily` | 182,516 | 90 | Session labels 2018-05-01 through 2026-09-10 |
| `short/suite_v2_xnas_itch_hourly_adjusted.duckdb` | `bars_hourly` | 1,369,018 | 57 | Hour labels passing the current 9–15 ET filter extend through 2026-09-08 15:00 ET |

These counts establish availability, not correctness or complete regular-session coverage. A provisional common experiment window is **[2020-09-09, 2026-09-09)** in exchange-session dates, with a two-year view **[2024-09-09, 2026-09-09)**. This includes September 8, 2026 if the calendar/data audit establishes that session is complete. Freeze the last complete common session, not the largest timestamp in any file. If the end changes, regenerate all fold boundaries together. Preload earlier cached history for warmup.

**Concrete cache anomaly:** daily rows labelled `IBIT` begin on 2022-09-08, whereas its hourly rows begin in January 2024. Nasdaq states the iShares Bitcoin Trust began listing on January 11, 2024. This requires instrument-ID, symbol-mapping, and pre-inception-row investigation; it does not establish how much any prior result was affected. Quarantine unresolved rows in a derived view, preserving the source cache. [Nasdaq listing notice](https://www.nasdaqtrader.com/TraderNews.aspx?id=ETP2024-04).

## 2. Phase zero: blockers before trusting a large search

These are observed code paths, not a completed estimate of their effect on old performance. Version repairs and rerun the control before claiming corrected Sharpe numbers.

| Priority | Observation | Required implementation and acceptance evidence |
|---|---|---|
| P0 | `backtest_hts_v1_local.py:148` excludes held symbols without a bar from portfolio value. | Maintain last valid marks with age flags; never remove an owned asset from NAV because its bar is absent. Block fresh trading on stale quotes; halt qualification on unresolved prolonged gaps/delistings. Synthetic missing-bar replay must preserve cash plus marked holdings. |
| P0 | Runner lines 134–145 moves orders to inflight, then skips unavailable symbols without retrying them. Core line 355 omits inflight buys from occupied symbols; line 391 overwrites quantity on a buy acknowledgement. | Explicit order lifecycle; deduplicate intent/fill IDs; retry or expire according to a frozen rule; aggregate actual quantities; reconcile position/cash after every event. Test missing bars, duplicate callbacks, partial fills, and recovery. |
| P0 | Core line 363 sizes new positions from NAV without enforcing aggregate buying power; the local fill function accepts debit cash. | Separate strategy target weights from executable orders and buying-power checks. Preserve an unmodified legacy diagnostic; give the bounded control a new revision. Test retained winners, multiple replacements, opening gaps, costs, rejected orders, and nonpositive equity. |
| P0 | The runner's `sharpe` means CAGR/volatility. Initial capital is absent from daily-return/drawdown anchors. | Use standard arithmetic excess-return Sharpe as primary, and label the legacy ratio `cagr_over_volatility`. Include initial capital and every eligible session. Independently verify metrics from the ledger. |
| P1 | RTH is filtered by integer hour rather than exchange-calendar intervals. | Store bar start/end, available-at, decision, submission, and fill times. Validate DST, early closes, and partial opening bars. An hour containing premarket trades cannot be cleaned by relabelling it. If necessary obtain appropriate bars through the configured data adapter before qualifying affected candidates. |
| P1 | Selection accepts a symbol's most recent prior daily bar even if stale. | Require the expected previous session and sufficient valid history; distinguish market holidays from symbol gaps. Check finite OHLCV, OHLC ordering, duplicates, zero-volume/synthetic bars, and instrument identity. |
| P1 | Split adjustment is declared rather than established by a manifest; dividends are not booked by the replay. | Verify split factors and volume adjustment, daily/hourly consistency, distributions, and identifier history. Use raw execution prices plus corporate actions, or a mathematically equivalent verified adjusted ledger. A price-only run must be labelled as such and cannot qualify total-return bond/cash strategies. |
| P1 | Financing adds one day's interest at each new session, including after weekends. | Charge actual elapsed calendar days on defined debit balances; keep broker-independent fees, slippage, interest, and dividends separate. |
| P1 | Final report does not explain remaining positions and stranded intents. | Publish marked terminal NAV, positions, stale marks, pending orders, and estimated liquidation costs separately. Never invent a terminal executable fill. |

Data provenance also needs to establish what `XNAS_ITCH` coverage means for volume. An exchange-feed volume is not automatically consolidated market volume. Do not use it as total-market capacity without validation. A present-day list of 57 symbols is a fixed research universe, not a historical investable-universe reconstruction; identify survivorship/selection bias explicitly.

## 3. Freeze the common experimental contract

Create `HTS_CONTROL_1` after phase zero. It retains the existing signal and virtual-stop rules but uses the audited accounting, calendar, and bounded allocation below. It is not silently substituted for the previously locked `hts_v1`.

- **Data:** immutable manifest of files, schemas, checksums, feed, adjustment basis, instrument eligibility dates, session boundaries, exclusions, and quality flags. Source databases remain read-only. No silent provider substitution or network download during a trial.
- **Universe U0:** the exact ordered 57-symbol `DEFAULT_UNIVERSE` from `strategy_lab/hts_backtest.py`, recorded in the manifest. A symbol becomes eligible only after verified listing and all required warmups. Remove uncertain rows by identity/date, not because they lose money.
- **Signal:** last completed session only; SMA20, return20, median dollar volume over 63 sessions >= $5 million; deterministic ties by frozen universe order. Do not require positive return unless the candidate explicitly says so.
- **Timing:** retain the two existing signal/rebalance bar slots after resolving their true completion times. Stops evaluated at each valid hourly completion. Record literal timestamps in the manifest; do not guess that a 09:00 label is an executable 09:00 decision.
- **Allocation control:** long-only; maximum two active/pending entry slots; target initial notional 99.5% of NAV divided by two, leaving a 0.5% transaction/gap reserve. Retained holdings may drift; at scheduled rebalances proportionally trim only if aggregate marked gross exposure exceeds 100% of NAV. Allocate new buys only from remaining budget after known/reserved obligations. No intentional margin borrowing; embedded leverage in ETFs still counts as economic risk.
- **Execution constraint:** determine submitted quantities with information available at submission. Process planned sells before dependent buys where the event path supports this. If a gap makes a submitted order unaffordable, reject it and journal the rejection; do not retrospectively choose an affordable quantity using the future open. A later retry needs a new causal decision. Full fills remain an approximation, and capacity tests constrain them.
- **Costs:** primary effective friction 3.5 bps each side, or 7 bps round trip, matching the previous assumption. It is not claimed to be an Alpaca commission schedule. Stress at 7 and 15 bps each side. No cost level is a tunable strategy parameter.
- **Stops:** inspect the level that existed before the completed bar; if breached, queue a market exit; otherwise ratchet upward from close minus 2× ATR14. Initial level uses the actual entry fill and ATR known when entry was decided. No retrospective fill at an already-crossed stop after observing the close.
- **Next-open approximation:** the immediately adjacent bar's opening print is not guaranteed obtainable after bar finalization and submission. Retain it as a labelled research convention, stress an additional hourly delay, and qualify finalist fills using finer data/recorded paper latency when available.
- **Cash and benchmarks:** zero cash interest/zero risk-free rate is an explicitly labelled comparison convention; also report an excess-return version using validated contemporaneous cash returns once distributions are available. Do not treat BIL price appreciation alone as its yield. Compare QQQ and SPY on identical dates, return basis, initial capital, and stated trading costs.
- **Terminal treatment:** marked NAV is primary; include open positions and pending orders. Fold liquidations occur only at an actual subsequent executable event and their costs belong to that fold's protocol.

All candidates inherit this contract except the explicitly listed changes. The original baseline is preserved separately as a diagnostic. Differences due to audited accounting or bounded sizing must be shown before differences due to candidate rules.

## 4. Research basis and limits

The sources motivate hypotheses. None validates the exact parameters below or promises Sharpe >2 on this cache.

| Research | Implication for this experiment | Important limit |
|---|---|---|
| [Moskowitz, Ooi & Pedersen: Time Series Momentum](https://w4.stern.nyu.edu/facdir/lpederse/papers/TimeSeriesMomentum.pdf) | Test longer trend horizons and own-return signals. | Futures and diversified long/short evidence does not transfer automatically to concentrated long-only leveraged ETFs. |
| [Moreira & Muir: Volatility Managed Portfolios](https://www.nber.org/papers/w22208) | Test reducing exposure when measured volatility rises. | Our capped inverse-volatility target is an adaptation, not an exact replication of their inverse-variance portfolios. |
| [Cederburg et al.: On the Performance of Volatility-Managed Portfolios](https://www.lehigh.edu/~xuy219/research/COWY.pdf) | Include unmanaged controls and truly causal sizing. | Their broader tests do not find systematic direct outperformance; volatility management is not a guaranteed improvement. |
| [Faber: tactical allocation research](https://mebfaber.com/white-papers/) | Test slower filters and monthly decisions. | Sideways markets and delayed re-entry can hurt trend filters. |
| [Antonacci: relative and absolute momentum](https://www.optimalmomentum.com/dual-relative-absolute-momentum/) | Separate being better than peers from having a positive absolute trend. | Our cached ETF implementation is an adaptation, not a reproduction of published GEM results. |
| [Maillard, Roncalli & Teiletche: equal risk contributions](https://www.thierry-roncalli.com/download/erc.pdf) | Compare equal capital, inverse volatility, and covariance-aware risk allocation. | Inverse volatility is not generally the same as equal risk contribution. |
| [Bailey & López de Prado: Deflated Sharpe Ratio](https://www.davidhbailey.com/dhbpapers/deflated-sharpe.pdf) | Count all trials and report selection-adjusted evidence. | DSR is assumption-dependent; it cannot repair bad data or turn previously inspected history into an untouched test. |

The exact buffers, cooldowns, correlation thresholds, and five combinations below are original design hypotheses for this project, not results asserted by the papers.

## 5. Exact HTS catalog: H001–H100

Definitions: `R(n) = C[t]/C[t-n]-1`, using the previous completed daily session as t. `vol(n)` is annualized sample standard deviation of n daily simple returns. `ATR(n)` is the simple mean of n hourly true ranges, matching the existing baseline convention. `DDvol(n) = sqrt(252 * mean(min(r,0)^2))`. Use at least n+1 closes for n returns. Invalid/zero denominators make a symbol ineligible; no infinity scores. All unspecified parameters inherit `HTS_CONTROL_1`.

### Family 1 — Trend-filter speed

Hypothesis: a 20-day filter may respond too readily to short rallies; slower filters might avoid more false starts but enter later. Only the symbol trend SMA changes.

| ID | SMA sessions |
|---|---:|
| H001 | 10 |
| H002 | 15 |
| H003 | 30 |
| H004 | 40 |
| H005 | 50 |
| H006 | 60 |
| H007 | 80 |
| H008 | 100 |
| H009 | 150 |
| H010 | 200 |

### Family 2 — Momentum-ranking horizon

Hypothesis: ranking a volatile universe on only one month's gain may select recent spikes. Only return lookback changes.

| ID | Rank on return over sessions |
|---|---:|
| H011 | 5 |
| H012 | 10 |
| H013 | 15 |
| H014 | 30 |
| H015 | 40 |
| H016 | 60 |
| H017 | 90 |
| H018 | 120 |
| H019 | 180 |
| H020 | 252 |

### Family 3 — ATR responsiveness and distance

Hypothesis: stop behavior depends on both the ATR estimator's speed and its multiplier. Ten predetermined combinations; all retain hourly-close triggers and next-executable market exits.

| ID | ATR hourly bars | Multiplier |
|---|---:|---:|
| H021 | 7 | 1.0 |
| H022 | 7 | 1.5 |
| H023 | 7 | 2.0 |
| H024 | 7 | 3.0 |
| H025 | 7 | 4.0 |
| H026 | 28 | 1.0 |
| H027 | 28 | 1.5 |
| H028 | 28 | 2.0 |
| H029 | 28 | 3.0 |
| H030 | 28 | 4.0 |

### Family 4 — Exit behavior

Hypothesis: confirmation can avoid temporary dips, while an actual protective order can limit exposure between observations. These are different executable strategies; their fill models are never silently interchanged.

| ID | Exact change |
|---|---|
| H031 | Require two consecutive completed hourly closes <= the active virtual stop. Freeze the trail during the first breach; reset the count if price recovers above it. |
| H032 | Breach only when close <= active stop minus 0.25× prior completed hourly ATR14. Otherwise use the normal trailing update. |
| H033 | As H032 with a 0.50× ATR buffer. |
| H034 | Chandelier virtual trail: max(previous stop, highest completed hourly high since entry minus 3× current ATR14). Start at fill minus 3× entry-known ATR14. New level active only after this bar. |
| H035 | As H034, but highest high over the last 14 completed post-entry hourly bars. Use available post-entry bars until 14 exist. |
| H036 | Fixed virtual stop at fill minus 2× entry-known ATR14; never trail. Selection-change exits remain active. |
| H037 | Baseline virtual trail plus exit at the rebalance decision on the tenth exchange session after the entry session. |
| H038 | Baseline trail plus break-even ratchet once a completed close reaches entry + 2R, where R=2× entry-known ATR14. Set stop >= entry, effective next bar. This does not guarantee a break-even fill. |
| H039 | Replace the virtual stop with a resting broker stop-market order, initially fill minus 2× entry-known ATR14, then amended after completed bars. Trigger on subsequent intrabar low; gap below the active level fills at the worse opening price plus friction. |
| H040 | Keep the baseline virtual trail and add a separate emergency resting stop initially 4× entry-known ATR14 below fill, ratcheted using close minus 4× ATR14. Protective fills take precedence; cancel sibling exits and prohibit double-selling. |

H039/H040 require native broker-order lifecycle support, entry-bar activation tests, and a clear cancel/replace assumption. Hourly OHLC stop simulation cannot prove real stop-market execution quality. Do not activate a revised trail earlier in the bar that produced it.

### Family 5 — Quality of the ranking signal

Hypothesis: reward persistence rather than just the biggest raw price rise. Only ranking/explicit eligibility changes.

| ID | Ranking rule |
|---|---|
| H041 | R(20) / vol(20). |
| H042 | R(60) / vol(60). |
| H043 | R(20) / DDvol(20). |
| H044 | Mean cross-sectional percentile rank of R(10), R(20), R(60). |
| H045 | Mean cross-sectional percentile rank of R(20), R(60), R(120). |
| H046 | OLS slope of log close on session index over 60 closes, multiplied by 252 and regression R-squared. |
| H047 | R(20) × efficiency ratio, where efficiency = abs(C[t]-C[t-20]) / sum(abs(one-session price changes)) over 20 changes. |
| H048 | Sum of last 20 daily residuals from a per-symbol OLS of daily returns on QQQ returns plus intercept, fitted on the last 60 completed returns. QQQ itself has score zero; missing aligned history makes a symbol ineligible. |
| H049 | Momentum skipping the last five sessions: C[t-5]/C[t-65]-1. |
| H050 | Retain R(20) ranking but require R(20)>0 in addition to SMA/liquidity eligibility. |

Percentile ranks use only the symbols eligible that session, ascending so stronger returns have larger scores; ties use average rank followed by the frozen universe order. Multi-component candidates require all components.

### Family 6 — Concentration and allocation

Hypothesis: two highly correlated holdings may be one large economic bet. Empty slots stay cash; never enlarge remaining positions to fill unused slots unless specified.

| ID | Allocation change |
|---|---|
| H051 | top_n=3, equal initial notional. |
| H052 | top_n=4, equal initial notional. |
| H053 | top_n=5, equal initial notional. |
| H054 | top_n=8, equal initial notional. |
| H055 | top_n=2; normalized inverse vol(20) weights; rebalance held quantities to target daily; 60% per-symbol cap, overflow to cash. |
| H056 | top_n=4; inverse vol(20) daily targets; 35% per-symbol cap, overflow to cash. |
| H057 | top_n=2; initial stop-distance budget 0.25% NAV per position: quantity=floor(0.0025×NAV/(2×ATR14)); cap each entry at control's per-slot notional and available aggregate budget. |
| H058 | As H057 with a 0.50% NAV stop-distance budget. |
| H059 | top_n=4; scan baseline rank order, accepting a symbol only if absolute 60-session return correlation with each already accepted symbol is <0.80. Require complete aligned history. Equal 1/4 initial slots. |
| H060 | top_n=4; at most one instrument from each documented economic-exposure group. Scan ranking until four slots are filled or candidates end. Equal 1/4 initial slots. |

For H060 freeze issuer/underlying-based groups before tests: QQQ/TQQQ, SPY/SSO/UPRO, SMH/SOXX/SOXL, XLK/TECL, IWM/TNA, XLF/FAS, XLE/ERX, XBI/LABU, GDX/NUGT, UNG/BOIL, and MSTR/COIN/IBIT/BITX. Other symbols are singleton groups. This is a deliberately broad economic-exposure constraint, not a claim those securities have identical underlying holdings. Validate leverage/group metadata against issuer information during implementation. H060 is distinct from correlation-based H059.

Stop-distance budgets are sizing heuristics, not maximum-loss guarantees; a gap or delayed virtual exit can lose more than the budget.

### Family 7 — Portfolio volatility target

For the selected portfolio's equal-slot unscaled weights w, estimate annual covariance Sigma from the last L aligned completed daily returns and set gross scale `g=min(0.995,target/sqrt(w' Sigma w))`. No upward scaling beyond the cap. Rebalance actual holdings to the scaled weights at the daily rebalance decision, including trims; sizing is part of the strategy. Missing required covariance history prevents entry, rather than substituting full exposure. Use actual ETF return volatility, including embedded leverage.

| ID | L sessions | Annual volatility target |
|---|---:|---:|
| H061 | 20 | 10% |
| H062 | 20 | 15% |
| H063 | 20 | 20% |
| H064 | 20 | 25% |
| H065 | 20 | 30% |
| H066 | 60 | 10% |
| H067 | 60 | 15% |
| H068 | 60 | 20% |
| H069 | 60 | 25% |
| H070 | 60 | 30% |

This changes both exposure and rebalancing. Report the contribution of each in diagnostics rather than attributing all improvement to the volatility estimator. A forecast target is not a bound on realized volatility.

### Family 8 — Market-condition filters

A false filter sets target selection to empty at the next scheduled rebalance, queues exits, and blocks entries; hourly stops still operate. All inputs come from completed prior daily sessions. Missing required market data invalidates the session's qualification rather than opening the gate silently.

| ID | Additional condition required to hold risk |
|---|---|
| H071 | SPY close > SMA50. |
| H072 | SPY close > SMA100. |
| H073 | SPY close > SMA200. |
| H074 | QQQ close > SMA100. |
| H075 | QQQ close > SMA200. |
| H076 | Both SPY and QQQ above their own SMA200. |
| H077 | More than 50% of a fixed unleveraged equity-ETF basket above their own SMA50. |
| H078 | As H077, threshold >60%. |
| H079 | As H077, threshold >70%. |
| H080 | SPY vol(20)/vol(120) <=1.5. |

Breadth basket: SPY, QQQ, IWM, XLK, XLE, XLF, XLY, XLV, XLI, XLP, XLU, XLRE, XLB, XLC. Require valid prior-session history for all members after their verified inception; otherwise mark the fold ineligible until full warmup exists. Do not count leveraged clones as separate breadth votes.

### Family 9 — Turnover and re-entry discipline

Scheduled-selection changes do not disable hourly risk exits. If stopped between scheduled selections, leave the slot cash until the next allowed selection/rebalance. For weekday rules, execute at the first exchange session on or after the named weekday within that calendar week; skip if none exists.

| ID | Change |
|---|---|
| H081 | Select/rebalance weekly on Monday. |
| H082 | Select/rebalance weekly on Wednesday. |
| H083 | Select/rebalance weekly on Friday. |
| H084 | Select/rebalance every second exchange session, anchored to the first valid session on/after 2020-09-09; retain the same anchor in every fold. |
| H085 | As H084, every third exchange session. |
| H086 | Rank buffer: retain an eligible holding while rank <=3, fill remaining two-slot capacity from the strongest unheld eligible symbols. |
| H087 | As H086, retention rank <=4. |
| H088 | After a stop fill, bar that symbol from new entry for one complete subsequent exchange session. |
| H089 | As H088, three complete subsequent sessions. |
| H090 | As H088, five complete subsequent sessions. |

For H086/H087 eligibility failures and stops always override retention; rank is among daily trend/liquidity-eligible symbols. Cooldowns start from actual stop execution, not from a signal that might never fill.

### Family 10 — Universe and predeclared combinations

| ID | Change |
|---|---|
| H091 | U0 minus leveraged products and individual stocks MSTR/COIN; retain unleveraged ETFs/trusts only after verified identity and listing. |
| H092 | Fixed diversified universe: SPY, QQQ, IWM, EFA, EEM, GLD, IEF, TLT, DBC, VNQ, BIL. |
| H093 | Sector-only universe: XLK, XLE, XLF, XLY, XLV, XLI, XLP, XLU, XLRE, XLB, XLC. |
| H094 | U0 excluding MSTR, COIN, BITX, IBIT, to test reliance on crypto-linked exposure. |
| H095 | U0 with total market-value allocation to leveraged products capped at 25% NAV at scheduled rebalances; overflow stays cash. |
| H096 | Combine H053 (five holdings) with H063 (20% vol target, 20-session covariance). |
| H097 | Combine H046 (regression-persistence rank) with H067 (15% target, 60-session covariance). |
| H098 | Combine H059 (correlation-screened four holdings) with H063 (20% target, 20-session covariance). |
| H099 | Combine H074 (QQQ SMA100 gate) with H090 (five-session stop cooldown). |
| H100 | Combine H039 (resting protective stops) with H068 (20% target, 60-session covariance). |

Initial leveraged-product list to verify: TQQQ, UPRO, SSO, UDOW, TNA, SOXL, TECL, FAS, ERX, LABU, NUGT, BITX, BOIL. Use dated instrument metadata if product objectives changed. H091/H095 must not infer leverage from recent returns. For combinations, apply eligibility/ranking, then selection constraints, then portfolio targets/caps, then order construction; protective exits override buys and allocation trims.

## 6. Ten additional strategy specifications

These are **ten distinct seeds**, not ten unspecified parameter-search spaces. Seed thresholds are proposed project choices unless explicitly described otherwise. Broader tuning comes later and counts as additional trials. ETF-based tests adapt research ideas to available data and execution; do not claim to reproduce a paper's published Sharpe.

Common alternative defaults: initial capital and accounting contract as above, maximum 99.5% gross target, whole shares, no pyramid entries, costs on each transaction, causal next-executable fills, and explicit terminal positions. Daily-close signals normally execute at the following valid regular-session open. Cash is the fallback when no eligible signal exists. Risk exits are specified per strategy; do not silently add HTS exits to a different strategy.

### A01 — Multi-horizon time-series momentum

- Universe: SPY, QQQ, IWM, EFA, EEM, GLD, TLT, IEF, DBC, VNQ.
- At each completed month end, compute signs of R(63), R(126), R(252). Allocate that asset's fixed 1/10 slot in proportion to positive votes (0, 1/3, 2/3, 1). Scale all slots by 0.995; unallocated balance stays cash. Rebalance next session open; reductions/exits occur when vote count falls. No intraday stop.
- Hypothesis: slower independent trends offer an alternative to selecting only recent top performers. Later knobs: horizon set and monthly/weekly schedule.
- Requirements: daily history and distributions; implement vote feature and monthly target scheduler; test individual vote changes and month-boundary availability.
- Research: [Time Series Momentum](https://w4.stern.nyu.edu/facdir/lpederse/papers/TimeSeriesMomentum.pdf). This is a long-only ETF adaptation of broader momentum research.

### A02 — Dual-momentum rotation

- Compare SPY and EFA trailing 252-session total returns at completed month end. Choose the stronger only if its return exceeds BIL's total return over the same dates; otherwise hold BIL. Target 99.5% in the chosen asset, execute next session open, switch only on month-end decisions. No intraday stop.
- Hypothesis: require both relative strength and an absolute hurdle. Later knobs: 126/252-session lookback and candidate assets.
- Requirements: verified BIL distributions are mandatory; price-only BIL is not an acceptable substitute. Implement total-return lookbacks and cash-equivalent rotation; test equal returns, missing distributions, and switch costs.
- Research: [Antonacci on dual momentum](https://www.optimalmomentum.com/dual-relative-absolute-momentum/). Adaptation, not exact GEM replication.

### A03 — Slow trend asset allocation

- Five equal sleeves: SPY, EFA, IEF, VNQ, DBC. At completed month end, each holds its asset only when its month-end close exceeds its 10-month SMA; otherwise its sleeve is cash. Each active sleeve targets 19.9% NAV. Trade at next session open; no hourly stops.
- Hypothesis: slow decisions and broad allocation reduce churn. Later knobs: 8/10/12-month SMA.
- Requirements: daily-to-monthly aggregation and distributions; test incomplete current month never enters the SMA.
- Research: [Faber's tactical allocation papers](https://mebfaber.com/white-papers/). Asset/proxy and execution differences must be disclosed.

### A04 — Daily channel breakout

- Universe: SPY, QQQ, IWM, EFA, EEM, GLD, TLT, DBC, VNQ. Buy after daily close exceeds the highest high of the preceding 55 sessions, excluding the current session. Exit after close is below the lowest low of the preceding 20 sessions, or below a fixed virtual level entry minus 3× daily ATR20 known at decision, whichever first signals. Both use next-session market execution.
- Maximum five positions, fixed 19.9% entry slots; rank simultaneous entries by R(126). No pyramiding. Later knobs: entry 20/55/100, exit 10/20/40 sessions, counted in a later experiment.
- Hypothesis: require a genuine range breakout rather than just an SMA test. Implement shifted channels and a distinct position-state rule; test current-bar exclusion and competing exits.
- Research: [Zarattini, Antonacci & Barbon on stock trend following](https://concretumgroup.com/wp-content/uploads/2026/02/Does-Trend-Following-Still-Work-on-Stocks.pdf) discusses breakout-based entry approaches. This proposed 55/20 rule is not their all-time-high stock strategy and is not presented as an exact Turtle replication.

### A05 — RSI(2) pullback in an uptrend

- Universe: SPY, QQQ, IWM, XLK, XLF, XLE, XLV, XLI, XLP, XLU. Daily close > SMA200 and Wilder RSI2 <5 triggers next-session entry. Exit after close > SMA5 or after five held sessions, at next session open. Maximum four positions, fixed 24.875% entry slots; lowest RSI wins simultaneous signals. No averaging down and no additional resting stop in this seed.
- Hypothesis: buy temporary weakness instead of recent winners. Later knobs: RSI threshold 2/5/10, maximum hold 3/5/10 sessions.
- Implement an explicitly seeded Wilder RSI, entry rank, and age exit; test zero gains/losses, threshold equality, and RSI warmup.
- Research: [Cesar Alvarez's mean-reversion construction](https://alvarezquanttrading.com/blog/the-abcs-of-creating-a-mean-reversion-strategy-part1/). Our next-open and time-exit choices differ from other published implementations.

### A06 — Internal-bar-strength rebound

- Universe: SPY, QQQ, IWM, EFA, EEM. `IBS=(close-low)/(high-low)` using a completed daily bar. Enter next open if IBS<0.2 and close<SMA5. Exit next open after IBS>0.8 or after three held sessions. Maximum three positions, equal fixed slots; lowest IBS wins simultaneous signals. A zero-range bar gives no signal; no additional stop in the seed.
- Hypothesis: an unusually weak daily finish may partially reverse. Later knobs: entry 0.1/0.2/0.3, exit 0.7/0.8/0.9.
- Implement daily range feature and holding counter; explicitly compare next-open execution with the literature's close-to-close return measurement without booking impossible closing fills.
- Research: [Pagonidis, The IBS Effect](https://www.naaim.org/wp-content/uploads/2014/04/00V_Alexander_Pagonidis_The-IBS-Effect-Mean-Reversion-in-Equity-ETFs-1.pdf).

### A07 — Gap-down recovery after confirmation

- Universe: SPY, QQQ, IWM. Prior session close > SMA200. Current regular-session opening gap <= -0.5× prior daily ATR20 in price units. Require the first verified regular-session hourly bar to close above its own open; buy at the following bar's executable open. Trade only if at least two tradable intervals remain; otherwise skip.
- At most one position, target 50% NAV; prioritize the most negative ATR-normalized gap. Pre-schedule liquidation for the start of the final regular-session hourly interval, with calendar-aware early-close handling. No assumed fill at a gap-recovery target, no overnight hold, and no intrabar stop in this initial seed.
- Hypothesis: some overnight pressure reverses after opening price discovery. The 0.5 ATR rule is our proposal; the source does not establish this exact edge. This is not assumed to reproduce the old `podhajsky_gap 0.5` code.
- Requirements: actual session-open and unpolluted opening bars. Existing integer-hour filtering may be insufficient; finer cached data may be needed. Test that observed opening price cannot also serve as a pre-signal fill.
- Research context: [New York Fed, The Overnight Drift](https://www.newyorkfed.org/research/staff_reports/sr917), which also discusses overnight/intraday reversal evidence and execution limitations.

### A08 — ETF pairs mean reversion

- Predeclared candidate pairs: SMH/SOXX, XBI/IBB, GLD/SLV, EFA/EEM. Fit log-price hedge ratio plus intercept on the preceding 252 sessions at each month end. Require an Engle–Granger test p<0.05 on that formation sample; this is a model screen, not a multiple-testing-adjusted significance claim.
- On later completed daily bars, compute spread z-score using frozen formation mean/std and hedge coefficients. Enter at abs(z)>2, long cheap leg/short rich leg; exit at abs(z)<0.5, abs(z)>3.5, ten held sessions, or formation-model expiry. No entry on the final session before expiry. Trade next session open; choose largest abs(z), at most one pair. Scale hedge-ratio quantities to <=99.5% gross exposure, not an assumed zero-risk net position.
- Requirements: adjusted daily prices PLUS historical borrow availability, borrow rates, margin, short distributions, and a two-leg execution model. With prices alone this is **research-only**; cannot qualify deployable results. Never fill just the profitable leg or assume simultaneous fills without recording the approximation.
- Tests: hedge fit uses formation data only, both-leg commissions, missing leg, borrowing denial, and forced close. Later knobs: formation 126/252 sessions and entry 1.5/2/2.5.
- Research: [Gatev, Goetzmann & Rouwenhorst, Pairs Trading](https://www.nber.org/papers/w7032). Cointegration is our adaptation; their original distance rule is different.

### A09 — Trend-filtered risk allocation

- Universe: SPY, EFA, IEF, TLT, GLD, DBC. At month end exclude assets below their SMA200; estimate covariance on 126 aligned completed daily returns for remaining assets. Use fixed shrinkage `0.9*sample_cov + 0.1*diag(sample_cov)` and solve long-only equal risk contributions. Cap each asset at 35% NAV, leave capped excess cash, scale to <=99.5% gross. Rebalance at next session open; no intraday stop.
- The cap can break exact equal risk contributions; publish the resulting contributions rather than calling the final capped weights perfect risk parity. If the solver fails or inputs are invalid, emit no new target and fail qualification for that segment; do not silently switch algorithms.
- Hypothesis: diversify sources of portfolio risk instead of dollars. Later knobs: covariance 63/126/252 sessions and trend gate.
- Requirements: daily prices/distributions; implement deterministic solver, residual checks, and weight constraints; test singular inputs, one eligible asset, and all-cash states.
- Research: [Maillard, Roncalli & Teiletche](https://www.thierry-roncalli.com/download/erc.pdf); trend gating and caps are project adaptations.

### A10 — Opening-range breakout

- Universe: QQQ and SPY with fixed half-budget sleeves. Define each day's range from actual 09:30–10:00 ET one-minute bars. Enter long only after a subsequent completed one-minute close exceeds the range high, filling at the next executable minute. At most one entry per symbol/day, none after 14:00 ET.
- Protective stop at the known range low, activated only after entry acknowledgement. Quantity limited by 0.25% NAV stop-distance budget and 49.75% per-symbol notional. Pre-schedule exit at 15:55 ET, or five minutes before an early close. No overnight hold. Later knobs: opening range 5/15/30 minutes.
- Requirements: new minute data, an exchange calendar, and realistic intraminute/stop handling. **Not implementable faithfully from the retained hourly cache alone.** A coarse hourly substitute would be a different strategy and a separately counted trial.
- Tests: range never includes future bars, stop cannot fill before entry, entry/stop same-minute ambiguity is flagged, time exits are scheduled before their execution event.
- Research: [Zarattini & Aziz, Can Day Trading Really Be Profitable?](https://concretumgroup.com/wp-content/uploads/2026/02/Can-Day-Trading-Really-Be-Profitable.pdf). Our 30-minute, long-only, capped seed differs from the published design.

## 7. Implementation sequence and deliverables

### Phase 0 — Audited baseline and data manifest

Repair/validate the blockers in section 2 through an explicitly authorized implementation task. Preserve old reports. Produce an attribution report: legacy replay, accounting-corrected replay with legacy sizing, then `HTS_CONTROL_1` with bounded allocation. This distinguishes a fix from a strategy change. Abort ranking if a blocker remains unresolved.

### Phase 1 — Parameter registry and shared feature preparation

Proposed strategy-owned modules, not new public LumiBot APIs:

| Component | Responsibility |
|---|---|
| `strategy_lab/experiment_config.py` | Typed StrategySpec, ExecutionSpec, DataSpec and EvaluationSpec; immutable hashes; reject unsupported combinations. |
| `strategy_lab/hts_variants.py` | Materialize exactly H001–H100 plus separately named control; no implicit Cartesian expansion. |
| `strategy_lab/feature_store.py` | Cache normalized bars and causal features by manifest, revision, dtype, lookback, feed, and adjustment hash. |
| `strategy_lab/execution_policies.py` | Explicit virtual exits, protective stops, fees, capacity, rejection, and latency scenarios. |
| `strategy_lab/alternative_strategies.py` | A01–A10 shared decision interfaces with declared data requirements. |
| `scripts/run_strategy_experiments.py` | Manifest validation, job scheduling, resume, progress, per-job artifacts and failures. |
| `scripts/evaluate_strategy_experiments.py` | Standard metrics, walk-forward scoring, benchmarks, trial ledger, confidence intervals, comparison tables. |

Extend/refactor the existing shared core where appropriate rather than copying it 100 times. All decision paths must support the same replay and native-LumiBot integration. Expose config to the runner; derive warmup from actual required sessions/bars, including 252-return and monthly indicators. Save each candidate's fully resolved config, not just its delta or a display name.

Registry acceptance: exactly 100 unique HTS IDs and resolved-spec hashes, 10 unique alternative IDs, no baseline counted as a variation, no unregistered parameter value, all required metadata present. Identical realized trades in a particular sample are possible and should be reported, not used to invent extra variants.

### Phase 2 — Implement in an order that makes differences easy to diagnose

1. Control; H001–H030 and H050–H054: existing parameter paths and simple allocation checks.
2. H041–H049, H055–H080: ranking, risk allocation, covariance, and market gates.
3. H031–H040 and H081–H090: explicit exit lifecycle and schedule/state behavior.
4. H091–H100: universe metadata and fixed combinations.
5. A01–A06 and A09: daily strategies with verified distributions where required.
6. A07 after session-open validation; A08 after short-data/execution support; A10 after minute-data availability.

Each phase adds unit and integration tests of its invariants, updates relevant engineering/public documentation for user-facing changes, and passes focused tests before any commit. No release, version change, or paper deployment is part of this plan.

### Phase 3 — Vectorized screening and native qualification

- Read the normalized cache once; precompute only distinct needed windows in float64 using NumPy/pandas array operations. These libraries may use SIMD internally; do not claim a particular instruction set or speedup without measuring it.
- Keep portfolio state, fills, stop ordering, and cash accounting serial within each simulation. Parallelize independent simulations only; bound BLAS threads to avoid oversubscription. Use per-worker read-only data or shared immutable arrays.
- Cache feature keys include all data/parameter/version dependencies. Train-fitted coefficients, thresholds, and hyperparameters are fold-scoped. Full-history causal rolling features are allowed only after prefix/truncation tests establish that later data cannot affect earlier values.
- Compare scalar and vectorized implementations on representative fixtures and all registered parameter windows. Require matching decisions/fills/quantities, float comparisons within declared tolerances, and matching cash/NAV.
- Validate every unique rule path in native LumiBot on a compact cached interval. Every finalist and the control must then complete a **full native LumiBot** run over the common two- and six-year windows with the same contract. Report native and screening runtimes separately. Fast screening alone cannot qualify a finalist.
- Native/replay parity must compare chronological intents, fills, positions, cash, fees, financing, dividends, and NAV, not merely final Sharpe. Historical parity between older engines does not prove parity for the new virtual-stop model.

### Phase 4 — Retrospective walk-forward evaluation

The entire 2020–2026 history has already been discussed. Label it **retrospective walk-forward evidence**, never an untouched holdout. Do not advertise a hidden last year after previously inspecting it.

With provisional end E=2026-09-09 and start S=2020-09-09, use six rolling outer folds:

| Fold | 36-month discovery interval (end excluded) | Six-month test interval (end excluded) |
|---|---|---|
| 1 | 2020-09-09 to 2023-09-09 | 2023-09-09 to 2024-03-09 |
| 2 | 2021-03-09 to 2024-03-09 | 2024-03-09 to 2024-09-09 |
| 3 | 2021-09-09 to 2024-09-09 | 2024-09-09 to 2025-03-09 |
| 4 | 2022-03-09 to 2025-03-09 | 2025-03-09 to 2025-09-09 |
| 5 | 2022-09-09 to 2025-09-09 | 2025-09-09 to 2026-03-09 |
| 6 | 2023-03-09 to 2026-03-09 | 2026-03-09 to 2026-09-09 |

Snap interval contents to eligible exchange sessions without overlapping them. Inside each 36-month discovery interval, validate on months 24–30 after fitting on months 0–24, then validate on months 30–36 after fitting on months 6–30. Fixed candidates need no optimization fit, but fitted model components (e.g. pair formation) remain causal.

Select per family using inner-validation results only, then freeze before each outer test. Evaluate all fixed candidates in outer folds for transparent comparison, but the reported selection-procedure performance must use the candidate actually selected beforehand. Choosing the best outer-test row afterward creates another selection bias.

For isolated fold diagnostics, start from cash, warm features from past data without trading, and count all entries/exits. For the deployable stitched selection procedure, carry real cash/holdings through selection boundaries and charge transition trades; never concatenate incompatible candidate curves with free position resets. Purge trades/labels whose outcome window crosses a fitting cutoff where supervised fitting is used. Do not purge legitimate past indicator warmup. Use nonoverlapping outer test periods and block-aware uncertainty estimation.

### Phase 5 — Ranking, robustness, and optional Optuna

Primary metric: `sqrt(252) * mean(daily excess returns) / sample_std(daily excess returns)`, with explicit risk-free convention. Keep CAGR/volatility as a separately named legacy diagnostic. Report both when reconciling historical numbers.

Default inner-validation ordering (a predeclared research choice, not a theorem):

`score = median(fold Sharpe) - 0.5 * IQR(fold Sharpe)`

Two inner windows provide weak uncertainty information; publish both values, not just this score. Tie-break by lower turnover, then simpler rule count, then ID. Reject invalid data/accounting, nonpositive NAV, or nonfinite metrics. For a proposed moderate-risk shortlist require worst validation daily drawdown <=35% and positive net return under 7 bps per-side stress. This 35% research filter is not a live loss guarantee or an inferred personal risk tolerance. Retain rejected rows visibly; do not delete inconvenient evidence. Sparse-signal strategies get an insufficient-evidence flag instead of an invented reliable Sharpe. If no candidate qualifies, report none.

Publish for every candidate: total return, CAGR, standard Sharpe and convention, volatility, daily/hourly max drawdown and duration, Sortino (zero-MAR lower partial moment), Calmar, worst day/week/month, average gross/net exposure, empirical beta/correlation versus QQQ/SPY, turnover, fills and completed round trips, win rate, average win/loss, profit factor, median holding time, costs, financing, distributions, terminal state, and runtime. Mark undefined ratios N/A. Trade metrics use a documented matching convention and do not treat open positions as completed wins.

For finalists, add paired block-bootstrap confidence intervals for Sharpe differences using the same return dates as control/benchmarks (fixed seed; block lengths 5, 10 and 20 sessions as robustness checks). Report multiple-testing-adjusted diagnostics such as DSR with actual search counts, candidate dependence, skewness, kurtosis, and finite-sample assumptions documented. Include prior explored variants as known historical selection baggage; if the full historical count is unknown, report a lower bound and sensitivity rather than an exact adjusted probability. No short six-month Sharpe establishes a stable edge by itself.

**Optuna is optional phase two of research, not required to enumerate the fixed 100.** First run the locked catalog. If tuning is subsequently chosen, allow at most 20 additional TPE proposals per selected family per outer fold, up to three families: at most 360 proposals and 720 inner-validation simulations. Search only the winning families' declared numeric dimensions on training/inner validation; never tune fill models, cost scenarios, or the outer test. Fit/selection is repeated independently for every fold. Pin the installed Optuna version and seed; use persisted study/trial manifests. Deterministic sequential proposal generation is preferable when reproducibility matters; asynchronous parallel completion can change adaptive proposals. No aggressive pruning from one short favorable subperiod. [Optuna sampler documentation](https://optuna.readthedocs.io/en/stable/reference/samplers/index.html).

Optuna selects parameters; it does not supply SIMD. Feature preparation provides vectorization, and independent backtest workers provide parallelism. Log all additional proposals, failed/pruned attempts, and manually revised combinations in the experiment budget.

### Phase 6 — Execution stress and forward evidence

For up to ten finalists plus the control, repeat outer tests with (1) 7 bps per side, (2) 15 bps per side, and (3) an additional hourly execution delay at baseline cost where the strategy cadence permits. For daily/minute alternatives use one additional interval at their actual execution cadence and label it explicitly. Stress protective-stop gaps without converting them into guaranteed stop-price fills. Add position-size/capacity checks against validated available volume; do not multiply a per-exchange volume into an unobserved consolidated figure.

Freeze final configs before collecting new forward paper observations. A first 60–90 trading-session paper stage is an operational/data/fill check, not sufficient evidence for a precise long-term Sharpe. Continue the forward record without changing rules in response to its performance; every rule change starts a new registered revision. Paper fills are useful diagnostics, not a guarantee of identical live execution.

## 8. Workload, performance measurement, and artifacts

No backtests were launched to produce this plan. A strategy count is different from a simulation count.

If all data dependencies are met, there are **111 model configurations**: 100 HTS variations + 10 alternatives + 1 audited HTS control.

| Planned stage | Logical simulations |
|---|---:|
| Six-year and two-year descriptive runs | 111 × 2 = 222 |
| Two inner-validation windows in each of six outer folds | 111 × 2 × 6 = 1,332 |
| Fixed-candidate outer-fold results | 111 × 6 = 666 |
| Core research total | **2,220** |
| QQQ, SPY, and cash controls over two descriptive windows | 3 × 2 = 6 additional |
| Full native descriptive reruns of up to 10 finalists + HTS control | Up to 22 additional |
| Three execution stresses for up to 10 finalists + control across six folds | Up to 198 additional |
| Optional Optuna stage | Up to 720 inner simulations, plus separately counted outer/native qualification |

These are logical workloads, not permission to launch them now. Fold benchmark returns may be sliced from identical standalone buy-and-hold histories with correct boundary accounting. Native path preflights, scalar parity, continuous selection-policy simulations, and any repeat/failure retries are extra jobs and recorded separately. Do not hide them in a claimed 100-run benchmark. A08 and A10 have known additional-data requirements; A02 and A07 may also be blocked until distributions/session integrity are verified. Show blocked candidates as blocked, not as zero-return successes.

Before estimating duration, measure one six-year control, one covariance-heavy variant, one protective-stop variant, and their native paths. Record cold/warm load, feature-prep, event-loop, report time, peak memory, worker count, numerical-library thread count, and event/fill counts. Estimate remaining wall time from measured job classes and worker efficiency. Do not extrapolate the earlier seconds-long custom replay to every full LumiBot run.

Proposed run artifacts under `reports/hts_variations/<experiment_id>/`: immutable manifest, resolved candidate registry, data-quality report, trial ledger, per-candidate run metadata, fills, hourly/daily equity, terminal state, fold assignments, metrics, benchmark comparison, scalar/native parity, cost stresses, and runtime summaries. Use atomic completion markers and hashes so resume cannot mix revisions. Failed and incomplete jobs remain visible.

Implementation is ready for a research run when: every ID resolves once; required data gates pass; accounting and scalar/native parity pass; sampling/selection rules are frozen; completed jobs can be resumed without duplication; and the report separates research approximations from native execution results.

## 9. Recommended starting sequence

First validate accounting and instrument history, then run the audited control. Prioritize **H052/H053** (four/five holdings), **H063/H068** (20% portfolio-volatility target), **H059/H060** (correlation/economic exposure limits), **H086/H089** (turnover/re-entry controls), and **H098** (four screened holdings plus a volatility target) for implementation checks. This priority is about interpretability and plausible risk improvement, not evidence of superior performance.

For alternatives, start with **A03, A05, A06, and A01**, which exercise slower trend allocation and a different return mechanism—mean reversion—using daily data. Include all ten in the plan, but explicitly resolve each data prerequisite before its backtest is treated as qualified evidence.
