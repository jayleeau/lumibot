# TERRA IMPLEMENT TASK — execute the amended HTS hourly-convention plan

You are Terra, the builder/implementer. Read the plan at `plans/hts_hourly_convention.md` (which now INCLUDES the Sol-review amendments A1-A7 at the end) and implement it exactly, in order, with real verification. Work in the repo at `/Users/openclaw/.hermes/profiles/free_money_glitch/home/lumibot`.

## Mandatory discipline
- Python: `.venv/bin/python`. Tests: `.venv/bin/python -m pytest -c pytest-glitch.ini`.
- NEVER commit unless explicitly asked. (This task asks you to STAGE + COMMIT per the plan's Item 1 and Step 4, so you MAY run `git add` for the exact allowlisted paths and `git commit`; report each commit SHA. Do NOT push.)
- NEVER touch `ai.glitch.live.*`, `live/common.py`, `live/strategies.py`, any `*_trader*.py`, or kill trader processes. Keep `lumibot/` READ-ONLY.
- The US market may be OPEN: the plan authorizes exactly TWO single control backtests (one `HTS_CONTROL_1`/`six_year` run in `reports/hts_hourly_alignment_2026-09-15/before/` BEFORE the mapping edit, one in `.../after/` AFTER it). Do NOT run the full suite or any wider backtests. Give control commands a 20-min timeout and fail normally if exceeded.
- Work only in `strategy_lab/`, `scripts/`, `docs/`, `tests/`, and the explicit compact `reports/` artifacts staged for Item 1. Do not `git add -A`/`.`, no stash/reset/clean/checkout, no broad deletes. Exact-path staging only.
- Re-read `git status --short --branch` immediately before every stage/commit; the branch is shared.

## Execute in this order

### Phase 0 — Preflight
- Confirm branch is exactly `version/4.5.92/clean-base` (else STOP).
- Run `git status --short --branch` + `git diff -- strategy_lab/native_experiments.py`; confirm no staged files (`git diff --cached --name-only`). Confirm the Item-1 allowlist files are unmodified or read/preserve concurrent work.

### Phase 1 — Item 1: commit the completed walk-forward suite (exact 11 paths)
- `py_compile` the four new scripts; run `tests/strategy_lab/test_native_experiments.py`; `git diff --check`.
- Stage EXACTLY: `strategy_lab/native_experiments.py`, `scripts/evaluate_walk_forward.py`, `scripts/build_walk_forward_page.py`, `scripts/stress_walk_forward_costs.py`, `scripts/audit_hts_session_cleanliness.py`, `reports/hts_walkforward_2026-09-14/suite_manifest.json`, `reports/hts_walkforward_2026-09-14/suite_summary.json`, `reports/hts_walkforward_2026-09-14/walk_forward_report.json`, `reports/hts_walkforward_2026-09-14/walk_forward.html`, `reports/hts_wf_cost_stress/cost_stress.json`, `reports/hts_session_cleanliness_audit.json`.
- Content-scan those 11 files for personal absolute paths / credentials (A7); report any redacted. Do NOT stage per-candidate dirs, `short/`, logs, `live/`, `plans/`, `.Codex/`, `reports/full_lumibot_*`, unlisted `reports/hts_native_2026-09-14/` items.
- Commit: `Add HTS walk-forward suite and hourly-convention groundwork`. Record SHA. Do not push.

### Phase 2 — Item 2: freeze the hourly/execution contract (documentation)
- Create `docs/investigations/2026-09-15_HOURLY_CONVENTION.md` with the exact header + all required sections from the plan (Scope; Bar labels/intervals/availability with the [T,T+1h)/known-at rule; Actionability table with 14:00=last same-day-actionable and 15:00-close=next-open gap-exposed; Stop contract trigger-not-price; Latched exits; `reentry_cooldown_bars` -1/0 disable, N>0 sessions, H088/H089/H090=1/3/5 with reference to the legacy `stop_cooldown_sessions` being migrated; Fixed-pool rotation; Session cleanliness with the audit numbers; Enforcement map; Control A/B table (fill in Sections 4/5); Consequences for prior results). Incorporate amendments A5/A6 accurately: scope completed-close trigger semantics to VIRTUAL stops; if slot enforcement is not fully added, honestly mark it a documented limitation; document resting-stop semantics separately or keep those blocked.
- Update the cross-links + stale-result labels in `docs/HTS_100_VARIATIONS_RESEARCH_PLAN.md`, `docs/HTS_V1_PAPER_PARITY.md`, `docs/HTS_NATIVE_RESULTS.md`, `docs/BACKTESTING_ARCHITECTURE.md` exactly as the plan's section 2.3 describes.
- Verify per plan: grep that active-contract matches for `09:30-15:30`/relabel are only in explicitly-historical contexts.

### Phase 3 — Item 3: align the native hourly path + apply A1-A7
- **3.1 Before-control**: run exactly the one control `HTS_CONTROL_1`/`six_year` into `reports/hts_hourly_alignment_2026-09-15/before/` (suite `hts-hourly-before-2026-09-15`), audit it, record metrics + `hour_mapping_convention`. Require one accepted audit, `problems=[]`.
- **3.2 Code changes** (all in strategy-owned harness, `lumibot/` untouched):
  - Add a module constant for the exact frozen convention string; include it in the feature-hash provenance payload so old/new inputs can't share a hash.
  - Bump `IMPLEMENTATION_REVISION` to `hts-native-2026-09-15-clock-hour-1`.
  - **A1:** build ONE canonical exact-minute 09:00-15:00 ET frame per symbol used by BOTH `_hourly_features()`/strategy feature math AND the LumiBot `Data` copy. Change `_lumibot_hourly` to retain whole-hour 09:00-15:00 labels without the 09:30 relabel, and ensure the strategy feature frame is filtered the same way (this is the critical fix sol flagged). Keep `_completed_row` strict-index-before-current semantics; keep `_execution_price` at the current stamp's open.
  - **A3:** fix cooldown expiry algebra to `i + N` (previous-session lookup) and migrate the read from `stop_cooldown_sessions` to `reentry_cooldown_bars`; `-1`/`0` disable, `N>0` blocks N completed sessions. Preserve H088/H089/H090 1/3/5 behavior.
  - **A4:** also update the `FAMILY_10_UNIVERSE` declaration in `hts_variants.py`; fully remove `stop_cooldown_sessions` across `strategy_lab/`, `scripts/`, `tests/`, and active/generated docs.
  - **A6:** add complete, joinable stop-gap event reporting: symbol, mode/reason, order ID, entry, stop, trigger timestamp/close, fill time/price/quantity, trigger/fill sessions, overnight flag; expose aggregate count in `build_payload`. Do NOT read the trigger bar's low or substitute the stop for the fill. Scope completed-close semantics to virtual stops.
  - Update `build_payload` hour-mapping text, `docs/HTS_VARIATIONS_CATALOG.md` (regenerate from registry), `scripts/run_hts_experiments.py::build_manifest` (record hourly convention cleanly, keep daily alternatives separate).
- **3.3 Deterministic tests** in `tests/strategy_lab/test_native_experiments.py`: rename/assert clock-hour preservation (08:00/09:00/09:30/10:00/15:00/16:00 rows -> output 09:00/10:00/15:00 unchanged, no sub-hour minutes/seconds, extended-hops absent); **A2** lifecycle causality fixture proving 14:00-close -> not before 15:00 open and 15:00-close -> next-session 09:00 open only; **A3** cooldown per-day for -1/0/1/3/5; **A4** zero legacy key; **A6** stop-gap fill-minus-stop / entry-to-fill hand-calc + overnight flag + trigger-bar-low independence. Run the three targeted test files.
- **3.4 After-control**: run exactly one `HTS_CONTROL_1`/`six_year` into `.../after/` (suite `hts-hourly-after-2026-09-15`), audit, require new revision + convention + `problems=[]`.
- **3.5 Compare economics**: compare both `run_result.json`/`run_trades.csv`/`run_stats.csv` after timezone-aware minus-30-min normalization of the old run. Report whether economic parity held (event order, prices, qty, fees, equity, cash) or the first divergence + metric impact. Populate the doc A/B table.

### Phase 4 — Step 4: final verification + second commit
- Expected tracked set per plan (native_experiments.py, hts_variants.py, run_hts_experiments.py (only if actually changed), the two test files, the 5 docs incl. catalog + investigation). Run the three targeted test files, `git diff --check`, `git status`.
- Stage EXACTLY that verified set; commit `Align HTS native bars to the clock-hour convention`. Record SHA. No push.

## Report (write to plans/build-prompts/TERRA_RESULT.md)
- Both commit SHAs (rev-parse HEAD after each).
- Targeted test results (real pass counts).
- Old vs new control metrics table + deltas; whether normalized trade/equity parity held or first divergence.
- Which of A1-A7 were implemented vs. documented-as-limitation (A5).
- Remaining untracked artifacts; the explicit off-hours rebaseline requirement (full H001-H100 suite must be rerun on the new revision later).
- Any blocker or divergence from the plan.
Do not touch the running fleet. Keep the doc honest: mark slot enforcement and any resting-stop semantics accurately.