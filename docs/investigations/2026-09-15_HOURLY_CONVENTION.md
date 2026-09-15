# Title: HTS Hourly and Execution Convention

Description: Frozen timing, virtual-stop, and evidence contract for the HTS native research harness.

Last Updated: 2026-09-15
Status: Contract frozen and native clock-hour wiring verified
Audience: Strategy owner and HTS research-harness maintainers

## Overview

This investigation freezes the cadence and execution contract used by the
strategy-owned HTS native research path. It provides the evidence boundary for
parity expectations with live Alpaca `1H` data, without changing LumiBot
framework behavior or authorizing any live operation.

## Scope and ownership

The contract applies to `strategy_lab/native_experiments.py`, its registry, and
the native backtesting artifacts it emits. It does not change `lumibot/`, the
retired custom replay, paper wrappers, broker configuration, or live trading.
The native control is the research engine; historical replay outputs are not.

## Bar labels, intervals, and availability

The hourly bar convention is clock-hour `09:00` through `15:00` ET, anchored to
whole clock hours. A bar labelled `T` contains `[T, T+1h)` and is only known at
`T+1h`. The convention is clock parity, not a claim that the first or last
clock-hour source bar is a clean regular-session aggregate.

| Source bar | Interval | Available at | Resulting actionability |
|---|---|---|---|
| `14:00` | `14:00-15:00` ET | `15:00` ET | Last same-day-actionable completed bar; a market order can use the `15:00` open. |
| `15:00` | `15:00-16:00` ET | `16:00` ET | No same-day executable clock-hour bar remains; a resulting market order fills at the next session `09:00` open. It is overnight gap-exposed, never instant, and never guaranteed. |

## Stop contract

For **VIRTUAL** stops, a completed close that breaches the stop is a trigger,
not a price guarantee. It latches a market exit; it never fills at the stop and
never uses the triggering bar's low. Realized loss is entry-to-actual-next-open.
Stop-to-fill gaps are a separate reported quantity. This completed-close rule
does not describe resting-stop modes: their broker-order/OHLC simulation semantics
are separately qualified, and H039, H040, and H100 remain subject to their
resting-stop implementation-and-fill-fidelity requirement.

### Latched exits

Once a virtual-stop exit is queued it is unconditional and cannot be canceled
because the following morning recovered. The only alternative is a separately
documented weaker contract that re-evaluates at the next executable bar; it is
not the HTS contract.

### Re-entry cooldown

`reentry_cooldown_bars` is the canonical name. Values `-1` and `0` disable it;
`N > 0` bars a stopped symbol for `N` completed exchange-session bars before it
can requalify. H088/H089/H090 exercise `1`/`3`/`5` complete-session values. The
former internal spelling is migrated by this change; there must not be two
active knobs.

## Fixed-pool rotation

The control targets two slots at `99.5% / 2 = 49.75%` NAV each. Sales are
submitted before replacement sizing, and a pending, failed, or unfilled sale
provides zero spendable proceeds; its notional must not be counted a second or
third time. Current cash budgeting enforces this conservatively. **Limitation:**
the harness does not yet enforce a combined active-plus-pending slot limit, so
residual cash can open a partial replacement while an outgoing sale remains
unresolved. Slot enforcement is an unverified qualification gap, not a claimed
control.

## Session cleanliness

`reports/hts_session_cleanliness_audit.json` records 4,660,438 fifteen-minute
bars. The archive is not regular-session-only: approximately 64% are regular
session (`2,982,084`, 63.99%), with premarket `04:00-09:30` ET and post-market
`16:00-20:00` ET ranges. A regular-session strategy requires an explicit
`09:30-16:00` ET filter. A10 remains blocked pending genuine `09:30-10:00`
one-minute bars and an exchange calendar; a clock-hour relabel is not a proxy.

## Enforcement map

| Contract clause | Current location | Item-3 action | Residual qualification gap |
|---|---|---|---|
| Clock-hour mapping | `native_experiments.py::_lumibot_hourly` | Feed one canonical exact-minute `09:00-15:00` frame to strategy features and LumiBot `Data`. | Clock parity is not strict RTH cleaning. |
| Completed-bar causality | `RegistryHtsStrategy._completed_row`, `_execution_price`, `on_trading_iteration` | Boundary and lifecycle traces cover `15:00`, `16:00`, and next `09:00`. | Early closes need calendar-specific qualification. |
| Virtual-stop trigger/fill | `_update_risk`, `_submit_sell`, `on_filled_order` | Persist joinable trigger-to-fill gap events. | Resting-stop behavior remains separately qualified. |
| Exit latch | `_pending_sells`, `_pending_sell_reason` | Preserve pending exit state until fill callback. | Broker rejection/retry policy remains a separate lifecycle matter. |
| Cooldown | `_cooldowns`, `_select`, `on_filled_order`; `hts_variants.py` | Rename knob and correct fill-index expiry algebra. | None after deterministic session tests. |
| Fixed pool | `hts_policies.py::target_weights`, `_rebalance` | Preserve cash-only replacement sizing. | Active-plus-pending slot cap is not enforced. |
| Session cleanliness | `audit_hts_session_cleanliness.py` | Keep evidence and block A10. | Explicit RTH filtering is still separate work. |

## Control A/B evidence

Both one-worker six-year controls were independently accepted with
`problems=[]`. Raw artifacts remain local under
`reports/hts_hourly_alignment_2026-09-15/`; this table retains the durable
aggregate and first-difference evidence.

| Field | Before: historical relabel | After: clock-hour | Delta / comparison |
|---|---:|---:|---|
| Hour mapping | `archive hours 09:00-15:00 ET relabelled to NYSE 09:30-15:30` | `clock-hour 09:00-15:00 ET; a bar labelled T contains [T,T+1h) and is only known at T+1h` | Convention deliberately changed. |
| Implementation revision / feature hash | `hts-native-2026-09-14-2` / `bbf1feb72eaec162e44b818d1468d1ed5a470aed405e430abca62d213248492d` | `hts-native-2026-09-15-clock-hour-1` / `deb194e102204134b9d60b60c375b99d9bd1ef57081c36d50ca842226c22d2aa` | Revision and provenance intentionally differ. |
| Fills / journal events | 3,376 / 6,752 | 2,588 / 5,176 | -788 / -1,576 |
| Final equity / total return / CAGR | 344566.7335993077 / 2.445667335993077 / 0.22903992915729843 | 465397.1869199659 / 3.653971869199659 / 0.292198876905853 | +120830.45332065818 / +1.208304533206582 / +0.06315894774855457 |
| Daily Sharpe / volatility / max drawdown | 0.6573189016895791 / 0.5345023626978476 / 0.6617897953915901 | 0.748575044644078 / 0.5408237706435634 / 0.6097841928511989 | +0.09125614295449891 / +0.006321407945715785 / -0.052005602540391216 |
| Minimum cash / terminal positions | 0.58939698326 / none | 0.099582091625 / BITX 13,308; MSTR 1,660 | -0.489814891635 / terminal state differs |
| First event-level difference | Normalized `2020-09-10 09:00` ET: IEF sell 398 @ 121.49, fee 16.923557, `bt_6` | `2020-09-10 10:00` ET: IEF sell 398 @ 121.56, fee 16.933308, `bt_6` | Event time, price, and fee differ; economic parity fails. |

After timezone-aware minus-30-minute normalization of old trade/stat timestamps,
the first five filled rows agree, but the sixth does not. The old feature frame
could select the prior `2020-09-09 17:00` ET post-market IEF close; the canonical
frame instead uses the prior `15:00` close at the next `09:00` decision. The
first IEF sell is consequently later and at a different executable open. This
is an intended causal correction, not label-only parity.

The after control emitted 883 joinable VIRTUAL stop-gap events, all reconciled
to their filled order's identifier, price, and quantity; each reported formula
was independently recomputed. Of those, 143 were overnight-gap-exposed. For
example, the `2020-09-10 15:00` NUGT trigger was filled at the next-session
`2020-09-11 09:00` open, correctly marked overnight. Resting-stop semantics are
not included in this completed-close result.

## Consequences for prior results

All old hourly/control artifacts use the historical `09:30-15:30` relabel
convention. They remain preserved as historical evidence, but cannot be
compared, selected, or ranked as current evidence until the revisioned
clock-hour rebaseline is complete. The later off-hours rebaseline must cover
HTS_CONTROL_1 and H001-H100 before rebuilding mixed walk-forward selection.
