# SOL — plan v2 HTS: new configurable parameters + 100-variant matrix on v1 winners

You are `sol`, the read-only planner/reviewer for the FreeMoneyGlitch HTS trading research. You PLAN ONLY — do not edit files. Write your plan to `plans/hts_v2_plan.md`.

## Mission
Design a **v2 parameter surface** that attacks the three measured v1 failure modes, plus the exact **100-variation matrix** layered on the v1 best performers. This plan will be executed by `terra` on the native LumiBot engine.

## Hard constraints (do not violate)
- HTS runs ONLY on the native LumiBot engine (`lumibot.strategies.Strategy.run_backtest + BacktestingBroker`). No custom replay.
- Every parameter a v2 candidate changes MUST be declared by a `RuleFamily` in `strategy_lab/experiment_config.py` / `hts_variants.py` — the registry enforces "no unregistered override" (see `validate_registry`). New knobs require new `ParameterSpec`s and a new v2 family (or extend the v1 families).
- Fingerprints must stay unique. Candidate IDs: propose a scheme — either `V001`..`V100` (new kind `hts-v2`) OR `H101`..`H200`. Price the tradeoff and pick ONE.
- Keep `EXECUTION_ENGINE` label as the native engine. Add an `IMPLEMENTATION_REVISION` for v2 and note it in run artifacts.
- The v2 catalog must be registry-validated: unique IDs, slugs, fingerprints; every override declared by its family; control untouched.
- Do NOT modify LumiBot core. All changes live in `strategy_lab/` + `scripts/` (our harness + registry).
- Cost convention stays 3.5 bps/side; clock-hour 09:00-15:00 ET bars; recompute daily-return Sharpe (engine `sharpe` field is CAGR/vol).

## The v1 failure modes the v2 parameters MUST attack (from real trade-level data)
1. **No durable edge — overfit to lucky winners.** 4/6 forward folds went negative; 62-97% of net PnL came from top-3 positions; median per-position PnL ~$0. → Need params that force *breadth of return* and *robust selection* (fewer, more distinct families; risk-normalized sizing; wider/coarser stops; selection on "positive most folds" not peak Sharpe).
2. **Cost drag.** 7-14% of gross burned on 2,000-3,000 fills; dead at 15 bps/side. → Need turnover-reduction params: wider ATR stops, re-entry cooldown enforcement, minimum-expected-trade-profit filter before entry (skip if expected move < N×cost), executing only on the last actionable bar.
3. **Concentration / drawdown.** H022/H027/H095 -43% to -57% maxDD. → Need a **global risk-off cash gate** (absolute-momentum / breadth gate flattens the whole book to cash, not just per-asset screen), and **per-position risk-contribution capping** (risk-normalized sizing so no single winner dominates).

## v1 best performers to layer v2 on (six-year daily Sharpe, native engine, clock-hour, 3.5bps)
- H100 1.095 (+323%, -27%): resting 2-ATR stops + 60-session 20% vol-target
- H027 1.059 (+1084%, -56%): ATR trail close-1.5xATR(28), hourly trigger
- A09 1.049 (+56%, -10%): SMA200 monthly filter + cov equal-risk long-only
- H022 0.986 (+859%, -57%): ATR trail close-1.5xATR(7)
- H095 0.973 (+525%, -43%): leverage cap 25% NAV
- control 0.749 (+365%, -61%)
(Note: all are DISCOVERY numbers; walk-forward selected track was Sharpe -0.49. v2 must be held to the same honest walk-forward gate.)

## What your plan MUST deliver
1. **New ParameterSpec list** (name, kind, min/max/allowed, description, default) for the v2 surface. At minimum target these, but refine/adjust based on what's already in `HTS_BASELINE`:
   - `risk_off_gate` (enum: none / spy-sma100 / spy-sma200 / qqq-sma100 / breadth-50 / breadth-60) = GLOBAL cash switch across the whole book
   - `risk_off_cooldown_bars` (int) = min bars in risk-off before re-risk
   - `risk_contribution_cap` (float 0..1) = max per-position RISK contribution (weight × ATR/dist) as NAV fraction
   - `weight_mode_v2` extension OR new `min_trade_edge_bps` (float) = skip entry unless expected move >= N bps (turnover cut)
   - `min_position_holding_bars` (int) = don't churn out under N bars
   - `atr_stop_multiplier` reuse of existing atr_k but wider defaults (e.g. 3-4) to cut stop-outs
   - any others you think are principled — but keep the set SMALL and distinct (avoid a 100-way Cartesian overfit trap).
2. **Which v1 best performers form the base** and how the new params combine onto each (the "layer on top of previous best performers from v1" requirement).
3. The **exact 100-variation matrix**: each candidate ID, name, family, and its full override dict. Prefer a principled grid (few knobs × a few values on a few bases) over a wild Cartesian explosion. Gate the number of *parameter combinations tested* — report it and add a multiple-testing note.
4. **Where the new params plug into the native strategy**: identify the sourcing point in `strategy_lab/native_experiments.py` (`RegistryHtsStrategy`), the sizing function, and the market-gate function — name them so terra can wire them. Read those files before writing the wiring notes.
5. **Tests** terra must add (unit + registry-validation + at least one before/after control to prove each new param is wired, not dead).
6. **Honest walk-forward plan** for v2 so we keep the same evidence bar.

## Read before planning
- `strategy_lab/experiment_config.py`, `strategy_lab/hts_variants.py`, `strategy_lab/experiment_registry.py`, `strategy_lab/native_experiments.py`, `strategy_lab/hts_v1_core.py`, `strategy_lab/hts_policies.py`, `strategy_lab/experiment_validation.py`
- `docs/HTS_NATIVE_RESULTS.md`, `docs/investigations/2026-09-15_HOURLY_CONVENTION.md`
- `reports/hts_rebaseline_2026-09-15/suite_summary.json` and `walk_forward_report.json`

## Report format
Write `plans/hts_v2_plan.md` with: objective, constraints honored, new parameter table, base-performers + combination rule, 100-variant matrix (exact overrides), wiring map (file:function), test plan, walk-forward plan, multiple-testing disclosure, and a clear list of the parameter-combinations count.

Do NOT edit any source file. Output path: `plans/hts_v2_plan.md`.