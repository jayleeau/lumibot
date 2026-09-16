# TERRA — implement v2 HTS (parameters + V001-V100 registry) on the native engine

Repo: `/Users/openclaw/.hermes/profiles/free_money_glitch/home/lumibot` (branch `version/4.5.92/clean-base`).

You are `terra` (implementation agent). Implement EXACTLY the plan in `plans/hts_v2_plan.md`.
Read that plan in full FIRST. Do not improvise the parameter surface or the variant matrix —
if something in the plan is impossible, STOP and report it in `plans/build-prompts/TERRA_V2_RESULT.md`
rather than substituting your own design.

## Hard constraints (non-negotiable)
- HTS runs ONLY on the native LumiBot engine (`lumibot.strategies.Strategy.run_backtest` +
  `BacktestingBroker`). Never introduce or revive the custom replay.
- Do NOT modify LumiBot core (`lumibot/**`). All changes live in `strategy_lab/` and `scripts/`
  and `tests/strategy_lab/`.
- The v1 registry must remain valid: `validate_registry` requires exactly H001-H100, A01-A10,
  and the untouched control. If you add a v2 kind/catalog, keep the v1 catalog intact and add
  the v2 catalog alongside it (do not renumber or repurpose H001-H100).
- Every new parameter a v2 candidate sets MUST be declared by a v2 RuleFamily `ParameterSpec`,
  so `validate_registry`'s "no unregistered override" rule still holds for the v2 catalog.
- Fingerprints must stay unique across the whole (v1+v2) catalog.
- Cost convention stays 3.5 bps/side; clock-hour 09:00-15:00 ET bars; the daily-return Sharpe
  is recomputed independently (the engine `sharpe` field is CAGR/vol — never quote it as Sharpe).
- Do NOT commit or push. Leave the tree for review. Do not create branches.
- Set an explicit `IMPLEMENTATION_REVISION` / contract id for v2 and surface it in run artifacts.

## Deliverables
1. New parameters (as specified in the plan) declared as `ParameterSpec`s + a v2 RuleFamily
   (or families), with the plan's exact names/kinds/ranges/defaults.
2. The V001-V100 catalog built from the plan's exact override table (do not invent combos).
3. Wiring into `strategy_lab/native_experiments.py` (`RegistryHtsStrategy`): the risk-off gate,
   risk-contribution cap, entry-edge hurdle, minimum-holding-period, and any reuse of existing
   knobs (atr_k / top_n / reentry_cooldown_bars / rebalance_hour) — name each call site.
4. Tests: registry validation for the v2 catalog + a before/after control proving EACH new
   parameter is actually wired (not dead). Follow the plan's test list.
5. A runner path to execute the whole v2 catalog (reuse `scripts/run_hts_experiments.py` if the
   plan says so; else add a minimal v2 runner). Windows: the plan's descriptive windows +
   walk-forward folds.
6. Write `plans/build-prompts/TERRA_V2_RESULT.md`: files changed, exact commit-free summary,
   the test command(s) and result, and any deviation from the plan.

## Verification before you report done
- `python -m pytest tests/strategy_lab/ -q` (all green) — report the exact count.
- `python scripts/list_strategy_experiments.py --verify` (or the v2 equivalent) passes.
- A smoke run of ONE v2 candidate through the native engine produces a valid result artifact
  (report the window, runtime, and headline numbers as a smoke test ONLY — not a qualification).

Report the smoke-test numbers as SMOKE TEST ONLY. Do not claim qualification.