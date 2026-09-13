"""Validate HTS v1 inputs from a local cache without starting a paper strategy.

This command deliberately does not load credentials or create a broker.  A
Live Alpaca data ingestion is intentionally injected into the LumiBot wrapper,
not implemented here. A production paper launcher must supply that provider,
an authenticated broker, and set both ``dry_run=False`` and
``allow_paper_orders=True`` explicitly.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from strategy_lab.hts_v1_core import HtsV1Config, prepare_features


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--daily-csv", type=Path, required=True)
    parser.add_argument("--hourly-csv", type=Path, required=True)
    parser.add_argument("--symbols", required=True, help="comma-separated strategy universe")
    parser.add_argument("--feed", required=True, help="recorded feed name, for example SIP or IEX")
    args = parser.parse_args(argv)
    config = HtsV1Config(universe=tuple(item.strip() for item in args.symbols.split(",") if item.strip()), market_data_feed=args.feed)
    features = prepare_features(pd.read_csv(args.daily_csv), pd.read_csv(args.hourly_csv), config)
    print("HTS v1 local-cache validation accepted")
    print(f"contract={config.contract_revision} config={config.fingerprint()}")
    print(f"input_hash={features.input_hash} daily_rows={len(features.daily)} hourly_rows={len(features.hourly)}")
    print("No broker was initialized; this command does not start paper trading or submit orders.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
