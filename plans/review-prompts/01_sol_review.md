# SOL PLAN-REVIEW TASK — adversarial review of the HTS hourly-convention plan

You are Sol, the architect, in REVIEW mode. Read the plan at `plans/hts_hourly_convention.md` and the repository facts it relies on, then give a verdict: **APPROVE** or **CHANGES REQUIRED** with concrete file:line findings.

DO NOT modify any code or files — this is a read-only review. Do not touch `ai.glitch.live.*`, `live/common.py`, `live/strategies.py`, any `*_trader*.py`, or `lumibot/`. The market may be open — do not run any backtest.

## Context you should trust (verified)
- Branch: `version/4.5.92/clean-base`. Python: `.venv/bin/python`. Tests: `.venv/bin/python -m pytest -c pytest-glitch.ini`.
- The plan's Item 1 stages 11 files for commit: 5 sources (`strategy_lab/native_experiments.py` walk-forward windows change + 4 new scripts) and 6 compact evidence reports. It must NOT stage the ~893MB per-candidate dirs, `short/`, logs, `live/`, `reports/full_lumibot_*`, or the pre-existing scratch.
- The plan's Item 2 writes `docs/investigations/2026-09-15_HOURLY_CONVENTION.md` and cross-links 4+ docs, pinning: clock-hour 09:00-15:00 ET bars, 14:00 = last same-day-actionable, 15:00 close = next-open gap-exposed, stop-as-trigger (never fill at stop / never use bar low), latched exits, `reentry_cooldown_bars` config (-1/0 disable, N>0 = N sessions), fixed-pool rotation, session-cleanliness caveat.
- The plan's Item 3 changes `strategy_lab/native_experiments.py::_lumibot_hourly` to keep whole-hour 09:00-15:00 ET bars (drop the 09:00→09:30 relabel), bumps `IMPLEMENTATION_REVISION` to `hts-native-2026-09-15-clock-hour-1`, migrates the cooldown key `stop_cooldown_sessions → reentry_cooldown_bars` in `hts_variants.py`, adds stop-gap reporting, adds deterministic tests, and requires one old + one new six-year control run for economic A/B.
- Known current facts to verify against the plan's claims: (a) `_lumibot_hourly` currently maps archive hour 9..15 to `(hour-9)h + 09:30`; (b) the harness already has `_cooldowns`, `stop_cooldown_sessions`, exit-latch `_pending_sells`/`_pending_sell_reason`, and sizing that processes sells before buys; (c) `IMPLEMENTATION_REVISION` current value is `hts-native-2026-09-14-2`.

## Review focus (find real defects, not style)
1. **Causality / look-ahead:** does the plan's clock-hour change + `_completed_row`/`_execution_price` design truly make the 14:00 close actionable at 15:00 and the 15:00 close actionable only at next-open? Any leak of the 15:00 bar into a same-day executable before 16:00?
2. **The 09:00 bar straddle:** the first clock-hour bar 09:00-10:00 contains ~30min of premarket. Does the plan honestly treat this (no pretending clock-parity = RTH-clean) and keep A10 blocked rather than proxying it?
3. **Cooldown migration integrity:** renaming the key changes fingerprints; does it preserve H088/H089/H090 1/3/5-session behavior exactly and regenerate the catalog? Any residual `stop_cooldown_sessions` reference that would leave two knobs?
4. **Commit hygiene:** is the 11-file Item-1 allowlist complete but not over-broad? Could the exact-path staging pick up anything it shouldn't? Is the "no opportunistic .gitignore change" boundary sound?
5. **Revision bump necessity:** is bumping `IMPLEMENTATION_REVISION` for the mapping change correct and sufficient to stop `--resume` from reusing old artifacts?
6. **Stop-gap reporting:** will the design produce the trigger-level and fill-level data needed to compute fill-minus-stop and entry-to-fill without reading the trigger bar's low? Is the overnight-gap flag pinned for 15:00 triggers?
7. **A/B comparability:** is normalizing the old run's `time` by minus-30-min the right comparison method, given DST/date boundaries? Is "economic parity after timestamp normalization" claimed only when truly justified?
8. **Scope creep:** does the plan stay inside `strategy_lab/`, `scripts/`, `docs/`, `tests/`, allowed `reports/`? No `lumibot/`, no live, no push, no full-suite rebaseline during this task?

## Output (write to plans/review-prompts/review1_sol_plan.md)
- Verdict line: `APPROVE` or `CHANGES REQUIRED`.
- Numbered findings: each with severity (BLOCKER / MAJOR / MINOR), file:line where relevant, and a concrete, minimal fix.
- Honest list of assumptions you could not verify from a read-only look (e.g. exact runtime behavior of the NYSE clock with whole-hour 09:00 bars) that Terra must prove empirically.