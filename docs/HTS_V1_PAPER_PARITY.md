# Title: HTS V1 Paper-Parity Contract

Description: The strategy-owned contract for matching HTS v1 replay and paper decisions.

Last Updated: 2026-09-13
Status: Active
Audience: Strategy developers and operators

## Overview

`strategy_lab/hts_v1_core.py` is the authority for daily selection, hourly ATR, virtual stops, order intents, restart state, and decision journals. It uses the prior completed daily session for intraday selection. A virtual-stop breach is observed only when the hourly bar completes and queues a market sell for a later executable bar; it never credits a historical stop price.

The runtime wrapper stays dry by default. It requires an explicit normalized bar provider so a paper session cannot silently use a different feed, adjustment basis, timezone, or hourly boundary than the replay. Live Alpaca ingestion is deliberately injected by the paper launcher; `scripts/run_hts_v1_paper.py` validates local-cache inputs only and neither starts paper trading nor loads broker credentials. Every configuration has a fingerprint and every feature set has an input hash. The replay comparator requires the same timestamps, selections, and order intents.

## Operating sequence

1. Record normalized completed daily and hourly bars, including the provider feed and adjustment setting.
2. Construct the same `HtsV1Config` for replay and paper execution.
3. Run dry mode first. It creates decision records but has no broker credentials or order submission path.
4. Re-run the recorded bars offline and compare the two JSONL journals with `scripts/compare_hts_v1_paper_replay.py`.
5. Enable paper order submission only after decision parity passes and startup reconciliation reports identical local and broker positions.

The decision contract does not promise equal fills: a paper broker controls fill timing and price. It does promise the same data and configuration produce the same selection, stop decision, and order intent.

## Cached replay

Run `scripts/backtest_hts_v1_local.py` to replay the same shared decision core against the retained local archives. Its virtual stop is intentionally post-close: a close at or below the trail creates a sell intent and the simulator fills that intent at the following hourly open. Its saved `run.json`, `fills.csv`, equity curve, and decision journal make that execution assumption reviewable beside any native LumiBot result.
