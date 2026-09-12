#!/usr/bin/env python3
"""Compare cached research fills with corrected native LumiBot fills."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from strategy_lab.daily_fleet_backtest import (  # noqa: E402
    LIQ,
    MEANREV_EMA,
    PODHAJSKY_GAP3,
    load_daily,
    run_backtest,
)
from strategy_lab.hts_backtest import (  # noqa: E402
    DAILY_DB as HTS_DAILY_DB,
    DEFAULT_UNIVERSE,
    HOURLY_DB,
    SUFFIX,
    _load,
    build_daily_sig,
    build_hour_map,
    run_cash_state_machine,
)

ET = "America/New_York"


def _native_fills(path: Path) -> list[dict[str, Any]]:
    frame = pd.read_csv(path)
    frame = frame.loc[(frame["event_kind"] == "trade") & (frame["status"] == "fill")].copy()
    stamps = pd.to_datetime(frame["time"], utc=True).dt.tz_convert(ET).dt.tz_localize(None)
    rows: list[dict[str, Any]] = []
    for (_, row), stamp in zip(frame.iterrows(), stamps):
        rows.append({
            "time": stamp,
            "symbol": str(row["symbol"]),
            "side": str(row["side"]),
            "price": float(row["price"]),
            "quantity": float(row["filled_quantity"]),
            "trade_cost": float(row["trade_cost"]),
        })
    return rows


def _daily_research_fills(strategy: str, start: str, end: str) -> list[dict[str, Any]]:
    spec = MEANREV_EMA if strategy == "meanrev_ema_native" else PODHAJSKY_GAP3
    frames = load_daily(LIQ, "2018-05-01", end)
    result = run_backtest(spec, frames, start=start, end=end)
    rows: list[dict[str, Any]] = []
    for _, row in result["fills"].iterrows():
        rows.append({
            "time": pd.Timestamp(row["time"]) + pd.Timedelta(hours=9, minutes=30),
            "symbol": str(row["symbol"]),
            "side": str(row["side"]),
            "price": float(row["price"]),
            "quantity": float(row["quantity"]),
            "trade_cost": float(row["trade_cost"]),
        })
    return rows


def _hts_research_fills(start: str, end: str) -> list[dict[str, Any]]:
    start_d = pd.Timestamp(start).date()
    end_d = pd.Timestamp(end).date()
    lookback = (pd.Timestamp(start_d) - pd.Timedelta(days=400)).date()
    high_end = str(end_d + dt.timedelta(days=1))
    symbols = sorted(set(DEFAULT_UNIVERSE) | {"SPY"})
    hourly = _load(HOURLY_DB, "bars_hourly", symbols, str(lookback), high_end, basis="exchange")
    daily = _load(HTS_DAILY_DB, "bars_daily", symbols, str(lookback), high_end, basis="session")
    hour_map = build_hour_map(hourly)
    daily_map, spy_map = build_daily_sig(daily)
    params = {
        "universe": sorted({f"{symbol}{SUFFIX}" for symbol in DEFAULT_UNIVERSE} - {"SPY.US"}),
        "top_n": 2,
        "k_atr": 2.0,
        "entry_hour": 9,
        "execution_hour": 10,
        "exit_hour": 15,
        "min_mdv": 5e6,
        "trend_filter": True,
        "use_spy_gate": False,
        "cost_per_side": 0.00035,
        "target_leverage": 1.0,
        "initial_cash": 100_000.0,
        "margin_rate": 0.05,
    }
    _returns, _trades, _signals, fills = run_cash_state_machine(
        hour_map,
        daily_map,
        spy_map,
        params,
        start_d,
        end_d,
        record_fills=True,
    )
    return [
        {
            "time": pd.Timestamp(fill["day"]) + pd.Timedelta(hours=int(fill["hour"]), minutes=30),
            "symbol": str(fill["symbol"]).removesuffix(SUFFIX),
            "side": str(fill["side"]),
            "price": float(fill["price"]),
            "quantity": float(fill["quantity"]),
            "trade_cost": float(fill["trade_cost"]),
        }
        for fill in fills
        if fill["side"] in {"buy", "sell"}
    ]


def compare_fills(
    native: list[dict[str, Any]],
    research: list[dict[str, Any]],
    *,
    price_tolerance: float = 0.01,
    cash_tolerance: float = 0.01,
) -> dict[str, Any]:
    """Compare ordered fill streams and return an actionable parity report."""
    # Broker callbacks may emit independent orders in a different order at the
    # same timestamp. Sort within the timestamp so parity measures decisions
    # and fills rather than queue serialization.
    def sort_key(row: dict[str, Any]) -> tuple[Any, ...]:
        return row["time"], row["symbol"], row["side"], float(row["price"])

    native = sorted(native, key=sort_key)
    research = sorted(research, key=sort_key)
    mismatches: list[dict[str, Any]] = []
    matched = 0
    for index, (left, right) in enumerate(zip(native, research)):
        reasons: list[str] = []
        for key in ("time", "symbol", "side"):
            if left.get(key) != right.get(key):
                reasons.append(key)
        if abs(float(left["price"]) - float(right["price"])) > price_tolerance:
            reasons.append("price")
        for key in ("quantity", "trade_cost"):
            if key in left and key in right:
                if abs(float(left[key]) - float(right[key])) > cash_tolerance:
                    reasons.append(key)
        if reasons:
            if len(mismatches) < 20:
                mismatches.append({"index": index, "fields": reasons, "native": left, "research": right})
        else:
            matched += 1
    return {
        "native_fill_count": len(native),
        "research_fill_count": len(research),
        "compared_fill_count": min(len(native), len(research)),
        "matched_fill_count": matched,
        "parity": len(native) == len(research) and matched == len(native),
        "mismatch_samples": mismatches,
    }


def main(argv: list[str] | None = None) -> int:
    """Run fill-level comparisons for one or all cached strategies."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report-dir", required=True, type=Path)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument(
        "--strategy",
        choices=("all", "meanrev_ema_native", "podhajsky_gap_native", "hourly_trend_stop_native"),
        default="all",
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)

    names = (
        ["meanrev_ema_native", "podhajsky_gap_native", "hourly_trend_stop_native"]
        if args.strategy == "all"
        else [args.strategy]
    )
    report: dict[str, Any] = {}
    for name in names:
        native = _native_fills(args.report_dir / f"{name}_trades.csv")
        research = (
            _hts_research_fills(args.start, args.end)
            if name == "hourly_trend_stop_native"
            else _daily_research_fills(name, args.start, args.end)
        )
        report[name] = compare_fills(native, research)

    output = args.output or args.report_dir / "parity_report.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2, default=str))
    return 0 if all(item["parity"] for item in report.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
