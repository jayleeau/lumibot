# HTS v2 walk-forward null-selection review

Independent review of whether the all-cash v2 walk-forward result is genuine or caused by the evaluator.

- Last Updated: 2026-09-17
- Status: Complete
- Audience: HTS research and review agents

## Overview

**Verdict: GENUINE NO-QUALIFY.**

The f1 all-cash selection is not caused by empty position records, missing candidates, a wrong artifact path, a revision mismatch, the positive-block expression, final-equity accounting, or mixed cost units. An independent recomputation from the 600 raw f1 discovery artifacts (`V001`-`V100` x `b01`-`b06`) produced **0 eligible candidates** and matched the 100 saved `ranking_values` rows to floating-point precision (largest absolute difference across the checked numeric fields: `1.50e-11`; no eligibility mismatches).

The decisive evidence is stronger than a near-threshold miss:

| Required f1 gate | Pass | Fail |
|---|---:|---:|
| positive Sharpe **and** positive net return in at least 4 blocks | 37 | 63 |
| aggregate median position net PnL > 0 | **0** | **100** |
| aggregate top-three PnL share <= 50% | **0** | **100** |
| chained max drawdown <= 35% | 35 | 65 |
| gross profit before cost > 0 | 85 | 15 |
| charged cost <= 10% of gross profit | **0** | **100** |

Thus every candidate fails at least three independently calculated, predeclared gates. The saved f1 lock correctly has an empty `eligible_set` and `selected_id: null` (`reports/hts_v2_walkforward_2026-09-16/f1/selection_lock.json:2-10`, `reports/hts_v2_walkforward_2026-09-16/f1/selection_lock.json:3927-3930`). The report's `positions: 0`, `-Infinity`, and `Infinity` describe the empty **selected outer track**, not missing discovery position data (`scripts/evaluate_walk_forward.py:222-231`; `reports/hts_v2_walkforward_2026-09-16/walk_forward_report.json:1-15`).

## Intended rule versus evaluator

The plan requires all five grouped gates and explicitly requires Sharpe and return to be positive in the same at-least-four discovery blocks (`plans/hts_v2_plan.md:479-495`). The evaluator implements that exact conjunction at `scripts/evaluate_walk_forward.py:121-126`. This is not an accidental double condition.

The ranking rule also matches the plan: the plan's lexicographic order is at `plans/hts_v2_plan.md:497-507`, and the implementation is at `scripts/evaluate_walk_forward.py:147-153`.

## Artifact and coverage trace

- The runner writes `out_dir/candidate_id/block/run_result.json` at `scripts/run_hts_experiments.py:348-359`; the evaluator reads the identical layout at `scripts/evaluate_walk_forward.py:33-41`.
- `_load_payload` rejects the wrong kind or implementation revision and calls the independent run audit before returning a payload (`scripts/evaluate_walk_forward.py:37-49`).
- `_discovery_row` passes the six loaded payloads directly to `_pnl_breadth` (`scripts/evaluate_walk_forward.py:103-120`), and `_position_pnls` reads top-level `position_pnl_records[*].net_pnl` (`scripts/evaluate_walk_forward.py:52-69`).
- Raw inventory: 1,200 distinct candidate/block artifacts, 100 candidate directories, all `kind="hts-v2"`, all revision `hts-native-v2-2026-09-16-2`, zero nonempty `problems`, and **zero artifacts with an empty `position_pnl_records` list**.
- V048 discovery record counts are `93, 107, 103, 97, 96, 101` (597 total), and all 597 `net_pnl` values are nonzero. For example, V048 b03 declares the list and real losses at `reports/hts_v2_walkforward_2026-09-16/V048/b03/run_result.json:1804-1824`.
- `prepare_fold` iterates `registry.hts_v2_variations` directly (`scripts/evaluate_walk_forward.py:161-180`). Registry validation requires exactly `V001`-`V100` in order (`strategy_lab/experiment_registry.py:301-305`). Runtime inspection returned 100 unique v2 IDs with none missing, and the saved f1 lock contains 100 ranking rows.
- The raw suite summary contains 1,200 unique result pairs, all with `status="ok"`. Targeted current audits of V048, V047, and V059 across b01-b06 returned `audit.ok=True` for all 18 artifacts.
- A full current `--prepare-fold f1` validation reloaded and audited all 600 discovery runs, recomputed the 100 rows, and returned the existing lock path. Under `prepare_fold`, that succeeds only when the recomputed lock hash equals the stored lock hash (`scripts/evaluate_walk_forward.py:183-190`).

## V048 f1 gate inputs from raw artifacts

The six stored block Sharpe/return pairs are:

| Block | Daily-return Sharpe | Net total return | Counts positive? |
|---|---:|---:|---|
| b01 | 1.7108194744 | +58.98715956% | yes |
| b02 | -1.0610826576 | -17.73820984% | no |
| b03 | -0.6422323396 | -18.59028632% | no |
| b04 | 0.8159167150 | +14.87438876% | yes |
| b05 | -0.7266090855 | -15.58526733% | no |
| b06 | 0.0151655860 | -5.49489177% | no: Sharpe is positive but return is negative |

These values are present in the raw JSON metrics: `reports/hts_v2_walkforward_2026-09-16/V048/b01/run_result.json:1696-1706`, `reports/hts_v2_walkforward_2026-09-16/V048/b02/run_result.json:1833-1843`, `reports/hts_v2_walkforward_2026-09-16/V048/b03/run_result.json:1784-1794`, `reports/hts_v2_walkforward_2026-09-16/V048/b04/run_result.json:1734-1744`, `reports/hts_v2_walkforward_2026-09-16/V048/b05/run_result.json:1726-1736`, and `reports/hts_v2_walkforward_2026-09-16/V048/b06/run_result.json:1771-1781`.

| Gate input | Raw recomputation | Threshold | Result |
|---|---:|---:|---|
| Positive blocks | 2 of 6 | >= 4 | **fail** |
| Position records | 597 | nonempty | present |
| Aggregate position-PnL sum | $16,490.980557 | denominator for share | positive |
| Median position net PnL | -$475.237812 | > $0 | **fail** |
| Top-three net PnL | $75,517.411312 | diagnostic numerator | — |
| Top-three PnL share | 4.579316012 (457.93%) | <= 0.50 | **fail** |
| Chained max drawdown | 0.511294911 (51.13%) | <= 0.35 | **fail** |
| Aggregate net PnL | $16,452.893057 | sum of `(final_equity - 100,000)` | — |
| Gross profit before cost | $37,196.922667 | > $0 | pass |
| Charged transaction cost | $20,744.029609 | <= 10% of gross | **fail** |
| Cost/gross ratio | 0.557681338 (55.77%) | <= 0.10 | **fail** |
| Final `eligible` | `false` | all gates must pass | **fail** |

The independently recomputed row agrees with the stored V048 f1 row at `reports/hts_v2_walkforward_2026-09-16/f1/selection_lock.json:1885-1897`.

### Final equity and cost units

`metrics.final_equity` is each from-cash block's ending equity. V048's six raw ending-equity PnLs are `$58,987.159556`, `-$17,738.209839`, `-$18,590.286319`, `$14,874.388759`, `-$15,585.267332`, and `-$5,494.891768`, summing to `$16,452.893057`. The independent audit recomputes final equity from the last daily portfolio value and compares it to the stored metric (`strategy_lab/experiment_validation.py:45-89`, `strategy_lab/experiment_validation.py:247-258`).

Charged cost is the sum of `trade_cost` over fill rows (`strategy_lab/experiment_validation.py:92-113`). Both cost and gross profit are dollars, so `20,744.029609 / 37,196.922667 = 0.557681338`; the evaluator's units and inequality are correct (`scripts/evaluate_walk_forward.py:108-125`).

## Named strong candidates and binding gates

`P` means pass and `F` means fail.

| ID | Positive blocks | Median PnL | Top-3 share | Chained DD | Gross profit | Cost ratio | Pos | Median | Breadth | DD | Gross | Cost |
|---|---:|---:|---:|---:|---:|---:|:---:|:---:|:---:|:---:|:---:|:---:|
| V048 | 2 | -$475.24 | 457.93% | 51.13% | $37,196.92 | 55.77% | F | F | F | F | P | F |
| V047 | 2 | -$473.09 | 232.72% | 46.99% | $52,876.10 | 40.31% | F | F | F | F | P | F |
| V042 | 3 | -$339.39 | 157.58% | 54.02% | $75,284.68 | 32.46% | F | F | F | F | P | F |
| V059 | 4 | -$45.61 | 133.05% | 28.98% | $19,179.00 | 36.07% | P | F | F | P | P | F |
| V022 | 3 | -$389.96 | 106.14% | 48.71% | $97,732.83 | 25.37% | F | F | F | F | P | F |

V059 is especially useful as a control: it passes the positive-block, drawdown, and positive-gross gates but independently fails median PnL, PnL breadth, and cost drag by wide margins.

## Top-five robust-Sharpe near misses

These are the five ineligible rows with the highest `median(block Sharpe) - 0.5 * IQR(block Sharpe)`.

| ID | Robust Sharpe | Positive blocks | Median PnL | Top-3 share | Chained DD | Gross profit | Cost ratio | Failed gates |
|---|---:|---:|---:|---:|---:|---:|---:|---|
| V032 | 0.666837 | 5 | -$166.97 | 89.45% | 25.43% | $29,217.30 | 34.89% | median, breadth, cost |
| V004 | 0.621832 | 5 | -$59.90 | 63.08% | 24.04% | $44,009.73 | 11.62% | median, breadth, cost |
| V005 | 0.602766 | 5 | -$188.26 | 82.60% | 27.53% | $43,748.18 | 16.67% | median, breadth, cost |
| V084 | 0.583690 | 4 | -$140.40 | 128.13% | 61.88% | $100,183.78 | 14.53% | median, breadth, drawdown, cost |
| V024 | 0.572966 | 5 | -$156.87 | 122.01% | 62.37% | $106,523.21 | 13.54% | median, breadth, drawdown, cost |

Even the closest cost miss, V004, also has a negative median position PnL and 63.08% top-three share. There is no candidate that misses only one gate.

## Non-causal evaluator defect found

There is a genuine chaining defect, but it cannot explain the null selection. `_daily_returns` uses `daily.pct_change().dropna()` (`scripts/evaluate_walk_forward.py:72-82`), and `_chained_metrics` consumes that result (`scripts/evaluate_walk_forward.py:85-100`). Therefore each from-cash block's first-session return relative to `$100,000` is omitted.

Including that first-session return changes V048 from evaluator total return/max drawdown `-1.0737% / 51.1295%` to full-series `-2.7672% / 52.6102%`. Across all 100 f1 candidates, only V065 crosses the 35% drawdown threshold, from evaluator `34.9327%` (pass) to full-series `35.0408%` (fail). Correcting the omission leaves **0 eligible candidates** because all 100 still fail median PnL, breadth, and cost. The defect biases this one decision toward eligibility; it does not incorrectly reject a candidate.

Proposed source change (not made): prepend each block's first daily return as `first_daily_equity / 100_000 - 1` before chaining the remaining daily percentage changes.

One-line regression-test idea: build two synthetic from-cash blocks with a known first-session loss and assert chained return equals the product of both full block return factors and boundary cost, while max drawdown includes the first-session loss.

## Conclusion

The f1 cash decision is robust to the evaluator concerns investigated. The exact predeclared gates genuinely reject all 100 candidates; in fact, median per-position PnL, top-three concentration, and cost drag each independently reject all 100. No evaluator source was changed.
