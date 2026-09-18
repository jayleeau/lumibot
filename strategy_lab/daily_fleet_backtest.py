"""Daily-bar backtest of two deployed Alpaca paper-fleet strategies.

Both are faithful ports of the live classes in the (now external) paper fleet:

``LiveMeanrevEma``      meanrev-ema 1d / s400 (stop 4%) / rsi_hi 2 70 / l250 (2.5x)
``LivePodhajskyGap3``   podhajsky_gap 0.5d / s300 (stop 3%) / rsi_hi 2 70 / l300 (3.0x)

The name grammar is ``<hold>/s<stop_bps>/rsi_hi <n> <th>/l<leverage*100>``,
recovered from the live classes' own header comments and cross-checked against
three of them (``meanrev_ema_1m`` 1m/s300/2 70/l300 -> STOP 0.03, SLEEVE 3.0;
``meanrev_ema_3d`` 3d/s200/2 80/l300 -> STOP 0.02, SLEEVE 3.0; ``podhajsky_gap3``
0.5d/s300/2 70/l300 -> STOP 0.03, SLEEVE 3.0).

Signal semantics are copied exactly: Wilder RSI via
``ewm(alpha=1/n, adjust=False)``, ``ema(span=n, adjust=False)``, a 4%-below-entry
hard stop, an RSI(2) exit level, and a single-slot book that scans the universe
in order and takes the first qualifying symbol.

Execution model, because the live classes do not fix one:

* Signals are computed on a *completed* bar and the resulting market order fills
  at the next session's open.  The live ``_bars`` helper explicitly rejects a bar
  dated today, so the decision bar is always fully closed before it is read.
* The protective stop is a resting GTC order, so it is checked intraday against
  the day's low and fills at the stop level -- or at the open when the session
  gaps straight through it.
* ``held_days`` increments once per processed bar, matching the live
  ``_holding_age_reached``; with ``HOLD_DAYS = 1`` a position exits on the open
  after one full session.

Known divergences from the live fleet, all noted in the output:

* The daily archive holds 59 of the 196 LIQ symbols, so the ordered scan sees a
  subset and picks differently from production.
* No borrow/margin-capacity model beyond a flat interest charge on the borrowed
  portion; the live path also enforces Alpaca marginability rules.
"""
from __future__ import annotations

import argparse
import math
import sys
from dataclasses import dataclass
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DAILY_DB = ROOT / "short" / "suite_monitored_xnas_itch_daily_adjusted.duckdb"

LIQ = list(dict.fromkeys([
    "AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL", "TSLA", "AVGO", "COST", "NFLX", "ADBE", "AMD",
    "INTC", "QCOM", "TXN", "AMGN", "HON", "IBM", "PYPL", "BKNG", "MDLZ", "GILD", "ISRG", "MU",
    "AMAT", "PLTR", "CRWD", "DASH", "ABNB", "PANW", "TMO", "PEP", "CSCO", "GOOG", "ABBV", "ABT",
    "ACN", "ADI", "ADP", "ADSK", "AEP", "ALGN", "AMT", "ANSS", "APO", "APP", "ARM", "ASML",
    "AXP", "AZN", "BA", "BAC", "BIIB", "BKR", "BLK", "BMY", "BNY", "C", "CAT", "CDNS",
    "CEG", "CHTR", "CL", "CMCSA", "COF", "COIN", "COP", "CPRT", "CRM", "CSX", "CTAS", "CVS",
    "CVX", "DBC", "DDOG", "DE", "DHR", "DIA", "DIS", "DLTR", "DUK", "DXCM", "EA", "EBAY",
    "EEM", "EFA", "EMR", "EXC", "FANG", "FAST", "FDX", "FTNT", "FXI", "GD", "GE", "GEHC",
    "GEV", "GFS", "GLD", "GM", "GS", "HD", "HOOD", "HYG", "IEF", "INTU", "IWM", "JNJ",
    "JPM", "KDP", "KKR", "KLAC", "KO", "LIN", "LLY", "LMT", "LOW", "LQD", "LRCX", "LULU",
    "MA", "MAR", "MCD", "MCHP", "MDB", "MDT", "MELI", "MMM", "MNST", "MO", "MRK", "MRNA",
    "MRVL", "MS", "MSTR", "NEE", "NKE", "NOW", "NXPI", "ORCL", "ORLY", "PAYX", "PCAR", "PFE",
    "PG", "PGR", "PM", "QQQ", "REGN", "ROST", "RTX", "SBUX", "SCHW", "SMCI", "SMH", "SNPS",
    "SO", "SOXQ", "SOXX", "SPG", "SPY", "SWKS", "T", "TLT", "TMUS", "TTD", "UBER", "UNH",
    "UNP", "UPS", "USB", "USO", "V", "VGK", "VNQ", "VRSK", "VRTX", "VWO", "VZ", "WBA",
    "WBD", "WDAY", "WFC", "WMT", "XEL", "XLE", "XLF", "XLI", "XLK", "XLP", "XLU", "XLV",
    "XLY", "XOM", "ZM", "ZS",
]))


# --- indicators, copied from the live common.py -----------------------------

def ema(s: pd.Series, n: int) -> pd.Series:
    """Return the span-based exponential moving average used by the strategy."""
    return s.ewm(span=n, adjust=False).mean()


def rsi(c: pd.Series, n: int) -> pd.Series:
    """Return Wilder-style RSI for a closing-price series."""
    d = c.diff()
    up = d.clip(lower=0).ewm(alpha=1.0 / n, adjust=False).mean()
    dn = (-d).clip(lower=0).ewm(alpha=1.0 / n, adjust=False).mean()
    return 100.0 - 100.0 / (1.0 + up / dn.replace(0.0, np.nan))


# --- strategy specs ---------------------------------------------------------

@dataclass(frozen=True)
class Spec:
    """Parameters that fully define one daily fleet strategy variant."""

    name: str
    encoded: str
    sleeve: float
    stop: float
    hold_days: int
    rsi_exit: float
    warmup: int
    family: str
    # family-specific
    elong: int = 0
    efast: int = 0
    rsi_n: int = 0
    rsi_th: float = 0.0
    k_gap: float = 0.0


MEANREV_EMA = Spec(
    name="meanrev_ema", encoded="meanrev-ema-1d-s400-rsi-hi-2-70-l250",
    sleeve=2.5, stop=0.04, hold_days=1, rsi_exit=70.0, warmup=165, family="meanrev_ema",
    elong=150, efast=10, rsi_n=3, rsi_th=25.0,
)
PODHAJSKY_GAP3 = Spec(
    name="podhajsky_gap3", encoded="podhajsky_gap 0.5d-s300-rsi-hi-2-70-l300",
    sleeve=3.0, stop=0.03, hold_days=1, rsi_exit=70.0, warmup=115, family="podhajsky_gap",
    elong=100, rsi_n=3, rsi_th=35.0, k_gap=0.002,
)
# meanrev_ema_3d (27aae2b1, dedicated paper account PA3R1ZNFYPHF). Identical
# meanrev-ema entry (EMA150 uptrend pullback to EMA10, RSI3<25), 3-day hold
# (~4 trading days), stop 2%, RSI2>80 exit, 3x leverage.
MEANREV_EMA_3D = Spec(
    name="meanrev_ema_3d", encoded="meanrev-ema-3d-s200-rsi-hi-2-80-l300",
    sleeve=3.0, stop=0.02, hold_days=4, rsi_exit=80.0, warmup=165, family="meanrev_ema",
    elong=150, efast=10, rsi_n=3, rsi_th=25.0,
)
SPECS = {"meanrev_ema": MEANREV_EMA, "podhajsky_gap3": PODHAJSKY_GAP3,
         "meanrev_ema_3d": MEANREV_EMA_3D}


def load_daily(symbols: list[str], start: str, end: str) -> dict[str, pd.DataFrame]:
    """Load daily OHLCV per symbol on the session calendar (UTC date)."""
    placeholders = ", ".join("?" for _ in symbols)
    query = (
        "SELECT symbol, ts, open, high, low, close, volume FROM bars_daily "
        f"WHERE symbol IN ({placeholders}) AND ts >= ? AND ts <= ? ORDER BY symbol, ts"
    )
    with duckdb.connect(str(DAILY_DB), read_only=True) as con:
        rows = con.execute(
            query, [*symbols, pd.Timestamp(start, tz="UTC"), pd.Timestamp(end, tz="UTC")]
        ).fetchdf()
    rows["ts"] = pd.to_datetime(rows["ts"], utc=True).dt.tz_convert("UTC").dt.tz_localize(None)
    out = {}
    for sym, g in rows.groupby("symbol", sort=False):
        f = g.set_index("ts")[["open", "high", "low", "close", "volume"]].astype(float)
        out[str(sym)] = f[~f.index.duplicated(keep="last")].sort_index()
    return out


def prepare(frames: dict[str, pd.DataFrame], spec: Spec) -> dict[str, pd.DataFrame]:
    """Attach the indicator columns each family needs."""
    out = {}
    for sym, f in frames.items():
        g = f.copy()
        c = g["close"]
        if spec.family == "meanrev_ema":
            g["sig_entry"] = (
                (c > ema(c, spec.elong)) & (c < ema(c, spec.efast)) & (rsi(c, spec.rsi_n) < spec.rsi_th)
            )
        else:
            prev = c.shift(1)
            gap = (g["open"] - prev) / prev.replace(0.0, np.nan)
            g["sig_entry"] = (gap < -spec.k_gap) & (c > ema(c, spec.elong)) & (rsi(c, spec.rsi_n) < spec.rsi_th)
        g["rsi_exit"] = rsi(c, 2)
        out[sym] = g
    return out


def run_backtest(spec: Spec, frames: dict[str, pd.DataFrame], *, start: str, end: str,
                 initial_cash: float = 100_000.0, cost_bps: float = 7.0,
                 margin_rate: float = 0.05, leverage: float | None = None,
                 risk_free_rate: float = 0.0) -> dict:
    """Run the completed-bar/next-open contract with a whole-share cash ledger."""
    lev = float(spec.sleeve if leverage is None else leverage)
    cost = cost_bps / 2.0 / 10_000.0
    available = [s for s in LIQ if s in frames]
    data = prepare({s: frames[s] for s in available}, spec)

    all_sessions = sorted({d for f in data.values() for d in f.index})
    axis = [d for d in all_sessions if pd.Timestamp(start) <= d <= pd.Timestamp(end)]
    if not axis:
        raise ValueError("no sessions in window")
    session_index = {day: index for index, day in enumerate(all_sessions)}

    cash = float(initial_cash)
    equity_curve: list[tuple[pd.Timestamp, float]] = []
    trades: list[dict] = []
    fills: list[dict] = []
    pos: dict | None = None
    total_interest = 0.0
    previous_day: pd.Timestamp | None = None

    def bar(sym, day):
        f = data.get(sym)
        if f is None or day not in f.index:
            return None
        return f.loc[day]

    def current_open(sym: str, day: pd.Timestamp) -> float | None:
        row = bar(sym, day)
        if row is None:
            return None
        value = float(row["open"])
        return value if math.isfinite(value) and value > 0 else None

    def sell(fill: float, day: pd.Timestamp, reason: str) -> None:
        nonlocal cash, pos
        if pos is None:
            return
        proceeds = pos["quantity"] * fill
        exit_fee = proceeds * cost
        cash += proceeds - exit_fee
        pnl = (fill - pos["entry"]) * pos["quantity"] - pos["entry_fee"] - exit_fee - pos["interest"]
        trades.append({
            "symbol": pos["symbol"], "entry_day": pos["entry_day"], "exit_day": day,
            "entry": pos["entry"], "exit": fill, "reason": reason,
            "ret": fill / pos["entry"] - 1.0, "pnl": pnl,
            "held_days": max((day - pos["entry_day"]).days, 1),
            "interest": pos["interest"], "quantity": pos["quantity"],
            "entry_fee": pos["entry_fee"], "exit_fee": exit_fee,
        })
        fills.append({
            "time": day, "symbol": pos["symbol"], "side": "sell",
            "price": fill, "quantity": pos["quantity"], "trade_cost": exit_fee,
            "reason": reason,
        })
        pos = None

    def buy(
        sym: str,
        fill: float,
        reference_price: float,
        day: pd.Timestamp,
        sizing_equity: float,
    ) -> None:
        nonlocal cash, pos
        quantity = math.floor((max(sizing_equity, 0.0) * lev) / reference_price)
        if quantity <= 0:
            return
        notional = quantity * fill
        entry_fee = notional * cost
        cash -= notional + entry_fee
        pos = {
            "symbol": sym, "entry": fill, "stop": fill * (1.0 - spec.stop),
            "held": 0, "entry_day": day, "quantity": quantity,
            "entry_fee": entry_fee, "interest": 0.0,
        }
        fills.append({
            "time": day, "symbol": sym, "side": "buy", "price": fill,
            "quantity": quantity, "trade_cost": entry_fee, "reason": "entry",
        })

    for day in axis:
        calendar_days = 1 if previous_day is None else max((day - previous_day).days, 1)
        previous_day = day
        all_index = session_index[day]
        signal_day = all_sessions[all_index - 1] if all_index > 0 else None
        target: tuple[str, float, float] | None = None
        if signal_day is not None:
            for sym in available:
                signal_row = bar(sym, signal_day)
                if signal_row is not None and bool(signal_row["sig_entry"]):
                    fill = current_open(sym, day)
                    if fill is not None:
                        reference_price = float(signal_row["close"])
                        if not math.isfinite(reference_price) or reference_price <= 0:
                            continue
                        target = (sym, fill, reference_price)
                        break

        sizing_equity = cash
        if pos is not None:
            # LumiBot's portfolio snapshot immediately before the callback is
            # marked from the latest completed bar, not the still-forming
            # execution bar. Use that same close for replacement sizing.
            completed = bar(pos["symbol"], signal_day) if signal_day is not None else None
            if completed is not None:
                sizing_equity += pos["quantity"] * float(completed["close"])

        # LumiBot sizes during the callback, then accrues financing, then lets
        # the broker process the submitted orders. Preserve that ordering.
        if cash < 0 and margin_rate > 0:
            before = cash
            cash *= (1.0 + margin_rate / 365.0) ** calendar_days
            interest = abs(cash - before)
            total_interest += interest
            if pos is not None:
                pos["interest"] += interest

        if pos is not None:
            pos["held"] += 1
            exit_row = bar(pos["symbol"], signal_day) if signal_day is not None else None
            rsi_exit = bool(
                exit_row is not None
                and math.isfinite(float(exit_row["rsi_exit"]))
                and float(exit_row["rsi_exit"]) > spec.rsi_exit
            )
            if rsi_exit or pos["held"] >= spec.hold_days:
                fill = current_open(pos["symbol"], day)
                if fill is not None:
                    sell(fill, day, "signal")
                    if target is not None:
                        buy(*target, day, sizing_equity)
            else:
                # Native child orders become eligible on the bar after their
                # parent entry. Existing stops use the current bar's range.
                today = bar(pos["symbol"], day)
                if today is not None and float(today["low"]) <= pos["stop"]:
                    fill = min(float(today["open"]), float(pos["stop"]))
                    sell(fill, day, "stop")
        elif target is not None:
            buy(*target, day, sizing_equity)

        if pos is None:
            equity = cash
        else:
            today = bar(pos["symbol"], day)
            mark = float(today["close"]) if today is not None else pos["entry"]
            equity = cash + pos["quantity"] * mark
        equity_curve.append((day, equity))

    curve = pd.Series([v for _d, v in equity_curve], index=[d for d, _v in equity_curve], dtype="float64")
    rets = curve.pct_change().fillna(0.0)
    span = max((curve.index[-1] - curve.index[0]).days / 365.25, 1 / 365.25) if len(curve) > 1 else 1 / 365.25
    total = float(curve.iloc[-1] / initial_cash - 1.0)
    growth = float((1.0 + total) ** (1.0 / span) - 1.0) if total > -1 else -1.0
    volatility = float(rets.std(ddof=1) * math.sqrt(rets.count() / span)) if len(rets) > 1 else 0.0
    dd = curve / curve.cummax() - 1.0
    tdf = pd.DataFrame(trades)
    return {
        "spec": spec, "leverage": lev, "cost_bps": cost_bps,
        "per_side_cost_bps": cost_bps / 2.0, "margin_rate": margin_rate,
        "sessions": len(curve), "universe_used": len(available), "universe_named": len(LIQ),
        "total_return": total,
        "cagr": growth,
        "sharpe": 0.0 if not np.isfinite(volatility) or volatility == 0 else (growth - risk_free_rate) / volatility,
        "volatility": volatility,
        "max_dd": float(dd.min()),
        "final_equity": float(curve.iloc[-1]),
        "n_trades": len(tdf), "total_interest": total_interest,
        "trades": tdf, "fills": pd.DataFrame(fills), "curve": curve,
    }


def report(res: dict) -> None:
    s = res["spec"]
    print("")
    print(f"strategy           {s.name}   ({s.encoded})")
    print(f"leverage           {res['leverage']:.1f}x        cost {res['cost_bps']:.0f} bp round trip")
    print(f"universe           {res['universe_used']} of {res['universe_named']} LIQ symbols present in the archive")
    print(f"sessions           {res['sessions']}")
    print("")
    print(f"total return       {res['total_return']:+.2%}")
    print(f"CAGR               {res['cagr']:+.2%}")
    print(f"sharpe             {res['sharpe']:.3f}")
    print(f"volatility         {res['volatility']:.2%}")
    print(f"max drawdown       {res['max_dd']:.2%}")
    print(f"final equity       ${res['final_equity']:,.0f}")
    print(f"round trips        {res['n_trades']}")
    t = res["trades"]
    if len(t):
        print(f"  by exit reason   {t['reason'].value_counts().to_dict()}")
        print(f"  win rate         {(t['pnl'] > 0).mean():.1%}")
        print(f"  mean trade ret   {t['ret'].mean():+.2%}   (unlevered)")
        print(f"  median hold      {t['held_days'].median():.0f} days")
        print(f"  most traded      {dict(t['symbol'].value_counts().head(6))}")
        print(f"  total interest   ${res['total_interest']:,.0f} at {res['margin_rate']:.1%} annual")
    covered = res["curve"].index
    if len(t) and len(covered):
        # count SESSIONS held, not calendar days, or a 1-day hold that spans a
        # weekend reads as 3 days and exposure can exceed 100%.
        pos = {d: i for i, d in enumerate(covered)}
        sessions = []
        for _i, row in t.iterrows():
            lo, hi = pos.get(row["entry_day"]), pos.get(row["exit_day"])
            if lo is not None and hi is not None:
                sessions.append(max(hi - lo, 1))
        if sessions:
            print(f"  exposure         {sum(sessions) / len(covered):.0%} of sessions in a position")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--strategy", default="both", choices=("meanrev_ema", "podhajsky_gap3", "both"))
    p.add_argument("--start", default="2019-01-01")
    p.add_argument("--end", default="2026-08-01")
    p.add_argument("--cost-bps", type=float, default=7.0)
    p.add_argument("--margin-rate", type=float, default=0.05)
    p.add_argument("--leverage", type=float, default=None,
                   help="override the strategy's sleeve leverage")
    p.add_argument("--initial-cash", type=float, default=100_000.0)
    p.add_argument("--risk-free-rate", type=float, default=0.0)
    args = p.parse_args(argv)

    specs = list(SPECS.values()) if args.strategy == "both" else [SPECS[args.strategy]]
    need = sorted({s for spec in specs for s in LIQ})
    # EMA uses an infinite decaying history, so load the full available cache
    # rather than choosing an arbitrary calendar-day warmup approximation.
    warmup_start = pd.Timestamp("2018-05-01")
    frames = load_daily(need, str(warmup_start.date()), args.end)
    print(f"daily archives     {len(frames)} of {len(LIQ)} LIQ symbols available")
    print(f"indicator warmup   {warmup_start.date()} .. {pd.Timestamp(args.start).date()}")

    for spec in specs:
        res = run_backtest(spec, frames, start=args.start, end=args.end,
                           initial_cash=args.initial_cash, cost_bps=args.cost_bps,
                           margin_rate=args.margin_rate, leverage=args.leverage,
                           risk_free_rate=args.risk_free_rate)
        report(res)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
