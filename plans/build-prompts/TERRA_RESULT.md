# TERRA RESULT — HTS hourly-convention implementation

## Commits

1. `a581f03d578d439ecbe45fb4a6cc0f42a8e4daeb` — `Add HTS walk-forward suite and hourly-convention groundwork`
2. `20af68d62e52aeb344906ea4a6d8b7cf5767e62a` — `Align HTS native bars to the clock-hour convention`

Neither commit was pushed.

## Verification

- Item 1: the four new scripts compiled and
  `tests/strategy_lab/test_native_experiments.py` passed **12** tests.
- Item 3/final: the three targeted files
  `test_native_experiments.py`, `test_native_alternatives.py`, and
  `test_experiment_registry.py` passed **48** tests.
- `git diff --check` passed before each commit.
- The exact 11 Item-1 staged files received a content scan for personal absolute
  paths and common credential signatures. No content was redacted; manifest
  path/URL fields were reviewed.
- The active source, scripts, tests, and generated/active docs contain no
  legacy cooldown-key references. Half-hour wording remains only where
  explicitly marked historical.

`pytest-glitch.ini` is absent from this checkout, so the required pytest command
could not start. The repository's tracked `setup.cfg` pytest configuration was
used for the real test runs above. The runtime has no configured downloader
credentials; the two local-archive controls used the documented localhost
placeholder environment only and made no downloader/Theta request.

## Authorized control A/B

Both one-worker controls were accepted by `evaluate_hts_experiments.py` with
`problems=[]`, no warnings, finite fills, and non-negative cash.

| Field | Before | After | Delta |
|---|---:|---:|---:|
| Suite / revision | `hts-hourly-before-2026-09-15` / `hts-native-2026-09-14-2` | `hts-hourly-after-2026-09-15` / `hts-native-2026-09-15-clock-hour-1` | revisioned |
| Fills | 3,376 | 2,588 | -788 |
| Journal events | 6,752 | 5,176 | -1,576 |
| Final equity | 344566.7335993077 | 465397.1869199659 | +120830.45332065818 |
| Total return | 2.445667335993077 | 3.653971869199659 | +1.208304533206582 |
| CAGR | 0.22903992915729843 | 0.292198876905853 | +0.06315894774855457 |
| Daily Sharpe | 0.6573189016895791 | 0.748575044644078 | +0.09125614295449891 |
| Volatility | 0.5345023626978476 | 0.5408237706435634 | +0.006321407945715785 |
| Max drawdown | 0.6617897953915901 | 0.6097841928511989 | -0.052005602540391216 |
| Minimum cash | 0.58939698326 | 0.099582091625 | -0.489814891635 |

Economic parity after timezone-aware minus-30-minute old-run normalization did
**not** hold. The first five filled rows match; the sixth diverges:

- before normalized `2020-09-10 09:00` ET: IEF sell 398 @ 121.49, fee 16.923557, `bt_6`;
- after `2020-09-10 10:00` ET: IEF sell 398 @ 121.56, fee 16.933308, `bt_6`.

The old unfiltered feature frame could select the preceding 17:00 ET post-market
IEF close. The canonical frame uses the preceding 15:00 close at 09:00, so this
exit first occurs at a later executable open. The first stats/equity divergence
is at the normalized 10:00 ET snapshot: before IEF is gone and cash is
99198.930511; after IEF remains, cash is 50862.834068, and portfolio value is
99243.714068.

The after run produced 883 joinable VIRTUAL stop-gap events. All joined to the
recorded fill by order identifier and passed independent price, quantity,
fill-minus-stop, percentage-gap, and entry-to-fill arithmetic checks. There are
143 overnight-gap events; the first audited 15:00 trigger filled at the next
session 09:00 open and is flagged accordingly.

## A1-A7 status

| Amendment | Status |
|---|---|
| A1 | Implemented: one exact-minute `09:00-15:00` canonical frame feeds both hourly feature math and LumiBot `Data`; pre/post-market source rows are excluded. |
| A2 | Implemented: deterministic `on_trading_iteration` lifecycle trace proves 14:00-close action at 15:00 and 15:00-close action only at the next 09:00 open. |
| A3 | Implemented: canonical `reentry_cooldown_bars`, `-1`/`0` disable, and expiry `i + N`; deterministic tests cover -1/0/1/3/5. |
| A4 | Implemented: Family 9, Family 10, H088-H090, H099, registry fingerprints, catalog, source, tests, and active docs use only the new key. |
| A5 | Documented limitation: cash-only replacement sizing is enforced, but an active-plus-pending slot cap is not. The contract explicitly marks this as an unenforced qualification gap. |
| A6 | Implemented for VIRTUAL stops: immutable trigger context joins actual broker fills, with the required fields, formulas, aggregate count, and overnight flag. Resting-stop modes remain separately qualified/blocked from this completed-close claim. |
| A7 | Implemented: staged-content secret hygiene scan completed with no redactions. |

## Remaining local artifacts and next action

Untracked artifacts were intentionally preserved. New local evidence is under
`reports/hts_hourly_alignment_2026-09-15/{before,after}/`; it was not staged.
Pre-existing untracked live, plan, full-backtest, native-candidate,
walk-forward-candidate, and `short/` directories remain untouched. This report
is itself untracked under `plans/build-prompts/`.

The new revision invalidates current hourly rankings. During an explicitly
scheduled off-hours follow-up, rerun the full H001-H100 plus HTS_CONTROL_1
revisioned suite (202 descriptive hourly jobs and 1,818 hourly walk-forward
jobs), audit every run, rebuild walk-forward JSON/HTML, then rerun selected cost
stresses for the newly selected leaders. Do not overwrite the September 14
historical artifacts.
