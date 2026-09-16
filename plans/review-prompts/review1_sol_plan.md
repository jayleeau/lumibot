CHANGES REQUIRED

The requested review file could not be created because the workspace is enforced read-only. No files were modified. Review content follows.

1. **BLOCKER — Extended-hours data breaks the claimed next-open causality.**
   The plan changes only `_lumibot_hourly()` and explicitly preserves the strategy feature frame ([plan:229](</Users/openclaw/.hermes/profiles/free_money_glitch/home/lumibot/plans/hts_hourly_convention.md:229>)). But `inputs.hourly` is built from every raw hourly row ([native_experiments.py:295](</Users/openclaw/.hermes/profiles/free_money_glitch/home/lumibot/strategy_lab/native_experiments.py:295>)), while filtering applies only to the separate LumiBot `Data` feed ([native_experiments.py:338](</Users/openclaw/.hermes/profiles/free_money_glitch/home/lumibot/strategy_lab/native_experiments.py:338>)). `_completed_row()` reads the unfiltered frame ([native_experiments.py:481](</Users/openclaw/.hermes/profiles/free_money_glitch/home/lumibot/strategy_lab/native_experiments.py:481>)).

   A read-only query confirmed the hourly archive contains 04:00–19:00 ET rows. At next-session 09:00, `_completed_row()` can therefore select that morning’s 08:00 premarket bar—not the prior 15:00 bar. ATR also includes extended-hours observations.

   Minimal fix: construct one canonical, exact-minute `09:00`–`15:00` frame per symbol and use it both for `_hourly_features()` and the LumiBot `Data` copy. Test with prior-day postmarket and next-day premarket rows present.

2. **BLOCKER — Helper tests do not prove scheduler or broker causality.**
   The proposed tests invoke `_completed_row()` and `_execution_price()` with ideal whole-hour timestamps ([plan:253](</Users/openclaw/.hermes/profiles/free_money_glitch/home/lumibot/plans/hts_hourly_convention.md:253>)). Runtime instead truncates the engine timestamp to an integer hour ([native_experiments.py:478](</Users/openclaw/.hermes/profiles/free_money_glitch/home/lumibot/strategy_lab/native_experiments.py:478>), [native_experiments.py:831](</Users/openclaw/.hermes/profiles/free_money_glitch/home/lumibot/strategy_lab/native_experiments.py:831>)). `_update_risk()` can submit an order without checking that `_execution_price()` exists ([native_experiments.py:647](</Users/openclaw/.hermes/profiles/free_money_glitch/home/lumibot/strategy_lab/native_experiments.py:647>)).

   A helper test cannot exclude half-hour scheduler ticks, retroactive source-open fills, or stale/same-day fills around 16:00.

   Minimal fix: add a deterministic native-lifecycle fixture or trace recording full engine time, completed source-bar time, submission time, fill time, and source fill bar. Assert:

   - 14:00 close → no earlier than 15:00 open.
   - 15:00 close → next session’s 09:00 open only.

3. **BLOCKER — Existing cooldown arithmetic is off by one.**
   The plan preserves the current expiry rule ([plan:233](</Users/openclaw/.hermes/profiles/free_money_glitch/home/lumibot/plans/hts_hourly_convention.md:233>)). A fill in session `i` currently stores `i + N + 1` ([native_experiments.py:879](</Users/openclaw/.hermes/profiles/free_money_glitch/home/lumibot/strategy_lab/native_experiments.py:879>)), while selection compares the previous completed session against that value ([native_experiments.py:543](</Users/openclaw/.hermes/profiles/free_money_glitch/home/lumibot/strategy_lab/native_experiments.py:543>)). Thus `N=1` blocks two complete subsequent sessions.

   Minimal fix: define the expiry algebra explicitly—given the current previous-session lookup, the boundary should be `i + N`—and test each selection day for `-1`, `0`, `1`, `3`, and `5`.

4. **MAJOR — The rename omits H099’s Family-10 declaration.**
   The migration list ([plan:235](</Users/openclaw/.hermes/profiles/free_money_glitch/home/lumibot/plans/hts_hourly_convention.md:235>)) omits `FAMILY_10_UNIVERSE`, which declares the legacy key at [hts_variants.py:277](</Users/openclaw/.hermes/profiles/free_money_glitch/home/lumibot/strategy_lab/hts_variants.py:277>). Renaming H099’s override without this declaration makes `resolve_parameters()` reject it ([experiment_config.py:289](</Users/openclaw/.hermes/profiles/free_money_glitch/home/lumibot/strategy_lab/experiment_config.py:289>)).

   Minimal fix: include the Family-10 declaration and require zero active `stop_cooldown_sessions` references across `strategy_lab/`, `scripts/`, `tests/`, and active/generated docs.

5. **BLOCKER — Fixed-pool slot enforcement is overstated.**
   The plan says cash and a slot must both be freed before replacement sizing ([plan:151](</Users/openclaw/.hermes/profiles/free_money_glitch/home/lumibot/plans/hts_hourly_convention.md:151>)), then describes the existing code as enforcing this ([plan:166](</Users/openclaw/.hermes/profiles/free_money_glitch/home/lumibot/plans/hts_hourly_convention.md:166>)). In `_rebalance()`, sells are submitted first, but buys are gated only by cash and same-symbol occupancy; no active-plus-pending slot limit exists ([native_experiments.py:769](</Users/openclaw/.hermes/profiles/free_money_glitch/home/lumibot/strategy_lab/native_experiments.py:769>)).

   Residual cash can open a partial replacement while the outgoing position still occupies its slot.

   Minimal fix: add explicit unresolved-sale slot gating and a test with positive residual cash and a pending/failed sale. Otherwise label this an unenforced qualification gap and do not mark the contract verified.

6. **MAJOR — Stop-gap scope and joinable evidence are underspecified.**
   The normative stop language appears universal ([plan:148](</Users/openclaw/.hermes/profiles/free_money_glitch/home/lumibot/plans/hts_hourly_convention.md:148>)), but registered resting-stop modes submit actual stop orders ([native_experiments.py:621](</Users/openclaw/.hermes/profiles/free_money_glitch/home/lumibot/strategy_lab/native_experiments.py:621>)). Those fills bypass `_submit_sell()` and use the protective fallback path ([native_experiments.py:871](</Users/openclaw/.hermes/profiles/free_money_glitch/home/lumibot/strategy_lab/native_experiments.py:871>)).

   The proposed event schema also lacks explicit order ID and fill timestamp, preventing robust joins and independent next-open verification.

   Minimal fix:

   - Scope completed-close trigger semantics explicitly to virtual stops.
   - Document resting-stop semantics separately or keep those candidates blocked.
   - Record symbol, mode/reason, order ID, entry, stop, trigger time/close, fill time/price/quantity, trigger/fill sessions, and overnight flag.
   - Pin formulas:
     - dollars/share: `fill - stop`
     - percentage gap: `(fill - stop) / stop`
     - realized return: `fill / entry - 1`

7. **MINOR — Filename-only secret hygiene is ineffective.**
   [Plan line 74](</Users/openclaw/.hermes/profiles/free_money_glitch/home/lumibot/plans/hts_hourly_convention.md:74>) requests a filename-only scan. The exact 11-file staging list is complete and safe, and a current content scan found no obvious personal path or credential marker, but filenames cannot detect content drift.

   Minimal fix: scan the contents of exactly those 11 staged files and manually review manifest path/URL fields. Keep exact-path staging and the no-`.gitignore` boundary.

Verified as sound:

- The 11-path Item-1 allowlist contains exactly five sources and six compact reports; it cannot recursively include candidate directories or unrelated scratch.
- The revision bump is sufficient for `--resume`, which requires both revision and fingerprint equality ([run_hts_experiments.py:195](</Users/openclaw/.hermes/profiles/free_money_glitch/home/lumibot/scripts/run_hts_experiments.py:195>)).
- A timezone-aware minus-30-minute normalization is DST/date-safe for 09:30–15:30 ET rows, but only if the new run actually emits timestamps exactly 30 minutes earlier.
- The plan honestly retains the 09:00-bar straddle caveat, keeps A10 blocked, avoids live/LumiBot changes, defers the large rebaseline, and forbids pushing.

Assumptions Terra must prove empirically:

- Whether the NYSE clock consumes whole-hour data or continues on half-hour ticks.
- The actual broker fill bar for orders submitted at 15:00 and 16:00 boundaries.
- Whether any 16:00 strategy iteration occurs.
- Whether the control contains enough 15:00 virtual-stop events; otherwise the lifecycle fixture must supply proof.
- That the index and shared tree remain uncontaminated immediately before each commit.

TL;DR: The revision, staging, A/B safeguards, and session caveat are sound, but the plan cannot be approved until the unfiltered strategy frame, runtime timing proof, cooldown off-by-one, missing Family-10 rename, fixed-slot gap, and stop-event schema are corrected.
