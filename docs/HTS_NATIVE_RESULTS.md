# Title: HTS Native Implementation and Descriptive Results

Description: The implementation matrix, validation evidence, and descriptive
backtest results for all 111 registered HTS research configurations on LumiBot's
native backtesting engine.

Last Updated: 2026-09-14

Status: Implementation complete; 214 descriptive runs accepted; four candidates
remain blocked on inputs that are not in the retained archives

Audience: Strategy owner and strategy developers

## Overview

This document records the outcome of
`docs/HTS_REMAINING_IMPLEMENTATION_PLAN.md`: the 71 configurations that were not
runnable are now implemented on `native-lumibot-backtesting` (`PandasDataBacktesting`
plus `BacktestingBroker`), except for the four alternative strategies whose
required data cannot be produced from the retained local archives.

The retired custom replay is not part of this work. No LumiBot public API was
added or changed; everything lives in the strategy-owned research layer under
`strategy_lab/`.

**Headline numbers**

| Metric | Value |
|---|---|
| Registered configurations | 111 (1 control + 100 HTS + 10 alternatives) |
| Implemented on the native engine | 107 |
| Blocked on data | 4 (A02, A07, A08, A10) |
| Native jobs run | 214 (107 configs x 2 windows) |
| Independent-audit accepted | 214 / 214 |
| Runs with validation problems | 0 |
| Unit tests in `tests/strategy_lab` | 77 passing |

## Suite identity

| Field | Value |
|---|---|
| Suite ID / implementation revision | `hts-native-2026-09-14-2` |
| Engine | `lumibot.strategies.Strategy.run_backtest` + `BacktestingBroker` |
| Windows | `six_year` 2020-09-08..2026-09-08, `two_year` 2024-09-08..2026-09-08 |
| Cadence | hourly for HTS/control, daily for alternatives A01-A06/A09 |
| Costs | 3.5 bps per side on buy and sell fills |
| Starting cash | $100,000 |
| Artifacts | `reports/hts_native_2026-09-14/<ID>/<window>/run_{stats,trades,result}` |
| Manifest | `reports/hts_native_2026-09-14/suite_manifest.json` |
| Independent audit | `reports/hts_native_2026-09-14/suite_audit.json` |

The manifest records the Git commit, registry hash, SHA-256 of both input
databases, window bounds, cost convention, and a per-candidate
`implementation_status` / `data_status`. `--resume` only reuses an artifact whose
fingerprint and implementation revision both match, so results from two
implementations cannot be mixed.

## What was implemented

| Batch | Candidates | Mechanism |
|---|---|---|
| 1 | H041-H050, H071-H080 | Causal ranking scores (risk-adjusted, multi-horizon percentile, log-price regression persistence, efficiency ratio, QQQ residual momentum, skip-five) and market gates (SPY/QQQ SMAs, breadth, SPY volatility ratio) |
| 2 | H060, H081-H090, H095 | Rebalance schedules (weekly carry-forward, every-Nth from a frozen anchor), rank-retention buffers, post-stop cooldowns, exposure groups, leveraged-product cap |
| 3 | H051-H058, H061-H070 | Inverse-volatility weights, stop-distance risk budgets, covariance volatility target, per-symbol caps |
| 4 | H031-H038 | Virtual exit state machines (two-close confirmation, ATR breach buffers, chandelier variants, fixed entry ATR, break-even ratchet, time exit) |
| 5 | H039, H040 | Resting broker stop-market orders plus the emergency-stop sibling |
| 6 | H096-H100 | Predeclared combinations of the above primitives in the fixed decision order |
| 7 | A01, A03, A04, A05, A06, A09 | Daily alternatives: multi-horizon sleeves, slow trend allocation, channel breakout, RSI(2) pullback, IBS rebound, shrunk-covariance equal-risk allocation |

Supporting modules:

| Component | Responsibility |
|---|---|
| `strategy_lab/feature_store.py` | Vectorized causal daily features, correlation, and covariance |
| `strategy_lab/hts_policies.py` | Pure ranking, gating, selection, schedule, weighting, and cap rules |
| `strategy_lab/native_experiments.py` | LumiBot bridge, event loop, orders, fills, metrics, artifacts |
| `strategy_lab/native_alternatives.py` | Daily alternative strategy cores and blocked-data ledger |
| `strategy_lab/experiment_validation.py` | Independent metrics, trade reconciliation, artifact hashing |
| `scripts/run_hts_experiments.py` | Parallel runner, suite manifest, revisioned resume, memory reporting |
| `scripts/evaluate_hts_experiments.py` | Suite audit and leaderboard |

## Validation evidence

1. **Registry gate** - `python scripts/list_strategy_experiments.py --verify`
   reports 111 configurations across 21 families; 107 runnable, 4 blocked-data.
2. **Unit tests** - `python -m pytest tests/strategy_lab -q` passes 77 tests,
   including hand-calculated formulas, zero-variance and tie boundaries, a NumPy
   OLS reference, ERC solver cases, schedule and cooldown state transitions, and
   prefix/stability causality checks.
3. **Independent audit** - `scripts/evaluate_hts_experiments.py` rebuilds daily
   equity, returns, arithmetic Sharpe, total return, CAGR, volatility, and
   drawdown from `run_stats.csv`, reconciles net positions and fees from
   `run_trades.csv`, and requires agreement with the recorded metrics at
   `rtol=1e-10`. Result: **214 accepted, 0 rejected.**
4. **Determinism** - one candidate from each distinct rule path (control, virtual
   exit state machine, resting protective stop, covariance target, stop cooldown,
   and a monthly alternative) was re-run at **one worker** and compared against
   the three-worker suite. All 12 artifacts (6 candidates x 2 windows) are
   byte-identical on the decision/fill/equity hashes.

## Runtime and memory

| Measure | Value |
|---|---|
| Full suite wall time | 591.5 s at 3 workers (214 jobs) |
| Per-job median runtime | 4.6 s (six-year 9.4 s, two-year 3.3 s) |
| Per-job max runtime | 19.6 s |
| Peak RSS per job | 1110 MB max, 787 MB mean |
| Suite-wide Python RSS observed | ~2.3-2.8 GB at 3 workers |

Memory controls, added after an eight-worker run exhausted a 16 GB machine:

- BLAS/OpenMP thread counts are pinned to 1 before NumPy is imported, so a worker
  cannot spawn a full thread pool.
- The runner recycles each worker process after a single job
  (`max_tasks_per_child=1`), so a job's ~1 GB returns to the OS before the next.
- Raw per-symbol frames are released once derived features exist.
- The suite default is 3 workers; `--report-memory` prints per-job and batch peak
  RSS so the limit can be re-measured before raising it.

## Results

Daily-return Sharpe, zero risk-free. `totret` is cumulative over the window and
`maxdd` is peak-to-trough on daily equity.

Six-year window (2020-09-08 to 2026-09-08), top 10 of 107:

| ID | Sharpe | Totret | CAGR | Vol | MaxDD |
|---|---|---|---|---|---|
| H100 | 1.128 | +3.383 | +0.279 | 0.248 | 0.282 |
| A09 | 1.049 | +0.561 | +0.077 | 0.074 | 0.101 |
| H010 | 0.994 | +8.426 | +0.454 | 0.519 | 0.459 |
| H084 | 0.932 | +6.297 | +0.393 | 0.483 | 0.634 |
| H009 | 0.924 | +6.619 | +0.403 | 0.521 | 0.471 |
| H082 | 0.910 | +5.782 | +0.376 | 0.474 | 0.520 |
| H095 | 0.891 | +3.814 | +0.299 | 0.377 | 0.390 |
| A03 | 0.873 | +0.472 | +0.067 | 0.078 | 0.092 |
| H008 | 0.836 | +4.875 | +0.343 | 0.527 | 0.553 |
| H025 | 0.833 | +5.331 | +0.360 | 0.569 | 0.533 |

Two-year window (2024-09-08 to 2026-09-08), top 10 of 107:

| ID | Sharpe | Totret | CAGR | Vol | MaxDD |
|---|---|---|---|---|---|
| H078 | 1.770 | +3.279 | +1.072 | 0.479 | 0.284 |
| H082 | 1.606 | +2.859 | +0.967 | 0.503 | 0.303 |
| H100 | 1.577 | +1.344 | +0.532 | 0.300 | 0.282 |
| H007 | 1.455 | +2.924 | +0.984 | 0.591 | 0.427 |
| H031 | 1.442 | +2.569 | +0.892 | 0.547 | 0.291 |
| H079 | 1.430 | +1.883 | +0.700 | 0.442 | 0.250 |
| H095 | 1.377 | +1.340 | +0.531 | 0.356 | 0.294 |
| H055 | 1.365 | +2.135 | +0.773 | 0.520 | 0.369 |
| H008 | 1.358 | +2.467 | +0.864 | 0.584 | 0.415 |
| H036 | 1.353 | +2.771 | +0.945 | 0.651 | 0.436 |

Reference: the audited control `HTS_CONTROL_1` returns Sharpe 0.657 / +2.446 with
a 0.662 drawdown over six years, and Sharpe 1.132 / +1.641 with a 0.409 drawdown
over two years.

Weakest six-year rows for calibration: H092 (-0.365), H099 (-0.115), H098
(-0.083), H012 (-0.018), H088 (+0.021). No run produced a zero Sharpe, so every
configuration traded.

The full 214-row table is in `suite_audit.json`; regenerate and print it with:

```bash
python scripts/evaluate_hts_experiments.py \
  --out-dir reports/hts_native_2026-09-14 --write --top 107
```

## Data limitations

Four alternatives are `blocked-data` and were deliberately **not** run with
substitute inputs:

| ID | Missing input |
|---|---|
| A02 | Verified BIL total-return distributions; the archive has a `split_events` table but no distributions table |
| A07 | Validated regular-session open bars and a session-integrity audit |
| A08 | Borrow availability/rates and a two-leg short execution contract |
| A10 | One-minute bars and an exchange calendar; no minute archive is retained |

Additional known limitations:

- Hourly bars are mapped from archived 09:00-15:00 ET sessions onto NYSE
  09:30-15:30 labels. This is a declared convention, not a verified statement of
  bar completion times.
- Simulated resting stop prices come from hourly OHLC. Gap-through fills use the
  broker's open-price convention, but these are not guaranteed live fills.
- Results are price-return evidence; the archives do not carry cash
  distributions, so income-producing assets are not measured on a total-return
  basis.
- The Sharpe reported here is the daily-return arithmetic Sharpe. LumiBot's own
  field is CAGR-over-volatility and is stored separately as
  `cagr_over_volatility`.

## How to look things up

```bash
# Sortable results browser: one row per configuration, both windows side by side,
# with links to a per-candidate page and the raw artifacts.
python scripts/build_hts_results_page.py
# -> reports/hts_native_2026-09-14/results.html  (open in a browser)

# Index of every configuration, with stable IDs and fingerprints
python scripts/list_strategy_experiments.py --list

# One candidate, resolved parameters
python scripts/list_strategy_experiments.py --show H100

# Search and filter
python scripts/list_strategy_experiments.py --search correlation
python scripts/list_strategy_experiments.py --kind alternative
python scripts/list_strategy_experiments.py --family family-7-vol-target

# Re-run the suite (resume skips artifacts already valid for this revision)
python scripts/run_hts_experiments.py --all --workers 3 --resume --report-memory

# Independent audit
python scripts/evaluate_hts_experiments.py --out-dir reports/hts_native_2026-09-14 --write
```

## Next steps

This work produced descriptive evidence, not a selection. The retrospective
walk-forward procedure in the research plan is still the North Star and should be
recomputed from these artifacts. The 2,220-job walk-forward program is separate
from implementation and should be budgeted from the complex-path runtimes
measured here rather than extrapolated from the control.

No release, version bump, branch switch, push, paper trading, or deployment is
part of this work.
