# Laya Offline-Feature Toolchain for Native HTS Backtests — Build Prompt

> **Driver:** Codex Astra CLI (codex 0.154.0)
> **Target repo:** `~/.../home/lumibot` on `version/4.5.92/clean-base`
> **Goal:** Honestly determine whether a Laya-generated regime label adds after-cost,
> out-of-sample edge to a native HTS backtest — WITHOUT putting Laya inside the signal
> or risk path at runtime.

---

## 1. Objective (why this exists)

We are evaluating a hypothesis, not shipping a production strategy:

> A TypeSafe-style "System One" decision model (Laya, open-source 421M Apache-2.0
> clone of Jev) can classify each symbol's pre-decision state into a regime label with
> meaningful calibrated confidence, and that label improves risk-adjusted
> out-of-sample return when used as a **precomputed, deterministic eligibility filter**
> in the native LumiBot HTS backtest.

The design constraint that makes this defensible (and auditable):

1. **Laya runs OFFLINE.** It consumes a deterministic text snapshot of the same
   causal pre-bar features the strategy reads, and emits a label + calibrated
   probability + confidence. Labels are stored in a per-symbol parquet keyed by
   session timestamp.
2. **The backtest only reads stored labels.** The native LumiBot engine loads the
   label column as an ordinary input feature. Nothing stochastic runs inside the
   signal or risk path. The backtest is fully deterministic and reproducible.
3. **Laya never gates orders or risk.** It is an optional *entry-eligibility* filter,
   additive to existing mechanisms, off by default.
4. **We measure edge honestly.** The same candidate runs with the Laya filter OFF and
   ON on identical windows/control, compared event-level and out-of-sample. If it does
   not beat the no-Laya baseline after costs, we report it as a rejected hypothesis.

This is a research bet with **zero prior evidence for Laya on market text**. Step 0 is
a hard feasibility/calibration gate with explicit kill criteria. Do not skip it.

---

## 2. Current context (ground truth from the workspace)

- Native engine and run entry point:
  - `strategy_lab/native_experiments.py`
    - `run_candidate(candidate, window, out_dir, *, control_baseline, tearsheet_dir=None)`
      at line ~1948.
    - `WINDOW_BY_LABEL: dict[str, ExperimentWindow]` at line 235.
    - `prepare_inputs(params, window)`, `RunContext`, `RegistryHtsStrategy`, `_run_engine`.
  - Data frames load via `_sql_frames(...)` from local DuckDB archives in
    `short/` (e.g. `suite_v2_xnas_itch_daily_adjusted.duckdb`, `..._daily_raw`,
    `..._hourly`, `..._hourly_adjusted`). Symbols/start/end come from the window.
- Deterministic causal feature library (the pre-bar state we snapshot):
  - `strategy_lab/feature_store.py`
    - `daily_feature_frame(frame, *, trend_sma, return_period, liquidity_period,
      benchmark_close=None)` → columns: close, ret1, high, low, open, volume, sma,
      ret, mdv, r10, r20, r60, r120, r252, vol20, vol60, vol120, ddvol20, reg_slope,
      reg_r2, reg60, eff20, skip5, res20.
    - Causality contract: "A feature at session S uses only bars that have completed
      by S's close. The strategy always reads the last completed session before the
      decision session." **The Laya label must match this same alignment** so no future
      data leaks into a decision.
- Strategy mechanism surface:
  - `strategy_lab/hts_variants.py` — `HTS_V2_BASELINE` (dict) + `HTS_V2_FAMILY`;
    new v2 params must be declared here so `check_supported` does not reject them.
  - `strategy_lab/experiment_config.py` — `CandidateSpec`, `resolve_parameters`,
    `frozen_pairs`; the v2 ParameterSpec/known-map for params.
- Discovery/search precedent (parallel, process-per-config, native engine):
  - `scripts/search_v2_sharpe.py`, `scripts/search_v2_sharpe_wave2.py`, `..._wave3.py`
    (each has `build_candidates()` returning `(cid, CandidateSpec)`; streams to JSONL).
  - `scripts/run_hts_soxlfree.py`, `scripts/run_soxlfree_extrawindows.py`.
- Evidence conventions (from git history): commit summary JSONs + html + small
  evidence JSONLs; never per-config `run_*.parquet/csv` dumps; never `short/` caches or
  `live/` logs.

### Environment / blockers to verify in Step 0

- `.venv/bin/python` is 3.11.15 and **does not have torch** (`ModuleNotFoundError`).
- `laya` (`pip install laya`) pulls weights from HuggingFace Hub on first load; 421M
  base + a 322M multilingual. On M4/16GB this is a real memory consideration.
- Laya docs warn: input budget 512 tokens/question; states longer are truncated;
  arithmetic/counting/date logic should stay in deterministic code. Keep the derived
  state in Python; Laya only classifies.

---

## 3. Architecture

```
DuckDB archive (short/*.duckdb)
        │  daily OHLCV frames (per symbol, per window)
        ▼
feature_store.daily_feature_frame()   ← deterministic, causal
        ▼  numeric pre-bar state
[laya_label_features.py]  builds a compact TEXT state row per (symbol, session)
        ▼  one typed question: noul "favorable long regime?" (+ optional choice)
Laya (offline, frozen weights, argmax choice, fixed seed)
        ▼  label + calibrated probability + confidence
per-symbol parquet:  (ts, symbol, laya_label, laya_prob, laya_conf)
        │  stored in reports/laya_labels/<window>/<symbol>.parquet
        ▼  loaded by prepare_inputs → joined into inputs as extra column
Native HTS backtest (run_candidate / RegistryHtsStrategy)
        ▼  optional eligibility filter off-by-default: laya_filter, laya_min_conf
run_result.json / run_stats / run_trades
        ▼
A/B comparison: OFF vs ON, same window/control, event-level + OOS
```

Nothing stochastic enters the engine. The label parquet is the single source of truth
for the backtest and keeps it reproducible.

---

## 4. Step-by-step tasks

> TDD where code is produced. Each task ends with a commit. Exact paths and commands.
> The engine reads labels keyed by the session the strategy would decide **on**; verify
> alignment explicitly (same as the causality contract above).

### Task 0 — Feasibility / calibration gate (HARD GATE, kill criteria)

**Objective:** Prove Laya installs, runs on this Mac, and can calibrate on our data
distribution before any engine work. If it cannot, stop and report rejected.

**Files:** none tracked; throwaway probe under `scripts/laya_feasibility_probe.py`.

**Steps:**
1. Create a throwaway venv or confirm `pip install laya` works against a python that
   has torch. Read `laya` quickstart (HF: `convaiinnovations/laya`).
2. Load the model. Measure load time, RAM (`ps -o rss`), and single-query latency
   (median of ~50).
3. Build N=300 labeled probe states from real daily features on 2–3 symbols. For each,
   ask `noul("Does this pre-decision state indicate a favorable regime for long
   exposure next session?")` and a `choice` over [`long_favorable`, `neutral`,
   `long_unfavorable`]. Record probabilities + confidence.
4. Calibration check: for a label threshold on `noul`, compute the empirical next-
   session forward return sign hit-rate vs the predicted probability across bins
   (Loess or equal-count deciles). Report ECE / hit-rate-by-bin.
5. Latency scale check: if ~0.5s+ per row on CPU, note that labeling the full catalog
   is a batch job with a persistence checkpoint, not an in-loop cost.

**Verify / kill criteria (write results to `reports/laya_feasibility_<date>.json`):**
- If install or model load fails on this machine → STOP, report FAILED (blocker).
- If calibration is not meaningfully better than the null (no monotone relationship
  between predicted probability and empirical hit-rate) → STOP, report hypothesis
  rejected on calibration grounds before touching the engine.

**Commit:** the probe script + feasibility JSON only.

### Task 1 — Read and document the exact integration points

**Objective:** Make the plan implementable by mapping the real code before writing.

**Files (read only):**
- `strategy_lab/native_experiments.py` (prepare_inputs, RunContext, RegistryHtsStrategy,
  RegistryHtsStrategy.on_trading_iteration, qizzy eligibility checks, `_run_engine`).
- `strategy_lab/hts_variants.py` (HTS_V2_BASELINE keys, HTS_V2_FAMILY).
- `strategy_lab/experiment_config.py` (ParameterSpec, known map, resolve_parameters).
- `strategy_lab/experiment_validation.py` + one existing runner script for the run/verify
  pattern.

**Output:** a short `docs/laya_offline_feature_INTEGRATION.md` (UPPERCASE, dated header)
listing exactly how a new input column reaches the strategy's eligibility decision and
where the OFF-by-default filter slots in, with line refs.

**Verify:** doc compiles to nothing (markdown); commit.

### Task 2 — Labeler: snapshot + offline Laya classification

**Objective:** Produce the deterministic per-symbol label parquets.

**Files:**
- Create: `strategy_lab/laya_features.py` (public, typed, documented)
  - `state_to_text(state: dict) -> str` — compact 512-token-safe text from the numeric
    pre-bar features (e.g. "close 104.2, r20 +0.031, vol20 0.19, eff20 0.42, sma_bias
    above, mdv 1.2e7, reg_r2 0.31 ..."). Log-appropriate scaled values; no raw dump.
  - `laya_decision(state_text) -> LayaDecision` dataclass(label, prob, conf) — frozen
    weights, argmax on choice, fixed seed; call once per row.
- Create: `scripts/laya_label_features.py`
  - Loads daily OHLCV per symbol from a chosen window via the same
    `_sql_frames`/DuckDB path the engine uses.
  - Compute `feature_store.daily_feature_frame(...)`; build state text; run Laya;
    persist `reports/laya_labels/<window>/<symbol>.parquet` with columns
    `(ts, symbol, laya_label, laya_prob, laya_conf)`.
  - **Checkpoint**: write incrementally; resume on repeat ids. Batch, do not stream
    into the engine.
  - **Alignment guard**: a unit test asserting the label at row `S` uses only data on
    or before `S`'s completed features (mirror the causality contract). See Task 4.

**Step (TDD):** write `tests/laya/test_state_to_text.py` and a determinism test
(same input → same label byte-for-byte across two loads) first; then implement.

**Verify:** `pytest tests/laya/ -v` GREEN; parse one parquet; alignment test GREEN.

### Task 3 — Engine: expose the label as an OFF-by-default eligibility feature

**Objective:** let a candidate opt into the Laya filter without widening the frozen v1.

**Files:**
- Modify `strategy_lab/hts_variants.py`: add v2-only declared param(s), e.g.
  `laya_filter` (bool, default False) and `laya_min_conf` (float, default 0.6), in the
  HTS_V2_* surface (modeled on existing v2 params like risk_off_gate) so
  `check_supported` accepts them.
- Modify `strategy_lab/experiment_config.py`: add to v2 ParameterSpec/known map.
- Modify `strategy_lab/native_experiments.py` `prepare_inputs`: when
  `params["laya_filter"]` is True, load `reports/laya_labels/<window>/<symbol>.parquet`
  and join label/prob/conf into the symbol frames aligned to the decision session
  timestamp. When False (default), inputs are unchanged — **backwards compatible**.
- Modify `RegistryHtsStrategy` eligibility: when `laya_filter` is enabled, require
  `label == long_favorable AND conf >= laya_min_conf` as an additional entry condition
  **only**; never used for exits or sizing; fails closed (no label in window → no entry).

**Tests:**
- `pytest tests/laya/test_engine_filter.py`:
  1. OFF path byte-identical metrics vs the same candidate with the param absent
     (prove no-op).
  2. ON path: a fixture label (long_favorable, conf high) turns on entries; a
     (long_unfavorable / low conf) label blocks entry; missing label blocks entry.
  3. Causality: a label only affects decisions at/after its timestamp (no lookahead).

**Verify:** targeted tests GREEN; existing v1 suite still GREEN (no regression).

### Task 4 — A/B qualification on identical windows

**Objective:** measure whether Laya adds after-cost, OOS edge vs the no-Laya control.

**Files:** create `scripts/laya_ab_compare.py` (modeled on search_v2_sharpe.py,
process-per-config, streams JSONL).

**Steps:**
1. Set the window(s): six_year + the walk-forward / 2022-24 OOS block
   (`WINDOW_BY_LABEL`).
2. For each of a small nominated set of top-20/family candidates: run with
   `laya_filter=False` (control) and `laya_filter=True` (with a couple of
   `laya_min_conf` thresholds), same window, same starting cash, same cost model.
3. Output JSONL with per-run: total_return, CAGR, daily-return Sharpe (not the
   engine's CAGR/vol alias — recompute per our rule), max_drawdown, turnover,
   exposure, fills, n_days.
4. Event-level parity: compare order/fill streams between OFF and ON for the same
   candidate to confirm the ONLY delta is Laya-eligible entries.

**Verify:** no engine crashes; event-level diff isolated; metrics recorded.

### Task 5 — Independent audit + honest headline

**Objective:** recompute numbers from raw trades/cash, out-of-sample, and decide.

**Files:** `scripts/laya_audit.py` (or extend existing audit).

**Steps:**
1. Recompute total return, CAGR, daily vol, daily-return Sharpe, MaxDD from
   `run_trades`/cash/equity samples (our standard independent recheck). Reject the
   engine's CAGR/vol "Sharpe" as the sole number.
2. Report control vs Laya ON for every candidate, per period, with OOS clearly
   separated from IS.
3. Multiple-testing / selection-bias note: state how many thresholds/settings were
   tried and the total trial count.
4. Verdict in `reports/laya_ab_conclusion_<date>.md` (UPPERCASE, dated): PASS
   (consistent OOS lift after costs), PARTIAL, or REJECTED (no reliable lift).

**Verify:** arithmetic double-checked; artifacts committed per convention (JSONLs +
conclusion md + html; no run_*.parquet/csv dumps).

### Task 6 — Documentation + reproducibility record

**Objective:** make the toolchain re-runnable.

**Files:**
- `docs/laya_offline_feature_ARCHITECTURE.md` + refactor the Task 1 INTEGRATION doc into
  the runbook.
- `docsrc/` touch only if a public user-facing surface changed (it should not — this is
  strategy_lab research, not public LumiBot API).
- Record in `CHANGELOG.md` only if a shared/lumibot core file changed (it should not —
  ALL changes are in strategy_lab/scripts/reports). If none, say so explicitly).

**Verify:** a fresh checkout + one command re-runs an existing candidate with the stored
labels and reproduces run_result.json within tolerance.

---

## 5. Files likely to change (summary)

- Create: `strategy_lab/laya_features.py`, `scripts/laya_label_features.py`,
  `scripts/laya_ab_compare.py`, `scripts/laya_audit.py`,
  `scripts/laya_feasibility_probe.py`
- Create: `tests/laya/test_state_to_text.py`, `tests/laya/test_engine_filter.py`
- Modify: `strategy_lab/native_experiments.py` (prepare_inputs + strategy eligibility)
- Modify: `strategy_lab/hts_variants.py`, `strategy_lab/experiment_config.py`
- Create: `docs/laya_offline_feature_INTEGRATION.md`,
  `docs/laya_offline_feature_ARCHITECTURE.md`, `reports/laya_ab_conclusion_<date>.md`
- Data (untracked, repo convention): `reports/laya_labels/<window>/<symbol>.parquet`

## 6. Tests / validation

- `pytest tests/laya/ -v` (state_to_text determinism, engine filter OFF no-op + ON
  gating + causality).
- Existing v1/v2 suite must stay GREEN (regression gate).
- Independent arithmetic recheck (Task 5) — never trust the engine's Sharpe as sole
  evidence.
- Alignment/causality assertion on labels (no lookahead).

## 7. Risks / tradeoffs / open questions

- **Laya on market text is unproven.** Step 0 gate is the acceptance bar; expect a
  likely REJECT. That is a successful outcome if reported honestly.
- **CPU inference cost** on M4/16GB for full catalog → checkpointed batch, off-line.
- **512-token input cap** → compact state text; keep arithmetic in Python.
- **Network/weights** → one explicit, documented download on first load; never silent.
- **torch absent in venv** → install into the research venv (not lumibot core deps).
- **Never in the risk path** → eligibility filter only, off by default, fails closed.
- **Public-repo hygiene** → all code in strategy_lab/scripts; no new LumiBot core API;
  no secrets; no local absolute paths in tracked docs (use the repo-root-relative
  convention already used by sibling plans, or `<PROFILE_LUMIBOT>` placeholder).

## 8. Reproducible reproduction

From repo root with `.venv/bin/python`:

```bash
# Step 0 (gate)
.venv/bin/python scripts/laya_feasibility_probe.py
# Step 2 (labels, checkpointed)
.venv/bin/python scripts/laya_label_features.py  # per window
# Step 4/5 (A/B + audit)
.venv/bin/python scripts/laya_ab_compare.py
.venv/bin/python scripts/laya_audit.py
# Tests
.venv/bin/python -m pytest tests/laya/ -v
```

Every task commits independently. No push/deploy/release without explicit approval.