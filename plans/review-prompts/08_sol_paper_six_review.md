# sol review: hourly HTS live/paper runner (6 strategies)

Review the live/paper deployment described below. This is PRE-LIVE: the
launchd agents are NOT loaded yet and no orders have been placed. Your verdict
unblocks (or blocks) a real paper-trading launch. Focus on P0 safety and
correctness. Sandbox: read-only is fine for a code review; re-run the dry
construction yourself with the venv python if you can.

## Files to review
- /Users/openclaw/.hermes/profiles/free_money_glitch/oracle_live_strategies/run_paper_six.py
  (the hourly live runner — THE key deliverable)
- /Users/openclaw/.hermes/profiles/free_money_glitch/oracle_live_strategies/paper_six_manifest.py
  (frozen config for the six strategies)
- /Users/openclaw/.hermes/profiles/free_money_glitch/oracle_live_strategies/verify_paper_six.py
  (account/paper check; already run: all six ACTIVE paper accounts, $50K each)
- /Users/openclaw/.hermes/profiles/free_money_glitch/oracle_live_strategies/register_paper_six_launchd.sh
  (launchd agent generator)
- Reference (READ-ONLY, do not edit): the signal core it reuses
  /Users/openclaw/.hermes/profiles/free_money_glitch/home/lumibot/strategy_lab/native_experiments.py
  class RegistryHtsStrategy + prepare_inputs + check_supported + _canonical_hourly_frame
  and /Users/openclaw/.hermes/profiles/free_money_glitch/home/lumibot/strategy_lab/feature_store.py

## What the runner does
Six hourly HTS strategies (w0007/w0006/w0018/s159/s158 U0; s159b = S159 with
SOXL removed, U0_EX_SOXL), each on its OWN dedicated Alpaca PAPER account
(keys in the profile .env as ALPACA_P6_{A..F}_API_KEY/_SECRET, already verified
ACTIVE paper, $50K each — never print secret values). Select: --only NAME,
--all (spawn child process per strategy), --dry (construct+validate, NO orders).

The strategy class is LiveHtsStrategy(NX.RegistryHtsStrategy): it reuses the
proven native signal/order engine verbatim and only (a) refreshes the raw
hourly frames with COMPLETED broker bars via get_historical_prices before each
iteration, and (b) overrides _execution_price to use the live last price
(because the backtest's "fill at bar open" proxy is irreproducible live — this
adaptation MUST be explicitly acknowledged/logged).

## Reviewer's specific checklist (answer each)
1. FAIL-CLOSED SAFETY: On ALPACA_IS_PAPER != 'true', does it refuse to run?
   Does --dry place any order? Is the broker forced to paper-api.alpaca.markets?
2. CADENCE/look-ahead: bar labelled T known at T+1h; decisions use last
   completed bar only. Is the live bar-extension causal (never uses the current
   uncompleted bar)? Does it avoid using any future bar?
3. CORRECTNESS of signal reuse: does LiveHtsStrategy actually call
   super().on_trading_iteration() unmodified? Any place the override changes
   signal behavior vs the backtested RegistryHtsStrategy?
4. POSITION/ORDER path: does the native core's order submission work through
   this live broker? Any double-order, orphan-stop, or no-drop risk introduced
   by the live extension on every iteration?
5. IMPORT ORDER: IS_BACKTESTING=false set before importing native_experiments
   (which setdefaults it 'true')? Is LUMIBOT_DISABLE_DOTENV handling right for
   live?
6. SECRETS: any place a key/secret or unmasked account number could print?
7. RESOURCE: 6 launchd agents, KeepAlive, one Trader per strategy — any obvious
   duplication/runaway risk (e.g. --all spawning against launchd, or --only
   picking up the wrong process)?
8. Anything that must be fixed BEFORE launch vs tolerable-to-log.

Run the dry construction yourself and report its output for at least two
strategies (e.g. w0007 and s159b) to confirm: params resolve, universe size,
check_supported empty, broker paper. Command:
  cd /Users/openclaw/.hermes/profiles/free_money_glitch/oracle_live_strategies
  ALPACA_IS_PAPER=true /Users/openclaw/.hermes/profiles/free_money_glitch/home/lumibot/.venv/bin/python run_paper_six.py --dry

## Output format
Verdict: APPROVED or CHANGES REQUIRED, then a numbered list mapping each
checklist item to a finding (OK / issue + file:line + fix). End with TL;DR.
Do not print secret values. Do not place orders.