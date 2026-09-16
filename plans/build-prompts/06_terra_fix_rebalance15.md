# TERRA — fix rebalance_hour=15 dead-trading bug in v2 HTS

Repo: `/Users/openclaw/.hermes/profiles/free_money_glitch/home/lumibot` (branch `version/4.5.92/clean-base`).

## The bug (verified empirically — do not re-derive)
In the native walkforward/descriptive runs, **every v2 candidate with `rebalance_hour: 15` produces 0 trades and flat $100k equity** (15 candidates: V002, V019, V020, V022, V039, V040, V042, V059, V060, V062, V079, V080, V082, V099, V100 — all P02/P19/P20 recipe variants).

Instrumentation on window b01 proved:
- V001 (`rebalance_hour=10`): `on_trading_iteration` sees hours {9..14}; `_rebalance` called 125x.
- V002 (`rebalance_hour=15`): sees hours {9..14} — **hour 15 never appears**; `_rebalance` called **0 times**.

Root cause: the frozen clock-hour convention says "15:00 order uses the completed 14:00 bar and fills at the 15:00 open". But LumiBot's backtesting iteration for this data does NOT present an hour-15 tick, so the `if hour == int(rebalance_hour)` gate at
`strategy_lab/native_experiments.py` `on_trading_iteration` (approx line 1238-1248) never fires for 15.

## What to fix
Re-map the rebalance-hour semantics so `rebalance_hour=15` is executable on the native engine while
**strictly preserving causality**:

1. Decide the correct mapping: when `rebalance_hour == 15`, the latest iteration LumiBot presents is
   hour 14 (completed 14:00 bar). The rebalance must therefore run on the **hour-14 iteration**, submit
   orders that fill at the **next executable open** (15:00 open of that session — but if LumiBot cannot
   model the 15:00 fill intrabar, fill at the next bar LumiBot can fill, i.e., the next session's open —
   and disclose which one in the artifact/convention note). NEVER let the order use data from the 15:00
   bar (that bar is unknown until 16:00).

2. Implement `hour == 15 → effective_rebalance_iterations = {15 → 14}` (or a general mapping) so the
   `_rebalance` gate at lines ~1238-1248 fires for the 15 variant at the correct causal iteration.
   Keep `signal_hour=9` selection intact. Confirm `_schedule_due(day)` and risk-off flatten also work
   at the remapped iteration.

3. If a rebalance_hour value would never be present in LumiBot iteration (>14), fail closed at
   registry/`check_supported` time instead of silently running flat. Add this to `check_supported`.

## Regression tests (required)
Add/extend `tests/strategy_lab/test_native_experiments.py`:
- `rebalance_hour=10` and `rebalance_hour=15` on the SAME synthetic bars must both submit entries,
  and the 15 variant must use the completed 14:00 bar (not the 15:00 close) — assert via the fill
  price/source timestamp or order metadata.
- A native pilot on window b01 (or f1_innerA) for V002 and V100 must now have non-zero fills and
  non-zero return; assert fills > 0.
- Registry `check_supported` rejects any `rebalance_hour` value that can never iterate (e.g. 16-23)
  with a clear error.

## Constraints
- No LumiBot core changes. Everything in `strategy_lab/`, `scripts/`, `tests/strategy_lab/`.
- Do not change the V001-V100 matrix, parameter defaults, or the clock-hour convention doc.
- v1 fingerprints must remain unchanged (run the registry verify).
- Do NOT commit or push. No branches.
- Keep `IMPLEMENTATION_REVISION` — bump to `hts-native-v2-2026-09-16-2`.

## Deliverable
1. The fix + tests, all green: `python -m pytest tests/strategy_lab/ -q` (report count).
2. Re-run the descriptive pilot for the 15 affected candidates on **six_year** AND **two_year**
   (use `run_hts_experiments.py --ids V002,V019,V020,V022,V039,V040,V042,V059,V060,V062,V079,V080,V082,V099,V100 --windows six_year,two_year --out-dir reports/hts_v2_recheck_2026-09-16`)
   and confirm NONE are flat anymore. Report the table.
3. Update `plans/build-prompts/TERRA_V2_RESULT.md` with a "FIX 2" section: root cause, mapping,
   test count, recheck table, and any caveat about the actual fill timestamp.
Do NOT report qualified performance — this is the fix + recheck step.