# SOL — investigate v2 walk-forward all-cash result: genuine or evaluator bug?

You are `sol` (read-only investigator). Repo:
`/Users/openclaw/.hermes/profiles/free_money_glitch/home/lumibot` (branch `version/4.5.92/clean-base`, venv `.venv/bin/python`).

## Background
The HTS v2 walk-forward ran and reported **all six folds selected cash**:
reports/hts_v2_walkforward_2026-09-16/walk_forward_report.json shows `selection_per_fold[*].selected_id=null`,
`outer_position_breadth": {"median_position_pnl": -Infinity, "positions": 0, "top_three_pnl_share": Infinity}`.
The per-fold locks (f1..f6/selection_lock.json) all have `eligible_set: []`, `selected: None`.

The plan (plans/hts_v2_plan.md) defines a strict eligibility rule. The reported -Inf "positions: 0" is
consistent with NO candidate passing (nothing to aggregate), OR with a bug that zeroes eligibility.

## Facts already established (do not re-derive, but verify as needed)
- 1,200 block jobs ran (100 hts-v2 x b01..b12), all ok. 626 blocks have daily-return Sharpe > 0.5.
- Block artifacts DO carry real position data: e.g. `reports/hts_v2_walkforward_2026-09-16/V048/b03/run_result.json`
  has `position_pnl_records` = 103 records, all non-zero `net_pnl`.
- evaluator code: `scripts/evaluate_walk_forward.py` lines 52-69 `_position_pnls` / `_pnl_breadth`
  (returns -inf/inf only when NO records), lines 103-144 `_discovery_row` (the gates), 161+ `prepare_fold`.

## Your task
Determine, with file:line + raw-data evidence, WHICH hypothesis is true:

(A) **Genuine no-qualify**: the strict gates reject every candidate for legitimate reasons
    (positive_blocks<4, or median_pnl<=0, or top_three_share>0.5, or chained maxDD>0.35, or
    gross_profit<=0, or cost>10% of gross). If so, identify the GATE(s) that bind for the strongest
    candidates (V048, V047, V042, V059, V022 — top by discovery Sharpe) on fold f1 (discovery b01-b06).

(B) **Evaluator bug**: a bug makes candidates fail eligibility they should pass. Look hard at:
    1. `_position_pnls` — does it actually receive payloads with `position_pnl_records` for the
       DISCOVERY blocks (b01..b06), or is it reading an empty/None list? Trace `_discovery_row`
       payload loading (line 104).
    2. `positive_blocks` (line 121) — it requires BOTH sharpe>0 AND total_return>0 in the same block.
       Is that the plan's intent, or a double condition that's too strict? Compare to plans/hts_v2_plan.md
       gate #1 ("daily-return Sharpe AND net total return are both positive in at least four of six").
    3. `net_pnl` (line 118) = sum(final_equity - 100_000). Is final_equity the block's ending equity?
       Could it be that block equity is measured wrong (e.g. not reset from cash, or chained)?
    4. `charged_cost <= 0.10 * gross_profit` — units/SKG: done right?
    5. `_chained_metrics` max_drawdown / returns — correct chaining across blocks with boundary cost?
    6. Whether `registry.hts_v2_variations` actually iterates all 100 v2 candidates in `prepare_fold`.
    7. Whether `_load_payload` (lines ~38-49) is finding the SAME artifact path the runner wrote
       (candidate_id/block/run_result.json) and passing the audit/revision checks.

## Deliverable
Compute, for V048 (and V047/V059 if useful) on fold f1 discovery blocks (b01..b06), the actual value of
each gate input from the RAW artifacts: positive_blocks, median_pnl, top_three_share, chained_max_drawdown,
gross_profit, charged_cost, cost_ratio, and the final `eligible` bool. Also compute how many of the 100
candidates are eligible on f1, and print the top-5 near-miss rows (highest robust_sharpe among ineligible).

Then write a verdict to `plans/review-prompts/06_sol_v2_wf_null.md`:
- Verdict: **GENUINE NO-QUALIFY** or **EVALUATOR BUG** (with the precise bug + file:line + repro value).
- The exact gate inputs table for V048 f1.
- If a bug: exactly what changed and a one-line regression test idea. Do NOT edit source — you are read-only.
- Read plans/hts_v2_plan.md and the plan's "Honest v2 walk-forward plan" section to confirm the intended gates.

Read-only: you MAY run python to compute from artifacts and run the evaluator's pure functions, run tests,
and grep. Do NOT edit files. Do NOT run 1,200-job backtests (blocks are already on disk).