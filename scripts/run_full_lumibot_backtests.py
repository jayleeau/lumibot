#!/usr/bin/env python3
"""Run the cached strategy suite through LumiBot's native backtest engine.

The research harnesses in ``strategy_lab`` are useful for fast signal studies, but
they do not exercise LumiBot's broker, order lifecycle, portfolio valuation, or
trade-event export.  This runner keeps their indicator definitions and local
archives, then routes orders through ``Strategy.backtest`` and
``BacktestingBroker``.

The runner deliberately uses a vectorized preparation phase and a small callback
phase.  Indicators are calculated once from completed bars; the native engine
still owns simulated time, order submission, next-bar market fills, stop fills,
fees, cash, positions, and performance analysis.
"""
from __future__ import annotations

import argparse
import bisect
import json
import math
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

# This runner is deliberately offline. Mark the process as a backtest before
# importing LumiBot so repository-adjacent .env files cannot auto-create a live
# broker or start a trading stream.
os.environ["IS_BACKTESTING"] = "true"
os.environ["LUMIBOT_DISABLE_DOTENV"] = "true"

import duckdb
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lumibot.backtesting import PandasDataBacktesting
from lumibot.entities import Asset, Data, Order, TradingFee
from lumibot.strategies import Strategy
from strategy_lab.daily_fleet_backtest import (
    LIQ,
    MEANREV_EMA,
    PODHAJSKY_GAP3,
    prepare as prepare_daily,
)
from strategy_lab.hts_backtest import (
    DEFAULT_UNIVERSE,
    build_daily_sig,
    build_hour_map,
)

DAILY_DB = ROOT / "short" / "suite_monitored_xnas_itch_daily_adjusted.duckdb"
HOURLY_DB = ROOT / "short" / "suite_v2_xnas_itch_hourly_adjusted.duckdb"
REPORT_DIR = ROOT / "reports" / "full_lumibot_corrected_2020_2026"
ET = "America/New_York"
USD = Asset("USD", "forex")
ROUND_TRIP_COST_BPS = 7.0
PER_SIDE_COST = ROUND_TRIP_COST_BPS / 2.0 / 10_000.0
MARGIN_DEBIT_RATE = 0.05
FEE = TradingFee(percent_fee=PER_SIDE_COST)


def _sql_frames(
    db: Path,
    table: str,
    symbols: list[str],
    start: str,
    end: str,
    *,
    hourly: bool,
) -> dict[str, pd.DataFrame]:
    """Load adjusted OHLCV frames from a local DuckDB archive.

    ``hourly=False`` preserves the archive's UTC session date for indicator
    lookups.  ``hourly=True`` converts timestamps to naive exchange-local time
    because the strategy's entry and exit hours are defined in New York time.
    """
    if not symbols:
        return {}
    placeholders = ", ".join("?" for _ in symbols)
    query = (
        f"SELECT symbol, ts, open, high, low, close, volume FROM {table} "
        f"WHERE symbol IN ({placeholders}) AND ts >= ? AND ts <= ? "
        "ORDER BY symbol, ts"
    )
    # The cache timestamps can be stored in an Australia/Melbourne display
    # zone.  Bound the SQL query by the complete UTC day so an America/New_York
    # session on the requested end date is not lost to timezone conversion.
    end_bound = pd.Timestamp(end, tz="UTC") + pd.Timedelta(days=1) - pd.Timedelta(microseconds=1)
    with duckdb.connect(str(db), read_only=True) as con:
        rows = con.execute(
            query,
            [*symbols, pd.Timestamp(start, tz="UTC"), end_bound],
        ).fetchdf()
    if rows.empty:
        return {}

    rows["ts"] = pd.to_datetime(rows["ts"], utc=True)
    result: dict[str, pd.DataFrame] = {}
    for symbol, group in rows.groupby("symbol", sort=False):
        group = group.sort_values("ts").copy()
        ts_utc = group.pop("ts")
        if hourly:
            index = ts_utc.dt.tz_convert(ET).dt.tz_localize(None)
        else:
            # The daily archive stores session bars at midnight UTC.  Keeping
            # the UTC calendar date avoids shifting a session to the prior ET
            # evening when deriving completed-bar signals.
            session_date = ts_utc.dt.tz_localize(None).dt.normalize()
            index = session_date
        group.index = pd.DatetimeIndex(index)
        group = group[["open", "high", "low", "close", "volume"]].astype(float)
        group = group[~group.index.duplicated(keep="last")].sort_index()
        result[str(symbol)] = group
    return result


def _daily_lumibot_data(frame: pd.DataFrame) -> pd.DataFrame:
    """Place a UTC-session daily frame at the NYSE open for LumiBot."""
    index = frame.index.tz_localize(ET) + pd.Timedelta(hours=9, minutes=30)
    result = frame[["open", "high", "low", "close", "volume"]].copy()
    result.index = index.tz_localize(None)
    return result


def _hourly_lumibot_data(frame: pd.DataFrame) -> pd.DataFrame:
    """Return an exchange-local hourly frame with a naive index."""
    result = frame[["open", "high", "low", "close", "volume"]].copy()
    result.index = pd.DatetimeIndex(result.index).tz_localize(None)
    return result


def _hts_lumibot_data(frame: pd.DataFrame) -> pd.DataFrame:
    """Map archived 09:00-15:00 ET bars onto NYSE hourly timestamps."""
    source = frame[(frame.index.hour >= 9) & (frame.index.hour <= 15)].copy()
    source.index = source.index.normalize() + pd.to_timedelta(source.index.hour - 9, unit="h")
    source.index = source.index + pd.Timedelta(hours=9, minutes=30)
    return source[["open", "high", "low", "close", "volume"]]


def _asset_data(frames: dict[str, pd.DataFrame], *, hourly: bool, hts: bool = False) -> list[Data]:
    """Build LumiBot ``Data`` objects from base-symbol frames."""
    builder = _hts_lumibot_data if hts else (_hourly_lumibot_data if hourly else _daily_lumibot_data)
    return [
        Data(Asset(symbol), builder(frame), timestep="hour" if hourly else "day", quote=USD)
        for symbol, frame in frames.items()
    ]


@dataclass
class DailyContext:
    """Prepared daily frames and strategy parameters for one native run."""

    frames: dict[str, pd.DataFrame]
    ordered_symbols: list[str]
    leverage: float
    stop: float
    hold_days: int
    rsi_exit: float


_DAILY_CONTEXT: DailyContext | None = None


class NativeDailyFleetStrategy(Strategy):
    """Single-slot daily fleet strategy executed by LumiBot's broker."""

    def initialize(self) -> None:
        self.set_market("NYSE")
        self.sleeptime = "1D"
        self._ctx = _DAILY_CONTEXT
        if self._ctx is None:
            raise RuntimeError("daily strategy context was not initialized")
        self._active_symbol: str | None = None
        self._active_qty = 0.0
        self._entry_price = 0.0
        self._stop_price = 0.0
        self._stop_order: Order | None = None
        self._entry_pending = False
        self._exit_pending = False
        self._held_bars = 0
        self._sessions = sorted({day for frame in self._ctx.frames.values() for day in frame.index})

    def _row(self, symbol: str, day: pd.Timestamp) -> pd.Series | None:
        frame = self._ctx.frames[symbol]
        try:
            row = frame.loc[day]
        except KeyError:
            return None
        return row if isinstance(row, pd.Series) else row.iloc[-1]

    def _previous_session(self, day: pd.Timestamp) -> pd.Timestamp | None:
        """Return the last fully completed session before ``day``."""
        index = bisect.bisect_left(self._sessions, day) - 1
        return self._sessions[index] if index >= 0 else None

    def _signal_target(self, signal_day: pd.Timestamp) -> tuple[str, float] | None:
        """Return the first LIQ-order entry signal from a completed session."""
        for symbol in self._ctx.ordered_symbols:
            row = self._row(symbol, signal_day)
            if row is None or not bool(row.get("sig_entry", False)):
                continue
            reference_price = float(row["close"])
            if math.isfinite(reference_price) and reference_price > 0:
                return symbol, reference_price
        return None

    def _submit_exit(self) -> None:
        if self._active_symbol is None or self._exit_pending:
            return
        if self._stop_order is not None and self._stop_order.is_active():
            self.cancel_order(self._stop_order)
        order = self.create_order(
            self._active_symbol,
            quantity=self._active_qty,
            side="sell",
            order_type="market",
        )
        self._exit_pending = True
        self.submit_order(order)

    def _sizing_value(self, signal_day: pd.Timestamp) -> float:
        """Value the book only with cash and the last completed daily close."""
        value = float(self.cash or 0.0)
        if self._active_symbol is not None:
            row = self._row(self._active_symbol, signal_day)
            if row is not None:
                value += self._active_qty * float(row["close"])
        return max(value, 0.0)

    def _submit_entry(self, symbol: str, reference_price: float, sizing_value: float) -> None:
        """Submit an entry sized from information available before the open."""
        if self._entry_pending:
            return
        quantity = math.floor((sizing_value * self._ctx.leverage) / reference_price)
        if quantity <= 0:
            return
        order = self.create_order(symbol, quantity=quantity, side="buy", order_type="market")
        self._entry_pending = True
        self.submit_order(order)

    def on_trading_iteration(self) -> None:
        now = self.get_datetime()
        day = pd.Timestamp(now.date())
        signal_day = self._previous_session(day)
        if signal_day is None:
            return
        target = self._signal_target(signal_day)
        sizing_value = self._sizing_value(signal_day)

        if self._active_symbol is not None:
            self._held_bars += 1
            row = self._row(self._active_symbol, signal_day)
            rsi_exit = bool(
                row is not None
                and math.isfinite(float(row["rsi_exit"]))
                and float(row["rsi_exit"]) > self._ctx.rsi_exit
            )
            if rsi_exit or self._held_bars >= self._ctx.hold_days:
                self._submit_exit()
                if target is not None:
                    self._submit_entry(*target, sizing_value)
            return

        if self._entry_pending or self._exit_pending:
            return
        if target is not None:
            self._submit_entry(*target, sizing_value)

    def on_filled_order(
        self,
        position: Any,
        order: Order,
        price: float,
        quantity: float,
        multiplier: float,
    ) -> None:
        """Attach a native stop after entry and clear state after exits."""
        symbol = order.asset.symbol
        if order.is_buy_order():
            self._entry_pending = False
            self._active_symbol = symbol
            self._active_qty = float(quantity)
            self._entry_price = float(price)
            self._held_bars = 0
            self._stop_price = self._entry_price * (1.0 - self._ctx.stop)
            self._stop_order = self.create_order(
                symbol,
                quantity=quantity,
                side="sell",
                order_type="stop",
                stop_price=self._stop_price,
                time_in_force="gtc",
            )
            self.submit_order(self._stop_order)
            return

        self._exit_pending = False
        self._entry_pending = False
        self._active_symbol = None
        self._active_qty = 0.0
        self._entry_price = 0.0
        self._stop_price = 0.0
        self._stop_order = None
        self._held_bars = 0


@dataclass
class HtsContext:
    """Prepared hourly and daily maps for the native HTS run."""

    hourly_frames: dict[str, pd.DataFrame]
    hour_map: dict[str, dict]
    daily_map: dict[str, dict]
    spy_map: dict
    ordered_symbols: list[str]
    top_n: int
    k_atr: float
    target_leverage: float
    min_mdv: float
    trend_filter: bool
    signal_hour: int
    execution_hour: int


_HTS_CONTEXT: HtsContext | None = None


class NativeHtsStrategy(Strategy):
    """Hourly trend-selection and ATR trailing-stop strategy on native LumiBot."""

    def initialize(self) -> None:
        # The source labels bars on the hour; _hts_lumibot_data maps those bars
        # to the NYSE half-hour clock while preserving their OHLC values.
        self.set_market("NYSE")
        self.sleeptime = "1H"
        self._ctx = _HTS_CONTEXT
        if self._ctx is None:
            raise RuntimeError("HTS strategy context was not initialized")
        self._positions: dict[str, dict[str, Any]] = {}
        self._pending_buys: set[str] = set()
        self._pending_sells: set[str] = set()

        self._daily_dates = sorted({day for values in self._ctx.daily_map.values() for day in values})
        self._hour_days = sorted({day for values in self._ctx.hour_map.values() for day in values})

    def _selection(self, day: datetime.date) -> list[str]:
        if not self._daily_dates:
            return []
        index = bisect.bisect_left(self._daily_dates, day) - 1
        if index < 0:
            return []
        previous = self._daily_dates[index]
        ranked: list[tuple[str, float, float]] = []
        for symbol in self._ctx.ordered_symbols:
            row = self._ctx.daily_map.get(symbol, {}).get(previous)
            if row is None:
                continue
            close, sma20, ret20, annvol, mdv, _sma200 = row
            if any(pd.isna(value) for value in (close, sma20, ret20, mdv)):
                continue
            if float(mdv) < self._ctx.min_mdv:
                continue
            if self._ctx.trend_filter and float(close) <= float(sma20):
                continue
            ranked.append((symbol, float(ret20), float(annvol) if not pd.isna(annvol) else math.nan))
        ranked.sort(key=lambda item: item[1], reverse=True)
        return [symbol.removesuffix(".US") for symbol, _ret, _vol in ranked[: self._ctx.top_n]]

    def _hour_values(self, symbol: str, day: datetime.date, hour: int) -> tuple[float, float, float] | None:
        day_map = self._ctx.hour_map.get(symbol + ".US", {}).get(day)
        if day_map is None:
            return None
        closes, atrs, opens, _highs, _lows = day_map
        close = closes.get(hour)
        atr = atrs.get(hour)
        open_ = opens.get(hour)
        if close is None or open_ is None:
            return None
        return float(open_), float(close), float(atr) if atr is not None else math.nan

    def _submit_sell(self, symbol: str) -> None:
        state = self._positions.get(symbol)
        if state is None or symbol in self._pending_sells:
            return
        stop_order = state.get("stop_order")
        if stop_order is not None and stop_order.is_active():
            self.cancel_order(stop_order)
        order = self.create_order(symbol, quantity=state["quantity"], side="sell", order_type="market")
        self._pending_sells.add(symbol)
        self.submit_order(order)

    def _sizing_value(self, day: datetime.date) -> float:
        """Value positions from the completed signal hour for entry sizing."""
        value = float(self.cash or 0.0)
        for symbol, state in self._positions.items():
            values = self._hour_values(symbol, day, self._ctx.signal_hour)
            if values is not None:
                value += float(state["quantity"]) * values[1]
        return max(value, 0.0)

    def _rebalance(self, day: datetime.date) -> None:
        selected = self._selection(day)
        selected_set = set(selected)
        sizing_value = self._sizing_value(day)

        for symbol in list(self._positions):
            if symbol not in selected_set:
                self._submit_sell(symbol)

        capacity = self._ctx.top_n - len(self._positions) - len(self._pending_buys) + len(self._pending_sells)
        if capacity <= 0:
            return
        reference_hour = self._ctx.signal_hour
        for symbol in selected:
            if capacity <= 0:
                break
            if symbol in self._positions or symbol in self._pending_buys or symbol in self._pending_sells:
                continue
            values = self._hour_values(symbol, day, reference_hour)
            if values is None:
                continue
            _open, close, _atr = values
            if not math.isfinite(close) or close <= 0:
                continue
            quantity = math.floor((sizing_value * self._ctx.target_leverage / self._ctx.top_n) / close)
            if quantity <= 0:
                continue
            order = self.create_order(symbol, quantity=quantity, side="buy", order_type="market")
            self._pending_buys.add(symbol)
            self.submit_order(order)
            capacity -= 1

    def _update_trailing_stops(self, day: datetime.date, hour: int) -> None:
        completed_day = day
        completed_hours = [hour - 1]
        if completed_hours[0] < self._ctx.signal_hour:
            index = bisect.bisect_left(self._hour_days, day) - 1
            if index < 0:
                return
            completed_day = self._hour_days[index]
            # The NYSE hourly loop may not invoke the strategy on the archive's
            # final 15:00-labelled bar. Catch up every completed source bar that
            # followed the last 14:30 callback before processing the new open.
            completed_hours = [14, 15]
        for symbol, state in list(self._positions.items()):
            if symbol in self._pending_sells:
                continue
            candidates: list[float] = []
            for completed_hour in completed_hours:
                values = self._hour_values(symbol, completed_day, completed_hour)
                if values is None:
                    continue
                _open, close, atr = values
                if math.isfinite(atr) and atr > 0:
                    candidates.append(close - self._ctx.k_atr * atr)
            if not candidates:
                continue
            candidate = max(candidates)
            if candidate <= state["stop_price"]:
                continue
            stop_order = state.get("stop_order")
            if stop_order is not None and stop_order.is_active():
                self.modify_order(stop_order, stop_price=float(candidate))
            state["stop_price"] = float(candidate)

    def on_trading_iteration(self) -> None:
        now = self.get_datetime()
        day = now.date()
        hour = now.hour
        if hour == self._ctx.execution_hour:
            self._rebalance(day)
        if hour >= self._ctx.signal_hour:
            self._update_trailing_stops(day, hour)

    def before_market_closes(self) -> None:
        """Activate the 14:00-bar trail before the final 15:00 bar is filled."""
        now = self.get_datetime()
        if now.hour >= 15:
            self._update_trailing_stops(now.date(), 15)

    def on_filled_order(
        self,
        position: Any,
        order: Order,
        price: float,
        quantity: float,
        multiplier: float,
    ) -> None:
        symbol = order.asset.symbol
        if order.is_buy_order():
            self._pending_buys.discard(symbol)
            current = self.get_datetime()
            # Use only the explicitly completed signal bar for the initial ATR.
            values = self._hour_values(symbol, current.date(), self._ctx.signal_hour)
            atr = values[2] if values is not None else math.nan
            if not math.isfinite(atr) or atr <= 0:
                atr = float(price) * 0.02
            stop_price = float(price) - self._ctx.k_atr * atr
            stop_order = self.create_order(
                symbol,
                quantity=quantity,
                side="sell",
                order_type="stop",
                stop_price=stop_price,
                time_in_force="gtc",
            )
            self._positions[symbol] = {
                "quantity": float(quantity),
                "entry_price": float(price),
                "stop_price": stop_price,
                "stop_order": stop_order,
            }
            self.submit_order(stop_order)
            return

        self._pending_sells.discard(symbol)
        self._pending_buys.discard(symbol)
        self._positions.pop(symbol, None)


def _run_native(
    strategy_class: type[Strategy],
    *,
    name: str,
    pandas_data: list[Data],
    start: datetime,
    end: datetime,
    out_dir: Path,
    sleeptime: str,
) -> dict[str, Any]:
    """Run one class through LumiBot and return its native analysis."""
    out_dir.mkdir(parents=True, exist_ok=True)
    prefix = out_dir / name
    started = time.perf_counter()
    result, _strategy = strategy_class.run_backtest(
        datasource_class=PandasDataBacktesting,
        pandas_data=pandas_data,
        backtesting_start=start,
        backtesting_end=end,
        sleeptime=sleeptime,
        minutes_before_opening=0,
        minutes_before_closing=0,
        stats_file=str(prefix.with_name(prefix.name + "_stats.csv")),
        trades_file=str(prefix.with_name(prefix.name + "_trades.csv")),
        settings_file=str(prefix.with_name(prefix.name + "_settings.json")),
        tearsheet_file=str(prefix.with_name(prefix.name + "_tearsheet.html")),
        tearsheet_metrics_file=str(prefix.with_name(prefix.name + "_metrics.json")),
        benchmark_asset=None,
        budget=100_000.0,
        risk_free_rate=0.0,
        parameters={
            "cash_financing": {
                "enabled": True,
                "account_mode": "margin",
                "day_count_basis": 365,
                "credit_rate_annual": 0.0,
                "debit_rate_annual": MARGIN_DEBIT_RATE,
            }
        },
        buy_trading_fees=[FEE],
        sell_trading_fees=[FEE],
        auto_adjust=False,
        analyze_backtest=True,
        show_plot=False,
        show_tearsheet=False,
        save_tearsheet=False,
        show_indicators=False,
        save_logfile=False,
        show_progress_bar=False,
        quiet_logs=True,
    )
    # The returned Trader payload can precede the executor's final stats dump.
    # Read the completed strategy analysis so the JSON matches the CSV's final
    # cash and marked positions, including fills on the last simulated bar.
    result = dict(_strategy.analysis)
    elapsed = time.perf_counter() - started
    payload = {"strategy": name, "runtime_seconds": elapsed, "analysis": result}
    with prefix.with_name(prefix.name + "_run.json").open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, default=str)
    return payload


def _prepare_daily(start: str, end: str) -> tuple[dict[str, pd.DataFrame], list[Data]]:
    """Load enough daily history for both indicator warmups."""
    raw = _sql_frames(DAILY_DB, "bars_daily", LIQ, "2018-05-01", end, hourly=False)
    ordered = [symbol for symbol in LIQ if symbol in raw]
    prepared = prepare_daily({symbol: raw[symbol] for symbol in ordered}, MEANREV_EMA)
    # The two daily strategies share the same bars, but require different
    # indicators.  Add the second set to each frame so the native class can be
    # reused with either context without changing the source definitions.
    gap_prepared = prepare_daily({symbol: raw[symbol] for symbol in ordered}, PODHAJSKY_GAP3)
    for symbol in ordered:
        prepared[symbol]["gap_sig_entry"] = gap_prepared[symbol]["sig_entry"]
        prepared[symbol]["gap_rsi_exit"] = gap_prepared[symbol]["rsi_exit"]
    return prepared, _asset_data(raw, hourly=False)


def _prepare_hts(start: str, end: str) -> tuple[HtsContext, list[Data]]:
    """Load and prepare the six-year hourly HTS input set."""
    symbols = [symbol for symbol in DEFAULT_UNIVERSE]
    hourly = _sql_frames(HOURLY_DB, "bars_hourly", symbols, "2019-01-01", end, hourly=True)
    ordered = [symbol for symbol in symbols if symbol in hourly]
    hdf = pd.concat(
        [frame.assign(symbol=symbol + ".US").reset_index(names="ts") for symbol, frame in hourly.items()],
        ignore_index=True,
    )
    hdf["ts"] = pd.to_datetime(hdf["ts"])
    d_raw = _sql_frames(DAILY_DB, "bars_daily", ordered, "2018-05-01", end, hourly=False)
    ddf = pd.concat(
        [frame.assign(symbol=symbol + ".US").reset_index(names="ts") for symbol, frame in d_raw.items()],
        ignore_index=True,
    )
    ddf["ts"] = pd.to_datetime(ddf["ts"])
    hour_map = build_hour_map(hdf)
    daily_map, spy_map = build_daily_sig(ddf)
    context = HtsContext(
        hourly_frames=hourly,
        hour_map=hour_map,
        daily_map=daily_map,
        spy_map=spy_map,
        ordered_symbols=[symbol + ".US" for symbol in ordered],
        top_n=2,
        k_atr=2.0,
        target_leverage=1.0,
        min_mdv=5e6,
        trend_filter=True,
        signal_hour=9,
        execution_hour=10,
    )
    return context, _asset_data(hourly, hourly=True, hts=True)


def run_suite(start: str, end: str, out_dir: Path, only: str = "all") -> list[dict[str, Any]]:
    """Run selected strategies through LumiBot's native backtest engine."""
    global _DAILY_CONTEXT, _HTS_CONTEXT
    start_dt = datetime.fromisoformat(f"{start}T00:00:00")
    end_dt = datetime.fromisoformat(f"{end}T23:59:59")
    daily_results: list[dict[str, Any]] = []

    if only in {"all", "daily"}:
        daily_frames, daily_data = _prepare_daily(start, end)
        for name, spec, signal_column, rsi_column in (
            ("meanrev_ema_native", MEANREV_EMA, "sig_entry", "rsi_exit"),
            ("podhajsky_gap_native", PODHAJSKY_GAP3, "gap_sig_entry", "gap_rsi_exit"),
        ):
            context_frames = {symbol: frame.copy() for symbol, frame in daily_frames.items()}
            for frame in context_frames.values():
                frame["sig_entry"] = frame[signal_column]
                frame["rsi_exit"] = frame[rsi_column]
            _DAILY_CONTEXT = DailyContext(
                frames=context_frames,
                ordered_symbols=[symbol for symbol in LIQ if symbol in context_frames],
                leverage=spec.sleeve,
                stop=spec.stop,
                hold_days=spec.hold_days,
                rsi_exit=spec.rsi_exit,
            )
            daily_results.append(
                _run_native(
                    NativeDailyFleetStrategy,
                    name=name,
                    pandas_data=daily_data,
                    start=start_dt,
                    end=end_dt,
                    out_dir=out_dir,
                    sleeptime="1D",
                )
            )

    if only in {"all", "hts"}:
        _HTS_CONTEXT, hts_data = _prepare_hts(start, end)
        daily_results.append(
            _run_native(
                NativeHtsStrategy,
                name="hourly_trend_stop_native",
                pandas_data=hts_data,
                start=start_dt,
                end=end_dt,
                out_dir=out_dir,
                sleeptime="1H",
            )
        )
    return daily_results


def main(argv: list[str] | None = None) -> int:
    """Parse the window, run all native strategies, and print JSON summaries."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", default="2020-09-08")
    parser.add_argument("--end", default="2026-09-08")
    parser.add_argument("--out-dir", default=str(REPORT_DIR))
    parser.add_argument("--only", choices=("all", "daily", "hts"), default="all")
    args = parser.parse_args(argv)
    out_dir = Path(args.out_dir)
    results = run_suite(args.start, args.end, out_dir, only=args.only)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "suite_run.json").write_text(
        json.dumps(
            {"start": args.start, "end": args.end, "only": args.only, "results": results},
            indent=2,
            default=str,
        )
        + "\n",
        encoding="utf-8",
    )
    print(json.dumps(results, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
