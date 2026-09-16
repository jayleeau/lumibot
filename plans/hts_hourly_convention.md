# HTS Hourly-Convention Freeze and Native-Path Alignment Plan

Description: Ordered handoff for committing the completed walk-forward work, freezing the HTS hourly/execution contract, and aligning the native research backtest with live clock-hour bars.

Last Updated: 2026-09-16

Status: Planning complete; no implementation, commits, pushes, or backtests performed by Sol

Audience: Terra implementing the HTS research-harness change

## Overview

Execute this plan in order. Commit the already-completed walk-forward work first, capture one old-convention control baseline, write the contract, change only the strategy-owned research harness, run one new-convention control, compare the two, and commit the alignment separately. The North Star is causal, reproducible walk-forward evidence; no headline metric is valid if the bar clock, execution timing, or capital lifecycle is ambiguous.

This is not a LumiBot framework change. `lumibot/`, the live paper fleet, launchd services, broker processes, and the protected `live/` files remain untouched. Do not push either commit.

The target investigation keeps the user-requested decision date and filename `docs/investigations/2026-09-15_HOURLY_CONVENTION.md`, even though this implementation handoff was finalized on 2026-09-16.

## Scope and hard guards

- Stay on `version/4.5.92/clean-base`; do not create or switch branches.
- Work only in `strategy_lab/`, `scripts/`, `docs/`, `tests/`, and the explicitly allowed compact `reports/` artifacts.
- Never edit, restart, unload, kickstart, or signal `ai.glitch.live.*`; never edit `live/common.py`, `live/strategies.py`, or any `*_trader*.py`; never kill trader processes.
- Keep `lumibot/` read-only. The change is in the HTS native harness, not the upstream framework.
- Use `.venv/bin/python` and `.venv/bin/python -m pytest -c pytest-glitch.ini`.
- Do not run the full descriptive or approximately 1,900-job walk-forward suites during market hours. This plan authorizes only one `HTS_CONTROL_1`/`six_year` run before the mapping edit and one after it.
- Do not use `git add .`, `git add -A`, stash, reset, clean, checkout, or broad deletion. The tree contains unrelated live, cache, plan, and report artifacts.
- The local timeout wrapper is currently absent. Give each control command a 20-minute orchestration timeout and stop it normally if it unexpectedly exceeds that bound; do not kill unrelated Python/trader processes.
- Re-read `git status --short --branch` immediately before every stage/commit because the branch is shared.

## Step 0 - Preflight and ownership snapshot

Files: none changed.

Why: another agent may have changed the shared branch after this plan was written. The current observed tree has the owned Item-1 files plus many unrelated untracked directories.

Actions:

1. Run `git branch --show-current` and require exactly `version/4.5.92/clean-base`. If it differs, stop; do not switch it.
2. Run `git status --short --branch` and `git diff -- strategy_lab/native_experiments.py`.
3. Confirm the Item-1 allowlist still contains only the described walk-forward window change and four new scripts. If another agent has edited an allowlisted file, read and preserve that work; do not overwrite or revert it.
4. Confirm there are no staged files with `git diff --cached --name-only`. If there are, stop and identify their owner before staging.

Verification: preflight is complete only when branch, dirty-file ownership, and the empty index are understood.

## Item 1 - Commit the completed walk-forward suite

### 1.1 Staging decision

Commit these source files:

- `strategy_lab/native_experiments.py` - `_add_months`, `_WF_FOLDS`, `WALK_FORWARD_WINDOWS`, and the expanded `WINDOW_BY_LABEL` only.
- `scripts/evaluate_walk_forward.py`
- `scripts/build_walk_forward_page.py`
- `scripts/stress_walk_forward_costs.py`
- `scripts/audit_hts_session_cleanliness.py`

Commit only these compact, reviewable evidence files (approximately 1.2 MB total):

- `reports/hts_walkforward_2026-09-14/suite_manifest.json`
- `reports/hts_walkforward_2026-09-14/suite_summary.json`
- `reports/hts_walkforward_2026-09-14/walk_forward_report.json`
- `reports/hts_walkforward_2026-09-14/walk_forward.html`
- `reports/hts_wf_cost_stress/cost_stress.json`
- `reports/hts_session_cleanliness_audit.json`

Do not commit:

- Any per-candidate directory below `reports/hts_walkforward_2026-09-14/` (the current tree is about 893 MB).
- `reports/hts_wf_cost_stress/7/`, `reports/hts_wf_cost_stress/15/`, or any generated stats/trades/settings/result directories.
- `short/`, logs, `live/`, `plans/`, `.Codex/`, `reports/full_lumibot_*`, unlisted `reports/hts_native_2026-09-14/` artifacts, or any other pre-existing scratch output.
- An opportunistic `.gitignore` change. Exact path staging is the safety boundary for this commit.

The compact evidence currently contains no detected personal absolute path or obvious credential term, but re-run a filename-only hygiene scan before staging because the shared tree may have changed. Do not hand-edit generated metrics or rewrite the manifest's recorded generation commit.

### 1.2 Cheap pre-commit verification

Why: the work was already validated in the originating session, so do not rerun the research suite. Use only cheap integrity checks before committing.

Run:

```bash
.venv/bin/python -m py_compile \
  scripts/evaluate_walk_forward.py \
  scripts/build_walk_forward_page.py \
  scripts/stress_walk_forward_costs.py \
  scripts/audit_hts_session_cleanliness.py
.venv/bin/python -m pytest -c pytest-glitch.ini -q \
  tests/strategy_lab/test_native_experiments.py
git diff --check
```

If the test fails only because a concurrent edit changed the shared file, diagnose that overlap; do not alter unrelated work merely to force the commit through.

### 1.3 Exact staging and commit

Run path-specific staging only:

```bash
git add -- \
  strategy_lab/native_experiments.py \
  scripts/evaluate_walk_forward.py \
  scripts/build_walk_forward_page.py \
  scripts/stress_walk_forward_costs.py \
  scripts/audit_hts_session_cleanliness.py \
  reports/hts_walkforward_2026-09-14/suite_manifest.json \
  reports/hts_walkforward_2026-09-14/suite_summary.json \
  reports/hts_walkforward_2026-09-14/walk_forward_report.json \
  reports/hts_walkforward_2026-09-14/walk_forward.html \
  reports/hts_wf_cost_stress/cost_stress.json \
  reports/hts_session_cleanliness_audit.json
git diff --cached --check
git diff --cached --name-only
git diff --cached --stat
```

The cached name list must be exactly the 11 files above. Then commit with:

```bash
git commit -m "Add HTS walk-forward suite and hourly-convention groundwork"
```

Record `git rev-parse HEAD` and inspect `git show --stat --oneline HEAD`. Do not push. The large untracked result directories remaining in `git status` are expected and must not be cleaned.

## Item 2 - Freeze the hourly and execution contract

### 2.1 Create the durable investigation

File: `docs/investigations/2026-09-15_HOURLY_CONVENTION.md`.

Required header:

- Title: `HTS Hourly and Execution Convention`
- One-line description
- `Last Updated: 2026-09-15`
- Status, initially `Contract frozen; native clock-hour wiring pending`; change it to `Contract frozen and native clock-hour wiring verified` only after Item 3 passes.
- Audience
- `## Overview`

Required sections and exact normative substance:

1. **Scope and ownership** - this contract governs the strategy-owned HTS native research path and parity expectations for live Alpaca `1H` data. It does not change LumiBot framework behavior and does not authorize live operations.
2. **Bar labels, intervals, and availability** - state verbatim that the hourly bar convention is clock-hour `09:00` through `15:00` ET, anchored to whole clock hours, and that a bar labelled T contains `[T, T+1h)` and is only known at T+1h.
3. **Actionability table** - explicitly show:
   - the `14:00` bar covers `14:00-15:00`, is known at `15:00`, and is the last same-day-actionable bar;
   - the `15:00` bar covers `15:00-16:00`, is known at `16:00`, and any resulting market order fills at the next session open;
   - the latter is overnight gap-exposed and is never an instant or guaranteed fill.
4. **Stop contract** - a stop is a trigger, not a price guarantee. A breached completed close queues a market exit. Never fill at the stop or use the triggering bar's low. Realized loss is entry-to-actual-next-open; stop-to-fill gaps are a separate reported quantity.
5. **Latched exits** - once queued, a stop exit is unconditional and cannot be canceled because the next morning recovered. Name the only alternative as a different, explicitly documented weaker contract that re-evaluates at the next executable bar; it is not this contract.
6. **Re-entry cooldown** - use the canonical contract name `reentry_cooldown_bars`: `-1` and `0` disable the cooldown; `N > 0` bars the stopped symbol for N completed exchange-session bars before requalification. State that H088/H089/H090 exercise 1/3/5 complete-session values. Identify the current `stop_cooldown_sessions` spelling as the legacy internal name that Item 3 migrates; do not leave two independently active knobs.
7. **Fixed-pool rotation** - sales must free both cash and the slot before replacement buys are sized. The control is two slots at `99.5% / 2 = 49.75%` NAV each. A pending, failed, or unfilled sale contributes zero spendable proceeds; never count its notional a second or third time.
8. **Session cleanliness** - cite `reports/hts_session_cleanliness_audit.json`: the 15-minute archive has 4,660,438 bars, is not regular-session-only, and is approximately 64% regular-session. State the premarket `04:00-09:30` and post-market `16:00-20:00` ET ranges, the explicit `09:30-16:00` filter requirement for a regular-session strategy, and A10's need for genuine `09:30-10:00` one-minute bars.
9. **Enforcement map** - include a table with contract clause, current file/method, Item-3 action, and any residual qualification gap.
10. **Control A/B evidence** - reserve a table for old mapping, new mapping, implementation revision, fills, journal events, final equity, total return, CAGR, daily Sharpe, volatility, max drawdown, minimum cash, terminal positions, and the first event-level difference. Populate it only from the two audited runs.
11. **Consequences for prior results** - identify old artifacts as historical under the `09:30-15:30` relabel convention and require a new revisioned rebaseline before they are compared or ranked as current evidence.

### 2.2 Enforcement map to document accurately

Use these current locations rather than vague statements:

- Clock-hour mapping: `strategy_lab/native_experiments.py::_lumibot_hourly`; currently not enforced and wired by Item 3.
- Completed-bar causality: `RegistryHtsStrategy._completed_row`, `_execution_price`, and `on_trading_iteration`; Item 3 adds boundary tests at `15:00`, `16:00`, and next-session `09:00`.
- Virtual-stop trigger and real fill: `RegistryHtsStrategy._update_risk`, `_submit_sell`, and `on_filled_order`; `BacktestingBroker` supplies the actual market fill. The current payload has only limited journal diagnostics; Item 3 must persist explicit trigger-level/fill-price gap events before the doc may claim this reporting clause is enforced.
- Exit latch: `_pending_sells` and `_pending_sell_reason` prevent duplicate/reopened exits until `on_filled_order` clears the state.
- Cooldown: `_cooldowns`, `_select`, and `on_filled_order`, with registry values in `strategy_lab/hts_variants.py` for H088/H089/H090. Item 3 migrates the registry key without changing those 1/3/5-session behaviors.
- Fixed pool: `strategy_lab/hts_policies.py::target_weights` sets equal slots; `RegistryHtsStrategy._rebalance` submits sells first, reads current broker cash, decrements one local budget for each buy, and does not include pending sale proceeds. Describe this as conservative enforcement: a replacement may wait, but it may not borrow an assumed sale.
- Session cleanliness: `scripts/audit_hts_session_cleanliness.py` and the JSON evidence. `_lumibot_hourly` is a clock-parity adapter, not an RTH-cleaning function. A10 remains blocked rather than receiving a proxy.

### 2.3 Cross-links and stale-result labels

Files:

- `docs/HTS_100_VARIATIONS_RESEARCH_PLAN.md`
  - Link the investigation near the execution-engine decision.
  - Replace the unresolved half-hour wording in sections 1-3 with the frozen clock-hour/availability rule.
  - Split the old P1 item: bar labelling is closed by Item 3; strict RTH cleanliness remains a separate limitation.
  - Keep the retrospective-evidence and A10 data requirements intact.
- `docs/HTS_V1_PAPER_PARITY.md`
  - Link the new contract and replace the generic “normalized hourly boundary” wording with the exact clock-hour/available-at rule.
  - Do not imply the retired custom replay or paper wrapper is the native research engine.
- `docs/HTS_NATIVE_RESULTS.md`
  - Add a prominent status note that all listed hourly/control figures use the old `09:30-15:30` relabel and are historical until rebaselined.
  - Link the convention doc; do not edit the old numbers.
- `docs/BACKTESTING_ARCHITECTURE.md`
  - Add the HTS convention link under related docs and clarify that post-close HTS virtual stops are stricter than the generic intrabar native stop-order paragraph.

No HTS-specific handoff currently exists under `docs/handoffs/`; the two hourly/native-suite handoffs there concern a different strategy path. Do not edit them merely to satisfy a link count. If a concurrent agent adds an HTS handoff before implementation, add the same investigation link to that handoff while preserving its local-only status. No `docsrc/` change is needed because this is research-harness behavior, not a public LumiBot API change.

Verification:

```bash
grep -RInE "09:30-15:30|relabelled to NYSE 09:30-15:30" \
  strategy_lab scripts tests docs
```

After Item 3, remaining matches are allowed only where the old convention is explicitly labelled historical or in the before-run artifact. Any active-contract match is a failure.

## Item 3 - Align the native hourly path

### 3.1 Capture the old-convention control before editing mapping code

Files written: untracked runtime artifacts only under `reports/hts_hourly_alignment_2026-09-15/before/`.

Why: this freezes the current relabel behavior after Item 1 is committed, so the A/B comparison is not confounded by the walk-forward code commit.

Run exactly one job, without `--resume`:

```bash
.venv/bin/python scripts/run_hts_experiments.py \
  --ids HTS_CONTROL_1 \
  --windows six_year \
  --workers 1 \
  --out-dir reports/hts_hourly_alignment_2026-09-15/before \
  --suite-id hts-hourly-before-2026-09-15
.venv/bin/python scripts/evaluate_hts_experiments.py \
  --out-dir reports/hts_hourly_alignment_2026-09-15/before \
  --write --top 1
```

Require one accepted audit, `problems=[]`, finite metrics/fills, and `hour_mapping_convention` equal to the old relabel text. Record the Item-1 commit SHA, input checksums, feature hash, and old implementation revision. Do not use the pre-existing September 14 control artifact as the formal “before” result; it is useful only as a sanity check.

### 3.2 Change the mapping and provenance

Files and methods:

- `strategy_lab/native_experiments.py`
  - Add one module constant for the exact frozen convention string so payloads, manifests, feature hashes, and tests cannot drift independently.
  - Bump `IMPLEMENTATION_REVISION` from `hts-native-2026-09-14-2` to `hts-native-2026-09-15-clock-hour-1`. This is mandatory because old artifacts must never satisfy `--resume` after a timing change.
  - Change `_lumibot_hourly(frame)` to retain only exact whole-hour labels from `09:00` through `15:00` ET and preserve those original indices and OHLCV values. Remove the normalize/rebuild step and the `+ 09:30` offset. Do not rewrite source prices or mutate the strategy feature frame.
  - Keep `_completed_row`'s strict “index before current stamp” lookup. Keep `_execution_price` at the current stamp's open. This is what makes the `14:00` close actionable at `15:00` and the prior session's `15:00` close actionable only at the next available session open.
  - Include the convention constant in `prepare_inputs`' feature-hash payload so old and new prepared inputs cannot share a provenance hash merely because row counts match.
  - In `build_payload`, replace the old text with the exact constant for hourly HTS/control runs. Remove the hourly-convention field from daily-alternative payloads instead of attaching irrelevant hourly noise.
  - Migrate the cooldown read from `stop_cooldown_sessions` to `reentry_cooldown_bars`. Preserve the existing fill-based start and `_session_index` expiry rule; both `-1` and `0` take the existing disabled branch, while positive values retain the H088/H089/H090 1/3/5-session behavior.
  - Make stop-gap reporting complete and explicit without changing execution: retain the active stop level, triggering completed-bar timestamp/close, entry price, reason, and whether the trigger was the `15:00` bar when `_submit_sell` latches a stop exit. In `on_filled_order`, pair that immutable trigger context with the broker's actual fill and record dollars/share and percentage fill-minus-stop gap plus entry-to-fill realized return. Expose the complete event list and an aggregate count in `build_payload`. Do not read the trigger bar's low and do not substitute the stop level for the fill.
- `strategy_lab/hts_variants.py`
  - Rename the parameter spec, `HTS_BASELINE` key, Family 9 parameter declaration, H088/H089/H090 overrides, and H099 combination override from `stop_cooldown_sessions` to `reentry_cooldown_bars`.
  - Set the declared minimum to `-1`; treat `-1` and `0` identically as disabled. Do not retain two knobs or silently map conflicting values.
- `docs/HTS_VARIATIONS_CATALOG.md`
  - Regenerate it from the registry after the key migration; do not hand-edit fingerprints. The 101 control/HTS fingerprints are expected to change and are another reason old artifacts cannot resume under the new revision.
- `scripts/run_hts_experiments.py::build_manifest`
  - Record the exact hourly convention in a cadence-specific `bar_conventions`/`hourly` manifest field. Keep daily alternatives explicitly separate.

Regenerate the catalog with `.venv/bin/python scripts/list_strategy_experiments.py --write-catalog docs/HTS_VARIATIONS_CATALOG.md`. Do not rename or alter `strategy_lab/hts_v1_core.py` or the live/paper wrapper in this patch. The native harness is the code target. Also do not turn `_lumibot_hourly` into a purported `09:30-16:00` cleaner; the first clock-hour bar can straddle the regular-session open, and that limitation belongs in the contract.

### 3.3 Deterministic tests

File: `tests/strategy_lab/test_native_experiments.py` (created 2026-09-13, so it is a new test rather than legacy/frozen).

Changes:

1. Rename `test_lumibot_hour_mapping_drops_premarket_and_relabels` to describe clock-hour preservation.
2. Use fixture rows at `08:00`, `09:00`, `09:30`, `10:00`, `15:00`, and `16:00`. Assert output indices are exactly `09:00`, `10:00`, and `15:00`; values are unchanged; no result has nonzero minutes/seconds; pre/post/out-of-convention rows are absent.
3. Add a deterministic completion-boundary test around `RegistryHtsStrategy._completed_row` and `_execution_price`:
   - at same-day `15:00`, the completed row is `14:00` and the executable open is `15:00`;
   - at `16:00`, the `15:00` close is completed but there is no same-day executable bar/open;
   - at next-session `09:00`, the completed row remains the prior `15:00` bar and the executable price is the new session's `09:00` open.
4. Assert the convention constant contains the `[T,T+1h)`/available-at rule and the payload/manifest uses it. If testing `build_payload` directly would require a full engine run, verify the payload field in the control integration run rather than constructing a brittle mock.
5. Add focused cooldown assertions that `-1` and `0` disable re-entry blocking and H088/H089/H090 resolve to 1/3/5 under the new key, with no legacy key left in resolved parameters.
6. Add a pure/deterministic stop-gap-record test: a known entry, stop, trigger close/time, and next-open fill must produce the hand-calculated fill-minus-stop and entry-to-fill values; changing the trigger bar's low must not change either result. Also assert a `15:00` trigger is marked overnight gap-exposed and remains latched until the fill callback.

Do not update expected values blindly if the native engine suppresses `09:00` because of the NYSE calendar. If that occurs, preserve `lumibot/` read-only, diagnose the harness scheduling boundary, and use the smallest strategy-owned setting that causes exact `09:00-15:00` inputs to be consumed. The result must still pass the `14:00`/`15:00` causality assertions; merely relabelling back to a half-hour is not an acceptable workaround.

Run targeted tests only:

```bash
.venv/bin/python -m pytest -c pytest-glitch.ini -q \
  tests/strategy_lab/test_native_experiments.py \
  tests/strategy_lab/test_native_alternatives.py \
  tests/strategy_lab/test_experiment_registry.py
git diff --check
```

### 3.4 Run the new-convention control and audit it

Files written: untracked runtime artifacts only under `reports/hts_hourly_alignment_2026-09-15/after/`.

Run exactly one job, without `--resume`:

```bash
.venv/bin/python scripts/run_hts_experiments.py \
  --ids HTS_CONTROL_1 \
  --windows six_year \
  --workers 1 \
  --out-dir reports/hts_hourly_alignment_2026-09-15/after \
  --suite-id hts-hourly-after-2026-09-15
.venv/bin/python scripts/evaluate_hts_experiments.py \
  --out-dir reports/hts_hourly_alignment_2026-09-15/after \
  --write --top 1
```

Require one accepted audit, `problems=[]`, the new implementation revision and convention, no negative cash beyond the existing tolerance, finite fills, and no unexpected terminal-state mismatch.

### 3.5 Compare economic behavior, not only hashes

Why: `stable_hash` will change when timestamps move by 30 minutes even if economic decisions, quantities, prices, fees, and equity are identical.

Comparison procedure:

1. Compare both `run_result.json` files for fills, journal-event count, warnings/problems, invariant minima, rejections, terminal positions, and every standard metric. Report absolute and relative deltas; do not round before comparing.
2. Compare filled rows in `run_trades.csv` after normalizing only the old run's `time` by minus 30 minutes. Require identical event order, symbol, side, type, status, price, filled quantity, fees, slippage, and identifiers for economic parity. If not identical, report the first differing row and its source-bar context.
3. Compare `run_stats.csv` after the same old-time normalization. Require identical cash, positions, portfolio value, financing fields, and returns if the change is label-only. If not identical, locate the first timestamp/value divergence.
4. Audit every emitted stop-gap event against the corresponding filled trade. Explicitly inspect prior-session `15:00` triggers, confirm their prices are next-session opens, and require the overnight-gap flag. Recompute fill-minus-stop and entry-to-fill independently; never compare a fill to the trigger bar's low as if that low were executable after close.
5. Populate the investigation's A/B table with the measured result. Say “economic parity after timestamp normalization” only if all non-time fields match. Otherwise state the exact impact and invalidate prior rankings pending rebaseline.

The before/after raw directories remain local and uncommitted. The durable evidence is the exact aggregate/delta table and first-difference statement in the investigation doc. Do not commit multi-megabyte trades/stats output.

### 3.6 Mark affected research and schedule the later rebaseline

Dropping the relabel invalidates every prior hourly/control artifact as current evidence, even if the one-control A/B happens to be economically identical. The affected set is `HTS_CONTROL_1` plus H001-H100:

- Descriptive results: 101 hourly configurations x 2 windows = 202 affected jobs.
- Walk-forward results: 101 hourly configurations x 18 fold windows = 1,818 affected jobs.
- Daily alternatives A01/A03/A04/A05/A06/A09 do not consume `_lumibot_hourly`, but the mixed walk-forward selection must be rebuilt against the new hourly results. Because the current implementation revision is suite-wide, the safest later run is a new full revisioned suite rather than mixing copied artifacts.
- Re-run selected cost stresses only after the new walk-forward evaluator chooses the new fold leaders.

Do not run those batches in this task or during RTH. In an explicitly scheduled off-hours follow-up, use a new output directory and suite ID, run with the conservative worker count, audit every run, rebuild the walk-forward JSON/HTML, rerun cost stress for the newly selected track, and update `docs/HTS_NATIVE_RESULTS.md`. Never overwrite the September 14 artifacts; they are the historical old-convention baseline.

## Step 4 - Final verification and second commit

Expected tracked files for the second commit:

- `strategy_lab/native_experiments.py`
- `strategy_lab/hts_variants.py`
- `scripts/run_hts_experiments.py`
- `tests/strategy_lab/test_native_experiments.py`
- `tests/strategy_lab/test_experiment_registry.py`
- `docs/investigations/2026-09-15_HOURLY_CONVENTION.md`
- `docs/HTS_100_VARIATIONS_RESEARCH_PLAN.md`
- `docs/HTS_V1_PAPER_PARITY.md`
- `docs/HTS_NATIVE_RESULTS.md`
- `docs/BACKTESTING_ARCHITECTURE.md`
- `docs/HTS_VARIATIONS_CATALOG.md`

If implementation proves that no runner-manifest edit is needed, omit `scripts/run_hts_experiments.py`; do not touch it gratuitously. Do not stage the `.Codex` plan, raw A/B artifacts, pre-existing scratch, caches, logs, live files, or `lumibot/`.

Run:

```bash
.venv/bin/python -m pytest -c pytest-glitch.ini -q \
  tests/strategy_lab/test_native_experiments.py \
  tests/strategy_lab/test_native_alternatives.py \
  tests/strategy_lab/test_experiment_registry.py
git diff --check
git status --short --branch
```

Then stage the exact verified allowlist with `git add -- <each-file>`, followed by:

```bash
git diff --cached --check
git diff --cached --name-only
git diff --cached --stat
git commit -m "Align HTS native bars to the clock-hour convention"
git rev-parse HEAD
git show --stat --oneline HEAD
```

Do not push. Final handoff must report both commit SHAs, targeted-test results, old/new control metrics and deltas, whether normalized trade/equity parity held, remaining untracked artifacts, and the off-hours rebaseline requirement.

## Risks and stop conditions

- **Global result invalidation:** all old hourly results use the relabel convention. Preserve them, label them historical, and do not merge them into a new revision.
- **NYSE-clock interaction:** whole-hour `09:00` data may interact with the native NYSE session clock. Prove actual event consumption; do not infer it from the helper's index alone.
- **Lookahead at the close:** the `15:00` source bar is unavailable until `16:00`; because no same-day executable clock-hour bar remains, its queued order belongs at the next session open.
- **Stop reporting gap:** the existing artifact schema does not contain a complete stop-gap event series. Item 3 closes that observability gap; do not commit the “implemented” doc status unless the event-level arithmetic and next-open provenance pass.
- **Cooldown migration:** renaming the key changes control/HTS fingerprints. Regenerate the catalog, assert the old key is absent, and preserve H088/H089/H090's 1/3/5-session behavior exactly.
- **Session contamination:** clock-hour parity does not prove strict RTH data. A10 remains blocked and regular-session strategies still require explicit `09:30-16:00` filtering or genuine minute aggregation.
- **Shared dirty tree:** if cached/staged diffs contain files outside the allowlist, unstage only the files Terra staged and coordinate; never reset or clean another agent's work.
- **Live fleet safety:** any command that would touch launchd, live modules, broker credentials, or trader processes is out of scope and must not run.
- **Unexpected A/B divergence:** record the first event-level difference and its metric impact. Do not commit a misleading “alignment” result if causality or invariants fail; fix only the strategy-owned harness or stop with a precise blocker.

## TL;DR

Make two local commits and no push: first the completed walk-forward sources plus six compact evidence files, then the documented/tested clock-hour mapping. Prove the second commit with one old and one new six-year control run, compare economics after removing the expected 30-minute label shift, preserve all old suites as historical, and defer the full 202/1,818-job rebaseline to an off-hours follow-up.

## AMENDMENT — Sol review (CHANGES REQUIRED), must be folded in before/ during implementation

These are MANDATORY corrections to the plan above. Terra must implement them; they are not optional.

### A1 (BLOCKER, replaces any contrary wording) — Canonical 09:00-15:00 frame must be shared by strategy AND broker feeds
The plan's Item 3 currently filters only the LumiBot `Data` feed (`native_experiments.py` ~line 338) and preserves the unfiltered strategy feature frame. That is WRONG: `prepare_inputs` builds `inputs.hourly` from every raw hourly row (~line 295), and `_completed_row()` reads that unfiltered frame (~line 481). The hourly archive contains 04:00-19:00 ET rows, so at next-session 09:00 `_completed_row()` can select that morning's 08:00 premarket bar instead of the prior 15:00 bar, and ATR sees extended-hours observations.
Fix: build ONE canonical exact-minute 09:00-15:00 frame per symbol and use it BOTH for `_hourly_features()`/strategy feature math AND the LumiBot `Data` copy. Test with a prior-day postmarket row and a next-day premarket row present in the source.

### A2 (BLOCKER) — Prove lifecycle causality, not just helper math
Helper tests on `_completed_row()`/`_execution_price()` with ideal whole-hour timestamps do NOT prove runtime causality. Runtime truncates the engine timestamp to integer hour (`native_experiments.py` ~478, ~831) and `_update_risk()` can submit an order without checking `_execution_price()` exists (~647).
Fix: add a deterministic native-lifecycle fixture/trace recording full engine time, completed source-bar time, submission time, fill time, and source fill bar; assert (a) a 14:00-close action is not actionable earlier than 15:00 open, (b) a 15:00-close action is actionable only at next session's 09:00 open. Do not rely on helper-level unit tests alone.

### A3 (BLOCKER) — Cooldown expiry off-by-one
Current rule stores `session_index + N + 1` on a fill (~879) while selection compares the previous completed session against it (~543); so N=1 blocks two complete subsequent sessions, not one.
Fix: with the current previous-session lookup, expiry boundary should be `i + N`. Test each selection day for -1, 0, 1, 3, 5.

### A4 (MAJOR) — Key migration must include Family-10 declaration
The plan's migration list omits `FAMILY_10_UNIVERSE` which declares the legacy `stop_cooldown_sessions` key (`hts_variants.py` ~277); renaming H099's override without updating that declaration makes `resolve_parameters()` reject it (`experiment_config.py` ~289).
Fix: include the Family-10 declaration in the rename; require ZERO active `stop_cooldown_sessions` references across `strategy_lab/`, `scripts/`, `tests/`, and active/generated docs.

### A5 (BLOCKER) — Fixed-pool slot gating: don't claim enforcement that doesn't exist
Sells are submitted first, but buys are gated only by cash and same-symbol occupancy — there is NO active+pending slot limit (`_rebalance` ~769). Residual cash can open a partial replacement while the outgoing position still occupies its slot.
Fix: (a) add explicit unresolved-sale slot gating and a test with positive residual cash + a pending/failed sale; OR (b) if out of scope for this harness change, label slot enforcement as an UNENFORCED qualification gap in the contract and do NOT claim it verified. Do not overstate.

### A6 (MAJOR) — Stop-gap semantics scope + joinable schema
Plan's stop language reads universal, but resting-stop modes submit actual stop orders (~621) that bypass `_submit_sell()` via the protective fallback (~871).
Fix: scope completed-close-trigger semantics explicitly to VIRTUAL stops; document resting-stop semantics separately or keep those candidates blocked. Record per event: symbol, mode/reason, order ID, entry price, stop level, trigger timestamp/close, fill time/price/quantity, trigger/fill sessions, overnight flag. Pin formulas: dollars/share = fill - stop; percentage gap = (fill - stop)/stop; realized return = fill/entry - 1.

### A7 (MINOR) — Secret hygiene should be content scan
Scan the CONTENTS of the exact 11 staged files (not just filenames) for personal paths/credentials; manually review manifest path/URL fields. Keep exact-path staging and the no-.gitignore boundary.

### Scope note after A1-A7
These corrections tighten the Item-3 harness change (shared canonical frame, lifecycle-causality proof, cooldown algebra, full key migration, honest slot-gating label, virtual-stop scoping + joinable stop events). They are still all inside `strategy_lab/`, `scripts/`, `docs/`, `tests/`, allowed `reports/`; no `lumibot/`, no live, no push, no full-suite rebaseline during this task. If A5(b) is chosen, the contract must state the slot limit is a documented limitation, not an enforced control.
