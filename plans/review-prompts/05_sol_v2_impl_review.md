# SOL — review terra's v2 HTS implementation

You are `sol` (read-only reviewer). Terra has implemented the v2 HTS build from
`plans/hts_v2_plan.md`. Review the implementation against the plan. Write your verdict to
`plans/review-prompts/05_sol_v2_impl_review.md`.

## Your job
Independently verify (read-only, no source edits) that terra did what the plan requires and did
NOT do these forbidden things:
1. Modify any LumiBot core file under `lumibot/`.
2. Change/break the v1 catalog (H001-H100, A01-A10, HTS_CONTROL_1) — fingerprints and resolved
   parameter maps MUST be identical to git HEAD.
3. Invent parameters or variant combos not in the plan's 100-variation matrix.
4. Introduce the custom replay or any non-native execution path.
5. Weaken or skip the honest walk-forward gate or selection-lock mechanism.
6. Bypass the registry's "no unregistered override" rejection.

## What to verify (each = specific evidence)
- `git status` / `git diff --stat`: confirm only `strategy_lab/`, `scripts/`, `tests/strategy_lab/`
  changed; `git diff --name-only lumibot/` is empty.
- Registry: `python scripts/list_strategy_experiments.py --verify` reports exactly
  `211 configurations (1 control, 100 hts, 100 hts-v2, 10 alternative)`.
- v1 parity: run the registry, confirm every H001-H100/A01-A10/HTS_CONTROL_1 fingerprint equals
  the value from `git HEAD` (e.g. by exporting HEAD to a temp dir and diffing).
- The V001-V100 matrix in code matches the plan's override dictionaries exactly (spot-check at
  least V001, V019, V020, V021, V039, V079, V081, V100; verify the plan's matrix SHA is embedded
  and enforced).
- The 5 new parameters are wired: search `native_experiments.py` for risk-off gate/state, risk-
  contribution cap in `_target_weights`, entry-edge in `_rebalance`, minimum-hold in `_select`,
  and `resting-stop-atr` stop alias; confirm each is reachable and not dead code.
- Tests: run `python -m pytest tests/strategy_lab/ -q` and report the pass count; confirm the
  before/after wiring controls and parent-parity hashes exist in the test files.
- Walk-forward: confirm `scripts/evaluate_walk_forward.py` has the six-block discovery + parent
  champions + selection-lock-before-reveal logic; confirm `scripts/stress_walk_forward_costs.py`
  reruns only frozen selections.
- Secret hygiene: grep the diff for credentials, account numbers, private paths, or API keys.

## Verification commands you MAY run (read-only)
git status, git diff, python registry queries, pytest (tests only, no backtests), grep/search.
Do NOT run backtests. Do NOT edit files.

## Report format (write to plans/review-prompts/05_sol_v2_impl_review.md)
- Verdict line: APPROVED or CHANGES REQUIRED (with a numbered blocker list).
- For each check above: PASS / FAIL + one-line evidence (file:line or command output).
- Any deviation found and whether it's acceptable.
- Secret-hygiene result.
Do NOT edit any source file. Output: plans/review-prompts/05_sol_v2_impl_review.md