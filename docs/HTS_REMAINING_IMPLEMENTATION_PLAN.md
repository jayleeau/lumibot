# Title: HTS Remaining Implementation and Validation Plan

Description: The execution plan for implementing and qualifying the 71 HTS catalog entries that are not yet runnable on LumiBot's native backtesting engine.

Last Updated: 2026-09-14

Status: Approved direction; remaining implementation not started

Audience: Strategy developers and the strategy owner

## Overview

The catalog contains 110 strategies plus the separately audited `HTS_CONTROL_1`.
At the start of this plan, 39 HTS variations plus the control are implemented and
have completed both descriptive windows. The remaining work is 61 HTS variations
and 10 alternative strategies.

This plan uses only LumiBot native backtesting (`PandasDataBacktesting` and
`BacktestingBroker`). The retired custom replay is not an implementation target,
validation target, fallback, or qualification path.

Feature calculations should use vectorized NumPy/pandas operations where that is
clear and testable. Portfolio state, order lifecycle, stop precedence, fills,
cash, and NAV remain inside LumiBot's event-driven simulation. There will be no
second vectorized portfolio simulator to reconcile.

The North Star remains the retrospective walk-forward selection procedure's
standard daily-return Sharpe, considered with drawdown, cost sensitivity, and
uncertainty. The immediate engineering gate is simpler: all 111 configurations
must be honest, deterministic, searchable, and either produce valid native
results or fail closed on a named data prerequisite.

## 1. Current State

| Item | Current evidence |
|---|---:|
| Registered configurations | 111: one control, 100 HTS variations, 10 alternatives |
| Runnable now | 40: control plus 39 HTS variations |
| Not runnable now | 71: 61 HTS variations plus 10 alternatives |
| Completed descriptive runs | 80: two windows for each runnable configuration |
| Completed runs with validation problems | 0 |
| Six-year median native runtime | 17.0 seconds per job |
| Two-year median native runtime | 6.3 seconds per job |

The existing runnable set is `HTS_CONTROL_1`, H001-H030, H050-H054, and
H091-H094. Existing reports are useful evidence, but the final qualification run
must use a revisioned suite manifest so results from different implementations
cannot be mixed by `--resume`.

## 2. Definition of Done

A candidate is **implemented** only when all of these are true:

1. Its catalog parameters are consumed by a real strategy path; no requested
   value is ignored or substituted with control behavior.
2. `check_supported()` reports no missing mechanism for the candidate.
3. Formula, causality, lifecycle, and native integration tests for its distinct
   behavior pass.
4. Its resolved config, data manifest, implementation revision, and artifact
   hashes are recorded.

A candidate is **qualified** only when, in addition:

1. Every required data gate is closed.
2. Its short native preflight passes with finite fills, nonnegative cash within
   tolerance, causal timestamps, and a reconciled terminal state.
3. Its two-year and six-year native runs complete with `problems=[]`.
4. An independent evaluator reproduces daily equity metrics and accounting from
   the emitted artifacts.
5. A repeated run produces identical decisions, fills, daily equity, and terminal
   state, apart from runtime and nondeterministic log metadata.

The project is not complete while a candidate is merely `blocked_data`. The final
target is `runnable=111`, `unsupported=0`, and 222 valid descriptive runs.

## 3. Implementation Shape

Keep this work in the strategy-owned research layer. No new public LumiBot API is
needed.

| Component | Planned responsibility |
|---|---|
| `strategy_lab/feature_store.py` | Normalized daily/hourly OHLCV, causal vectorized features, explicit symbol eligibility, and feature caching |
| `strategy_lab/native_experiments.py` | LumiBot bridge, per-run context, event loop, orders, fills, and run artifacts |
| `strategy_lab/hts_policies.py` | Pure HTS ranking, gate, selection, schedule, weighting, and exit calculations |
| `strategy_lab/native_alternatives.py` | Native Strategy implementations/factory for A01-A10 |
| `strategy_lab/experiment_validation.py` | Independent metrics, artifact reconciliation, determinism hashes, and acceptance checks |
| `scripts/run_hts_experiments.py` | Eight-worker scheduling, revisioned suite IDs, atomic resume, progress, and failures |
| `scripts/evaluate_hts_experiments.py` | Suite audit, comparison tables, benchmarks, walk-forward scoring, and uncertainty |
| `tests/strategy_lab/` | Pure formula tests, state-machine tests, synthetic native integrations, causality tests, and deterministic reruns |

Avoid a class per HTS ID. Each H candidate stays a named immutable configuration
composed from a small number of tested policies. Alternative strategies remain
separate decision cores because their rules are materially different.

The HTS decision order is fixed as:

1. Read only completed, valid inputs.
2. Apply eligibility and market gates.
3. Calculate rank scores.
4. Apply retention, cooldown, correlation, and exposure-group selection rules.
5. Calculate target weights and portfolio caps.
6. Apply protective exits before allocation trades.
7. Construct causal orders from current cash and confirmed fills.

## 4. Data and Harness Foundation

### Batch 0 - Freeze the contract

Candidates unlocked: none directly.

Implementation:

- Add a revisioned suite manifest containing the Git commit, resolved registry
  hash, input database checksums, table schemas, date coverage, exclusions,
  candidate fingerprints, engine version, cost convention, and window bounds.
- Add `implementation_status` and `data_status` to the generated catalog and
  runner output so code readiness and data readiness are not conflated.
- Resolve the documented half-open experiment window versus the code's current
  inclusive-looking 2020-09-08/2026-09-08 boundaries, then use one definition in
  the manifest, runner, folds, and reports.
- Make resume depend on the suite manifest and artifact hashes. A result from a
  different implementation revision must not count as complete.
- Replace silent daily/hourly symbol intersection with explicit required-symbol
  validation and dated eligibility. Missing required benchmark or basket symbols
  must fail the relevant candidate closed.
- Replace the module-global run context with a per-run strategy factory, or at
  minimum guarantee process-local cleanup in `finally` after every success and
  exception.
- Resolve or quarantine the known IBIT pre-inception rows by verified instrument
  identity and date; never alter the source databases.
- Audit source bar start/end semantics, regular-session coverage, DST, and early
  closes. Record whether the current hourly mapping is usable for A07.
- Verify split adjustment and distribution coverage. Label every result as
  price-return or total-return evidence.
- Keep a zero Sharpe as a visible diagnostic warning, not an invalidating
  `problems` entry when all other invariants pass.
- Update stale plan/catalog status text that still says no candidates have run.

Validation:

- Recompute file checksums and manifest hashes twice and require equality.
- Fail closed if a required table, symbol, session, or adjustment field is absent.
- Run the current control once with one worker and once inside an eight-worker
  batch; require identical decision/fill/equity hashes.
- Re-run `tests/strategy_lab` before the first mechanism batch.

## 5. HTS Mechanism Batches

The six batches below implement all 61 remaining HTS variations. Each batch lands
only after its focused tests, a short native smoke, and the two descriptive
windows for newly unlocked candidates are green.

| Batch | Name | Candidates | Count |
|---|---|---|---:|
| 1 | Causal ranking and market features | H041-H049, H059, H071-H080 | 20 |
| 2 | Selection memory, schedules, and caps | H060, H081-H090, H095 | 12 |
| 3 | Target weights and volatility control | H055-H058, H061-H070 | 14 |
| 4 | Virtual exit state machines | H031-H038 | 8 |
| 5 | Native protective stop lifecycle | H039-H040 | 2 |
| 6 | Predeclared combinations | H096-H100 | 5 |
|  | Total |  | **61** |

### Batch 1 - Causal ranking and market features

Implement vectorized daily features for return volatility, downside deviation,
cross-sectional percentile ranks, log-price regression slope and R-squared,
efficiency ratio, QQQ residual momentum, skip-five momentum, aligned return
correlation, benchmark SMAs, breadth, and the SPY short/long volatility ratio.

Important behavior:

- Scores use the preceding completed session only.
- Cross-sectional percentile ranks include only eligible symbols for that date.
- QQQ residuals use an intercept and exactly the preceding aligned return window.
- Zero denominators, nonfinite results, or incomplete aligned history make the
  symbol/session ineligible; they never become infinite scores or an open gate.
- A false market gate exits at the next scheduled rebalance and blocks new
  entries, while hourly risk exits continue to run.

Validation:

- Hand-calculated fixtures for every formula, including ties and zero variance.
- A small independent NumPy/SciPy reference for OLS and covariance alignment.
- Prefix tests: append future rows and prove all earlier features/selections are
  unchanged.
- Shift tests: deliberately include an extreme current-session value and prove it
  cannot affect that session's decision.
- Native smoke tests showing H041 differs from raw R(20), H059 rejects a correlated
  second holding, and a closed market gate produces no risk entry.

### Batch 2 - Selection memory, schedules, and caps

Implement economic-exposure groups, weekly and every-N-session schedules, rank
retention buffers, stop-fill cooldowns, and the total leveraged-product cap.
Keep schedule and cooldown state explicit in the strategy artifact.

Important behavior:

- Monday/Wednesday/Friday schedules use the first valid exchange session on or
  after the named weekday, including holiday weeks.
- Every-second/every-third schedules use the fixed catalog anchor in every fold.
- Rank-buffer retention never overrides trend/liquidity failure or a stop.
- Cooldowns start from an actual stop fill, not a stop intent.
- Exposure groups and leveraged-product metadata are dated, frozen inputs.
- Unused capacity remains cash; caps do not inflate uncapped positions.

Validation:

- Synthetic holiday weeks and truncated folds for schedule anchoring.
- State-transition tests for pending stop, rejected stop, filled stop, cooldown
  expiry, eligibility loss, and rank re-entry.
- Selection fixtures covering every declared economic group and singleton.
- Cap arithmetic with zero, one, and several leveraged holdings plus whole-share
  rounding.
- Serial-versus-eight-worker determinism for one stateful candidate.

### Batch 3 - Target weights and volatility control

Implement inverse-volatility weights, stop-distance position budgets, portfolio
volatility targets, per-symbol caps, daily target rebalancing, and trim orders.
The allocator should return explicit target notionals before order construction.

Important behavior:

- Covariance uses aligned completed returns and the exact requested 20- or
  60-session window.
- Gross scale is `min(0.995, target / forecast_vol)`; no candidate leverages up to
  hit a target.
- Missing covariance history prevents entry rather than substituting full risk.
- Rebalance reductions are submitted and filled before dependent purchases use
  their proceeds. A future opening price cannot be used to choose today's size.
- Fee reserve, share rounding, per-symbol cap, aggregate cap, and cash constraints
  are applied in that order and recorded.

Validation:

- Hand-worked two-asset covariance and forecast-volatility examples.
- Equal-volatility, zero-volatility, singular covariance, missing-history, cap,
  and overflow-to-cash cases.
- Stop-distance quantities independently recomputed from NAV and entry-known ATR.
- Post-fill reconciliation of targets, actual weights, fees, cash, and rounding
  residuals.
- Native integration tests for trim-first execution and rejected dependent buys.

### Batch 4 - Virtual exit state machines

Implement H031-H038 with explicit per-position state: breach count, frozen stop,
highest high since entry, rolling post-entry highs, entry session, entry ATR,
initial risk R, and break-even activation.

Validation:

- Two-breach sequences with recovery and frozen-trail checks for H031.
- Exact 0.25 ATR and 0.50 ATR buffer boundaries for H032-H033.
- Chandelier windows, entry-bar exclusion, and next-bar activation for H034-H035.
- Fixed-stop immutability for H036.
- Exchange-session age, holiday gaps, and tenth-session exit for H037.
- Break-even threshold and no guaranteed break-even fill for H038.
- For every mode, protective exits beat rebalance entries and a position cannot be
  sold twice.

### Batch 5 - Native protective stop lifecycle

Implement H039 and H040 with actual LumiBot stop-market orders. Record protective
order ID, active level, activation time, replace sequence, sibling relationship,
cancel result, and fill precedence.

Validation:

- Synthetic intrabar touch, no-touch, gap-through, same-bar activation, and
  cancel/replace paths against `BacktestingBroker`.
- Prove a revised trail cannot trigger from the bar that created it.
- Prove an emergency stop fill cancels the virtual/rebalance exit path and cannot
  double-sell.
- Independently verify the expected gap-through fill convention from the emitted
  trades and source OHLC.
- Report hourly-OHLC stop fill limitations explicitly; do not describe simulated
  stop prices as guaranteed live fills.

### Batch 6 - Predeclared combinations

Unlock H096-H100 only by composing already-tested primitives in the fixed decision
order. Do not duplicate or specialize the primitive implementations.

Validation:

- Configuration tests prove each combination contains exactly the declared
  component settings.
- Metamorphic tests prove each component still binds: removing the component from
  a fixture changes only the expected decision stage.
- H100 receives the complete protective-stop lifecycle suite plus the 60-session
  volatility-target suite.

## 6. Alternative Strategy Batches

Alternative strategies share data loading, artifact, fee, metric, and order
helpers, but not the HTS selection/exit rules.

### Batch 7 - Daily-data alternatives

Candidates: A01, A03, A04, A05, A06, and A09.

Implementation and focused validation:

| ID | Implementation | Minimum distinct validation |
|---|---|---|
| A01 | Month-end multi-horizon vote weights | Month boundary, 0/1/2/3 votes, reduction and next-open fill |
| A03 | Five independent 10-month trend sleeves | Incomplete month excluded, sleeve-to-cash transition, exact 19.9% cap |
| A04 | Shifted 55/20 channels and fixed daily ATR exit | Current bar excluded, competing exit precedence, simultaneous-entry ranking |
| A05 | Seeded Wilder RSI(2), SMA200 gate, SMA5/time exit | All-gain/all-loss/flat RSI, equality boundaries, fifth-session exit |
| A06 | IBS signal and three-session state | Zero-range rejection, equality boundaries, signal ordering, third-session exit |
| A09 | Shrunk covariance and deterministic long-only equal-risk-contribution solver | Symmetric case, cap binding, cash remainder, convergence and failure behavior |

These can be implemented and smoke-tested from the retained daily bars. Results
that omit cash distributions remain explicitly price-return evidence; they must
not be presented as total-return comparisons for income-producing assets.

### Batch 8 - Data-conditioned alternatives

Candidates: A02, A07, A08, and A10.

Use the configured Data Downloader path for additional backtest data. Do not start
a local ThetaTerminal or contact Theta directly. Store new inputs as immutable,
checksummed derived archives with documented coverage.

| ID | Data gate | Implementation after the gate | Acceptance evidence |
|---|---|---|---|
| A02 | Verified BIL distributions and total-return adjustment | Month-end SPY/EFA relative momentum versus the BIL total-return hurdle | Hand-recomputed total returns across ex-dates; tie and switch-cost tests |
| A07 | Verified regular-session open and uncontaminated opening interval | Gap normalization, first-bar confirmation, final-interval liquidation | DST, early-close, no-lookahead, and no-overnight-position tests |
| A08 | Borrow availability/rates plus two-leg short execution contract | Monthly Engle-Granger formation, hedge ratio, spread z-score, paired entry/exit | Independent statistical fit; borrow rejection; leg failure/unwind; borrow-cost reconciliation |
| A10 | One-minute bars plus exchange-calendar sessions | 09:30-10:00 range, later close breakout, range-low stop, 15:55 exit | Half-day/DST boundaries, current-minute exclusion, stop/close precedence |

If a data gate cannot be closed, keep the candidate visibly blocked and report
the missing artifact. Do not manufacture a zero-return run or weaken the rule.

## 7. Validation Ladder for Every Batch

Each batch uses the same sequence:

1. **Registry gate:** only the intended IDs move from unsupported to runnable;
   unrelated candidates remain unchanged.
2. **Pure unit tests:** formula and boundary fixtures pass without running a full
   backtest.
3. **Causality gate:** prefix/truncation tests prove future rows cannot change past
   decisions, sizes, or fitted values.
4. **State gate:** orders, fills, cooldowns, stops, schedules, and terminal state
   reconcile through synthetic paths.
5. **Native smoke:** each unique rule path completes a compact LumiBot interval
   with expected decisions and no invariant violations.
6. **Determinism gate:** repeat one worker versus eight workers and compare stable
   artifact hashes.
7. **Descriptive runs:** run each newly unlocked ID over two-year and six-year
   windows with eight workers.
8. **Independent audit:** rebuild daily returns, Sharpe, return, CAGR, volatility,
   drawdown, cash, fees, positions, and terminal NAV from saved source artifacts.

The independent evaluator must not import the strategy's feature or metric
functions. Shared code would only prove the same calculation agrees with itself.

Batch acceptance requires:

- no `error`, `invalid`, silent fallback, nonfinite fill, or unrecorded rejection;
- cash never below the declared tolerance;
- no position quantity mismatch between the strategy journal, trade ledger, and
  terminal broker state;
- exact symbol/side/timestamp/quantity agreement, cash and NAV agreement within
  one cent, and metric agreement at `rtol=1e-10`, `atol=1e-12`;
- daily arithmetic Sharpe labelled separately from LumiBot's
  `cagr_over_volatility` field;
- exact result counts and a failure ledger that preserves every attempted job;
- no changes to earlier candidate fingerprints or decisions unless the batch
  explicitly fixes a shared defect and reruns all affected candidates.

## 8. Full Native Qualification Run

After all code and data gates pass:

1. Freeze a new suite ID and manifest.
2. Run all 111 configurations over the two descriptive windows with eight
   workers: 222 native LumiBot jobs.
3. Run the independent suite audit and require 222 accepted results.
4. Repeat one candidate from every unique rule path and compare hashes.
5. Compare one-worker and eight-worker results for control, a covariance target,
   a stateful cooldown, a protective stop, a monthly alternative, and A10.
6. Publish the implementation matrix, result table, data limitations, and runtime
   report. Preserve failed attempts rather than overwriting them.

The runner command shape remains:

```bash
bin/safe-timeout 1200s .venv/bin/python scripts/run_hts_experiments.py \
  --all --workers 8 --windows six_year,two_year --suite-id <revision>
```

The exact CLI addition (`--suite-id`) is part of Batch 0. The timeout should be
raised only after measured pilot runtimes justify it.

## 9. Runtime Expectations

The current 80 successful jobs provide a baseline: median runtimes are 17.0
seconds for six years and 6.3 seconds for two years. At those rates, the 142
currently missing descriptive jobs represent about 28 worker-minutes, or roughly
3.5 minutes at perfect eight-worker efficiency.

That is a lower bound. Covariance, protective-stop, minute-data, and alternative
paths will add work, and eight processes may contend for memory and DuckDB reads.
Plan for **10-30 minutes** for the final 142-job descriptive batch until the three
required pilots have been measured. A clean 222-job rerun should provisionally be
budgeted at **15-40 minutes** on the current machine.

Before publishing an ETA, profile:

- one six-year 60-session covariance candidate;
- one six-year protective-stop candidate;
- A10 over its full six-year minute-data window;
- peak RSS and data-load time at one, four, and eight workers;
- numerical-library thread counts to prevent eight workers each spawning their
  own large thread pools.

The later 2,220-job walk-forward research program is separate from implementing
and descriptively backtesting all candidates. Its estimate must be recalculated
from these complex-path pilots rather than extrapolated from the control.

## 10. Commit and Handoff Boundaries

Use one reviewed commit per batch after focused tests and native smokes pass.
Do not include report databases or generated backtest directories in commits.
Every batch handoff records:

- candidates newly implemented and newly qualified;
- exact tests and native commands run;
- suite/registry/data hashes;
- failed or blocked candidates with reasons;
- observed runtime and peak memory;
- next batch and its prerequisites.

No release, version bump, branch switch, push, paper trading, or deployment is
part of this implementation plan.
