APPROVE

No BLOCKER, MAJOR, or MINOR implementation defects found. The workspace is read-only, so this review is inline rather than written to `plans/review-prompts/review2_sol_impl.md`.

1. A1 wiring is correct. [`_canonical_hourly_frame()`](/Users/openclaw/.hermes/profiles/free_money_glitch/home/lumibot/strategy_lab/native_experiments.py:248) retains only exact 09:00–15:00 rows. [`prepare_inputs()`](/Users/openclaw/.hermes/profiles/free_money_glitch/home/lumibot/strategy_lab/native_experiments.py:316) builds ATR/features from that frame and supplies copies of the same canonical rows to LumiBot `Data` at line 355. `hourly_raw` has no remaining route to `_completed_row()` or ATR.

2. Causality is correct. [`_completed_row()`](/Users/openclaw/.hermes/profiles/free_money_glitch/home/lumibot/strategy_lab/native_experiments.py:527) uses strict-before-current indexing; [`_execution_price()`](/Users/openclaw/.hermes/profiles/free_money_glitch/home/lumibot/strategy_lab/native_experiments.py:542) requires the current timestamp and returns its open. The lifecycle test at [`test_native_experiments.py:239`](/Users/openclaw/.hermes/profiles/free_money_glitch/home/lumibot/tests/strategy_lab/test_native_experiments.py:239) asserts 14:00 close → 15:00 execution and 15:00 close → next-session 09:00 execution, including 17:00 and 08:00 contaminant rows.

3. The economic divergence is genuine. Independent recomputation reproduced Sharpe `0.6573189016895791 → 0.748575044644078`, fills `3376 → 2588`, and every other documented metric exactly. The first five normalized fills match; row six diverges exactly as documented. For IEF, the old path sees the 2020-09-09 17:00 post-market close `121.30`, which breaches the active `121.5114` stop; the corrected 09:00 decision sees the 15:00 close `121.68`, which does not. The later 10:00 exit is the scheduled selection-change exit at the `121.56` open. All 3,376 before and 2,588 after fills match archive opens.

4. Documentation is honest. [`HTS_NATIVE_RESULTS.md:16`](/Users/openclaw/.hermes/profiles/free_money_glitch/home/lumibot/docs/HTS_NATIVE_RESULTS.md:16) marks every old hourly/control number historical. [`2026-09-15_HOURLY_CONVENTION.md:99`](/Users/openclaw/.hermes/profiles/free_money_glitch/home/lumibot/docs/investigations/2026-09-15_HOURLY_CONVENTION.md:99) records the full A/B divergence, explicitly says economic parity failed, and requires the off-hours rebaseline at line 123.

5. Cooldown is correct. Expiry is `i + N` at [`native_experiments.py:1034`](/Users/openclaw/.hermes/profiles/free_money_glitch/home/lumibot/strategy_lab/native_experiments.py:1034), paired with previous-session selection at line 589. `-1` and `0` disable it; H088/H089/H090 are 1/3/5 at [`hts_variants.py:556`](/Users/openclaw/.hermes/profiles/free_money_glitch/home/lumibot/strategy_lab/hts_variants.py:556). No active legacy-key occurrence exists.

6. Revision and provenance pass. The required constant and revision are at [`native_experiments.py:102`](/Users/openclaw/.hermes/profiles/free_money_glitch/home/lumibot/strategy_lab/native_experiments.py:102); the convention enters the feature hash at line 367. [`_resumable()`](/Users/openclaw/.hermes/profiles/free_money_glitch/home/lumibot/scripts/run_hts_experiments.py:200) checks revision and fingerprint; the preserved old control evaluates as non-resumable.

7. Stop-gap evidence passes. The schema and formulas are at [`native_experiments.py:451`](/Users/openclaw/.hermes/profiles/free_money_glitch/home/lumibot/strategy_lab/native_experiments.py:451). All 883 events had every required field, unique order IDs, exact trade joins, zero arithmetic discrepancies, and source-open fill parity. All 143 cross-session events were marked overnight; all had 15:00 triggers and next-session 09:00 fills. Virtual/resting-stop scope is separated at [`2026-09-15_HOURLY_CONVENTION.md:35`](/Users/openclaw/.hermes/profiles/free_money_glitch/home/lumibot/docs/investigations/2026-09-15_HOURLY_CONVENTION.md:35).

8. Commit hygiene passes. Both commits contain exactly their planned 11 files, with no framework, live, trader, `short/`, log, plan, or scratch paths. Both diffs pass `git diff --check`; an independent content scan found no personal paths or credential signatures, and manifest paths are repository-relative. Local refs show the branch ahead of its upstream and no remote-tracking ref containing these commits. The slot-cap limitation is explicitly disclosed at [`2026-09-15_HOURLY_CONVENTION.md:60`](/Users/openclaw/.hermes/profiles/free_money_glitch/home/lumibot/docs/investigations/2026-09-15_HOURLY_CONVENTION.md:60).

9. Tests are substantive. Exactly 48 tests collect. In this read-only review, 47 non-writing tests passed; the one `tmp_path` metric test was not executable without writable temporary storage, but its metric arithmetic was independently recomputed exactly from both A/B artifacts. The critical causality and cooldown tests passed directly.

Not verified read-only:

- Live Alpaca parity, broker rejection/retry or delayed-market-fill behavior.
- Early-close/DST behavior and strict regular-session cleanliness.
- Resting-stop fill fidelity for H039/H040/H100.
- The acknowledged active-plus-pending slot cap.
- Current remote-server push state; only local Git references were available.
- Byte-for-byte provenance of the after artifact to `20af68d6`: the plan ran it before the second commit, so its manifest records HEAD `a581f03d`, although its revision, hashes, behavior, and committed implementation are mutually consistent.

The full HTS_CONTROL_1 plus H001–H100 rebaseline is correctly deferred to an explicitly scheduled off-hours run. Its absence is not an implementation failure.

TL;DR: APPROVE. A1–A7 are implemented as required; the causal divergence, metrics, fills, stop-event arithmetic, cooldown behavior, tests, documentation, and commit boundaries check out.


tokens used
214,142
APPROVE

No BLOCKER, MAJOR, or MINOR implementation defects found. The workspace is read-only, so this review is inline rather than written to `plans/review-prompts/review2_sol_impl.md`.

1. A1 wiring is correct. [`_canonical_hourly_frame()`](/Users/openclaw/.hermes/profiles/free_money_glitch/home/lumibot/strategy_lab/native_experiments.py:248) retains only exact 09:00–15:00 rows. [`prepare_inputs()`](/Users/openclaw/.hermes/profiles/free_money_glitch/home/lumibot/strategy_lab/native_experiments.py:316) builds ATR/features from that frame and supplies copies of the same canonical rows to LumiBot `Data` at line 355. `hourly_raw` has no remaining route to `_completed_row()` or ATR.

2. Causality is correct. [`_completed_row()`](/Users/openclaw/.hermes/profiles/free_money_glitch/home/lumibot/strategy_lab/native_experiments.py:527) uses strict-before-current indexing; [`_execution_price()`](/Users/openclaw/.hermes/profiles/free_money_glitch/home/lumibot/strategy_lab/native_experiments.py:542) requires the current timestamp and returns its open. The lifecycle test at [`test_native_experiments.py:239`](/Users/openclaw/.hermes/profiles/free_money_glitch/home/lumibot/tests/strategy_lab/test_native_experiments.py:239) asserts 14:00 close → 15:00 execution and 15:00 close → next-session 09:00 execution, including 17:00 and 08:00 contaminant rows.

3. The economic divergence is genuine. Independent recomputation reproduced Sharpe `0.6573189016895791 → 0.748575044644078`, fills `3376 → 2588`, and every other documented metric exactly. The first five normalized fills match; row six diverges exactly as documented. For IEF, the old path sees the 2020-09-09 17:00 post-market close `121.30`, which breaches the active `121.5114` stop; the corrected 09:00 decision sees the 15:00 close `121.68`, which does not. The later 10:00 exit is the scheduled selection-change exit at the `121.56` open. All 3,376 before and 2,588 after fills match archive opens.

4. Documentation is honest. [`HTS_NATIVE_RESULTS.md:16`](/Users/openclaw/.hermes/profiles/free_money_glitch/home/lumibot/docs/HTS_NATIVE_RESULTS.md:16) marks every old hourly/control number historical. [`2026-09-15_HOURLY_CONVENTION.md:99`](/Users/openclaw/.hermes/profiles/free_money_glitch/home/lumibot/docs/investigations/2026-09-15_HOURLY_CONVENTION.md:99) records the full A/B divergence, explicitly says economic parity failed, and requires the off-hours rebaseline at line 123.

5. Cooldown is correct. Expiry is `i + N` at [`native_experiments.py:1034`](/Users/openclaw/.hermes/profiles/free_money_glitch/home/lumibot/strategy_lab/native_experiments.py:1034), paired with previous-session selection at line 589. `-1` and `0` disable it; H088/H089/H090 are 1/3/5 at [`hts_variants.py:556`](/Users/openclaw/.hermes/profiles/free_money_glitch/home/lumibot/strategy_lab/hts_variants.py:556). No active legacy-key occurrence exists.

6. Revision and provenance pass. The required constant and revision are at [`native_experiments.py:102`](/Users/openclaw/.hermes/profiles/free_money_glitch/home/lumibot/strategy_lab/native_experiments.py:102); the convention enters the feature hash at line 367. [`_resumable()`](/Users/openclaw/.hermes/profiles/free_money_glitch/home/lumibot/scripts/run_hts_experiments.py:200) checks revision and fingerprint; the preserved old control evaluates as non-resumable.

7. Stop-gap evidence passes. The schema and formulas are at [`native_experiments.py:451`](/Users/openclaw/.hermes/profiles/free_money_glitch/home/lumibot/strategy_lab/native_experiments.py:451). All 883 events had every required field, unique order IDs, exact trade joins, zero arithmetic discrepancies, and source-open fill parity. All 143 cross-session events were marked overnight; all had 15:00 triggers and next-session 09:00 fills. Virtual/resting-stop scope is separated at [`2026-09-15_HOURLY_CONVENTION.md:35`](/Users/openclaw/.hermes/profiles/free_money_glitch/home/lumibot/docs/investigations/2026-09-15_HOURLY_CONVENTION.md:35).

8. Commit hygiene passes. Both commits contain exactly their planned 11 files, with no framework, live, trader, `short/`, log, plan, or scratch paths. Both diffs pass `git diff --check`; an independent content scan found no personal paths or credential signatures, and manifest paths are repository-relative. Local refs show the branch ahead of its upstream and no remote-tracking ref containing these commits. The slot-cap limitation is explicitly disclosed at [`2026-09-15_HOURLY_CONVENTION.md:60`](/Users/openclaw/.hermes/profiles/free_money_glitch/home/lumibot/docs/investigations/2026-09-15_HOURLY_CONVENTION.md:60).

9. Tests are substantive. Exactly 48 tests collect. In this read-only review, 47 non-writing tests passed; the one `tmp_path` metric test was not executable without writable temporary storage, but its metric arithmetic was independently recomputed exactly from both A/B artifacts. The critical causality and cooldown tests passed directly.

Not verified read-only:

- Live Alpaca parity, broker rejection/retry or delayed-market-fill behavior.
- Early-close/DST behavior and strict regular-session cleanliness.
- Resting-stop fill fidelity for H039/H040/H100.
- The acknowledged active-plus-pending slot cap.
- Current remote-server push state; only local Git references were available.
- Byte-for-byte provenance of the after artifact to `20af68d6`: the plan ran it before the second commit, so its manifest records HEAD `a581f03d`, although its revision, hashes, behavior, and committed implementation are mutually consistent.

The full HTS_CONTROL_1 plus H001–H100 rebaseline is correctly deferred to an explicitly scheduled off-hours run. Its absence is not an implementation failure.

TL;DR: APPROVE. A1–A7 are implemented as required; the causal divergence, metrics, fills, stop-event arithmetic, cooldown behavior, tests, documentation, and commit boundaries check out.
