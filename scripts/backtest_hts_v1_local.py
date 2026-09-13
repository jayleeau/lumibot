"""Backtest the shared HTS v1 decision core against the retained local cache.

This is the deterministic companion to ``HtsV1PaperStrategy``.  It supplies
the decision core only completed hourly bars, fills intents produced by an
earlier bar at the next hourly open, and applies the same 3.5 bps one-way cost
used by the previous HTS comparison.  A virtual stop is therefore detected at
an hourly close and sold at the following hourly open; it is not credited with
an intrabar stop-price fill.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import sys

import duckdb
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from strategy_lab.hts_backtest import DAILY_DB, DEFAULT_UNIVERSE, HOURLY_DB
from strategy_lab.hts_v1_core import DecisionJournal, HtsV1Config, HtsV1DecisionCore, OrderIntent, prepare_features


def _load_bars(
    path: Path, table: str, symbols: tuple[str, ...], start: pd.Timestamp, end: pd.Timestamp
) -> pd.DataFrame:
    """Load raw archive timestamps without changing their exchange/session basis."""
    placeholders = ", ".join("?" for _ in symbols)
    sql = (
        f"SELECT symbol, ts AS timestamp, open, high, low, close, volume FROM {table} "
        f"WHERE symbol IN ({placeholders}) AND ts >= ? AND ts < ? ORDER BY symbol, ts"
    )
    with duckdb.connect(str(path), read_only=True) as connection:
        return connection.execute(sql, [*symbols, start, end]).fetchdf()


def _metrics(equity: pd.Series, initial_cash: float) -> dict[str, float | int]:
    """Calculate daily-return performance statistics with zero risk-free rate."""
    daily = equity.groupby(equity.index.date).last()
    daily.index = pd.to_datetime(daily.index)
    returns = daily.pct_change().dropna()
    days = max((daily.index[-1] - daily.index[0]).days, 1)
    years = days / 365.25
    total_return = float(daily.iloc[-1] / initial_cash - 1.0)
    cagr = float((daily.iloc[-1] / initial_cash) ** (1.0 / years) - 1.0)
    volatility = float(returns.std(ddof=1) * math.sqrt(252.0)) if len(returns) > 1 else 0.0
    # LumiBot's existing report uses annualized CAGR divided by annualized
    # volatility. Keep that definition in ``sharpe`` so the cached replay can
    # be compared directly with its native report, while retaining the usual
    # mean-daily-return Sharpe as a separately labelled diagnostic.
    sharpe = cagr / volatility if volatility else 0.0
    daily_return_sharpe = float(returns.mean() / returns.std(ddof=1) * math.sqrt(252.0)) if returns.std(ddof=1) else 0.0
    drawdown = daily / daily.cummax() - 1.0
    trough = drawdown.idxmin()
    return {
        "sessions": int(len(daily)),
        "total_return": total_return,
        "cagr": cagr,
        "sharpe": sharpe,
        "daily_return_sharpe": daily_return_sharpe,
        "volatility": volatility,
        "max_drawdown": float(-drawdown.min()),
        "max_drawdown_date": trough.date().isoformat(),
        "final_equity": float(daily.iloc[-1]),
    }


def _fill(
    intent: OrderIntent, bar: pd.Series, cash: float, cost_per_side: float
) -> tuple[float, dict[str, object]]:
    """Apply one full next-open fill to the simple cash ledger."""
    price = float(bar["open"])
    gross = intent.quantity * price
    fee = gross * cost_per_side
    if intent.side == "buy":
        cash -= gross + fee
    elif intent.side == "sell":
        cash += gross - fee
    else:
        raise ValueError(f"unsupported side: {intent.side}")
    return cash, {
        "decision_id": intent.decision_id,
        "decision_timestamp": intent.timestamp.isoformat(),
        "fill_timestamp": pd.Timestamp(bar["timestamp"]).isoformat(),
        "symbol": intent.symbol,
        "side": intent.side,
        "quantity": intent.quantity,
        "reason": intent.reason,
        "reference_price": intent.reference_price,
        "fill_price": price,
        "fee": fee,
    }


def run_backtest(
    *, start: str, end: str, output_dir: Path, initial_cash: float = 100_000.0,
    cost_per_side: float = 0.00035, margin_rate: float = 0.05,
) -> dict[str, object]:
    """Run the versioned contract and write its journal, fills, and summary."""
    start_stamp = pd.Timestamp(start, tz="UTC")
    end_stamp = pd.Timestamp(end, tz="UTC") + pd.Timedelta(days=1)
    daily_start = start_stamp - pd.Timedelta(days=400)
    hourly_start = start_stamp - pd.Timedelta(days=14)
    universe = tuple(DEFAULT_UNIVERSE)
    daily = _load_bars(DAILY_DB, "bars_daily", universe, daily_start, end_stamp)
    hourly = _load_bars(HOURLY_DB, "bars_hourly", universe, hourly_start, end_stamp)
    config = HtsV1Config(universe=universe, market_data_feed="XNAS_ITCH", price_adjustment="split")
    features = prepare_features(daily, hourly, config)

    output_dir.mkdir(parents=True, exist_ok=True)
    journal = DecisionJournal(output_dir / "decision_journal.jsonl")
    core = HtsV1DecisionCore(config, features, journal)
    bars_by_time = {timestamp: group.set_index("symbol", drop=False) for timestamp, group in features.hourly.groupby("timestamp", sort=True)}
    cash = initial_cash
    last_session: object | None = None
    fills: list[dict[str, object]] = []
    equity_rows: list[tuple[pd.Timestamp, float]] = []

    for timestamp, bars in bars_by_time.items():
        if timestamp.date() < start_stamp.date():
            continue
        # Cash debit financing is booked once per new exchange session.  At 1x
        # leverage it normally represents only fees that temporarily exceed cash.
        if last_session is not None and timestamp.date() != last_session and cash < 0:
            cash *= 1.0 + margin_rate / 365.0
        last_session = timestamp.date()

        executable = [intent for intent in core.pending if timestamp > intent.timestamp]
        # This is the executor transition normally performed by
        # ``process_completed_bar``.  Perform it before the fill because the
        # broker fill callback must see the intent as inflight, then let the
        # core process the completed bar and create only new decisions.
        core.pending = [intent for intent in core.pending if timestamp <= intent.timestamp]
        core.inflight.extend(executable)
        for intent in executable:
            if intent.symbol not in bars.index:
                continue
            cash, fill = _fill(intent, bars.loc[intent.symbol], cash, cost_per_side)
            core.acknowledge_fill(intent, float(fill["fill_price"]), timestamp)
            fills.append(fill)

        mark_value = cash + sum(
            position.quantity * float(bars.loc[symbol, "close"])
            for symbol, position in core.positions.items() if symbol in bars.index
        )
        core.process_completed_bar(timestamp, mark_value)
        equity_rows.append((timestamp, mark_value))

    equity = pd.Series([value for _, value in equity_rows], index=[timestamp for timestamp, _ in equity_rows], dtype=float)
    result: dict[str, object] = {
        "contract": config.contract_revision,
        "config_fingerprint": config.fingerprint(),
        "input_hash": features.input_hash,
        "data": {"feed": config.market_data_feed, "price_adjustment": config.price_adjustment},
        "period": {"start": start, "end": end},
        "execution": {
            "entry_and_exit": "intent after completed bar; full fill at next hourly open",
            "stop": "virtual: close <= trailing stop queues a next-open market sell",
            "cost_per_side": cost_per_side,
            "margin_rate": margin_rate,
        },
        "metrics": _metrics(equity, initial_cash),
        "fills": {"count": len(fills), "buys": sum(row["side"] == "buy" for row in fills), "sells": sum(row["side"] == "sell" for row in fills)},
    }
    pd.DataFrame(fills).to_csv(output_dir / "fills.csv", index=False)
    equity.rename("equity").to_csv(output_dir / "equity.csv", header=True)
    (output_dir / "run.json").write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return result


def main(argv: list[str] | None = None) -> int:
    """Parse command-line arguments and print the saved performance summary."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", default="2020-09-08")
    parser.add_argument("--end", default="2026-09-08")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "reports" / "hts_v1_virtual_stop_2020_2026")
    parser.add_argument("--initial-cash", type=float, default=100_000.0)
    parser.add_argument("--cost-per-side", type=float, default=0.00035)
    parser.add_argument("--margin-rate", type=float, default=0.05)
    args = parser.parse_args(argv)
    result = run_backtest(**vars(args))
    metrics = result["metrics"]
    print(f"HTS v1 virtual-stop cached backtest: {args.start} through {args.end}")
    print(f"total return {metrics['total_return']:+.2%}  CAGR {metrics['cagr']:+.2%}  Sharpe {metrics['sharpe']:.3f}")
    print(f"volatility {metrics['volatility']:.2%}  max drawdown {metrics['max_drawdown']:.2%}")
    print(f"fills {result['fills']['count']}  final equity ${metrics['final_equity']:,.2f}")
    print(f"saved {args.output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
