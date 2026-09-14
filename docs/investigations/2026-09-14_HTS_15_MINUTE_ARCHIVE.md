# HTS 15-Minute Archive

One durable, memory-bounded 15-minute archive derived from the downloaded XNAS.ITCH one-minute DBNs.

Last Updated: 2026-09-14

Status: Implemented

Audience: HTS research and backtest maintainers

## Overview

`scripts/build_hts_15m_archive.py` reads the five immutable `short/hts_xnas_itch_1m_*.dbn.zst` inputs, stages one source at a time as fixed-nanoprice Parquet, and publishes `short/hts_xnas_itch_15m_split_adjusted.duckdb` only after all checks pass. The source DBNs remain the long-term one-minute record; the DuckDB is the normal backtest-ready 15-minute representation.

## Stored Tables

- `bars_15m_raw`: UTC 15-minute OHLCV directly aggregated from the one-minute bars.
- `bars_15m`: the split-adjusted counterpart for historical backtests.
- `split_events` and `split_event_sources`: selected event records and their provenance.
- `source_files`, `quarantined_rows`, `source_anomalies`, `archive_metadata`, and `validation_results`: reproducibility and integrity evidence.

The builder quarantines `IBIT` before `2024-01-11 14:30:00 UTC`, avoiding the unrelated ticker history before the iShares Bitcoin Trust's first regular session. Known Databento source-quality dates are retained and explicitly recorded; they are not silently repaired.

## Split Rule

For a split whose effective date is later than a bar, the builder divides that bar's OHLC values by the product of all later split ratios and multiplies volume by the same product. A 4:1 split therefore turns a prior `$500` price and `10` shares into `$125` and `40` shares. `close * volume` remains invariant.

The initial selected events come from the local daily adjusted archive's HTS-universe `split_events` table. Event dates are normalized to midnight in `America/New_York`, so the entire ex-date, including premarket trading, uses the post-split share basis. A Databento adjustment-factor request was attempted on 2026-09-14, but the account does not have the required Reference-data subscription. `--verify-yahoo` therefore checks the archive's exact dates and all 57 symbols; it fails the build unless all 31 events agree exactly.

## Runbook

```bash
bin/safe-timeout 1200s .venv/bin/python scripts/build_hts_15m_archive.py --verify-yahoo --memory-limit 1GB --threads 2
```

The default refuses to overwrite a completed archive. Use `--force` only when intentionally replacing it after checking the existing artifact. A failed run leaves its temporary database and staging directory unpublished for diagnosis.

## Validation

The archive records and enforces no overlapping source partitions, no duplicate output bars, valid raw and adjusted OHLC ranges, exact accepted-source-minute conservation, exactly 31 positive split events, and split-adjustment notional conservation. Tests are offline and cover first-open/last-close aggregation, split boundaries and arithmetic, all-symbol event verification, the IBIT boundary, and missing-input failure.

The completed build contains 4,660,438 raw and 4,660,438 adjusted 15-minute bars for all 57 symbols. It accounts for all 48,326,468 source records as 48,325,938 accepted minutes, 39 isolated invalid rows on the declared degraded dates, and 491 quarantined pre-inception `IBIT` rows. All seven stored validations pass, and a separate pandas aggregation reproduced all 7,632 bars in the final three-day source file exactly.

## Later Use

The raw DBNs remain in `short/` for future one-minute work. The 15-minute archive is immediately queryable without rerunning the conversion:

```sql
SELECT symbol, ts, open, high, low, close, volume
FROM bars_15m
WHERE symbol = 'SPY'
ORDER BY ts;

SELECT symbol, ex_ts, ratio
FROM split_events
ORDER BY ex_ts, symbol;

SELECT *
FROM validation_results
ORDER BY check_name;
```
