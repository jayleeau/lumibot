# SOL IMPLEMENTATION-REVIEW — audit Terra's HTS hourly-convention build

You are Sol, the architect, in REVIEW mode. TWO commits landed on `version/4.5.92/clean-base` in repo `/Users/openclaw/.hermes/profiles/free_money_glitch/home/lumibot`:
- `a581f03d` "Add HTS walk-forward suite and hourly-convention groundwork"
- `20af68d6` "Align HTS native bars to the clock-hour convention"

The authoritative plan (with Sol-review amendments A1-A7) is at `plans/hts_hourly_convention.md`. Terra's report is at `plans/build-prompts/TERRA_RESULT.md`. Give a verdict: **APPROVE** or **CHANGES REQUIRED** with file:line findings.

DO NOT modify code. Read-only. Do not touch `ai.glitch.live.*`, `live/common.py`, `live/strategies.py`, `*_trader*.py`, or `lumibot/`. The market is open — do not run backtests.

## Verify these specifically (find real defects, not style)
1. **A1 wiring (the critical one):** does `_canonical_hourly_frame` really produce ONE 09:00-15:00 exact-clock-hour frame used by BOTH `_hourly_features()`/strategy feature math AND the LumiBot `Data` copy — with NO residual unfiltered `hourly_raw` path reaching `_completed_row()`/ATR? Confirm pre/post-market rows (e.g. 17:00, 08:00) are truly excluded from strategy decisions, not just from the broker Data feed.
2. **Causality:** confirm `_completed_row` strict-index-before-current + `_execution_price` current-open semantics make a 14:00 close actionable at 15:00 and a 15:00 close actionable only at next-session open. Confirm the reported "economic divergence is material + causal" (old numbers inserted a post-market 17:00 close; new numbers use the 15:00 close) is honestly correct, not a bug that accidentally LOWERED fills.
3. **The divergence is what it claims:** after-run Sharpe 0.749 / fills 2,588 vs before 0.657 / 3,376. Verify this is a legitimate removal of extended-hours contamination, and confirm the doc marks ALL old hourly results as historical + requires an off-hours rebaseline (and did NOT silently claim "economic parity").
4. **Cooldown (A3):** confirm expiry is `i + N` (previous-session lookup), `-1`/`0` disable, H088/H089/H090 = 1/3/5, zero legacy `stop_cooldown_sessions` anywhere active.
5. **Revision + provenance (A4/sound):** `IMPLEMENTATION_REVISION` = `hts-native-2026-09-15-clock-hour-1`; `HTS_HOURLY_CONVENTION` constant included in feature-hash provenance; `--resume` cannot reuse old artifacts.
6. **Stop-gap events (A6):** schema has symbol, mode/reason, order ID, entry, stop, trigger time/close, fill time/price/qty, trigger/fill sessions, overnight flag; formulas fill−stop, (fill−stop)/stop, fill/entry−1; trigger-bar low NOT used; completed-close semantics scoped to VIRTUAL stops and resting-stop modes kept blocked/qualified separately.
7. **Commit hygiene:** the exact 11-file Item-1 commit and the 11-file Item-3 commit contain only owned files; no `lumibot/`, no live, no push, no `short/`/logs/scratch; content secret scan done; A5 honestly marked slot cap as an unenforced gap.
8. **Tests:** the new/renamed tests actually assert the causality boundary and cooldown days (not vacuous); independent run reported 48 passed.

## Output (write to plans/review-prompts/review2_sol_impl.md if writable, else inline)
- Verdict line: `APPROVE` or `CHANGES REQUIRED`.
- Numbered findings with severity (BLOCKER/MAJOR/MINOR), file:line, concrete minimal fix.
- Honest list of anything you could NOT verify read-only (e.g. production-suite behavior, fill-bar edge cases) that the off-hours rebaseline must prove.
- Note (no action): the full H001-H100 rebaseline is explicitly deferred to off-hours per the plan; do not treat its absence as an implementation failure.