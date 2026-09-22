# TERRA — align run_full_lumibot_backtests.py to the frozen clock-hour convention

Repo: `/Users/openclaw/.hermes/profiles/free_money_glitch/home/lumibot` (branch `version/4.5.92/clean-base`).

## Why
The frozen HTS contract (docs/investigations/2026-09-15_HOURLY_CONVENTION.md, committed) is **clock-hour 09:00–15:00 ET bars**, where a bar labelled T contains `[T,T+1h)` and is only known at T+1h. `strategy_lab/native_experiments.py` already implements this via `_canonical_hourly_frame` (exact whole-hour 09–15 rows feeding both features and LumiBot `Data`).

But `scripts/run_full_lumibot_backtests.py` — the "full lumibot backtests" runner — is INCONSISTENT:
- Its own `_hts_lumibot_data(frame)` (currently ~line 138) still does the OLD relabel: `source.index = normalize + (hour-9)h + 09:30`, i.e. archive 09:00→09:30 … 15:00→15:30. That is the RETIRED convention and makes this runner's HTS leg diverge from the committed harness.
- Its `NativeHtsStrategy._update_trailing_stops` currently uses `completed_hours = [14, 15]` and a comment about the NYSE half-hour clock, plus `signal_hour`/`execution_hour`. Under the new whole-hour labels these need to be consistent with the clock-hour convention.

## Task
Bring `scripts/run_full_lumibot_backtests.py` in line with the frozen convention WITHOUT changing its economics beyond the intended clock correction. Use the same canonical mapping as `strategy_lab/native_experiments.py` (`_canonical_hourly_frame`): keep only exact whole-clock-hour rows 09:00–15:00 ET, preserve OHLCV, no `+09:30` offset, no sub-hour rows, and treat a bar labelled T as known at T+1h.

Concretely:
1. Replace `_hts_lumibot_data` so it retains only exact whole-hour 09:00–15:00 ET rows with unchanged values (mirror `_canonical_hourly_frame`). Update its docstring. Do NOT make it an RTH 09:30–16:00 cleaner — the first clock-hour bar 09:00–10:00 may contain premarket minutes; that limitation is documented and accepted.
2. Review `NativeHtsStrategy` (<`_update_trailing_stops`, `_hour_values`, signal/execution hour logic) and adjust only what is needed so completed-bar selection is causal under the whole-hour labels: a decision at hour T uses rows with index < T; there is no same-day executable row at 16:00 (15:00 close is known at 16:00, action at next session open). Keep signal/entry semantics as close to the existing strategy's intent as possible; do not rewrite the strategy's economic rules.
3. Add/update a deterministic unit test asserting the new `_hts_lumibot_data` output labels are exactly 09:00–15:00 whole hours with unchanged OHLCV and no sub-hour rows, and that pre/post (e.g. 08:00, 09:30, 16:00, 17:00) rows are absent. Mirror the existing strategy_lab clock-hour tests. Add the runner module to the targeted test file or a sibling test.
4. Run: `.venv/bin/python -m pytest tests/strategy_lab/test_native_experiments.py tests/strategy_lab/test_native_alternatives.py tests/strategy_lab/test_experiment_registry.py -q`; then run the runner's own existing/related tests if any. `git diff --check`.
5. DO NOT commit. Report what you changed (file:line), the test output, and anything you could not verify.

## Hard guards
- Never touch `lumibot/`, `live/common.py`, `live/strategies.py`, `*_trader*.py`, `ai.glitch.live.*`. No push. No full backtest runs — just the targeted tests.
- Keep changes minimal and strategy-harness-only. This runner already exists; align it, don't rewrite its strategy logic.
- If the NYSE clock consumes whole-hour data differently (e.g. suppresses 09:00), diagnose and use the smallest strategy-owned setting that makes exact 09:00–15:00 inputs consumed; do not fall back to the 09:30 relabel.
- Report honestly any test that fails because of pre-existing code vs your change.

Write your result to `plans/build-prompts/TERRA_RUNNER_ALIGN.md`.