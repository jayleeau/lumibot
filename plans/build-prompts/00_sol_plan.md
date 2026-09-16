# SOL PLANNING TASK — HTS hourly-convention freeze + hourly-path alignment + commit

You are Sol, the architect. THINK first, plan precisely, write the plan to `.Codex/plans/hts_hourly_convention.md`. DO NOT implement code — that is Terra's job.

## Working context (verified facts, do not re-derive)
- Repo: `/Users/openclaw/.hermes/profiles/free_money_glitch/home/lumibot` on branch `version/4.5.92/clean-base` (share branch, multi-agent).
- Python: `.venv/bin/python`. Tests: `.venv/bin/python -m pytest -c pytest-glitch.ini` (and glitch_tests/).
- A LIVE paper fleet runs from this host (launchd `ai.glitch.live.*`, `live/*.py`). NEVER modify/restart/unload/kickstart any `ai.glitch.live.*` service; NEVER edit `live/common.py`, `live/strategies.py`, any `*_trader*.py`, NEVER kill trader processes. Work only in allowed dirs. The US market is often OPEN — keep any backtest re-runs to the minimal set (single control run), never the full ~1,900-job suite during RTH.
- LumiBot framework core (`lumibot/`) is READ-ONLY (upstream upstream=Lumiwealth). The HTS harness lives in `strategy_lab/`, `scripts/`, `docs/`, `tests/` — that is OUR research code and is the change target.

## The three work items the plan must cover (in order)

### Item 1 — Commit today's completed HTS walk-forward work
The following are uncommitted on the branch (they are OUR harness, correct, and already validated this session — the walk-forward evaluator, cost-stress, session audit, and page builder):
- `strategy_lab/native_experiments.py` (MODIFIED — adds f1..f6 × innerA/innerB/test walk-forward windows to `WINDOW_BY_LABEL` + `_add_months` helper)
- `scripts/evaluate_walk_forward.py` (NEW — walk-forward selection + stitching evaluator)
- `scripts/build_walk_forward_page.py` (NEW — sortable HTML report)
- `scripts/stress_walk_forward_costs.py` (NEW — Phase-6 cost stress at 7/15 bps)
- `scripts/audit_hts_session_cleanliness.py` (NEW — session-cleanliness audit vs NYSE calendar)
- `reports/hts_walkforward_2026-09-14/` + `reports/hts_wf_cost_stress/` + `reports/hts_session_cleanliness_audit.json` (evidence artifacts — determine whether to commit; do NOT commit the raw `short/` DBN/data caches or logs, and be careful with `reports/full_lumibot_*` scratch dirs which pre-exist and are NOT ours)
Commit message should clearly describe the walk-forward suite + hourly-convention groundwork. Do NOT push (push only with explicit user authorization). Do NOT commit unrelated pre-existing scratch artifacts.

### Item 2 — Freeze the hourly/execution convention into the strategy contract (documentation, the P1 fix)
Write a durable, dated engineering doc under `docs/investigations/` (uppercase, date-first: `2026-09-15_HOURLY_CONVENTION.md`) and cross-link it from `docs/HTS_100_VARIATIONS_RESEARCH_PLAN.md` and any relevant handoff. The doc MUST pin down, verbatim and unambiguously, the decisions reached this session:
1. **Hourly bar convention = clock-hour 09:00→15:00 ET** (matching live Alpaca `1H` bars). Bars are anchored to whole clock hours.
2. **Actionability timing:** a bar at time T contains T→T+1h and is only known at T+1h. Therefore:
   - **14:00 bar = the last same-day-actionable bar** (its close is known 15:00, fills can still occur before 16:00 close).
   - **15:00 bar close is known at 16:00 (after close)** → any order it triggers fills at NEXT session open = overnight gap-exposed. This must be labelled and treated as gap-exposed, never an instant/guaranteed fill.
3. **Stop semantics: the stop is a TRIGGER, not a price guarantee.** At 16:00 a breached stop only QUEUES a market exit; it fills at the next session's open whatever that open is. NEVER fill at the stop price; NEVER claim the bar's intraday low as a fill (retrospective/look-ahead). Realized loss = entry → next-open, not entry → stop. Stop gaps must be measured/reported separately.
4. **Latched exits:** once a stop breaches at the close and the exit is queued, it is UNCONDITIONAL. Do not cancel/re-open the exit because the next morning "recovered" — that makes stops discretionary. (The only legitimate alternative is a separate, explicitly documented weaker contract where the stop re-evaluates at the next executable bar.)
5. **Re-entry after a stop = configurable cooldown.** New parameter `reentry_cooldown_bars`: `-1` or `0` = re-enter at any time; `N > 0` = the stopped symbol waits N completed hourly bars (sessions) before re-qualifying for entry. Note the existing registry machinery H088/H089/H090 already test 1/3/5-session cooldowns.
6. **Fixed-pool rotation:** sells free cash + the slot BEFORE buys size; buys only spend cash/buying-power that sells actually freed. Two-slot 99.5%-NAV-divided-by-two model. Never fund a new slot's full notional from an unfilled/failing sell (the "capital triple-counted" bug).
7. **Session-cleanliness caveat:** the archives carry premarket (04:00–09:30 ET) and post-market (16:00–20:00 ET) bars. Audit evidence: `reports/hts_session_cleanliness_audit.json` — the 15-min archive is only ~64% regular-session. A regular-session strategy must explicitly filter to 09:30–16:00 ET; A10's opening range needs genuine 09:30–10:00 one-minute bars.
The doc should also note WHERE each convention is enforced (file/method) or, if not yet enforced, that it is contract-first and which harness change in Item 3 wires it.

### Item 3 — Align the backtest hourly path to clock-hour bars (the code change)
Currently `strategy_lab/native_experiments.py: _lumibot_hourly()` RELABELS archived clock-hour bars onto NYSE half-hour timestamps (archive 09:00→09:30, ..., 15:00→15:30). The agreed convention (Item 2) is clock-hour 09:00→15:00 so backtest bars MATCH live Alpaca `1H` bars. Plan the change that:
- Stops the 09:30 relabel; keeps bars at whole clock hours 09:00–15:00 (identically what live Alpaca delivers), OR otherwise makes backtest bar anchoring match the live convention exactly.
- Keeps causality (decisions only on completed bars; the 15:00→next-open semantics preserved).
- Updates `hour_mapping_convention` and any related noise in the payload/docs to the new convention.
- Adds/updates a deterministic unit test for the mapping.
- Proves it: re-run the audited control (`HTS_CONTROL_1`) over `six_year` on the native engine BOTH before-commit (current relabel) and after the change, so we can state the parity/impact of dropping the relabel. Keep re-runs minimal during open market (single control run is fine; do NOT run the ~1,900-job suite during RTH).

## What to produce
A precise, ordered plan (steps, each with: exact file(s) + method(s) to change, why, and how to verify). Include the Item-1 commit staging decisions (what to include/exclude, exact message), the Item-2 doc structure, and the Item-3 code/task/test/parity-verification steps. Flag risks (e.g. dropping the relabel changes ALL historical hourly-based results; the plan must state that re-running affected configs is required and how, but NOT do it during market hours). Give Terra a safe, scoped, verify-each-step plan.

Write the plan to `.Codex/plans/hts_hourly_convention.md`. Planning only — no code changes.