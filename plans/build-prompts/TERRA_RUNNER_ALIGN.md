# TERRA - Runner Clock-Hour Alignment

Description: Completed, uncommitted alignment of the full native LumiBot HTS runner with the frozen clock-hour contract.

Last Updated: 2026-09-16
Status: Completed and targeted-test verified
Audience: HTS research-harness maintainers

## Overview

Aligned `scripts/run_full_lumibot_backtests.py` to the frozen convention:
exact New York clock-hour labels `09:00` through `15:00`, where label `T`
represents `[T, T+1h)` and becomes available at `T+1h`.

## Changes

- `_hts_lumibot_data` now retains only exact whole-hour `09:00`-`15:00`
  labels and preserves source OHLCV values and timestamps. The retired
  `+09:30` relabel is removed.
- `_prepare_hts` applies that same canonical frame before constructing the
  HTS hour/ATR map and the LumiBot `Data` payload, so off-hours and sub-hour
  rows cannot affect the feature leg while being absent from the broker leg.
- The strategy retains `signal_hour=9` and `execution_hour=10`. Its timing
  comments now state the causal boundary: a decision at `T` reads earlier
  labels, and the prior `15:00` close has no `16:00` executable row.
- Kept the established prior-session `[14, 15]` trailing-stop catch-up and
  clarified that it runs before the next session's `09:00` open.
- Added deterministic runner regression coverage that checks exact labels,
  unchanged OHLCV, and removal of pre-market, post-market, and sub-hour rows.

## Verification

```text
.venv/bin/python -m pytest tests/strategy_lab/test_native_experiments.py tests/strategy_lab/test_native_alternatives.py tests/strategy_lab/test_experiment_registry.py -q
48 passed, 1 warning in 1.07s

.venv/bin/python -m pytest tests/backtest/test_cached_strategy_execution_contract.py -q
6 passed, 5 warnings in 0.63s

git diff --check
passed
```

The warnings are existing third-party deprecations from `websockets.legacy` and
`jsonpickle`; no test failed.

## Not Verified

No full backtest was run, by request. The patch was checked against the frozen
native harness implementation and targeted execution-contract tests only.

No files under `lumibot/`, live strategy files, trader files, or deployment
files were changed. Nothing was committed or pushed.
