# Title: TERRA v2 HTS Native Implementation Result

Description: Commit-free implementation and verification record for the frozen HTS v2 catalogue.

Last Updated: 2026-09-17

Status: Implemented and locally verified; retrospective research only, not qualified.

Audience: HTS strategy reviewers and the release owner.

## Overview

Implemented the exact native-only HTS v2 contract from `plans/hts_v2_plan.md`.
No LumiBot core file under `lumibot/` was changed. No branch was created,
committed, or pushed.

## Files Changed

- `strategy_lab/experiment_config.py` — adds `hts-v2` and v2-only parent lineage fingerprint metadata while preserving legacy fingerprint payloads.
- `strategy_lab/hts_variants.py` — adds the five v2 ParameterSpecs, the v2 baseline/family, `resting-stop-atr`, the frozen five-parent/twenty-recipe matrix, V001-V100, and plan SHA-256 verification.
- `strategy_lab/experiment_registry.py` — registers and validates 211 total configurations, including semantic v2 parameter uniqueness and lineage.
- `strategy_lab/hts_policies.py` — adds pure global-gate, risk-cap, entry-edge, and mandatory-hold policies.
- `strategy_lab/native_experiments.py` — adds native risk-off state/lifecycle, cap sizing, entry filtering, minimum holds, stop aliasing, v2 blocks, revisioned artifacts, and audit diagnostics.
- `strategy_lab/experiment_validation.py` — independently checks v2 artifact identity, primary cost metadata, cap arithmetic, and position-PnL breadth inputs.
- `scripts/run_hts_experiments.py` — adds `hts-v2`, v2 lineage/manifest fields, and `--v2-walk-forward` for b01-b12.
- `scripts/list_strategy_experiments.py` — lists, filters, renders, and shows v2 lineage and full resolved parameters.
- `scripts/evaluate_walk_forward.py` — uses six discovery blocks, parent champions, selection locks, and lock-before-reveal enforcement.
- `scripts/stress_walk_forward_costs.py` — reruns only frozen selected outer rows at 7/15 bps per side.
- `tests/strategy_lab/test_experiment_registry.py`, `test_hts_policies.py`, and `test_native_experiments.py` — v2 registry, formula, lifecycle, wiring, clock, artifact, and native parent-parity coverage.

## Native Call Sites

- Risk-off gate: `RegistryHtsStrategy._risk_off_gate`, `_refresh_risk_off_state`, `_flatten_risk_off`, and `on_trading_iteration`.
- Risk-contribution cap: `RegistryHtsStrategy._target_weights` after `target_weights` and before `apply_leveraged_cap`.
- Entry-edge hurdle: `RegistryHtsStrategy._rebalance`, immediately before a new buy.
- Minimum holding period: `RegistryHtsStrategy._select` through `select_holdings(mandatory_held=...)`; risk-off and stops bypass it.
- Reused knobs: `atr_k` in target/stop/edge paths; `top_n` and `exposure_group_limit` in `_select`; `reentry_cooldown_bars` in `on_filled_order`; `rebalance_hour` in `on_trading_iteration`.

## Verification

- `.venv/bin/python -m pytest tests/strategy_lab/ -q` — `112 passed`.
- `.venv/bin/python scripts/list_strategy_experiments.py --verify` — passed: `211 configurations (1 control, 100 hts, 100 hts-v2, 10 alternative), 22 families`.
- Native default-parent parity on b01 (`2020-09-09` to `2021-03-09`): V001/H100, V021/H027, V041/H022, V061/H095, and V081/HTS_CONTROL_1 each had identical normalized trade/equity hashes and clean independent audits.

## Smoke Test Only

`V001` ran through `Strategy.run_backtest + BacktestingBroker` on b01
(`2020-09-09` to `2021-03-09`) at 3.5 bps/side. Native runtime was 1.34 seconds
(2.3 seconds runner wall time). The independently audited result was valid:
125 sessions, final equity `$106,552.25`, total return `+6.5523%`, independently
recomputed daily-return Sharpe `0.63085`, and max drawdown `18.9931%`.

These are SMOKE TEST ONLY numbers. They are not qualification evidence.

## Deviations

None from the specified parameter surface, IDs, parent seeds, overlay dictionaries,
matrix hash, engine, cost convention, or v1 catalog/fingerprints.

Not run: the 1,272-job retrospective walk-forward/benchmark programme and the
subsequent 7/15 bps selected-track stress programme. The runner, locks, evaluator,
and stress path are implemented; their outputs remain required before any
qualification statement.

## FIX 2 — Rebalance Hour 15 Native-Iteration Repair

### Root Cause and Mapping

The native LumiBot hourly loop exposes callbacks for clock hours 09:00 through
14:00 only. V2 P02/P19/P20 variants configured with `rebalance_hour=15` were
therefore never admitted through the equality gate and remained flat.

`15 -> 14` is now an explicit native-iteration mapping. The 14:00 callback
plans the logical 15:00 rebalance from the completed 14:00 source bar. It pins
selection, ATR, edge, target-weight, and notional decisions to the completed
14:00 data; no 15:00 OHLC value is read. Values with no native callback (for
example 16 through 23) fail closed through `check_supported` with a clear
unsupported-mechanism reason.

Ordinary market orders submitted at LumiBot's observed 14:00 callback would
fill at the earlier 14:00 open. The strategy therefore queues the causal
intent, then submits planned exits and replacements together at the next native
bar: the following session's 09:00 open. The per-run
`deferred_rebalance_events` artifact records the 14:00 source timestamp, the
logical 15:00 target, and the actual 09:00 submission/source-fill timestamp;
`run_trades.csv` remains the fill record of authority. This is the native
engine's next-bar fallback, not a claim of an intrabar 15:00 fill.

The normal `_schedule_due(day)` path and v2 risk-off flatten both use the same
mapping and deferred next-bar behavior. `signal_hour=9` is unchanged.

### Verification

- `.venv/bin/python -m pytest tests/strategy_lab/ -q` — **118 passed**.
- `.venv/bin/python scripts/list_strategy_experiments.py --verify` — passed:
  211 configurations (1 control, 100 frozen v1 HTS, 100 HTS v2, 10 alternatives).
  The frozen v1 fingerprint registry remains valid.
- Synthetic lifecycle coverage proves both `rebalance_hour=10` and `15`
  submit entries. The mapped case records the completed 14:00 source and never
  uses the synthetic 15:00 close sentinel. It also covers risk-off flatten and
  rejected non-iterable hours.
- Native b01 regression coverage runs V002 and V100 through
  `Strategy.run_backtest + BacktestingBroker` and requires non-zero fills and
  non-zero return.

### Descriptive Recheck Only

Command run:

```bash
.venv/bin/python scripts/run_hts_experiments.py \
  --ids V002,V019,V020,V022,V039,V040,V042,V059,V060,V062,V079,V080,V082,V099,V100 \
  --windows six_year,two_year \
  --out-dir reports/hts_v2_recheck_2026-09-16
```

All 30 result artifacts are clean and non-flat: every run has fills greater
than zero, a non-zero return, and final equity different from $100,000. All
20,510 recorded deferred submissions are at 09:00. These descriptive figures
are repair evidence only, **not qualified performance**.

| ID | six_year fills | six_year return | two_year fills | two_year return |
|---|---:|---:|---:|---:|
| V002 | 1592 | +144.6678% | 502 | +100.7316% |
| V019 | 1653 | +34.9282% | 577 | +51.0697% |
| V020 | 1276 | +13.6372% | 430 | +24.4617% |
| V022 | 2578 | +1000.5727% | 842 | +272.4259% |
| V039 | 2276 | +27.5425% | 822 | +78.0997% |
| V040 | 1622 | -14.0420% | 552 | +5.2817% |
| V042 | 2586 | +1146.8129% | 840 | +504.4982% |
| V059 | 2244 | +178.0957% | 852 | +82.0824% |
| V060 | 1700 | +4.8960% | 590 | +15.4836% |
| V062 | 2222 | +456.3868% | 734 | +132.8744% |
| V079 | 2282 | +31.8821% | 868 | +35.0901% |
| V080 | 1676 | +5.0642% | 624 | +6.4811% |
| V082 | 2234 | +392.4422% | 734 | +331.2237% |
| V099 | 2280 | +51.0005% | 868 | +74.5095% |
| V100 | 1676 | -0.7811% | 624 | +5.6792% |
