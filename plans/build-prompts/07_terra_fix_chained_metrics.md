# TERRA — fix the walk-forward chained-metrics first-session omission

Repo: `/Users/openclaw/.hermes/profiles/free_money_glitch/home/lumibot` (branch `version/4.5.92/clean-base`).

## The bug (diagnosed by sol; verified — do not re-derive)
`scripts/evaluate_walk_forward.py::_daily_returns` (lines 72-82) computes
`daily.pct_change().dropna()`. For a from-cash block, this OMITS the first session's return
relative to `$100,000` because there is no prior row. `_chained_metrics` (lines 85-100) then
chains those daily returns, so each block's chained total-return and max-drawdown understate
the first session.

Independent numbers: including the omitted first return changes V048 (f1 discovery b01-b06)
from evaluator `-1.0737% / 51.1295%` to full-series `-2.7672% / 52.6102%`; across all 100 f1
candidates only V065 crosses the 35% drawdown threshold (`34.9327%` -> `35.0408%` fail). So the
defect biases TOWARD eligibility and does NOT change the current verdict (0 eligible on f1), but
it is a genuine correctness bug in reported metrics.

## The fix
Make `_chained_metrics` (and any other consumer of `_daily_returns`) account for each from-cash
block's first-session return. Concretely:

- Either change `_daily_returns` so it prefixes each block's first-session return as
  `(first_daily_equity / 100_000.0) - 1.0` before `pct_change().dropna()`
  (use the block's starting-cash constant, not `INITIAL_CASH` only — the blocks start from cash
  at $100,000; confirm the constant in `overnight_state`/`run_hts_experiments.py`),
  OR do it inside `_chained_metrics` so `_daily_returns` stays a pure per-session helper.
- Prefer the minimal, correct change. Preserve the boundary-transition cost charge
  (`2 * PRIMARY_COST_BPS / 10_000`) exactly as today.
- Do NOT change: the eligibility rule conjunction, the ranking lexicographic key, the gate
  thresholds, the V001-V100 matrix, or any strategy/registry/parameter code. This is an
  evaluator-metrics fix only.
- Confirm the fix does NOT reintroduce double-counting of the first session when chaining across
  block boundaries (boundary cost is a separate multiplicative factor, not a return row).

## Tests
Add ONE regression test to `tests/strategy_lab/` (or the walk-forward test module if one exists):
- Build two synthetic from-cash blocks with a KNOWN first-session loss and subsequent returns.
  Assert chained total-return equals the product of both full block return factors times the
  boundary-cost factor, AND that max-drawdown includes the first-session loss.
- Also assert the existing "drop first session" behavior is gone (i.e. total return is not 0.0 for
  a block whose only move is a first-session loss).

## Verification before you report done
- `python -m pytest tests/strategy_lab/ -q` green; report the count.
- Re-run the evaluator ONLY (blocks are on disk, no backtest re-run):
  `.venv/bin/python scripts/evaluate_walk_forward.py --all-folds --out-dir reports/hts_v2_walkforward_2026-09-16_02`
  and report the new `walk_forward_report.json`: does it still select cash on all six folds, and
  are the chained metrics now the full-series values? Do NOT reconfigure gates to make a candidate pass.
- Update `plans/build-prompts/TERRA_V2_RESULT.md` with a "FIX 3" section: the defect, the change,
  the test count, and the post-fix evaluator outcome (expect: still 0 eligible, metrics corrected).

## Constraints
- No LumiBot core changes. Only `scripts/evaluate_walk_forward.py` + its tests.
- No commit/push. No branches. Keep `IMPLEMENTATION_REVISION` unchanged (evaluator-metrics only;
  block artifacts don't change, so their revision stays `hts-native-v2-2026-09-16-2`).
- The run out-dir for the re-run is `reports/hts_v2_walkforward_2026-09-16_02` (do not overwrite the original).
- Report the outcome honestly. If the fix unexpectedly makes any candidate eligible, that is the
  correct result — report it rather than suppressing it.