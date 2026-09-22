# terra: fix the paper-six hourly live runner per sol review

sol reviewed /Users/openclaw/.hermes/profiles/free_money_glitch/oracle_live_strategies/run_paper_six.py
and returned CHANGES REQUIRED. Do NOT load any launchd agents. Do NOT place
orders. Fix the runner + manifest + verifier + launchd script per the numbered
findings below, then verify with the dry construction and add focused tests.

WORKING CHECKOUT: /Users/openclaw/.hermes/profiles/free_money_glitch/home/lumibot/
Python: /Users/openclaw/.hermes/profiles/free_money_glitch/home/lumibot/.venv/bin/python
LumiBot CORE is READ-ONLY (never edit lumibot/, tests/backtest baselines, or
release artifacts). You MAY edit strategy_lab/ (only if required), and
oracle_live_strategies/{run_paper_six.py, paper_six_manifest.py,
verify_paper_six.py, register_paper_six_launchd.sh}. Prefer adding NEW helper
modules over editing strategy_lab/native_experiments.py (the shared backtest
core must stay byte-stable).
Keys: in profile .env as ALPACA_P6_{A..F}_API_KEY/_SECRET (verified ACTIVE paper,
$50K). NEVER print secret values or unmasked account numbers.

## sol's findings to fix (address EACH, item number + fix)

1. FAIL-CLOSED SAFETY (P0): do the ALPACA_IS_PAPER guard BEFORE any LumiBot
   import. `--dry` must not start broker websocket/order threads (use only
   `TradingClient(..., paper=True)` for account check, no lumibot Alpaca()).
   Enforce BEFORE live execution: status==ACTIVE, check_supported empty,
   endpoint is paper, and the six accounts are distinct.

2. CADENCE / LOOK-AHEAD (P0) — the live hourly extension is broken:
   - Remove the unsupported `futures_contract=None` kwarg (it makes every
     get_historical_prices raise TypeError, silently swallowed to "no bars").
   - Normalize the fetched index to NAIVE America/New_York BEFORE filtering and
     merging (archive is naive; Alpaca returns aware). Must actually install the
     normalized index on the df used for the merge.
   - Backfill: the seed ends ~2026-09-08 hourly / 2026-09-10 daily. Fetching
     length=1 cannot bridge the gap. Batch-fetch + backfill every MISSING
     completed hourly AND daily bar from the live source, then derive daily /
     benchmark / breadth / sessions from the refreshed data (do NOT leave daily
     frozen — ranking/gates read it).
   - Data-finalization policy: AlpacaData delays ~16 min and may return an
     incomplete top-of-hour bar. Only merge bars that are genuinely complete
     (bar label < floor(now)-1h by the data-delay policy). Refuse/flag iter if
     required symbols or expected timestamps are missing (never trade on a stale
     set silently).

3. SIGNAL REUSE: freeze the resolved 37-parameter hash (sha256 over sorted
   canonical params) + the native implementation revision and assert/version it
   at runtime; snapshot the executable price ONCE per iteration so the same
   quote drives sizing/risk/eligibility consistently; log the live-price + data
   provenance adaptation at startup (requirement, not just a docstring).

4. POSITION / ORDER PATH (P0) — restart-safe for live:
   - On initialize, RECONCILE broker positions and open orders; re-register any
     held long (entry price/hold-days) and any live protective stop so a
     KeepAlive restart can never orphan a position or duplicate an entry.
   - Fail closed on unexplained broker state.
   - Persist strategy state (ATR/trail/stop) across restarts via spec.state_path.
   - Handle partial fills, cancellations, rejections (fail closed, don't
     double-submit).
   - Use deterministic client_order_id; wait for protective-stop cancellation
     confirmation before submitting a competing exit (avoid cancel/fill race).
   NOTE: native_experiments.py initialize() clears _positions/pending at every
   start — if reusing it, override initialize() in LiveHtsStrategy to add the
   live reconcile AFTER the base init (do not edit native_experiments).

5. IMPORT ORDER (P0): set IS_BACKTESTING=false and
   LUMIBOT_DISABLE_DOTENV=true with EXPLICIT assignment before imports (not
   setdefault). Do not let import construct a default broker; there must be
   exactly ONE broker per live process (the one owned by the live Trader), and
   only that one has streams enabled. Lazy-load credentials; never build an
   Alpaca() just for a dry check.

6. SECRETS: verify_paper_six.py must not print raw SDK exception text (log only
   exception type + HTTP status + sanitized reason).

7. RESOURCE / DUPLICATION (P0): one broker per live process; per-strategy
   process lock (pidfile) so a manual run can't duplicate a launchd agent;
   prohibit --all if any launchd instance is active; validate --only against the
   allowed names; the launchd script must validate names and cap/rotate logs.

8. DRY GATE: the credential-free local construction must print, for every one of
   the six, `{name, catalog, universe, symbols, params_count, soxl_present,
   check_supported_missing}`, and the account check (when creds resolve) must
   confirm ACTIVE/paper/distinct before any live path.

## Verification you must run (report everything)
1. Import the module with the venv python (no syntax errors).
2. `ALPACA_IS_PAPER=true` + `--dry` for all six via the venv python — expect
   distinct ACTIVE paper accounts, correct universe sizes (U0=57, U0_EX_SOXL=56),
   check_supported_missing==[] , and NO lumibot websocket broker thread spawned
   during dry (confirm no streams open).
3. Add focused unit tests under tests/ (oracle_live_strategies side) covering:
   stale-gap backfill, completed-bar boundary (label == now.floor('h')-1h is the
   newest merged), restart-with-position reconcile, partial/cancel order
   handling, duplicate-process rejection via pidfile, account uniqueness.
   Run them with pytest -c pytest-glitch.ini or the venv pytest.
4. Confirm NOTHING under lumibot/ changed (git diff --name-only lumibot/ empty).

Do not place orders. Do not load launchd. Return exact file paths changed, the
dry verification output for all six, test results, and a short "ready to launch
or still blocked" statement.