# SOL — verify the time-decisions are wired into the code (read-only, focused)

The user made explicit timing decisions. Verify the HTS native harness at `/Users/openclaw/.hermes/profiles/free_money_glitch/home/lumibot` actually IMPLEMENTS them, and report whether any code update is required. Read-only. Do not modify files, do not touch `ai.glitch.live.*`, `live/*.py`, `*_trader*.py`, or `lumibot/`. Market may be open — do not run backtests.

## The decided time-convention (must all hold)
1. Hourly bars are **clock-hour 09:00 through 15:00 ET** (matching live Alpaca `1H` bars), anchored to whole clock hours.
2. A bar labelled T contains `[T, T+1h)` and is only KNOWN at T+1h. Therefore:
   - the **14:00 bar** (14:00-15:00) is the **last same-day-actionable** bar (its close is known at 15:00, fills can still happen before the 16:00 close);
   - the **15:00 bar** (15:00-16:00) close is known at **16:00 (after close)** -> any order it triggers fills at the **next session's open** = overnight gap-exposed; never an instant/guaranteed fill.
3. **Stop = trigger, not price guarantee.** At a breached completed close, queue a market exit; it fills at the next session's open whatever it is. Never fill at the stop, never use the triggering bar's low. Realized loss = entry -> next-open.
4. **Latched exits:** once queued, a stop exit is unconditional.
5. **Re-entry cooldown** `reentry_cooldown_bars`: -1/0 = anytime; N>0 = N completed sessions. H088/H089/H090 = 1/3/5.
6. **Fixed-pool rotation:** sells free cash+slot BEFORE buys size; buys only spend freed cash.
7. Extended-hours (pre 04:00-09:30, post 16:00-20:00 ET) rows are excluded from BOTH the strategy feature math and the broker LumiBot Data feed (canonical 09:00-15:00 frame).

## Where to check (but verify with actual reads)
- `strategy_lab/native_experiments.py`: `_canonical_hourly_frame`, `_lumibot_hourly`, `prepare_inputs`, `_hourly_features`, `_completed_row`, `_execution_price`, `_update_risk`, `_submit_sell`, `on_filled_order`, `_rebalance`, `_cooldowns`, `build_payload`, `HTS_HOURLY_CONVENTION`, `IMPLEMENTATION_REVISION`.
- `strategy_lab/hts_variants.py`: `reentry_cooldown_bars` spec + baseline + H088/H089/H090 overrides; confirm NO residual `stop_cooldown_sessions`.
- `tests/strategy_lab/test_native_experiments.py`: the causality + cooldown + stop-gap tests.
- `docs/investigations/2026-09-15_HOURLY_CONVENTION.md`: contract vs code parity.

## Give verdict
- `CONFIRMED` if all seven decisions are correctly implemented (cite file:line for each of the 7).
- `UPDATE REQUIRED` otherwise, listing the exact file:line + minimal change for each unmet decision.
- List anything you cannot verify read-only. Write to `plans/review-prompts/review3_sol_times.md` if writable, else inline. Keep it evidence-based, not assertions.