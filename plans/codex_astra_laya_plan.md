# Laya Offline Entry Eligibility for Native HTS — Implementation Plan

An evidence-first experiment to decide whether frozen offline Laya forecasts justify an optional native-backtest entry filter.

Last Updated: 2026-09-22

Status: PLAN ONLY; no implementation, dependency installation, model download, backtest, or commit performed for this plan.

Audience: HTS research implementer, lead analyst, and independent reviewer.

## Overview

Task 0 is a hard feasibility and predictive-calibration gate. Nothing is integrated into native HTS unless that gate passes. A technical failure, insufficient data, or rejected hypothesis is a complete, reportable endpoint. Passing permits implementation; it does not establish trading edge.

This plan uses the prior draft at `plans/build-prompts/09_laya_offline_feature_build.md` as context, with the following corrections: quantitative gate criteria; separate source/decision/outcome dates; probability and confidence definitions; a fresh-entry hook that cannot directly alter selection exits or allocation; content-addressed labels; realistic event-parity requirements; and explicit limits on historical out-of-sample claims.

Only this requested plan is written now. All future executable code, including new tests, belongs in `strategy_lab/` or `scripts/`. Do not modify `lumibot/`, public APIs, package dependencies, root configuration, live strategies, or broker adapters. Preserve unrelated working-tree changes, stay on the existing branch, and do not push, create a PR, or deploy as part of this work. Implementation requires a subsequent implementation instruction. Future commits below describe reviewable task boundaries, not actions authorized by this planning request.

## Objective

Test the frozen hypothesis: **a probability derived offline from a deterministic rendering of existing causal daily features can reject some otherwise eligible new HTS entries and improve independently audited after-cost results on later data.** Laya supplies no new data source; its possible contribution is a transformation of information already available to HTS.

The North Star follows `docs/HTS_100_VARIATIONS_RESEARCH_PLAN.md:13` and `plans/hts_v2_plan.md:14`: standard daily-return Sharpe on a predeclared held-out procedure, assessed with drawdown, uncertainty, cost sensitivity, and breadth of performance. Operational acceptance objectives are 100% causal/provenance/accounting checks passing, every attempted experiment recorded, and zero inference or label fitting inside native execution.

The first experiment has one prespecified parent: the HTS v2 baseline, with `parent_candidate_id="HTS_CONTROL_1"`, its current U0 universe, 10:00 ET rebalance, and 3.5 bps per side. It is chosen by protocol, not by historical performance. Name the experimental pair `LAYA_BASE_OFF` and `LAYA_BASE_ON`; keep the registry's actual `HTS_CONTROL_1` as an additional reference. Do not select the draft's top 20 historical winners or sweep thresholds. A rejection is a successful research result.

## Architecture

### Verified integration map

Line numbers describe the inspected checkout, HEAD `f9447e35`; use function names if they move. The checkout was on `version/4.5.92/clean-base` and had existing untracked research artifacts. No branch change is proposed.

| Existing location | Verified behavior | Planned use |
|---|---|---|
| `strategy_lab/native_experiments.py:92–93` | Uses `short/suite_monitored_xnas_itch_daily_adjusted.duckdb` and `short/suite_v2_xnas_itch_hourly_adjusted.duckdb`. | Freeze these actual sources; do not assume the draft's daily archive filename. |
| `_sql_frames`, line 244 | Read-only DuckDB load; daily UTC date is retained as a session label; hourly stamps become New York clock time. Duplicate indices currently keep the last row. | Reuse loading semantics; preflight raw duplicates before this deduplication can conceal them. |
| `feature_store.daily_feature_frame`, line 120 | Causal daily close/SMA/return/liquidity/volatility features. | Call with exactly the candidate's lookbacks and aligned QQQ history. Do not reimplement the indicators. |
| `PreparedInputs` / `prepare_inputs`, lines 331/347 | Daily strategy frames are separate from hourly `Data` payloads. | Join label columns into daily frames only when enabled; the broker's OHLCV stays unchanged. |
| `_previous_session` / `_daily_row`, lines 655/620 | Uses the preceding session and exact date lookup. | Enforce an exact source-session join, not nearest-row or forward fill. |
| `_select`, line 748 | Determines held selection, ranks, retention, and replacement. | **Do not filter here:** removing a held symbol could trigger a sell or change weights. |
| `_target_weights`, line 1088 | Uses the selected basket for sizing and risk caps. | No Laya reads, filtered basket, renormalization, or replacement selection. |
| `_rebalance`, line 1167 | Sells selection changes, computes weights, skips occupied symbols, validates entry inputs, then creates buys. | Add an eligibility check only for a fresh entry, after existing entry checks and before notional/budget mutation. |
| Deferred queue/flush, lines 1271/1387 | Logical 15:00 planning calls `_rebalance`, captures intents, and submits on a later native callback. | Freeze the label decision when the intent is formed. Never look up a later label at fill time. |
| `RunContext`, `_run_engine`, `run_candidate`, lines 523/1690/1948 | Module-level context; native `Strategy.run_backtest`, `PandasDataBacktesting`, and `BacktestingBroker`. | Preserve this sole execution path; separate processes, not threads, for concurrent runs. |
| `hts_variants.py:646–712` | `HTS_V2_DEFAULTS`, baseline and `_V2_NEW_PARAMETER_SPECS` define the new v2 surface. | Add three v2 keys and typed declarations here. |
| `experiment_config.py:300` | Generic `resolve_parameters` / `ParameterSpec`; there is no separate v2 known-map here. | Reuse unchanged. `run_candidate` already merges `HTS_V2_BASELINE` into `check_supported`'s known map. |
| `build_payload`, line 1845; `experiment_validation.py` | Existing metadata and independent accounting helpers; payload truncates general rejections. | Add dedicated complete Laya evidence and use the old audit as a regression check, supplemented by an independent Laya audit. |

The current `feature_hash` hashes configuration and row counts, not OHLCV contents. It is insufficient to identify a label input dataset. Add a separate content digest for this research path; do not claim the existing hash proves data identity.

### Offline and execution boundaries

| Stage | Interpreter / responsibility | Output |
|---|---|---|
| Freeze + export | Existing `.venv/bin/python`; immutable archive validation, `daily_feature_frame`, deterministic snapshots. | Features-only dataset and manifest; outcomes stored separately. |
| Explicit setup | Isolated `.venv-laya`; install pinned optional dependencies and fetch the one English model revision. | Local model snapshot, resolved dependency lock, file hashes. |
| Offline scoring + calibration | Isolated model process; no market-data network calls. Python handles all arithmetic. | Raw probability, frozen calibrated probability, derived label/confidence. |
| Publish local label bundle | Plain Python/Parquet; checkpoint, validate, atomically finalize. | One parquet per symbol plus a bundle manifest under `short/laya_labels/`. |
| Native A/B | Existing `.venv/bin/python`, which must still run without torch/Laya. | Native events and local run files; compact evidence summaries. |
| Independent audit | Fresh existing-venv process, no model and no runner metric implementation. | Arithmetic/parity/provenance checks, uncertainty intervals, JSON + HTML verdict. |

The isolated environment does not import the native engine. Source extraction happens in the existing venv and passes an explicitly validated, features-only file to inference. The model process cannot access target columns through an accidental DataFrame rendering. Offline calibration reads a separate outcomes file after inference. The backtest sees neither the outcomes nor the calibrator.

### Laya interface and source pins

Use the English root `convaiinnovations/laya`, not `Router`, multilingual, typed-decisions, or fine-tuning. The official model card describes the 421M Apache-2.0 English checkpoint and its 512-token context. This is not evidence of market forecasting skill. [Official model card](https://huggingface.co/convaiinnovations/laya)

The official repository currently reports that shipped models are overconfident and that calibration depends on domain fitting. Its English input budget splits a 512-token sequence into a default 192-token question head and the remaining state budget. Validate the actual assembled tokens and reject truncation. [Official repository and limitations](https://github.com/NandhaKishorM/laya)

The inspected SDK exposes `laya.load(local_directory, device="cpu")` and `agent.predict(state, questions)`. `noul` lives in `result["answers"][question_id]["noul"]`, with four-decimal SDK probabilities. The load API inspected has no revision argument; download a pinned snapshot first and load its local directory. Check for device fallback and tokenizer-file rewrites during loading. [Official inference source](https://github.com/NandhaKishorM/laya/blob/main/laya/agent.py)

Candidate package pin: `laya==0.3.5`, as declared by the inspected project metadata. Validate the installed wheel's API and hash in Task 0; never assume moving `main` equals that wheel. [Official package metadata](https://github.com/NandhaKishorM/laya/blob/main/pyproject.toml)

Initial model revision to verify and freeze: `1c5edc17a7acd8701df6fc341c0d179f1c62c982`, returned by the Hub during planning. Record actual file SHA-256 values during download. If unavailable, report a setup failure; changing the pin requires a new protocol revision before outcome inspection. [Hub model metadata](https://huggingface.co/api/models/convaiinnovations/laya)

### Exact snapshot, prediction, and label contract

For decision session D, S is the previous expected exchange session. Snapshot S uses only daily bars completed by S's close. Labels are **keyed by source session S**, and consumed by `_daily_row(symbol, S)` during D. Persist D too, for validation, but never shift twice. A daily midnight date is an identifier, not the instant the close became observable. Record `source_available_at` as the calendar's actual S close in UTC, including early closes; require it before the native decision. Use the installed exchange calendar and freeze its version; missing expected symbol sessions are not filled from an older session.

Initial numeric snapshot fields, in this exact order: `close_over_sma_minus_1`, `ret`, `log10_mdv`, `ret1`, `r10`, `r20`, `r60`, `vol20`, `ddvol20`, `eff20`, `reg_slope`, `reg_r2`. They are a fixed subset/transformation of the existing daily frame, with ratios and logarithms computed in Python. Do not include intraday values, portfolio state, outcomes, news, ticker names, session dates, raw prices, or narrative explanations. The symbol/date remain metadata, outside model text. This tests daily-state eligibility, not the entire intraday risk state. More fields or prompts are separate future trials.

Serialize in fixed ASCII field order with explicit units, six decimal places, no locale variation, normalized negative zero, and a frozen schema version. Hash both original float64 feature values and exact text bytes. Nonfinite required fields or insufficient history produce an invalid snapshot and a missing/invalid label, not prose such as "unknown". Hash lookbacks and source rows, so changed parameters cannot silently reuse labels. Test that the numeric competitor uses precisely the information retained by this serialization, rather than extra precision unavailable to the model.

Use one binary `noul` question, frozen verbatim in `protocol_v1.json`:

> Given only these completed daily indicators, will buying at the next exchange session's 10:00 New York hourly open and selling at the following exchange session's 10:00 hourly open yield a strictly positive return after 0.035 percent cost on each side?

The objective calibration target is `y = 1[R_net > 0]`, with `R_net = P_exit * (1 - 0.00035) / (P_entry * (1 + 0.00035)) - 1`. Both prices come from the exact hourly archive timestamps in the question. Missing prices, bad session identity, or unresolved adjustments invalidate the observation. This is a one-session predictive screening target under the native source-price convention; actual HTS stops, holding periods and fills are evaluated separately in A/B. Do not use S-close-to-D-close returns, which would score an overnight move that an entry during D cannot earn.

Let `q` be the SDK's event probability, not its confidence. Fit one monotone logistic calibration `p = sigmoid(a * logit(clip(q, 1e-4, 1-1e-4)) + b)`, with `a >= 0`, on the calibration split only. Freeze optimizer, initial point `(1, 0)`, tolerance, and tiny fixed L2 regularization in protocol. No model weight training. An intercept-only solution cannot pass the predictive gate. Store `q` and calibrated `p` separately.

Define `laya_label = long_favorable` when `p > 0.5`, otherwise `long_unfavorable`; invalid rows use `unavailable`. Define `laya_confidence = max(p, 1-p)` as the calibrated probability assigned to the selected binary label, **not a statistical confidence interval**. Preserve `model_confidence_raw` separately for diagnostics. The SDK's choice/score entropy-based confidence is a different quantity and must not become a probability threshold. [Official confidence implementation](https://github.com/NandhaKishorM/laya/blob/main/laya/common.py)

The filter requires a valid favorable label and `p >= 0.55`. This one threshold is preregistered, not optimized. A second confidence knob is redundant and is deliberately omitted.

### Bundle and runtime contract

Each immutable bundle directory is `short/laya_labels/<bundle_id>/`; `bundle_id` is the SHA-256 of canonical semantic manifest content, excluding timestamps, paths and the ID itself. It includes model, tokenizer, dependencies, calibration, prompt, source data, feature parameters, calendar and schema digests. Within one bundle, `<SYMBOL>.parquet` has one unique `(symbol, source_session)` row, sorted by source session.

Required fields: `symbol`, `source_session` (date), `decision_session` (date), `source_available_at` (UTC), `snapshot_sha256`, `laya_label`, `laya_prob_raw`, `laya_prob` (float64), `laya_confidence` (float64), `model_confidence_raw`, `valid`, `reason`. Manifest fields additionally contain per-file byte hashes, canonical row hashes, row counts, coverage/exclusions, frozen fit boundaries, maximum target timestamp used to fit calibration, target definition, and actual device/dtype. No forward return or target label is stored in engine-consumed parquet.

Artifact creation time may be later than the historical decision: these are retrospective features. It must never be represented as historical availability. `source_available_at` establishes causal input availability; `calibration_fit_through` prevents applying a future-fitted mapping to an earlier scored decision. Model-training chronology is a separate limitation.

Runtime parameters, v2 only:

| Key | Default | Validation |
|---|---|---|
| `laya_filter` | `False` | Strict bool. |
| `laya_min_probability` | `0.55` | Finite float in `[0.5, 1.0]`; the first study uses only 0.55. |
| `laya_bundle_id` | `None` | Nullable string; enabled requires exactly a lowercase 64-character SHA-256 ID. |

Use an explicit optional strategy-lab-only keyword `label_store_root: Path | None = None` on `run_candidate` and `prepare_inputs`. When enabled and unspecified, resolve `ROOT / "short" / "laya_labels"`. The research CLI supplies `--label-root`. Paths are not candidate parameters and never become personal paths in tracked manifests. Off returns before touching the label root, validating a bundle, joining columns, or importing model modules. No new environment variables are needed.

Enabled behavior:

- Load/validate the immutable bundle once in `prepare_inputs`, join on the exact source session with one-to-one validation, and attach provenance metadata to `PreparedInputs` with backward-compatible defaults. Labels remain ordinary scalar input columns during execution.
- Missing symbol/file/row and explicitly invalid feature rows make the affected fresh entry ineligible. Emit a complete reasoned evidence event. A missing entire bundle, malformed schema, duplicate key, hash mismatch, wrong universe/features/calibrator, or invalid numeric probability aborts the run before orders. Both policies fail closed; corrupt data must not masquerade as a profitable all-cash result.
- Qualification requires 100% label coverage of valid, otherwise evaluated entry snapshots; permitted warmup/data exclusions are fixed before outcomes. A runtime missing row test should block entry, while a research A/B with unexpected missing rows is invalid evidence.
- In `_rebalance`, after occupied-symbol and existing price/ATR/edge checks, call a pure `entry_allowed(row, threshold)` before notional and budget mutation. Preserve `_selected`, rankings, all weights, exits, stop processing, and pending-order handling. Do not select a lower-ranked replacement, renormalize weights, or add a redistribution mechanism. A denied buy initially leaves its cash unspent; the unchanged budget algorithm may let a later entry in the same rebalance use that cash up to its original target. Do not add virtual reservations to force identical quantities. This is an existing cash-budget consequence of blocking an entry, not a Laya sizing input; subsequent holdings and quantities can also diverge.
- Record source/decision/intent timestamps, symbol, row hash, probability, threshold, result and reason. For deferred entries, carry this decision's identifier with the captured intent; `_flush_deferred_rebalance` consumes that frozen approval without consulting a new session's label. Later risk-off or broker decisions retain their existing authority.
- No live strategy imports or uses this package. No hooks in `_select`, `_target_weights`, `_update_risk`, `_submit_sell`, `_submit_buy`, broker order submission, or fill callbacks. Putting the check in `_submit_buy` would mishandle deferred intent timing.

## Tasks

Every implementation unit below follows red → green → inspect diff → commit. Keep failing-test evidence in the task record, but do not commit a red test suite. Each numbered task ends in a small commit; Task 0 is split into three commits so the protocol is frozen before results exist. Tasks after 0c require a machine-readable gate status of `PASS`; no override flag exists.

### Task 0 — HARD feasibility and predictive-calibration gate

**Stop condition:** do not modify any existing native engine/configuration file until all Task 0 criteria pass. A smoke query proves only installation, never financial usefulness.

**0a: Freeze the contract and causal dataset.**

Create `strategy_lab/laya_research/__init__.py` (no inference imports), `contracts.py`, `snapshots.py`, `protocol_v1.json`, `scripts/laya_feasibility_probe.py` with `freeze` and `export` subcommands, and `strategy_lab/laya_research/tests/test_snapshots.py`. Start `strategy_lab/LAYA_RESEARCH.md` with this research-only boundary and the protocol. Reuse `daily_feature_frame` and the exact archive semantics above without modifying them. The exporter does raw duplicate/OHLC/session/inception/adjustment checks before accepting records. Unverified data fail; never silently substitute a provider or repair raw archives.

Freeze these inclusive **decision-session** intervals; use source history before each interval only for warmup:

| Role | Dates | What may be fit or inspected |
|---|---|---|
| Numeric-baseline training | 2019-01-01 through 2021-12-31 | Scaling and fixed numeric comparator only. |
| Calibration | 2022-01-01 through 2022-12-31 | Laya mapping, comparator mapping, base-rate null. |
| Hard predictive gate | 2023-01-01 through 2023-12-31 | One pass/fail evaluation; no tuning. |
| Final retrospective holdout | 2024-01-01 through 2026-09-08 | Features-only export/scoring allowed after Task 0 PASS with frozen calibration; explicit target/performance evaluation only after native parity checks. No fitting on any final rows. |

Drop target observations whose entry or exit falls outside their assigned interval. A previous-year source session is allowed for the next year's first decision because it is past information. Do not randomly split symbol rows. Resolve the complete U0 symbol order, warmup requirements and inception exclusions before outcomes; do not select two winning symbols. Warmup rows lacking any of the 12 required features are excluded consistently. At least 60 returns/61 closes are required for the longest selected feature; other feature parameters may demand longer.

Create a read-only manifest with source file size/checksum, canonical queried OHLCV digests, query bounds, expected sessions, exclusions and code hashes. Keep targets in `short/laya_work/gate/outcomes.parquet` and features in `short/laya_work/gate/snapshots.parquet`. Export only training/calibration/gate at this stage. The CLI enforces protocol roles; no `--include-final` shortcut. Compute a compact all-eligible daily observation mask independently of Laya and report how it differs from actual portfolio entry opportunities.

TDD: future-row mutation/truncation invariance, exact S→D calendar lookup, holiday/DST/early-close cases, missing prior symbol bar, target crossing a split, arithmetic on a hand-worked return, deterministic text bytes, invalid/nonfinite inputs, and no outcome/ticker/date in model text. Add synthetic symbols/values only, never vendor data fixtures.

Verification: command group A and `test_snapshots.py` below. Commit explicit created paths plus `reports/laya_research_v1/protocol_manifest.json` after scanning it. Suggested message: `research: freeze causal Laya gate protocol and snapshot contract`.

**0b: Install, load, and verify local inference feasibility.**

Create `strategy_lab/laya_research/inference.py`, `scripts/requirements/laya-research.in`, the resolved `laya-research-macos-arm64-py311.lock`, and `tests/test_inference.py` under the new package. Extend the probe with `download`, `technical`, and `score` subcommands. Dependencies stay isolated: `.venv` exports data/runs native; `.venv-laya` contains Laya/torch plus pandas, pyarrow, scipy, pytest and the lock tooling needed for this experiment. Pin the complete dependency closure with hashes after resolution. Do not install LumiBot into the model venv or torch into `.venv`.

Download only root `rl_agent_config.json`, `model.safetensors`, `tokenizer/*`, `encoder/*`, and license/model-card provenance at the pinned revision. Preserve an immutable original download; if the SDK requires a tokenizer normalization, make a separate working copy, record before/after hashes and exact deterministic normalization, and reject unaccounted changes. Do not modify weights or temperatures silently. Subsequent loading must succeed offline with complete local tokenizer/encoder files. Mock deny network in the offline smoke process, not just a best-effort cache setting.

Reference backend: one CPU worker, float32, fixed seeds `20260922`, torch deterministic algorithms, eval/no-grad, and fixed thread count. Record actual device/dtype after load and each batch; fallback or changed output fails. MPS is an optional later experiment, not an automatic alternative when CPU fails. Never promise cross-backend bit equality. Verify exact returned SDK probabilities and canonical row values across two fresh CPU loads on 300 deterministic calibration-split rows. Store both canonical decoded-row hashes and parquet byte hashes; parquet metadata equality is not the definition of inference determinism.

Measure cold load, warmup, p50/p95 latency over at least 300 representative real snapshots, peak process-tree RSS, and projected total rows/runtime for the frozen corpus. Enforce actual tokenizer assembly: all state and head tokens fit their separate budgets without lost fields or markers. Fixed precision can be compressed only by a new protocol revision before gate outcomes are evaluated.

Explicit technical kill criteria:

- Install, model load or offline repeat load fails; incompatible API/schema; unexpected model revision; nonfinite or out-of-range outputs; any implicit network access after download.
- Returned outputs differ across the two reference loads; a label or threshold decision changes; CPU/device/dtype is not the pinned configuration.
- Any silent token truncation; no valid way to fit the frozen question and snapshot within the checkpoint's actual budget.
- Peak process-tree RSS exceeds 10 GiB on the 16-GB machine, sustained swap growth exceeds 1 GiB during the probe, or projected complete corpus inference exceeds 24 hours. Record these as preregistered resource choices. Chunk each job to at most 15 minutes under a 20-minute watchdog.

TDD: fake SDK validates actual nested result extraction and four-decimal endpoints; lazy import separation; cache-only load; tokenizer-budget rejection; duplicate/resume handling; backend drift; invalid outputs. Real load/repeat tests are an explicit probe command with a nonzero failure exit, not a default pytest download.

Verification: group B and `test_inference.py`. Commit code/tests/requirements/lock plus sanitized `reports/laya_research_v1/technical_gate.json`. If failed, commit the failure record and stop. Message: `research: qualify pinned offline Laya runtime on the target machine`.

**0c: Calibrate and evaluate real forward-return probabilities.**

Create `strategy_lab/laya_research/calibration.py` and `tests/test_calibration.py`; extend the probe with `gate`. Score all eligible rows in the three permitted historical splits with the one frozen question. Do not change the prompt after viewing any predictive gate output.

Fit the monotone Laya calibration on 2022 only, using equal total weight per session. Use a fixed scipy optimizer and record convergence and coefficients. The comparator is an L2 logistic model on the same 12 serialized numeric features: means/scales fitted in 2019–2021, fixed penalty `0.01 * sum(nonintercept_coefficients**2)` added to session-weighted mean log loss, no cross-validation or hyperparameter search. Give it the same monotone calibration procedure on 2022. Reject nonconvergence and record zero-variance fields; no feature search.

Nulls: (a) one calibration-period base-rate forecast with Laplace smoothing; (b) per-symbol calibration frequencies shrunk with 50 pseudo-observations toward that global rate, falling back to the global rate for unavailable histories. Freeze both before 2023 and require improvement against both. Compute all model/null losses on identical rows and masks.

For each 2023 decision session, average Brier loss `(p-y)^2` across its eligible symbols, then average those daily losses. Log loss uses the same weighting and clipping at `1e-6`. Resample whole cross-sections in common moving blocks of 20 sessions, 10,000 repetitions, seed `20260922`. Report paired loss differences and their one-sided 95% lower confidence bounds. Ten- and forty-session block sensitivities are diagnostics; do not choose the passing block length. Do not bootstrap thousands of correlated symbol rows as independent observations.

**All** predictive pass conditions are required:

1. At least 200 distinct gate decision sessions, 2,000 eligible symbol-session targets, and 200 observations in each class; sufficient 2022 observations to fit the frozen calibration. Otherwise `INSUFFICIENT_EVIDENCE`, stop.
2. Brier skill `1 - BS_Laya / BS_null` is at least 1% against each null, and the one-sided 95% lower bound of `BS_null - BS_Laya` is strictly positive for each.
3. Calibrated log loss is no worse than either null. Equal-count reliability bins, ECE, accuracy, class balance, raw-vs-calibrated scores and dispersion are published as diagnostics, not substitutes for predictive skill.
4. The one-sided 95% lower bound of `BS_numeric - BS_Laya` is positive. If ordinary numeric modeling explains the gain, reject the extra Laya complexity.
5. At the fixed 0.55 threshold, accepted observations cover 5%–95% of valid rows, at least 200 accepted outcomes, and at least 100 distinct sessions. Otherwise the proposed filter lacks enough variation/evidence for this study.
6. Provenance, causality, token, data and determinism checks all pass. No protocol changes, hidden retries, reversed signal, or held-out fit.

These numerical bars are conservative research decisions, not guarantees of statistical truth. Because all comparisons must pass, the gate is a conjunction; adding prompts/models would require a new multiple-testing protocol. Preserve every attempted protocol, including failures. Never retry a failed gate until it passes by renaming it.

TDD: perfect forecast passes on synthetic independent data; constant/base-rate forecast cannot pass; calibration alone cannot create discrimination; negative monotone slope is prohibited; duplicated same-session symbols do not increase effective time sample; seeded block intervals reproduce; train/calibration/outcome split sentinels reject leakage; fit refuses final dates; all kill statuses return nonzero.

Verification: group C and `test_calibration.py`. Emit `reports/laya_research_v1/gate.json`, `gate.html`, and a small `gate_bins.jsonl`, including raw and calibrated scores, uncertainty, exclusions, every criterion and verdict. Fitted coefficients are a small versioned JSON artifact; raw prediction/target tables remain untracked. Commit these with code/tests. Message: `research: record Laya predictive calibration gate and verdict`.

Gate statuses: `PASS`, `FAILED_TECHNICAL`, `FAILED_DATA`, `INSUFFICIENT_EVIDENCE`, `REJECTED_PREDICTIVE`. Only `PASS` unlocks Task 1. An installation failure is not financial evidence; a failed predictive gate is a rejection of this frozen hypothesis, not a proof about every possible model.

### Task 1 — Immutable, resumable per-symbol label bundles

Depends on 0c PASS. Create `strategy_lab/laya_research/labels.py`, `scripts/laya_label_features.py`, and package test `test_labels.py`.

Implement `LabelBundleManifest`, `read_label_bundle`, validation, and canonical digest routines without importing inference. The builder alone imports `inference.py` in its score subcommand. Export final **features only** now; use the frozen 2022 calibrator and model. Export all feature-complete source sessions needed by the full final native interval, including its preceding session, independently of future outcomes. Do not fit on the final split or generate outcome columns in bundles.

Stage writes in a run-specific untracked directory with atomic chunk rename and a checkpoint keyed by source/text/model/calibration hashes. Resume validates every completed chunk, deduplicates keys, and refuses incompatible runs. Finalize each symbol parquet in sorted order, then the manifest last; a partial directory is never a usable bundle. Publish via atomic rename on the same filesystem. One label worker only. Missing/invalid feature rows have explicit counts and reasons.

TDD: crash mid-chunk; safe resume; missing/duplicate/conflicting keys; source/model/prompt/calibration mismatch; path traversal; changed probability with unchanged row counts; corrupt parquet; byte and canonical hashes; late/future calibration; feature params mismatch; pure loader imports with torch/Laya unavailable. Use synthetic tiny fixtures.

Verification: group D and `test_labels.py`; uninterrupted vs resumed canonical rows exactly match. Commit implementation/tests only and compact label-manifest evidence with hashes, not parquet. Message: `research: persist immutable causal Laya label bundles`.

### Task 2 — Declare the optional v2 research parameters

Depends on 0c; can be built independently of Task 1. Modify `strategy_lab/hts_variants.py` for defaults/specs and add `strategy_lab/laya_research/tests/test_parameters.py` plus cross-field validators in `contracts.py`. Add a temporary explicit enabled-run rejection at the start of `run_candidate` in `strategy_lab/native_experiments.py`; Task 3 replaces it with the complete integration. An intermediate commit must never silently run an enabled candidate without its filter.

Declare the three keys in `HTS_V2_DEFAULTS` and `_V2_NEW_PARAMETER_SPECS` so `HTS_V2_FAMILY` and `HTS_V2_BASELINE` agree. Use the existing nullable-string `ParameterSpec` kind for bundle ID. Explicitly reject bool-as-float, NaN and infinity in the Laya contract: generic numeric range checks alone do not reject NaN. Check direct manually constructed `CandidateSpec` parameters before enabling the feature as well as `resolve_parameters` output.

No modification to `experiment_config.py` is needed: it supplies the generic types/resolver; the v2 specs live in `hts_variants.py`. No new global candidates or change to registry counts: the dedicated research runner creates named `CandidateSpec`s through the existing mechanism. V1 control and H/A candidate fingerprints stay fixed. Adding resolved defaults changes v2 fingerprints; record that metadata migration and invalidate old result reuse instead of hiding it in hashing.

TDD: default off; accepted declarations; undeclared keys still fail; invalid types/ranges/bundle combinations fail; frozen v1 IDs/parameters/fingerprint unchanged; existing v2 candidates remain supported; an enabled native invocation raises `UnsupportedCandidateError` before preparing inputs until Task 3 lands. Verification: `test_parameters.py` plus existing registry tests. Commit exact changed/new files. Message: `research: declare off-by-default Laya v2 eligibility parameters`.

### Task 3 — Load columns and gate only fresh native entries

Depends on Tasks 1–2. Modify only `strategy_lab/native_experiments.py` and add package tests `test_native_entry.py` and `test_native_parity.py`.

Implement the architecture's lazy enabled-only join, explicit label-root keyword and metadata handoff, fresh-entry predicate, complete `laya_entry_events`, deferred intent provenance, and payload digests. Bump `IMPLEMENTATION_REVISION` to a new unique research revision; never overwrite an old run directory. Add no dependency on Laya/torch, no runtime inference, no label generation, and no stochastic call. Ensure a direct enabled run validates the matching PASS gate/protocol/calibration identity before starting.

The entry predicate reads a row and returns a boolean/reason. It cannot take or mutate an order, position, cash balance or risk state. Record accepted and blocked opportunities once, including existing eligibility context, without relying on the first 50 general rejections. Stream full local evidence if large; keep counts/hash plus small samples in committed summaries. Early returns on the OFF path preserve existing event behavior.

TDD with native synthetic OHLCV fixtures and controlled stored labels:

- Param absent and OFF produce identical canonical orders/fills, cash/equity, existing diagnostics and metrics; missing label root is never touched.
- ON all-allow matches OFF trading events/equity exactly; new evidence metadata is the sole difference.
- Favorable/high p allows only an otherwise valid fresh entry; unfavorable/low p/missing label blocks it. Labels cannot override original trend/liquidity, risk-off, cooldown, slots, edge, price/ATR or budget checks.
- A held symbol's changed/missing label does not trigger a sell, change its stop, or alter target weights. Risk-off still cancels/flattens; protective exits remain effective; no blocked label suppresses risk actions.
- There is no replacement selection, weight renormalization or Laya sizing input; ranking and target-weight dictionaries remain identical given the same pre-decision state. Test the declared shared-budget consequence explicitly: a later entry may use newly unspent cash only through the existing allocator and original target. Pending buys are not canceled merely because a later label changes.
- Logical 15:00 capture and next-day flush use the original source-session approval even when the new label reverses. Stops/risk-off still preempt queued entries through existing code.
- Forward/future-dated rows, stale prior-session rows, duplicated labels and hash/schema errors are rejected according to the stated fail-closed policy.
- Network denied and inference imports forbidden; run with the ordinary venv where torch is absent. Include one actual `run_candidate` + native broker test, not only an object-constructed strategy unit test.

Verification: group E; compare a pre-change synthetic/control golden artifact captured in 0a with the OFF run, excluding candidate IDs, absolute paths, elapsed time and new provenance fields from canonical hashes, never economic fields. Commit engine diff/tests and small parity JSONL only. Message: `research: read stored Laya columns for fresh-entry eligibility only`.

### Task 4 — Bounded native A/B orchestration and complete trial accounting

Depends on Task 3. Create `scripts/laya_ab_compare.py`, `strategy_lab/laya_research/experiments.py`, and package `test_experiments.py`.

Use the process-per-config pattern from `scripts/search_v2_sharpe.py` / `scripts/run_hts_soxlfree.py`, but do not copy machine paths, winner stopping rules, broad grids or their worker counts. Resolve root from `__file__`. Default one worker, maximum two after measured memory headroom; spawn context, numerical-library threads one, and `max_tasks_per_child=1`. Never run inference concurrently with native trials. `_CONTEXT` makes threaded parallelism unsuitable.

Build full `CandidateSpec`s with `resolve_parameters` and `frozen_pairs`. Before starting, persist a trial ledger with every arm, cost, candidate fingerprint, source/bundle/protocol hash, date interval and planned output. A failed or timed-out run remains in that ledger and prevents a complete report; no first-success stopping.

Primary window: create a local `ExperimentWindow("laya_oos_v1", "2024-01-01", "2026-09-08")`; do not extend global `WINDOW_BY_LABEL`. Run continuously from identical cash; primary statistics use this uninterrupted trajectory. Also summarize its nonoverlapping half-year slices, with the final slice ending 2026-09-08. Reset-to-cash slice runs, if shown, are labeled supplementary and are not spliced into a claimed continuous return series.

Arms at primary cost: `LAYA_BASE_OFF`, `LAYA_BASE_ON`, `LAYA_BASE_PLACEBO`, and actual `HTS_CONTROL_1`. OFF and ON differ only in the three declared Laya settings and necessary identity metadata. Placebo uses a precomputed deterministic hash of `(seed, symbol, source_session)` compared with the 2023 gate acceptance rate; it has no market probabilities. Store it as a separately identified `bundle_kind="coverage_placebo"` diagnostic bundle and route it through the same entry filter, with 0/1 values encoding deny/allow. These values are not calibrated forecasts and must never enter calibration scores. The trial manifest permits this bundle only for its named placebo arm, requires the same PASS gate/source/schema, and excludes it from the claim of Laya inference provenance. Primary ON requires `bundle_kind="laya"` and the actual frozen model/calibrator. It matches expected opportunity coverage, not exact realized exposure. No random generator runs in native execution; label it clearly as a placebo. Synthetic all-allow fixtures are confined to tests and cannot be passed off as primary research bundles.

Repeat the paired OFF/ON/placebo arms at 7 and 15 bps per side as prespecified cost stresses. Keep the label question/calibrator/bundle fixed at the primary-cost target: never relabel to rescue a stress result. Adjust the resolved baseline cost explicitly for stress candidates and identify the existing payload's `cost_role="stress"`. Expected initial ledger: 10 continuous native jobs (4 primary, 3 per stress), plus explicitly named parity/reproduction checks. No candidate or threshold search.

Resume only when candidate parameters, implementation/code digest, source content, interval, costs, bundle/calibrator and completed-output hashes match. Refuse implicit reuse based on an old `run_result.json` existing. Parent process alone appends/fsyncs JSONL; worker completion order must not change final sorted summaries. Record RSS, failures, elapsed time and every job's native engine label.

Event parity has two separate meanings: exact trading-event equality for absent/OFF and OFF/all-allow; for real ON, prove the first economic divergence is an otherwise admissible entry blocked by its stored label. Thereafter different cash, holdings, fees, later trades and exits are expected. Audit each trajectory separately; do not require identical exits across diverged portfolios or describe later differences as automatic filter leakage.

Verification: group F and `test_experiments.py`; one-worker/two-worker canonical results match, interrupted resume matches uninterrupted, altered bundle/source/cost prevents reuse, and every planned job has a final status. Commit runner/tests plus compact trial manifest/summary. Message: `research: run fixed native Laya A-B and placebo comparisons`.

### Task 5 — Independent out-of-sample re-audit and decision

Depends on Task 4, but its mathematical test fixtures can be developed independently after Task 0. Create `strategy_lab/laya_research/audit.py`, `scripts/laya_audit.py`, and package `test_audit.py`.

Use a fresh process. Read frozen manifests, complete native orders/fills, cash/position snapshots, source bars and label evidence. Recompute the return target, calibration test and bundle alignment independently; do not call the gate's score function or the runner's summary formula to confirm them. Existing `experiment_validation.audit_run` remains a supplementary regression check, not the sole audit.

Reconstruct cash as initial cash plus net sell proceeds minus buys and both-side fees. Reconstruct inventory from actual fills; reconcile marked holdings/NAV at every available valuation sample, including open terminal positions and pending intents. Validate the native sample's actual mark/timestamp convention on synthetic traces before applying it to data. Never invent a close-out fill or substitute gross trade PnL for equity. If source data do not support a valuation, block qualification and report it rather than guessing.

The inspected `standard_metrics` and existing independent metric helper drop the initial cash-to-first-session return and omit initial cash from the running drawdown maximum. The latter also groups UTC date rather than exchange date. Preserve their output for compatibility, but add explicitly named **initial-capital-anchored audit metrics** in this research audit. Do not broaden this feature into an unrequested metric rewrite.

From the initial cash anchor E0 and every expected New York session's equity E1..En, calculate `r_t=E_t/E_(t-1)-1`, net total return, CAGR using actual elapsed dates from the start anchor, sample daily volatility, `sqrt(252)*mean(r)/std(r,ddof=1)` at zero risk-free rate, and MaxDD over E0..En. Undefined volatility/too few returns gives a documented null statistic, never a misleading winning score. Report fees, filled-order/round-trip counts, traded notional turnover, average gross exposure and holding time. Price-only archives and zero financing/dividends remain stated limitations.

Numerical checks: exact integer counts and IDs; probability finite/range/schema checks exact; recomputed float quantities use `rtol=1e-10, atol=1e-8` where serialized precision permits; cash/NAV reconciliation additionally requires absolute error <= $0.01. A tolerance failure cannot be waived by a similar Sharpe. Output both legacy-compatible metrics and anchored audit metrics with a reconciliation note explaining any difference.

For financial lift, use paired daily ON-minus-OFF returns and paired block bootstraps of the full return vectors (20-session blocks, 10,000 replicates, fixed seed). Report delta Sharpe, net return, CAGR, drawdown, turnover, exposure, and trade counts with uncertainty; show all half-year slices and paired placebo results. Recompute label calibration on the final data **for evaluation only**, never refit.

Preregister a conservative retrospective-support rule: all audits pass; >=100 filled entries in each main arm; at least five half-year slices with >=60 sessions; ON has higher total net return and a point delta anchored Sharpe >=0.10; the one-sided 95% lower bound for mean daily ON-minus-OFF return is positive; ON-minus-placebo mean return lower bound is positive; ON has positive lift in at least 60% of eligible half-year slices; MaxDD is no more than 2 percentage points worse; point net-return lift survives both cost stresses. These are fixed research bars, not optimized claims. Count every protocol/model/prompt/candidate/threshold tried; a later expansion needs a new selection/multiple-testing design.

Verdicts: `REJECT` for a failed statistical/economic rule; `INSUFFICIENT_EVIDENCE` for inadequate samples; `INVALID_EVIDENCE` for an integrity/parity/accounting failure; at most `RETROSPECTIVE_SUPPORT_PENDING_PROSPECTIVE` if every historical rule passes. Never call this a live edge or production qualification. Explain that insufficient power is not proof of zero effect.

TDD: first-session loss and initial drawdown; zero trades; open position at end; fills with fees and partial fills; duplicate events; missing mark; UTC-midnight/ET date; manual Sharpe/turnover/exposure examples; tampered label and provenance; false parity; bootstrap common-session resampling; final-data fit forbidden; all verdict branches. One independent fixture contains hand-calculated values, rather than using production formulas as expected answers.

Verification: group G and `test_audit.py`; an independent reviewer can recompute the compact daily equity/fee/position evidence with a calculator or a separate implementation. Commit audit code/tests plus final summary, HTML, small daily evidence and full gate-event digests/samples. Message: `research: independently audit Laya OOS evidence and record verdict`.

### Task 6 — Cold-start reproduction and research handoff

Depends on Task 5, or use a short rejection-only version if Task 0 stops. Complete `strategy_lab/LAYA_RESEARCH.md`, package test `test_reproduction.py`, and the CLI reproduction/verification subcommands specified below. Document exact invocation, artifact schemas, statuses, exclusions, private-cache reconstruction and result interpretation.

Review relevant `docs/` and `docsrc/` guidance; this adds no LumiBot public API. Keep the scoped engineering runbook in `strategy_lab/` and do not expand into public Sphinx/changelog/release edits. New runnable tests are explicitly invoked because existing `setup.cfg` discovers only `tests/`; no root test configuration or legacy tests need modification. Public functions have annotations and docstrings.

Reproduce one complete primary OFF/ON pair in a fresh output directory, with no model runtime available and network denied. Same stored label bytes and source bars must produce identical canonical trading/equity hashes and audited metrics. Demonstrate cache rebuild from the frozen source/model/calibration manifests separately; local licensed archives are prerequisites, so a public checkout alone cannot reproduce private market data. Test `--help` and the complete documented command sequence against synthetic data; run the actual cold reproduction when Task 0 passed.

Future prospective follow-up: freeze timestamp and all decisions now; collect at least 252 new complete archived sessions after both the protocol freeze and pinned model release, and at least 100 entries per main arm. Use the same offline-label/native-read workflow and frozen rules, with no live orders or automatic archive downloads. It is not available from the currently inspected historical window and is not something this implementation can claim to have completed. Record it as a separate future study, not an excuse to withhold the historical rejection/report.

Commit the runbook/test plus sanitized reproducibility JSON. Message: `docs: document reproducible Laya research and its qualification boundary`.

## Files-to-change

| Action | Exact paths | Owner / scope |
|---|---|---|
| Write now | `plans/codex_astra_laya_plan.md` | This planning deliverable only. |
| Modify after gate | `strategy_lab/hts_variants.py`, `strategy_lab/native_experiments.py` | v2 declarations; stored input join and fresh-entry predicate/evidence. |
| Create research modules | `strategy_lab/laya_research/__init__.py`, `contracts.py`, `snapshots.py`, `inference.py`, `calibration.py`, `labels.py`, `experiments.py`, `audit.py` under that package | Pure contracts/data/statistics separated from optional inference. |
| Create frozen protocol | `strategy_lab/laya_research/protocol_v1.json` | Exact dates, features/question, model/costs, numerical gate/rule settings and trial budget. |
| Create scripts | `scripts/laya_feasibility_probe.py`, `scripts/laya_label_features.py`, `scripts/laya_ab_compare.py`, `scripts/laya_audit.py` | Typed CLI entrypoints with no import-time actions; subprocess watchdog/chunking built into long commands. |
| Create dependency inputs | `scripts/requirements/laya-research.in`, `scripts/requirements/laya-research-macos-arm64-py311.lock` | Optional isolated inference environment only. |
| Create tests | `strategy_lab/laya_research/tests/test_snapshots.py`, `test_inference.py`, `test_calibration.py`, `test_labels.py`, `test_parameters.py`, `test_native_entry.py`, `test_native_parity.py`, `test_experiments.py`, `test_audit.py`, `test_reproduction.py` | New unit + integration tests remain within allowed code roots. |
| Create runbook | `strategy_lab/LAYA_RESEARCH.md` | Engineering handoff, not a public API/release document. |
| Small versioned evidence | `reports/laya_research_v1/protocol_manifest.json`, `technical_gate.json`, `gate.json`, `gate.html`, `gate_bins.jsonl`, `calibration.json`, `label_manifest.json`, `parity.jsonl`, `trial_manifest.json`, `summary.json`, `results.html`, `daily_evidence.jsonl`, `entry_evidence_sample.jsonl`, `reproduction.json` | Commit only applicable artifacts; JSON schema records unrun stages and failed attempts honestly. |
| Local, never staged | `.venv-laya/`, `short/laya_models/`, `short/laya_work/`, `short/laya_labels/`, `short/laya_native_runs/` | Model/dependency/data caches, per-symbol labels, complete raw native run files and full event journals. |

Read-only references: `feature_store.py`, `experiment_config.py`, `experiment_validation.py`, `experiment_universes.py`, existing search/native runners, native tests, and the prior draft. No edits to `tests/`, `lumibot/`, `live/`, raw archive files, repository instruction files, public docs, global dependencies or release/version files.

Use repo-relative paths and placeholders in tracked files. Recursively sanitize generated nested payloads; third-party settings/errors may contain host paths. Never copy the machine-specific path found in a search precedent. Do not remove unrelated leaks during this planning task; report their presence and keep new output clean. Use explicit staging lists, never `git add .` or `git add -A`. No committed per-config `run_*.parquet`/CSV, model weights, label parquet, full downloaded data, `short/` caches, or `live/` logs.

## Tests

All new tests run without model downloads; actual installed-model feasibility is an explicit separate gate. New tests do not inherit `tests/conftest.py`, so their fixtures must set existing backtest/dotenv controls before importing the native engine and ensure no live stream/network starts. Do not invent a skip environment variable.

| Verification layer | Required evidence |
|---|---|
| Causality | Mutating data after source close leaves earlier feature/text/raw prediction unchanged; fit mapping uses only its allowed past; expected prior session required. |
| Financial target | Exact source-clock prices, fees, split purge and hand-calculated net sign agree. |
| Predictive validity | Base-rate/constant cannot pass; calibrated proper scores beat null with time-block uncertainty; simple numeric comparator reported. |
| Storage | Hash/schema/date/lookback identity enforced; crash-safe resume; missing blocks entry and corrupt data abort. |
| Parameters | v1 unchanged; v2 declared/default off; invalid type/NaN/infinite or enabled without a valid bundle fails. |
| Native isolation | No torch/Laya/network; OFF is a no-op; ON all-allow has exact economic parity. |
| Entry-only behavior | Held, pending, stop, global risk and deferred timing fixtures; no ranking/weight mutation. |
| Audit | Independent cash/inventory/NAV/fees, initial-capital-anchored returns and metric arithmetic, terminal treatment and uncertainty. |
| Reproducibility | Same frozen bundle/data gives same canonical outputs across fresh processes, worker counts and resume; changed inputs invalidate reuse. |

Existing regression command: `python -m pytest tests/strategy_lab -q` using `.venv/bin/python`, alongside explicit new-test paths. Run targeted tests after each unit and the full strategy-lab subset before the integration and final commits. Do not claim the entire LumiBot package test suite was run; no package release is part of this research task. If an old regression fails, inspect its history and repair the scoped implementation rather than weakening the test.

## Risks/tradeoffs

- **Rejection is likely.** Domain calibration cannot manufacture predictive information, and a constant forecast can have excellent ECE. Proper scoring against meaningful nulls is the first gate.
- **Historical selection and pretrained knowledge.** Existing six-year/two-year/2022–24 windows and v2 folds overlap and have been researched. Call the final split "held out from this Laya fit, retrospective"; do not call it untouched. The pinned model was released after the last proposed historical session, and its training cutoff is unknown. Removing names/dates reduces obvious leakage, not all contamination.
- **Forecast horizon versus strategy horizon.** One-session entry-clock returns make the question falsifiable but do not encode HTS stops or holding duration. Task 0 can reject a model that might help another hypothesis; do not change targets to save it. Actual edge requires native A/B.
- **No direct sizing change does not imply identical portfolio exposure.** A rejected entry leaves cash and changes future economics. Placebo, turnover, fees and exposure help distinguish predictive gain from reduced activity.
- **Data reliability.** Daily labels are date keys; timezone conversion can shift them incorrectly. Raw deduplication, corporate actions, pre-inception IBIT data, early closes, fixed-universe survivorship and unverified volume coverage can invalidate a study. Fail closed and preserve the archive; this plan does not authorize a broad data repair.
- **Finite memory and changing SDKs.** Pin wheels and model files, one CPU model worker, bounded native workers, actual RSS/latency measurements, and no Router preload. A published GPU benchmark is not an M4 measurement.
- **Output reproducibility.** Frozen parquet gives deterministic backtests even if regenerating model outputs on a different backend would differ. Require canonical equality on the qualified reference runtime; any other backend produces a new artifact identity and needs qualification.
- **Statistical power and trials.** Session-block uncertainty is conservative with correlated ETFs and limited regimes. All exploratory changes count; if intervals are wide, report insufficient evidence instead of expanding a hidden search.
- **Audit independence.** Existing "independent" helpers share important conventions with the runner. Recompute initial-day arithmetic and valuations explicitly; passing the same formula twice is not independent verification.
- **Documentation scope.** The requested lowercase plan filename is an explicit exception to the general uppercase-doc convention. Future runbook uses uppercase. The research-only implementation stays within the named roots and adds no public LumiBot contract.

## Reproducible reproduction commands

The new commands below are **contracts to implement**, not commands that exist or were executed during planning. Run from repo root after implementation authorization, in order, and stop on any nonzero gate result. CLI tests must cover these exact forms. All paths are relative; binaries/caches stay local. Existing commands are identified separately.

### Existing read-only preflight

```bash
git branch --show-current
git status --porcelain=v1
.venv/bin/python --version
.venv/bin/python -c 'import importlib.util; print({x: importlib.util.find_spec(x) is not None for x in ("torch", "laya", "duckdb", "pyarrow", "scipy", "pytest")})'
```

At planning time: Python 3.11.15; torch/Laya absent; DuckDB, PyArrow, SciPy and pytest present. Neither `bin/safe-timeout` nor a `timeout`/`gtimeout` executable was found. For future commands use the following shell function, which supervises a process group with the existing Python interpreter; it creates no helper file outside `scripts/`:

```bash
bounded() {
  .venv/bin/python -c '
import os, signal, subprocess, sys
p = subprocess.Popen(sys.argv[1:], start_new_session=True)
try:
    result = p.wait(timeout=1200)
except subprocess.TimeoutExpired:
    os.killpg(p.pid, signal.SIGTERM)
    try:
        p.wait(timeout=10)
    except subprocess.TimeoutExpired:
        os.killpg(p.pid, signal.SIGKILL)
        p.wait()
    result = 124
raise SystemExit(result)
' "$@"
}
```

Commands use existing `IS_BACKTESTING=true` and `LUMIBOT_DISABLE_DOTENV=true` where the native stack is imported. No Theta/downloader API is needed: this study reads local DuckDB only. Missing archives produce a data failure, not an automatic download.

### A — Task 0a: freeze, unit tests, source export

```bash
bounded env IS_BACKTESTING=true LUMIBOT_DISABLE_DOTENV=true .venv/bin/python -m pytest strategy_lab/laya_research/tests/test_snapshots.py -q
bounded env IS_BACKTESTING=true LUMIBOT_DISABLE_DOTENV=true .venv/bin/python scripts/laya_feasibility_probe.py freeze --protocol strategy_lab/laya_research/protocol_v1.json --out reports/laya_research_v1/protocol_manifest.json
bounded env IS_BACKTESTING=true LUMIBOT_DISABLE_DOTENV=true .venv/bin/python scripts/laya_feasibility_probe.py export --protocol reports/laya_research_v1/protocol_manifest.json --roles train calibration gate --out short/laya_work/gate
```

The `freeze` operation resolves read-only archive/model metadata and records the exact protocol. It also captures a small native synthetic OFF golden fixture in local work and its compact canonical digest. Its output is reviewed/committed before prediction or outcome-gate inspection. Future hashes must not be invented in this plan.

### B — Task 0b: optional model environment and technical gate

```bash
.venv/bin/python -m venv .venv-laya
bounded .venv-laya/bin/python -m pip install 'pip-tools==7.5.1'
bounded .venv-laya/bin/python -m piptools compile --generate-hashes --output-file scripts/requirements/laya-research-macos-arm64-py311.lock scripts/requirements/laya-research.in
bounded .venv-laya/bin/python -m pip install --require-hashes -r scripts/requirements/laya-research-macos-arm64-py311.lock
bounded .venv-laya/bin/python -m pytest strategy_lab/laya_research/tests/test_inference.py -q
bounded .venv-laya/bin/python scripts/laya_feasibility_probe.py download --protocol reports/laya_research_v1/protocol_manifest.json --model-root short/laya_models
bounded .venv-laya/bin/python scripts/laya_feasibility_probe.py technical --protocol reports/laya_research_v1/protocol_manifest.json --model-root short/laya_models --snapshots short/laya_work/gate/snapshots.parquet --device cpu --sample-count 300 --out reports/laya_research_v1/technical_gate.json
```

For reproduction, install the committed hash lock directly; do not re-run dependency resolution. The only allowed network stages are explicit package installation and `download`; the latter applies the exact repository revision and allow-list above. `technical` denies sockets during the offline load/inference check. If a tooling pin is unavailable or incompatible, stop and record it; do not silently upgrade.

### C — Task 0c: score in chunks, fit once, hard gate

```bash
bounded .venv/bin/python -m pytest strategy_lab/laya_research/tests/test_calibration.py -q
bounded .venv-laya/bin/python scripts/laya_feasibility_probe.py score --protocol reports/laya_research_v1/protocol_manifest.json --technical-gate reports/laya_research_v1/technical_gate.json --model-root short/laya_models --snapshots short/laya_work/gate/snapshots.parquet --out short/laya_work/gate/predictions --max-seconds 900 --resume
bounded .venv/bin/python scripts/laya_feasibility_probe.py gate --protocol reports/laya_research_v1/protocol_manifest.json --predictions short/laya_work/gate/predictions --outcomes short/laya_work/gate/outcomes.parquet --features short/laya_work/gate/snapshots.parquet --out reports/laya_research_v1
```

`score --max-seconds 900 --resume` completes an atomic chunk, emits `complete=false` if work remains, and may be repeated without altering predictions. `gate` refuses partial prediction sets. `gate` writes the calibrated mapping, verdict JSON, HTML and bin evidence, and exits nonzero unless PASS. The next commands independently require that PASS with matching hashes; shell continuation cannot bypass it.

### D — Task 1: build the final-period label bundle

```bash
bounded .venv/bin/python -m pytest strategy_lab/laya_research/tests/test_labels.py -q
bounded env IS_BACKTESTING=true LUMIBOT_DISABLE_DOTENV=true .venv/bin/python scripts/laya_label_features.py export --protocol reports/laya_research_v1/protocol_manifest.json --gate reports/laya_research_v1/gate.json --role final --out short/laya_work/final
bounded .venv-laya/bin/python scripts/laya_label_features.py score --protocol reports/laya_research_v1/protocol_manifest.json --gate reports/laya_research_v1/gate.json --calibration reports/laya_research_v1/calibration.json --model-root short/laya_models --snapshots short/laya_work/final/snapshots.parquet --staging short/laya_work/final/labels --max-seconds 900 --resume
bounded .venv/bin/python scripts/laya_label_features.py finalize --staging short/laya_work/final/labels --label-root short/laya_labels --manifest-out reports/laya_research_v1/label_manifest.json
bounded .venv/bin/python scripts/laya_label_features.py verify --manifest reports/laya_research_v1/label_manifest.json --label-root short/laya_labels
```

Repeat the score command until complete; finalize refuses missing chunks. It also creates the prespecified diagnostic placebo bundle from the frozen gate acceptance rate. The compact manifest identifies both bundles without needing a manually pasted hash or an absolute path.

### E — Tasks 2–3: parameter and native-parity tests

```bash
bounded env IS_BACKTESTING=true LUMIBOT_DISABLE_DOTENV=true .venv/bin/python -m pytest strategy_lab/laya_research/tests/test_parameters.py strategy_lab/laya_research/tests/test_native_entry.py strategy_lab/laya_research/tests/test_native_parity.py -q
bounded env IS_BACKTESTING=true LUMIBOT_DISABLE_DOTENV=true .venv/bin/python -m pytest tests/strategy_lab -q
bounded env IS_BACKTESTING=true LUMIBOT_DISABLE_DOTENV=true .venv/bin/python scripts/list_strategy_experiments.py --verify
```

### F — Task 4: fixed native comparisons

```bash
bounded .venv/bin/python -m pytest strategy_lab/laya_research/tests/test_experiments.py -q
bounded env IS_BACKTESTING=true LUMIBOT_DISABLE_DOTENV=true .venv/bin/python scripts/laya_ab_compare.py plan --protocol reports/laya_research_v1/protocol_manifest.json --gate reports/laya_research_v1/gate.json --labels reports/laya_research_v1/label_manifest.json --out reports/laya_research_v1/trial_manifest.json
bounded env IS_BACKTESTING=true LUMIBOT_DISABLE_DOTENV=true .venv/bin/python scripts/laya_ab_compare.py run --manifest reports/laya_research_v1/trial_manifest.json --label-root short/laya_labels --run-root short/laya_native_runs/v1 --workers 1 --max-jobs 1 --resume
```

Repeat the run command until all 10 planned jobs have succeeded or have a recorded failure. Each job is bounded; if one cannot finish in 20 minutes, stop and diagnose/obtain a justified longer acceptance-run budget rather than silently resetting the portfolio into shorter windows. Timing out does not count as a research rejection.

### G — Tasks 5–6: independent audit and cold reproduction

```bash
bounded .venv/bin/python -m pytest strategy_lab/laya_research/tests/test_audit.py strategy_lab/laya_research/tests/test_reproduction.py -q
bounded env IS_BACKTESTING=true LUMIBOT_DISABLE_DOTENV=true .venv/bin/python scripts/laya_audit.py --manifest reports/laya_research_v1/trial_manifest.json --run-root short/laya_native_runs/v1 --label-root short/laya_labels --out reports/laya_research_v1
bounded env IS_BACKTESTING=true LUMIBOT_DISABLE_DOTENV=true .venv/bin/python scripts/laya_ab_compare.py reproduce --manifest reports/laya_research_v1/trial_manifest.json --arms LAYA_BASE_OFF LAYA_BASE_ON --label-root short/laya_labels --run-root short/laya_native_runs/reproduction --workers 1 --max-jobs 1 --deny-network --resume
bounded .venv/bin/python scripts/laya_audit.py compare --manifest reports/laya_research_v1/trial_manifest.json --original short/laya_native_runs/v1 --reproduction short/laya_native_runs/reproduction --out reports/laya_research_v1/reproduction.json
bounded env IS_BACKTESTING=true LUMIBOT_DISABLE_DOTENV=true .venv/bin/python -m pytest strategy_lab/laya_research/tests tests/strategy_lab -q
git diff --check
```

Repeat `reproduce` once per primary arm; comparison rejects missing runs and excludes only nonsemantic metadata. The audit positional/default mode and `compare` subcommand must have parser tests for these exact invocations. A valid statistical REJECT is a successful audit computation with a rejected research verdict; corrupt/incomplete evidence exits nonzero.

Before each future task commit: inspect `git status --porcelain=v1`, read the task's exact diff, inspect generated evidence for sensitive values and large raw files, explicitly stage only its listed files, run `git diff --cached --check` and inspect `git diff --cached --stat`, then commit with the task message. Do not create branches, change version, publish, or deploy. The implementation report must list completed/failed/unrun stages, exact tests and commands actually executed, and the experiment verdict.

**TL;DR:** Prove local feasibility and predictive calibration first. If the frozen forecast cannot beat nulls and a simple numeric model, stop with a documented rejection. Otherwise persist causal labels, add one optional fresh-entry check to native HTS, and independently audit fixed historical A/B results without claiming unearned prospective edge.
