"""Backtest the pasted ``HourlyTrendStop`` strategy against the local archives.

The pasted file imports a private ``src.*`` framework (engine, ledger, margin,
costs, metrics, data.corporate_actions) that does not exist on this machine.
The signal calculations are retained from the paste. Execution follows the
same explicit contract as the native LumiBot runner: completed data only,
orders at the next hourly open, low-based protective-stop triggers, worse-open
fills on gap-through bars, and 7 bps combined round-trip costs.

Two deliberate substitutions, both forced by the missing framework:

1. Prices come from the already split-adjusted archives, so
   ``CorporateActionResolver`` is unnecessary (``price_basis="raw"`` on adjusted
   data).  Hourly and daily therefore share one adjustment basis, which is the
   consistency that resolver exists to guarantee.
2. The fast path uses a whole-share cash ledger with the same sizing prices,
   per-fill fees, and debit financing as the native LumiBot runner. Indicator
   preparation remains vectorized; order-dependent accounting remains serial.

Two conventions are restored so the paste behaves as designed:

* Symbols are suffixed ``.US``, because the strategy resolves its regime gate as
  ``out.get("SPY.US")``.  Against bare-symbol archives that lookup returns ``{}``
  and the SPY gate silently never opens.
* Hourly timestamps are converted to ``America/New_York`` before ``.hour`` is
  read: the gate is an exchange-session clock (hour 9 == the 09:00 ET bar), and
  reading a UTC stamp as local is the off-by-four-hours defect that has bitten
  this strategy before.

  Daily timestamps are deliberately NOT converted to ET.  The daily archive
  stamps each session at 00:00 UTC, so an ET conversion moves it to 20:00 the
  *previous* day and keys every daily bar one day early.  That shift is not
  cosmetic: the state machine looks up features at ``dts[i-1]``, so a one-day
  shift makes session *d* read session *d*'s own daily bar -- its closing price
  and full-day volume -- while "entering" at that same session's 09:00 close.
  On this data that lookahead inflated the result to a 377-883% CAGR; with the
  daily calendar date restored the same runs return single digits.  Hourly keeps
  exchange time for the gate, daily keeps its session date.

IMPORTANT -- indentation had to be reconstructed.  The pasted file carries only
two indent widths (0 and 1 space) and does not parse as Python at all
(``IndentationError`` at the first method body), so the paste cannot be used as
the source of truth for block structure.  Every nested block here is a reading,
and one of them matters: the ``sel = sel[:top_n]`` cap is placed at statement
level, NOT inside the ``if rank_seed is not None:`` branch.  ``rank_seed`` is not
one of the strategy's parameters, so nesting the cap inside that branch means the
book holds every qualifying symbol instead of top_n, which contradicts both the
module docstring ("top-N by 20-day return") and the weight arithmetic
(``w = target_leverage / top_n``).  Both readings are reported by
``--truncate-inside-if`` so the difference is visible rather than assumed.
"""
from __future__ import annotations

import argparse
import datetime as dt
import math
import sys
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

HOURLY_DB = ROOT / "short" / "suite_v2_xnas_itch_hourly_adjusted.duckdb"
DAILY_DB = ROOT / "short" / "suite_monitored_xnas_itch_daily_adjusted.duckdb"
EXCHANGE_TZ = "America/New_York"
SUFFIX = ".US"

DEFAULT_UNIVERSE = [
    "TQQQ", "QQQ", "SPY", "IWM", "SMH", "XLK", "XLE", "XLF", "XLY", "XLV",
    "XLI", "XLP", "XLU", "XLRE", "XLB", "XLC", "GLD", "SLV", "TLT", "IEF",
    "HYG", "LQD", "EEM", "EFA", "FXI", "VNQ", "DBC", "USO", "SOXX", "IBB",
    "XBI", "GDX", "XRT", "KRE", "XHB", "XME", "XOP", "UPRO", "SSO", "UDOW",
    "TNA", "SOXL", "TECL", "FAS", "ERX", "LABU", "NUGT", "BIL", "BOIL", "UNG",
    "MSTR", "BITX", "IBIT", "COIN", "EWZ", "INDA", "UUP",
]

DIVERSIFIERS = {"BOIL", "UNG", "MSTR", "BITX", "IBIT", "COIN", "EWZ", "INDA", "UUP"}


def _load(db: Path, table: str, symbols: list[str], start: str, end: str,
          *, basis: str) -> pd.DataFrame:
    """Load one archive into a ``.US``-suffixed frame on the requested calendar.

    ``basis="exchange"`` converts to exchange time (hourly: the hour gate).
    ``basis="session"`` keeps the UTC calendar date (daily: the session date).
    """
    if basis not in {"exchange", "session"}:
        raise ValueError("basis must be 'exchange' or 'session'")
    placeholders = ", ".join("?" for _ in symbols)
    query = (
        f"SELECT symbol, ts, open, high, low, close, volume FROM {table} "
        f"WHERE symbol IN ({placeholders}) AND ts >= ? AND ts <= ? ORDER BY symbol, ts"
    )
    with duckdb.connect(str(db), read_only=True) as con:
        frame = con.execute(
            query, [*symbols, pd.Timestamp(start, tz="UTC"), pd.Timestamp(end, tz="UTC")]
        ).fetchdf()
    stamps = pd.to_datetime(frame["ts"], utc=True)
    zone = EXCHANGE_TZ if basis == "exchange" else "UTC"
    frame["ts"] = stamps.dt.tz_convert(zone).dt.tz_localize(None)
    frame["symbol"] = frame["symbol"] + SUFFIX
    return frame


# ---------------------------------------------------------------------------
# Signal preparation from the pasted strategy; execution contract corrected.
# ---------------------------------------------------------------------------


def build_hour_map(hdf: pd.DataFrame, atr_period: int = 14) -> dict:
    """Build per-session OHLC/ATR maps without discarding intrabar extremes."""
    hdf = hdf.copy()
    hdf["ts"] = pd.to_datetime(hdf["ts"])
    out: dict = {}
    for sym, g in hdf.groupby("symbol", sort=True):
        g = g.sort_values("ts")
        pc = g["close"].shift(1)
        tr = np.maximum(
            g["high"] - g["low"],
            np.maximum((g["high"] - pc).abs(), (g["low"] - pc).abs()),
        )
        atr = pd.Series(tr, index=g.index, dtype="float64").rolling(atr_period).mean()
        sym_map: dict = {}
        for ts, o, h_, l_, c, a in zip(
            g["ts"], g["open"], g["high"], g["low"], g["close"], atr
        ):
            d = ts.date()
            h = ts.hour
            if d not in sym_map:
                sym_map[d] = ({}, {}, {}, {}, {})
            closes, atrs, opens, highs, lows = sym_map[d]
            closes[h] = float(c)
            opens[h] = float(o)
            highs[h] = float(h_)
            lows[h] = float(l_)
            atrs[h] = float(a)
        out[sym] = sym_map
    return out


def build_daily_sig(ddf: pd.DataFrame, trend_sma: int = 20) -> tuple[dict, dict]:
    ddf = ddf.copy()
    ddf["ts"] = pd.to_datetime(ddf["ts"])
    out: dict = {}
    for sym, g in ddf.groupby("symbol", sort=True):
        g = g.sort_values("ts")
        close = g["close"]
        logret = np.log(close / close.shift(1))
        logret_s = pd.Series(logret, index=g.index, dtype="float64")
        sma20 = close.rolling(trend_sma).mean()
        ret20 = close / close.shift(20) - 1.0
        annvol = logret_s.rolling(63).std() * math.sqrt(252.0)
        mdv = (close * g["volume"]).rolling(63).median()
        sma200 = close.rolling(200).mean()
        sym_map: dict = {}
        for ts, c, s20, r20, v63, m63, s200 in zip(
            g["ts"], close, sma20, ret20, annvol, mdv, sma200
        ):
            d = ts.date()
            vals = (
                float(c) if not pd.isna(c) else np.nan,
                float(s20) if not pd.isna(s20) else np.nan,
                float(r20) if not pd.isna(r20) else np.nan,
                float(v63) if not pd.isna(v63) else np.nan,
                float(m63) if not pd.isna(m63) else np.nan,
                float(s200) if not pd.isna(s200) else np.nan,
            )
            sym_map[d] = vals
        out[sym] = sym_map
    spy_sma = out.get("SPY.US", {})
    return out, spy_sma


def stop_fill_price(open_price: float, low_price: float, stop_price: float) -> float | None:
    """Return a conservative sell-stop fill, including gap-through slippage."""
    if not all(math.isfinite(float(value)) for value in (open_price, low_price, stop_price)):
        return None
    if float(low_price) > float(stop_price):
        return None
    return min(float(open_price), float(stop_price))


def run_cash_state_machine(
    hm: dict,
    ds: dict,
    spy_sma: dict,
    params: dict,
    start: dt.date,
    end: dt.date,
    *,
    record_fills: bool = False,
) -> tuple[pd.Series, list[dict], list[dict], list[dict]]:
    """Run the HTS decisions with native-equivalent whole-share accounting.

    Vectorized preparation supplies immutable daily and hourly maps. This loop
    remains serial because cash, position quantities, fees, and stop state all
    depend on the preceding fill.
    """
    top_n = int(params.get("top_n", 1))
    k_atr = float(params.get("k_atr", 2.0))
    use_spy_gate = bool(params.get("use_spy_gate", False))
    vol_target = params.get("vol_target")
    vol_cap = float(params.get("vol_cap", 1.5))
    min_mdv = float(params.get("min_mdv", 5e6))
    trend_filter = bool(params.get("trend_filter", True))
    target_leverage = float(params.get("target_leverage", 1.0))
    cost = float(params.get("cost_per_side", 0.00035))
    margin_rate = float(params.get("margin_rate", 0.05))
    initial_cash = float(params.get("initial_cash", 100_000.0))
    entry_hour = int(params.get("entry_hour", 9))
    execution_hour = int(params.get("execution_hour", entry_hour + 1))
    exit_hour = int(params.get("exit_hour", 15))
    universe = params.get("universe")
    if top_n <= 0:
        raise ValueError("top_n must be greater than zero")
    if execution_hour <= entry_hour:
        raise ValueError("execution_hour must follow the completed entry signal hour")
    if not math.isfinite(target_leverage) or target_leverage <= 0:
        raise ValueError("target_leverage must be finite and greater than zero")

    daily_dates = sorted({day for values in ds.values() for day in values if day <= end})
    daily_index = {day: index for index, day in enumerate(daily_dates)}
    days = sorted({day for values in hm.values() for day in values if start <= day <= end})
    cash = initial_cash
    total_interest = 0.0
    previous_day: dt.date | None = None
    positions: dict[str, dict] = {}
    fills: list[dict] = []
    trades: list[dict] = []
    signals: list[dict] = []
    equity_rows: list[tuple[pd.Timestamp, float]] = []

    def append_fill(day: dt.date, hour: int, symbol: str, side: str, price: float,
                    quantity: int, fee: float, reason: str) -> None:
        if record_fills:
            fills.append({
                "day": day,
                "hour": hour,
                "symbol": symbol,
                "side": side,
                "price": float(price),
                "quantity": int(quantity),
                "trade_cost": float(fee),
                "reason": reason,
            })

    for day in days:
        daily_pos = daily_index.get(day)
        if daily_pos is None or daily_pos == 0:
            continue
        signal_day = daily_dates[daily_pos - 1]
        calendar_days = 1 if previous_day is None else max((day - previous_day).days, 1)
        previous_day = day

        # The native intraday path accrues financing before the first strategy
        # callback of the session. With 1x target leverage this normally applies
        # only to the small fee-created debit balance.
        if cash < 0 and margin_rate > 0:
            before = cash
            cash *= (1.0 + margin_rate / 365.0) ** calendar_days
            total_interest += abs(cash - before)

        gate_ok = True
        if use_spy_gate:
            gate_ok = False
            spy = spy_sma.get(signal_day)
            if spy is not None:
                close, _sma20, _ret20, _annvol, _mdv, sma200 = spy
                gate_ok = not pd.isna(close) and not pd.isna(sma200) and close > sma200

        selected: list[tuple[str, float, float]] = []
        for symbol, daily_values in ds.items():
            if universe is not None and symbol not in universe:
                continue
            row = daily_values.get(signal_day)
            if row is None:
                continue
            close, sma20, ret20, annvol, mdv, _sma200 = row
            if any(pd.isna(value) for value in (close, sma20, ret20, mdv)):
                continue
            if mdv < min_mdv or (trend_filter and close <= sma20):
                continue
            selected.append((symbol, float(ret20), float(annvol)))
        selected.sort(key=lambda item: item[1], reverse=True)
        truncate_inside_if = bool(params.get("truncate_inside_if", False))
        rank_seed = params.get("rank_seed")
        if rank_seed is not None:
            import random

            random.Random(int(rank_seed)).shuffle(selected)
            if truncate_inside_if:
                selected = selected[:top_n]
        if not truncate_inside_if:
            selected = selected[:top_n]
        selected_names = (
            {symbol for symbol, _ret20, _annvol in selected} if gate_ok else set()
        )

        for hour in range(entry_hour, exit_hour + 1):
            if hour == execution_hour:
                # Size every replacement from the same pre-fill snapshot and
                # the completed signal-hour close, matching NativeHtsStrategy.
                sizing_value = cash
                for symbol, position in positions.items():
                    day_map = hm.get(symbol, {}).get(day)
                    close = day_map[0].get(entry_hour) if day_map is not None else None
                    if close is not None:
                        sizing_value += position["quantity"] * float(close)

                for symbol in list(positions):
                    if symbol in selected_names:
                        continue
                    day_map = hm.get(symbol, {}).get(day)
                    fill = day_map[2].get(execution_hour) if day_map is not None else None
                    if fill is None:
                        continue
                    position = positions.pop(symbol)
                    quantity = int(position["quantity"])
                    fee = quantity * float(fill) * cost
                    cash += quantity * float(fill) - fee
                    append_fill(day, hour, symbol, "sell", fill, quantity, fee, "sel_change")
                    trades.append({
                        "symbol": symbol,
                        "entry_date": pd.Timestamp(position["entry_d"]),
                        "exit_date": pd.Timestamp(day),
                        "exit_type": "sel_change",
                        "entry_price": position["entry_price"],
                        "exit_price": float(fill),
                        "pnl_pct": float(fill) / position["entry_price"] - 1.0 - 2.0 * cost,
                        "weight": position["weight"],
                    })

                target_slots = (
                    len(selected) if truncate_inside_if and rank_seed is None else top_n
                )
                capacity = target_slots - len(positions)
                for symbol, _ret20, annvol in selected:
                    if capacity <= 0 or not gate_ok:
                        break
                    if symbol in positions:
                        continue
                    day_map = hm.get(symbol, {}).get(day)
                    if day_map is None:
                        continue
                    closes, atrs, opens, _highs, _lows = day_map
                    fill = opens.get(execution_hour)
                    reference_close = closes.get(entry_hour)
                    signal_atr = atrs.get(entry_hour)
                    if any(value is None for value in (fill, reference_close, signal_atr)):
                        continue
                    if reference_close <= 0 or signal_atr <= 0 or pd.isna(signal_atr):
                        continue
                    target_weight = target_leverage / top_n
                    if vol_target is not None:
                        if not math.isfinite(annvol) or annvol <= 0:
                            continue
                        target_weight *= min(float(vol_target) / annvol, vol_cap)
                    quantity = math.floor(
                        (max(sizing_value, 0.0) * target_weight) / reference_close
                    )
                    if quantity <= 0:
                        continue
                    fee = quantity * float(fill) * cost
                    cash -= quantity * float(fill) + fee
                    positions[symbol] = {
                        "quantity": quantity,
                        "entry_d": day,
                        "entry_price": float(fill),
                        "stop": float(fill) - k_atr * float(signal_atr),
                        "next_stop": None,
                        "stop_active_from": (day, execution_hour + 1),
                        "last_mark": float(fill),
                        "weight": target_weight,
                    }
                    append_fill(day, hour, symbol, "buy", fill, quantity, fee, "entry")
                    capacity -= 1

            for symbol, position in list(positions.items()):
                day_map = hm.get(symbol, {}).get(day)
                if day_map is None:
                    continue
                closes, atrs, opens, _highs, lows = day_map
                open_price = opens.get(hour)
                low = lows.get(hour)
                close = closes.get(hour)
                if open_price is None or low is None or close is None:
                    continue
                pending_stop = position.pop("next_stop", None)
                if pending_stop is not None:
                    position["stop"] = max(position["stop"], pending_stop)
                stop_active = (day, hour) >= position["stop_active_from"]
                fill = stop_fill_price(open_price, low, position["stop"]) if stop_active else None
                if fill is not None:
                    probe = params.get("_stop_probe")
                    if probe is not None:
                        probe.append((float(fill), float(position["stop"]), float(open_price)))
                    quantity = int(position["quantity"])
                    fee = quantity * float(fill) * cost
                    cash += quantity * float(fill) - fee
                    append_fill(day, hour, symbol, "sell", fill, quantity, fee, "stop")
                    trades.append({
                        "symbol": symbol,
                        "entry_date": pd.Timestamp(position["entry_d"]),
                        "exit_date": pd.Timestamp(day),
                        "exit_type": "stop",
                        "entry_price": position["entry_price"],
                        "exit_price": float(fill),
                        "pnl_pct": float(fill) / position["entry_price"] - 1.0 - 2.0 * cost,
                        "weight": position["weight"],
                    })
                    positions.pop(symbol)
                    continue
                atr = atrs.get(hour)
                if atr is not None and atr > 0 and not pd.isna(atr):
                    position["next_stop"] = float(close) - k_atr * float(atr)
                position["last_mark"] = float(close)

        equity = cash + sum(
            position["quantity"] * position["last_mark"] for position in positions.values()
        )
        equity_rows.append((pd.Timestamp(day), float(equity)))
        for symbol, position in positions.items():
            signals.append({
                "ts": pd.Timestamp(day),
                "symbol": symbol,
                "quantity": position["quantity"],
                "weight": position["weight"],
                "close": position["last_mark"],
            })

    curve = pd.Series(
        [value for _day, value in equity_rows],
        index=[day for day, _value in equity_rows],
        dtype="float64",
    )
    returns = curve.pct_change().fillna(0.0)
    if not returns.empty:
        returns.iloc[0] = curve.iloc[0] / initial_cash - 1.0
    returns.attrs["equity_curve"] = curve
    returns.attrs["final_cash"] = cash
    returns.attrs["total_interest"] = total_interest
    return returns, trades, signals, fills


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


def metrics_from_returns(
    rets: pd.Series,
    initial_cash: float = 1_000_000.0,
    risk_free_rate: float = 0.0,
) -> dict:
    """Calculate metrics with the same annualization convention as LumiBot."""
    rets = rets.astype(float).fillna(0.0)
    equity = (1.0 + rets).cumprod() * initial_cash
    span_days = (rets.index[-1] - rets.index[0]).days if len(rets) > 1 else 0
    years = max(span_days / 365.25, 1 / 365.25)
    volatility = float(rets.std(ddof=1) * math.sqrt(rets.count() / years)) if len(rets) > 1 else 0.0
    total = float(equity.iloc[-1] / initial_cash - 1.0) if len(equity) else 0.0
    growth = float((1.0 + total) ** (1.0 / years) - 1.0) if total > -1 else -1.0
    dd = equity / equity.cummax() - 1.0 if len(equity) else pd.Series(dtype=float)
    return {
        "obs": int(len(rets)),
        "total_return": total,
        "cagr": growth,
        "sharpe": 0.0 if not np.isfinite(volatility) or volatility == 0 else (growth - risk_free_rate) / volatility,
        "volatility": volatility,
        "max_dd": float(dd.min()) if len(dd) else 0.0,
        "final_equity": float(equity.iloc[-1]) if len(equity) else initial_cash,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", default="2022-08-06")
    parser.add_argument("--end", default="2026-08-13")
    parser.add_argument("--lookback-days", type=int, default=320)
    parser.add_argument("--top-n", type=int, default=2)
    parser.add_argument("--k-atr", type=float, default=2.0)
    parser.add_argument("--cost-per-side", type=float, default=0.00035,
                        help="one-way cost; 0.00035 is 7 bps combined round trip")
    parser.add_argument("--use-spy-gate", action="store_true")
    parser.add_argument("--no-trend-filter", action="store_true")
    parser.add_argument("--universe", default="full", choices=("full", "etf"))
    parser.add_argument("--initial-cash", type=float, default=1_000_000.0)
    parser.add_argument("--risk-free-rate", type=float, default=0.0)
    parser.add_argument("--truncate-inside-if", action="store_true",
                        help="read the top_n cap as nested inside the rank_seed branch")
    args = parser.parse_args(argv)

    universe = ([s for s in DEFAULT_UNIVERSE if s not in DIVERSIFIERS]
                if args.universe == "etf" else list(DEFAULT_UNIVERSE))
    symbols = sorted(set(universe) | {"SPY"})
    sm_universe = {f"{s}{SUFFIX}" for s in universe} - {"SPY.US"}

    start_d = pd.Timestamp(args.start).date()
    end_d = pd.Timestamp(args.end).date()
    lb = (pd.Timestamp(start_d) - pd.Timedelta(days=args.lookback_days)).date()
    hi_end = str(end_d + dt.timedelta(days=1))

    hourly = _load(HOURLY_DB, "bars_hourly", symbols, str(lb), hi_end, basis="exchange")
    daily = _load(DAILY_DB, "bars_daily", symbols, str(lb), hi_end, basis="session")
    print(f"hourly rows        {len(hourly):,}   symbols={hourly['symbol'].nunique()}")
    print(f"daily rows         {len(daily):,}   symbols={daily['symbol'].nunique()}")
    print(f"hour clock (ET)    {sorted(hourly['ts'].dt.hour.unique())}")
    print(f"first hourly day   {hourly['ts'].dt.date.min()}   "
          f"first daily day {daily['ts'].dt.date.min()}")
    print(f"window            {start_d} .. {end_d}   (indicators warm from {lb})")

    hm = build_hour_map(hourly, atr_period=14)
    ds, spy_sma = build_daily_sig(daily, trend_sma=20)
    print(f"SPY gate history   {len(spy_sma)} sessions")

    params = {
        "universe": sorted(sm_universe), "top_n": args.top_n, "k_atr": args.k_atr,
        "atr_period": 14, "trend_sma": 20, "entry_hour": 9,
        "execution_hour": 10, "exit_hour": 15,
        "min_mdv": 5e6, "vol_target": None, "vol_cap": 1.5,
        "use_spy_gate": args.use_spy_gate, "fill_open_aware": True,
        "trend_filter": not args.no_trend_filter,
        "cost_per_side": args.cost_per_side, "target_leverage": 1.0,
        "initial_cash": args.initial_cash, "margin_rate": 0.05,
        "truncate_inside_if": args.truncate_inside_if,
    }
    stop_probe: list = []
    params["_stop_probe"] = stop_probe
    rets, trade_rows, signal_rows, _fills = run_cash_state_machine(
        hm, ds, spy_sma, params, start_d, end_d
    )
    m = metrics_from_returns(rets, args.initial_cash, args.risk_free_rate)

    trades = pd.DataFrame(trade_rows)
    sig = pd.DataFrame(signal_rows)
    print("")
    print(f"top_n {args.top_n}   k_atr {args.k_atr}   cost/side {args.cost_per_side:.4%}   "
          f"universe={args.universe}({len(universe)})   spy_gate={args.use_spy_gate}   "
          f"trend_filter={not args.no_trend_filter}   execution=next_open/intrabar_stop")
    print("")
    print(f"sessions           {m['obs']}")
    print(f"total return       {m['total_return']:+.2%}")
    print(f"CAGR               {m['cagr']:+.2%}")
    print(f"sharpe             {m['sharpe']:.3f}")
    print(f"volatility         {m['volatility']:.2%}")
    print(f"max drawdown       {m['max_dd']:.2%}")
    print(f"final equity       ${m['final_equity']:,.0f}")
    print(f"round trips        {len(trades)}")
    if len(trades):
        print(f"  by exit type     {trades['exit_type'].value_counts().to_dict()}")
        print(f"  win rate         {(trades['pnl_pct'] > 0).mean():.1%}")
        print(f"  mean pnl/trade   {trades['pnl_pct'].mean():+.2%}")
        print(f"  best / worst     {trades['pnl_pct'].max():+.1%} / {trades['pnl_pct'].min():+.1%}")
    # Concurrency from holding intervals.  Summing ``weight`` per day inside the
    # state machine double-counts, because it books a row for the entry, the
    # exit *and* the daily mark; spans are the honest measure.
    if len(trades):
        spans = list(zip(trades["entry_date"], trades["exit_date"], trades["weight"]))
        held_days = concurrent_days = 0
        worst_gross = 0.0
        gross_sum = 0.0
        for day in rets.index:
            # Half-open interval: a position is held on its entry day and no
            # longer on its exit day.  Closing the interval at both ends double
            # counts every handover day and reports 2.0x gross for top_n=1.
            live = [w for lo, hi, w in spans if lo <= day < hi]
            if live:
                held_days += 1
                gross = float(sum(live))
                gross_sum += gross
                worst_gross = max(worst_gross, gross)
                concurrent_days = max(concurrent_days, len(live))
        print(f"days invested      {held_days} / {len(rets)}")
        print(f"mean gross         {(gross_sum / held_days if held_days else 0.0):.3f}   "
              f"max {worst_gross:.3f}   (target_leverage 1.0)")
        print(f"max concurrent     {concurrent_days} symbols   (top_n was {args.top_n})")
    if stop_probe:
        booked = [b for b, _stop, _open in stop_probe]
        stops = [s for _b, s, _open in stop_probe]
        gaps = [(s - b) / s for b, s, _o in stop_probe]
        gaps_sorted = sorted(gaps)
        below = sum(1 for g in gaps if g > 1e-9)
        p90 = gaps_sorted[min(len(gaps_sorted) - 1, int(0.9 * (len(gaps_sorted) - 1)))]
        print(f"stop exits         {len(gaps)}   booked at the stop level: "
              f"{len(gaps) - below}   booked worse: {below}")
        print(f"stop-fill gap      mean {sum(gaps)/len(gaps)*1e4:,.0f} bps   "
              f"median {gaps_sorted[len(gaps_sorted)//2]*1e4:,.0f} bps   p90 {p90*1e4:,.0f} bps")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
