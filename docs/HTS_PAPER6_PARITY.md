# Title: HTS paper6 Decision Persistence and Parity Harness

Description: The strategy-owned decision snapshot schema, atomic restart state,
event-stream serializer, and offline replay verifier that make paper6 evidence
replayable and joinable across restarts.

Last Updated: 2026-09-23

Status: In-repo strategy contract qualified offline; the real production
serializer output is consumed end-to-end by the CLI
(`tests/strategy_lab/test_hts_parity.py::test_production_serializer_output_is_accepted_by_cli_end_to_end`).
External live-runner wiring in `oracle_live_strategies/` and controlled
six-bot evidence remain **pending, separate, and unauthorized here**; no fleet
persistence, state files, or audit directories exist yet.

Audience: Strategy owner and strategy developers

## Overview

The six `paper6` HTS bots execute through the shared native strategy core
(`strategy_lab/native_experiments.py::RegistryHtsStrategy`). Before this change
their decision inputs, order lineage, and stop state existed only in memory and
were lost with the process. This contract makes every rebalance decision produce
a full, self-contained, replayable snapshot from the real `_rebalance()` path
before broker submission, including deferred normal and risk-off decisions. It
also keeps each typed order-lifecycle event joinable through a restart. The
serializer output is accepted end-to-end by the verifier CLI, and the six-bot
live runner still has to be wired to call the writer.

The selection/allocation policies and cumulative-fill calculation are unchanged.
The strategy layer now suppresses a broker submission when its decision is
already committed, including sell, risk-off, reactive, and deferred paths.
Persistence is **fail-open**: a persistence exception is recorded as a sanitized
diagnostic and never blocks `create_order()`/`submit_order()`.

## Ownership and scope

- All code lives in the strategy-owned layer: `strategy_lab/`, `tests/strategy_lab/`,
  and `scripts/verify_paper_six_parity.py`.
- No edits under `lumibot/`; no broker-adapter or framework changes.
- No new environment variables; no credentials, account numbers, or absolute
  paths in tracked files.

## Schemas

### Decision snapshot (`schema_version: 1`)

Built by `strategy_lab.hts_audit.build_decision_snapshot(...)` and validated by
`validate_decision_snapshot(...)`. Required sections: `identity`, `time`,
`selection`, `allocation`, `account`, `book`, `planned_orders`, `parameters`,
`fills`, `protective_stop`. An `outcome`/`outcome_reason` pair lets no-op,
pending-exit, risk-off, and deferred decisions carry an explicit outcome instead
of fabricated allocation inputs. Validation rejects non-finite ATR/prices/weights,
a malformed or policy-conditional-missing covariance matrix, a missing mandatory
held-symbol list, a missing full quote snapshot, a missing weight stage, an
invalid timezone, a planned order without a budget trail, and unknown schema
versions, each with the exact offending field path.

The real `RegistryHtsStrategy._rebalance()` captures the selection and allocation
inputs at the moment they are consumed, plans stable intent IDs, appends one full
pre-submission snapshot, and checkpoints it before entering the broker submission
phase. `build_strategy_event_payload` copies those snapshots verbatim (never
stubs).

### Event payload (`schema_version: 2`)

`build_strategy_event_payload(strategy, session=...)` serializes the existing
in-memory streams — `_journal`, `_risk_cap_events`, `_lifecycle_trace`,
`_stop_gap_events`, `_entry_edge_events`, `_rejections`, `_diag`,
`_risk_off_transitions`, `_deferred_rebalance_events`, `_session_end_events` —
plus the full `decisions` and the `open_orders` lineage. Every event gets a
stable content-derived `event_id` that includes `event_time` and
`event_sequence`, so equal-sized repeated fills cannot collapse.
`merge_session_events(...)` merges full snapshots by `identity.decision_id`,
deduplicates events by id, and raises on conflicting snapshots with the same ID
rather than silently keeping one.

### Restart state (`schema_version: 2`)

`serialize_runtime_state(strategy, reason=...)` emits a compact checkpoint:
identity, timezone-aware `saved_at`, `checkpoint_reason`, `event_sequence`,
`decision_sequence`, `intent_sequence`, `active_decision_id`,
`last_decision_id`, `processed_decision_ids`, `signal_day`, `selected`, `ranks`,
`positions`, `pending_buys`, pending-sell **lineage objects**, `stop_exit_context`,
`protective_orders`, `cooldowns`, risk-off active/deadline/last-gate state,
`deferred_rebalance` (with its decision ID and one-way `submission_started`
gate), and a structured `last_quote_snapshot`. Every pending buy, pending sell,
and protective order has explicit symbol, side, local/broker order, decision,
intent, quantity, and fill lineage appropriate to that order type. Raw `Order`
objects are never serialized; on restore, stored identifiers are rebound to
fresh broker order objects through the existing public order accessors. v1 state
is rejected, not silently migrated.

## Atomic storage

`AtomicJsonStore` validates a complete payload (`write_state`/`write_session`)
before opening a temp file, so an invalid replacement leaves the previous
destination byte-for-byte unchanged. The atomic write uses a same-directory temp
file with mode `0600`, flushes and `fsync()`s it, `os.replace()`s the
destination, and `fsync()`s the parent directory where supported.
`read_state(expected_identity=...)` rejects a wrong strategy, parameter hash,
feature hash, or schema version before any strategy mutation; `read_session()`
runs session-schema validation before use.

## Restart reconciliation

`RegistryHtsStrategy._restore_persisted_state()` loads, strictly validates, and
then applies state, so an incomplete checkpoint returns `False` without mutating
any in-memory structure. `_apply_runtime_state(...)` restores event/decision/intent
sequences, active/last decision, deferred and risk-off state, pending-buy and
pending-sell lineage, protective lineage, and the quote snapshot.
`_reconcile_broker_state_live(...)` treats broker positions and active-order
status as the only authority: a persisted position absent at the broker is
removed and flagged (`reconcile_position_dropped`), a quantity mismatch is
replaced by broker truth and recorded, broker-only positions are registered as
`legacy_unverifiable`, and pending buys, pending sells, and protective orders are
rebound by broker ID first with a unique symbol/side fallback supported by the
session artifact. Unresolved pending/protective state is removed and diagnosed;
it is never retained as if broker-confirmed. `_order_index` is rebuilt only after
rebinding. Reconciliation never creates, submits, cancels, or replaces an order.

## Offline verifier

`scripts/verify_paper_six_parity.py` reads one or more persisted session files and
re-runs the real production policies through `strategy_lab.hts_parity`:

- `replay_decision(snapshot)` re-runs `rank_scores`, `select_holdings`,
  `target_weights`, `cap_risk_contributions`, `apply_leveraged_cap`, independent
  whole-share quantity arithmetic, fill accumulation, and the protective-stop
  formula. Every policy-consumed weight stage is mandatory; a missing stage is
  `UNVERIFIABLE`, never skipped. A non-volatility-target snapshot may omit
  covariance only with an explicit `covariance_not_required` reason.
- `assert_decision_parity(snapshot)` raises `ParityMismatch` on the first
  divergence and `UnverifiableError` when a required input or timestamp is
  missing. A missing input is `UNVERIFIABLE`, never a substituted value.
- `validate_lifecycle(payload)` enforces
  `intent_created → order_submitted → partial_fill* → terminal`, strictly
  increasing sequences, nondecreasing timestamps, decision-snapshot joins,
  stable local/broker order IDs, cumulative-fill arithmetic, and that every
  intent is either terminal or declared with exact lineage in `open_orders`.
  Structural defects are `UNVERIFIABLE` and join/transition mismatches are
  `MISMATCH`; both return nonzero.
- A decision stub or malformed decision is `UNVERIFIABLE`; it is never skipped.
- `compare_pnl(live, native, tolerances=...)` compares aligned session-end
  normalized equity (absolute `1e-4`, relative `1e-3`, dollar guard
  `max($1, starting_equity × 1e-4)`), requires at least two aligned observations,
  requires every live session in the native curve, and reports the first
  divergent session.
- `--run-native` attempts a local-archive-only native run even without
  `--compare-pnl`; `--compare-pnl` without `--run-native`, and `--run-native`
  without `--native-out`, return exit `2`. The persisted `identity.catalog_id`
  resolves each paper strategy to its declared search lineage: W0006, W0007,
  and W0018 use H100; S158 and S159 use H022. Missing or unknown lineage,
  missing local archives, native construction/run failures, and an empty native
  equity curve return structured `UNVERIFIABLE` with exit `2`; they do not
  escape as a traceback and never trigger a download.
- After a successful native run, the native curve is read from the run's real
  `run_stats.csv` session-end observations, never a repeated final equity.
  Repeated live session inputs are grouped by full strategy identity, their date
  bounds define one native window, and their session-end observations form one
  live comparison curve.

Exit codes: `0` complete PASS, `1` mismatch, `2` invalid/unverifiable input.

```bash
# Pure-policy and lifecycle verification only (offline)
.venv/bin/python scripts/verify_paper_six_parity.py \
  --live-session '<paper6-state-root>/paper6_audit/<strategy>/<YYYY-MM-DD>.json'

# Optional same-window native + PnL comparison (local archives only)
.venv/bin/python scripts/verify_paper_six_parity.py \
  --live-session '<paper6-state-root>/paper6_audit/<strategy>/<YYYY-MM-DD>.json' \
  --run-native --native-out '$TMPDIR/paper6_native' \
  --compare-pnl --pnl-abs-return 1e-4 --pnl-rel 1e-3
```

## Native behavioral evidence

A single-worker, local-archive `two_year` run on 2026-09-23 completed all three
representative mechanisms under revision `hts-native-v2-2026-09-16-2`:

| Candidate | Mechanism | `run_stats.csv` SHA-256 | `run_trades.csv` SHA-256 |
|---|---|---|---|
| H039 | Resting protective stop | `4e4667b8b8af62930d011d6060eeb57eafd9c94765ed12f36fe1c690e462f235` | `cf84ab235000e45b2e51744211ed68c9184a4673a71690407bb130f7409cee6c` |
| H061 | Volatility target | `14a76c5665a244ef187fcb0b913dca71de6df5edaab11ae7331d0c97a277fcb6` | `e0be4a6a8520d21a0066704acc0ecc9a34f1cc43f8133f0a8476de04bee956c2` |
| V031 | Risk-contribution cap | `1127d361e9c8ca3cf18ee19e5bff424dce5a9c18b585fbbd29aea353dda47289` | `270d5f1d48f68fe1d335b6b13fe53cb085075b3de098818e23b123ce95c73cb6` |

These are post-remediation hashes, not proof of pre/post equality. The checkout
contains older H039/H061 artifacts from different implementation revisions and
no retained V031 raw artifacts, so they are not valid before-state baselines.

## Evidence boundaries

- Live quote/fill behavior and native bar-open fills are intentionally distinct
  evidence layers; pure-policy replay may pass while strict native outcome/PnL
  parity fails. PnL comparison is off unless requested.
- Pre-rollout holdings cannot reconstruct their original ATR, decision equity,
  or quote; only broker-visible facts are bootstrapped and labeled
  `legacy_unverifiable`.
- A persistence failure may leave a later restart without proof; the harness
  reports that gap rather than reconstructing it.
- Historical pre-change `run_stats.csv` and `run_trades.csv` hashes for H039,
  H061, and V031 were not retained with this remediation. Current artifacts can
  be hashed, but that cannot establish pre/post behavioral identity by itself.
