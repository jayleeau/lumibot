# Laya Offline Entry-Eligibility Research (Tasks 0a+)

Engineering runbook for the offline Laya forecast experiment defined by `plans/codex_astra_laya_plan.md`. Task 0a freezes the causal research protocol and exports a features-only dataset; no model exists yet.

Last Updated: 2026-09-22

Status: Task 0a implemented (protocol freeze + causal snapshot/outcome export). Handoff for later tasks.

Audience: HTS research implementer and independent reviewer. Not a public LumiBot API.

## Overview

This directory is **research-only**. It adds no public LumiBot contract, broker adapter, live-strategy hook, or dependency. Nothing here may run inside native execution: the native engine never imports `strategy_lab.laya_research`, and the package never imports torch, Laya, a model, or an archive loader at import time.

Task 0 is a hard feasibility and predictive-calibration gate. Task 0a (this document's scope) only freezes the protocol and produces data:

- `strategy_lab/laya_research/contracts.py` — pure typed contracts: interval roles, the frozen `Protocol`, snapshot/outcome/eligibility records, deterministic ASCII serialization, and fail-closed export-role validation.
- `strategy_lab/laya_research/snapshots.py` — the causal snapshot builder, exact S→D calendar mapping, and the forward-return target. Pure Python over pandas frames; its only strategy-lab import is `feature_store`.
- `strategy_lab/laya_research/protocol_v1.json` — the frozen protocol (intervals, feature order, target, costs, seeds, model/calibrator identity placeholders).
- `scripts/laya_feasibility_probe.py` — the typed CLI with `freeze` and `export`.
- `reports/laya_research_v1/protocol_manifest.json` — the read-only manifest (committed).

## Causal contract (frozen)

For a decision session `D`, the source session `S` is the previous expected exchange session. The snapshot is the twelve frozen daily features at `S`, computed from `feature_store.daily_feature_frame` with the protocol lookbacks (`trend_sma=20`, `return_period=20`, `liquidity_period=63`). Field order is fixed:

`close_over_sma_minus_1`, `ret`, `log10_mdv`, `ret1`, `r10`, `r20`, `r60`, `vol20`, `ddvol20`, `eff20`, `reg_slope`, `reg_r2`.

`close_over_sma_minus_1 = close/sma - 1` and `log10_mdv = log10(mdv)`. A snapshot is invalid (never prose) when any field is missing, nonfinite, or requires a non-positive ratio/log input. Symbol and session dates are metadata carried outside the serialized model text; the text is deterministic ASCII, six decimals, with negative zero normalized.

The target is the one-session screening return:

```
R_net = P_exit * (1 - fee_per_side) / (P_entry * (1 + fee_per_side)) - 1
y     = 1[R_net > 0]
```

Entry is `D` 10:00 America/New_York from the back-adjusted hourly archive; exit is the following exchange session's 10:00 New York open. This is **not** an `S`-close-to-`D`-close return. Missing prices, bad session identity, or unresolved adjustment invalidate the observation. Target observations whose exit session falls outside the assigned interval are dropped consistently.

## Frozen intervals (decision-session, inclusive)

| Role | Dates | Task 0a action |
|---|---|---|
| train | 2019-01-01 .. 2021-12-31 | export features + outcomes |
| calibration | 2022-01-01 .. 2022-12-31 | export features + outcomes |
| gate | 2023-01-01 .. 2023-12-31 | export features + outcomes |
| final | 2024-01-01 .. 2026-09-08 | **forbidden**; never queried or exported in 0a |

A previous-year source session may feed the next year's first decision because it is past information. Warmup rows lacking any of the twelve features are excluded consistently. A `--include-final` shortcut does not exist, and both `contracts.assert_export_roles` and the CLI reject `final`.

## Data sources (read-only)

- Daily: `short/suite_monitored_xnas_itch_daily_adjusted.duckdb`, table `bars_daily`.
- Hourly: `short/suite_v2_xnas_itch_hourly_adjusted.duckdb`, table `bars_hourly`.
- Load semantics reuse `strategy_lab.native_experiments._sql_frames` exactly (daily UTC session date, hourly New York clock; keep-last dedup). Raw duplicate, OHLC, session, and adjustment checks run **before** deduplication can conceal a defect; any failure aborts the freeze.

These local archives must already exist. Missing archives produce a data failure, never an automatic download. Do not launch ThetaTerminal locally with production credentials.

## Commands (Task 0a group A)

```bash
IS_BACKTESTING=true LUMIBOT_DISABLE_DOTENV=true .venv/bin/python -m pytest strategy_lab/laya_research/tests/test_snapshots.py -q

IS_BACKTESTING=true LUMIBOT_DISABLE_DOTENV=true .venv/bin/python \
  scripts/laya_feasibility_probe.py freeze \
  --protocol strategy_lab/laya_research/protocol_v1.json \
  --out reports/laya_research_v1/protocol_manifest.json

IS_BACKTESTING=true LUMIBOT_DISABLE_DOTENV=true .venv/bin/python \
  scripts/laya_feasibility_probe.py export \
  --protocol reports/laya_research_v1/protocol_manifest.json \
  --roles train calibration gate \
  --out short/laya_work/gate
```

`freeze` writes the read-only manifest plus a small synthetic OFF golden fixture under `short/laya_work/gate/golden_off_synthetic.json`. `export` verifies both archives against the manifest size/SHA-256 and refuses to proceed on mismatch.

## Outputs

All outputs under `short/` are local and untracked.

- `short/laya_work/gate/snapshots.parquet` — features only: `symbol`, `source_session`, `decision_session`, `source_available_at`, and the twelve feature columns. No forward return, target, or ticker text.
- `short/laya_work/gate/outcomes.parquet` — `symbol`, sessions, entry/exit price, `r_net`, `y`, and a `valid`/`reason` audit trail.
- `short/laya_work/gate/eligibility_mask.parquet` + `eligibility_summary.json` — the all-eligible observation mask computed independently of Laya (features complete AND target valid).
- `reports/laya_research_v1/protocol_manifest.json` — committed, read-only manifest of archive hashes, canonical OHLCV digests, query bounds, expected sessions, inception exclusions, code hashes, and the golden fixture digest.

## Boundaries and intentional omissions

- No model download, install, import, or inference; no calibration fit.
- No modification to `lumibot/`, `tests/`, public docs, dependencies, or live strategies.
- No new environment variables; tests are not skipped by a toggle.
- Task 0b (model technical gate), 0c (calibration/predictive gate), and Tasks 1-6 remain undone. Task 1+ requires a machine-readable gate status of `PASS`; there is no override.

## Reproducibility notes

- The manifest is written `0o444`; re-running `freeze` rewrites it.
- `snapshots.py` uses the installed `exchange_calendars` calendar (`XNAS` resolves to `XNYS`) for holiday, DST, and early-close handling; the resolved name and version are frozen in the manifest.
- New tests live only under `strategy_lab/laya_research/tests/` and do not inherit `tests/conftest.py`; they set `IS_BACKTESTING`/`LUMIBOT_DISABLE_DOTENV` before importing and use synthetic values only.
