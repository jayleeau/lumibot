"""Causal, provider-neutral implementation of the HTS v1 strategy contract.

The module deliberately has no LumiBot or Alpaca imports.  Backtests, paper
trading, and replay feed the same :class:`HtsV1DecisionCore` completed bars;
only their clock, data source, and order executor differ.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from hashlib import sha256
import json
import math
from pathlib import Path
from typing import Any, Iterable, Mapping

import numpy as np
import pandas as pd


CONTRACT_REVISION = "hts_v1_contract_1"
REQUIRED_OHLCV = {"symbol", "timestamp", "open", "high", "low", "close", "volume"}


@dataclass(frozen=True)
class HtsV1Config:
    """Versioned parameters for the reconstructed strategy supplied by the user."""

    universe: tuple[str, ...]
    top_n: int = 2
    trend_sma: int = 20
    return_period: int = 20
    atr_period: int = 14
    liquidity_period: int = 63
    min_median_dollar_volume: float = 5_000_000.0
    k_atr: float = 2.0
    target_leverage: float = 1.0
    signal_hour: int = 9
    rebalance_hour: int = 10
    regular_hours_only: bool = True
    timezone: str = "America/New_York"
    price_adjustment: str = "split"
    market_data_feed: str = "UNSPECIFIED"
    contract_revision: str = CONTRACT_REVISION

    def __post_init__(self) -> None:
        if not self.universe:
            raise ValueError("universe must not be empty")
        if len(set(self.universe)) != len(self.universe):
            raise ValueError("universe must contain each symbol once")
        if self.top_n <= 0 or self.top_n > len(self.universe):
            raise ValueError("top_n must be between one and the universe size")
        if min(self.trend_sma, self.return_period, self.atr_period, self.liquidity_period) <= 0:
            raise ValueError("indicator periods must be positive")
        if self.k_atr <= 0 or self.target_leverage <= 0:
            raise ValueError("k_atr and target_leverage must be positive")
        if not 0 <= self.signal_hour <= 23 or not 0 <= self.rebalance_hour <= 23:
            raise ValueError("hours must be between 0 and 23")
        if not self.market_data_feed or self.market_data_feed == "UNSPECIFIED":
            raise ValueError("market_data_feed must name the recorded provider feed")
        if self.price_adjustment not in {"raw", "split", "dividend", "all"}:
            raise ValueError("price_adjustment must be raw, split, dividend, or all")

    def fingerprint(self) -> str:
        """Return a stable fingerprint included in every decision journal row."""
        encoded = json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))
        return sha256(encoded.encode()).hexdigest()


@dataclass(frozen=True)
class OrderIntent:
    """An order decision, independent of any broker-specific order object."""

    decision_id: str
    timestamp: pd.Timestamp
    symbol: str
    side: str
    quantity: int
    reason: str
    reference_price: float

    def as_dict(self) -> dict[str, Any]:
        row = asdict(self)
        row["timestamp"] = self.timestamp.isoformat()
        return row


@dataclass
class PositionState:
    """Minimal state required to reproduce a virtual trailing stop."""

    quantity: int
    entry_price: float
    stop_price: float
    opened_at: pd.Timestamp

    def as_dict(self) -> dict[str, Any]:
        return {
            "quantity": self.quantity,
            "entry_price": self.entry_price,
            "stop_price": self.stop_price,
            "opened_at": self.opened_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, row: Mapping[str, Any]) -> "PositionState":
        return cls(
            quantity=int(row["quantity"]), entry_price=float(row["entry_price"]),
            stop_price=float(row["stop_price"]), opened_at=pd.Timestamp(row["opened_at"]),
        )


@dataclass(frozen=True)
class PreparedFeatures:
    """Vectorized, normalized daily and hourly inputs used by the serial core."""

    daily: pd.DataFrame
    hourly: pd.DataFrame
    input_hash: str


def _canonical_frame(
    frame: pd.DataFrame, *, config: HtsV1Config, time_basis: str
) -> pd.DataFrame:
    missing = REQUIRED_OHLCV - set(frame.columns)
    if missing:
        raise ValueError(f"bars missing required columns: {sorted(missing)}")
    out = frame.loc[:, sorted(REQUIRED_OHLCV)].copy()
    out["symbol"] = out["symbol"].astype(str)
    unknown = set(out["symbol"]) - set(config.universe)
    if unknown:
        raise ValueError(f"bars contain symbols outside config universe: {sorted(unknown)}")
    stamps = pd.to_datetime(out["timestamp"], utc=True, errors="raise")
    # Daily vendor archives frequently label a completed exchange session at
    # midnight UTC. Converting that label to New York time turns, for example,
    # Monday's session into Sunday and reintroduces daily lookahead. Hourly
    # session gates, conversely, must use exchange time.
    if time_basis == "session":
        out["timestamp"] = stamps
    elif time_basis == "exchange":
        out["timestamp"] = stamps.dt.tz_convert(config.timezone)
    else:
        raise ValueError("time_basis must be 'session' or 'exchange'")
    for column in ("open", "high", "low", "close", "volume"):
        out[column] = pd.to_numeric(out[column], errors="raise").astype(float)
    out = out.sort_values(["symbol", "timestamp"], kind="stable").reset_index(drop=True)
    if out.duplicated(["symbol", "timestamp"]).any():
        raise ValueError("bars must have one row per symbol and timestamp")
    if (out[["open", "high", "low", "close"]] <= 0).any().any() or (out["volume"] < 0).any():
        raise ValueError("OHLC prices must be positive and volume non-negative")
    return out


def _hash_frames(*frames: pd.DataFrame) -> str:
    digest = sha256()
    for frame in frames:
        digest.update(frame.to_csv(index=False, date_format="iso", float_format="%.12g").encode())
    return digest.hexdigest()


def prepare_features(
    daily_bars: pd.DataFrame, hourly_bars: pd.DataFrame, config: HtsV1Config
) -> PreparedFeatures:
    """Build causal features using vectorized group operations.

    Daily values are labelled by the session that has just completed.  The
    decision core deliberately selects with a date *strictly before* its
    intraday session, preventing accidental same-day close/volume lookahead.
    """
    daily = _canonical_frame(daily_bars, config=config, time_basis="session")
    hourly = _canonical_frame(hourly_bars, config=config, time_basis="exchange")
    daily["session"] = daily["timestamp"].dt.date
    hourly["session"] = hourly["timestamp"].dt.date
    hourly["hour"] = hourly["timestamp"].dt.hour
    if config.regular_hours_only:
        hourly = hourly.loc[hourly["hour"].between(9, 15)].copy()

    daily_group = daily.groupby("symbol", sort=False)
    daily["sma"] = daily_group["close"].transform(
        lambda values: values.rolling(config.trend_sma, min_periods=config.trend_sma).mean()
    )
    daily["return"] = daily_group["close"].transform(
        lambda values: values.pct_change(config.return_period, fill_method=None)
    )
    daily["median_dollar_volume"] = daily_group.apply(
        lambda group: (group["close"] * group["volume"]).rolling(
            config.liquidity_period, min_periods=config.liquidity_period
        ).median(), include_groups=False
    ).reset_index(level=0, drop=True).reindex(daily.index)

    hourly_group = hourly.groupby("symbol", sort=False)
    previous_close = hourly_group["close"].shift(1)
    hourly["true_range"] = np.maximum.reduce([
        (hourly["high"] - hourly["low"]).to_numpy(),
        (hourly["high"] - previous_close).abs().to_numpy(),
        (hourly["low"] - previous_close).abs().to_numpy(),
    ])
    hourly["atr"] = hourly_group["true_range"].transform(
        lambda values: values.rolling(config.atr_period, min_periods=config.atr_period).mean()
    )
    return PreparedFeatures(daily=daily, hourly=hourly, input_hash=_hash_frames(daily, hourly))


class DecisionJournal:
    """Append-only JSONL journal that makes a paper session replayable."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path
        self.rows: list[dict[str, Any]] = []

    def append(self, row: Mapping[str, Any]) -> None:
        value = json.loads(json.dumps(row, default=str, sort_keys=True))
        self.rows.append(value)
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as output:
                output.write(json.dumps(value, sort_keys=True) + "\n")

    @staticmethod
    def load(path: Path) -> list[dict[str, Any]]:
        with path.open(encoding="utf-8") as source:
            return [json.loads(line) for line in source if line.strip()]


class HtsV1DecisionCore:
    """Serial portfolio-state machine using only completed bars.

    A virtual stop is inspected only once an hourly bar is complete.  A breach
    queues a market sell which becomes eligible at the *next* supplied bar;
    this core never retrospectively credits the prior stop price.
    """

    def __init__(self, config: HtsV1Config, features: PreparedFeatures, journal: DecisionJournal | None = None) -> None:
        self.config = config
        self.features = features
        self.journal = journal or DecisionJournal()
        self.positions: dict[str, PositionState] = {}
        self.pending: list[OrderIntent] = []
        self.inflight: list[OrderIntent] = []
        self.selected: tuple[str, ...] = ()
        self.last_timestamp: pd.Timestamp | None = None

    def snapshot(self) -> dict[str, Any]:
        """Return JSON-serializable state for restart recovery."""
        return {
            "config_fingerprint": self.config.fingerprint(),
            "positions": {symbol: position.as_dict() for symbol, position in self.positions.items()},
            "pending": [intent.as_dict() for intent in self.pending],
            "inflight": [intent.as_dict() for intent in self.inflight],
            "selected": list(self.selected),
            "last_timestamp": self.last_timestamp.isoformat() if self.last_timestamp is not None else None,
        }

    def restore(self, state: Mapping[str, Any]) -> None:
        """Restore state only when it was produced by the identical contract."""
        if state.get("config_fingerprint") != self.config.fingerprint():
            raise ValueError("cannot restore HTS state created with another config")
        self.positions = {symbol: PositionState.from_dict(row) for symbol, row in state.get("positions", {}).items()}
        self.pending = [
            OrderIntent(
                decision_id=row["decision_id"], timestamp=pd.Timestamp(row["timestamp"]), symbol=row["symbol"],
                side=row["side"], quantity=int(row["quantity"]), reason=row["reason"],
                reference_price=float(row["reference_price"]),
            ) for row in state.get("pending", [])
        ]
        self.inflight = [
            OrderIntent(
                decision_id=row["decision_id"], timestamp=pd.Timestamp(row["timestamp"]), symbol=row["symbol"],
                side=row["side"], quantity=int(row["quantity"]), reason=row["reason"],
                reference_price=float(row["reference_price"]),
            ) for row in state.get("inflight", [])
        ]
        self.selected = tuple(state.get("selected", []))
        stamp = state.get("last_timestamp")
        self.last_timestamp = pd.Timestamp(stamp) if stamp else None

    def reconcile(self, broker_quantities: Mapping[str, int]) -> None:
        """Fail closed if broker holdings disagree with decision state."""
        expected = {symbol: position.quantity for symbol, position in self.positions.items() if position.quantity}
        actual = {str(symbol): int(quantity) for symbol, quantity in broker_quantities.items() if int(quantity)}
        if expected != actual:
            raise RuntimeError(f"HTS position reconciliation failed: local={expected}, broker={actual}")

    def _daily_selection(self, session: object) -> tuple[str, ...]:
        rows = self.features.daily.loc[self.features.daily["session"] < session]
        if rows.empty:
            return ()
        latest = rows.groupby("symbol", sort=False)["session"].transform("max") == rows["session"]
        candidates = rows.loc[latest & rows["symbol"].isin(self.config.universe)].copy()
        candidates = candidates.loc[
            (candidates["close"] > candidates["sma"])
            & (candidates["median_dollar_volume"] >= self.config.min_median_dollar_volume)
            & candidates["return"].notna()
        ]
        order = {symbol: index for index, symbol in enumerate(self.config.universe)}
        candidates["_universe_order"] = candidates["symbol"].map(order)
        ranked = candidates.sort_values(["return", "_universe_order"], ascending=[False, True], kind="stable")
        return tuple(ranked["symbol"].head(self.config.top_n))

    def _bar(self, timestamp: pd.Timestamp) -> pd.DataFrame:
        bars = self.features.hourly.loc[self.features.hourly["timestamp"] == timestamp]
        if bars.empty:
            raise ValueError(f"no hourly bars for {timestamp.isoformat()}")
        return bars.set_index("symbol", drop=False)

    def _intent(self, timestamp: pd.Timestamp, symbol: str, side: str, quantity: int, reason: str, reference_price: float) -> OrderIntent:
        key = f"{self.config.fingerprint()}|{timestamp.isoformat()}|{symbol}|{side}|{quantity}|{reason}"
        return OrderIntent(sha256(key.encode()).hexdigest()[:20], timestamp, symbol, side, quantity, reason, float(reference_price))

    def process_completed_bar(self, timestamp: pd.Timestamp, portfolio_value: float) -> list[OrderIntent]:
        """Process one completed timestamp and return new broker order intents.

        ``portfolio_value`` is supplied by the caller from the broker or the
        deterministic simulator.  This is the only account-specific input.
        """
        timestamp = pd.Timestamp(timestamp)
        if timestamp.tzinfo is None:
            raise ValueError("timestamp must be timezone-aware")
        timestamp = timestamp.tz_convert(self.config.timezone)
        if self.last_timestamp is not None and timestamp <= self.last_timestamp:
            raise ValueError("completed bars must be processed once in ascending order")
        bars = self._bar(timestamp)
        new: list[OrderIntent] = []

        # Orders generated at a prior completed bar are executable now.  The
        # executor owns actual fills; the core updates its state via acknowledge_fill.
        executable = [intent for intent in self.pending if timestamp > intent.timestamp]
        self.pending = [intent for intent in self.pending if timestamp <= intent.timestamp]
        self.inflight.extend(executable)
        new.extend(executable)

        for symbol, position in list(self.positions.items()):
            if symbol not in bars.index:
                continue
            bar = bars.loc[symbol]
            close, atr = float(bar["close"]), float(bar["atr"])
            outstanding = self.pending + self.inflight + new
            if close <= position.stop_price and not any(intent.symbol == symbol and intent.side == "sell" for intent in outstanding):
                intent = self._intent(timestamp, symbol, "sell", position.quantity, "virtual_stop_breach", close)
                self.pending.append(intent)
                new.append(intent)
                continue
            if math.isfinite(atr) and atr > 0:
                position.stop_price = max(position.stop_price, close - self.config.k_atr * atr)

        if int(timestamp.hour) == self.config.signal_hour:
            self.selected = self._daily_selection(timestamp.date())
        if int(timestamp.hour) == self.config.rebalance_hour:
            target = set(self.selected)
            for symbol, position in list(self.positions.items()):
                if symbol not in target and not any(intent.symbol == symbol and intent.side == "sell" for intent in self.pending + self.inflight + new):
                    if symbol in bars.index:
                        intent = self._intent(timestamp, symbol, "sell", position.quantity, "selection_change", float(bars.loc[symbol, "close"]))
                        self.pending.append(intent)
                        new.append(intent)
            occupied = set(self.positions) | {intent.symbol for intent in self.pending if intent.side == "buy"}
            for symbol in self.selected:
                if symbol in occupied or symbol not in bars.index:
                    continue
                bar = bars.loc[symbol]
                price, atr = float(bar["close"]), float(bar["atr"])
                if not (price > 0 and math.isfinite(atr) and atr > 0):
                    continue
                quantity = math.floor(portfolio_value * (self.config.target_leverage / self.config.top_n) / price)
                if quantity:
                    intent = self._intent(timestamp, symbol, "buy", quantity, "selection_entry", price)
                    self.pending.append(intent)
                    new.append(intent)

        self.last_timestamp = timestamp
        record = {
            "event": "decision", "timestamp": timestamp.isoformat(), "input_hash": self.features.input_hash,
            "config_fingerprint": self.config.fingerprint(), "selected": list(self.selected),
            "positions": {symbol: value.as_dict() for symbol, value in self.positions.items()},
            "orders": [intent.as_dict() for intent in new],
        }
        self.journal.append(record)
        return new

    def acknowledge_fill(self, intent: OrderIntent, fill_price: float, filled_at: pd.Timestamp) -> None:
        """Apply an executor-reported full fill; partial fills remain executor-owned."""
        if fill_price <= 0 or not math.isfinite(fill_price):
            raise ValueError("fill_price must be finite and positive")
        if intent.side == "buy":
            # The initial stop uses the ATR that was available when the entry
            # was decided. A fill arriving in a later volatile bar must not
            # read that later bar's completed ATR backwards into the signal.
            bar = self._bar(intent.timestamp)
            atr = float(bar.loc[intent.symbol, "atr"])
            if not math.isfinite(atr) or atr <= 0:
                raise ValueError("cannot establish a position without a completed ATR")
            self.positions[intent.symbol] = PositionState(intent.quantity, float(fill_price), float(fill_price - self.config.k_atr * atr), pd.Timestamp(filled_at))
        elif intent.side == "sell":
            self.positions.pop(intent.symbol, None)
        else:
            raise ValueError(f"unsupported side: {intent.side}")
        self.inflight = [item for item in self.inflight if item.decision_id != intent.decision_id]
        self.journal.append({"event": "fill", "intent": intent.as_dict(), "fill_price": fill_price, "filled_at": pd.Timestamp(filled_at).isoformat()})


def compare_decision_journals(expected: Iterable[Mapping[str, Any]], actual: Iterable[Mapping[str, Any]]) -> None:
    """Raise at the first decision mismatch, pinpointing its bar and field."""
    left = [row for row in expected if row.get("event") == "decision"]
    right = [row for row in actual if row.get("event") == "decision"]
    if len(left) != len(right):
        raise AssertionError(f"decision count differs: expected {len(left)}, got {len(right)}")
    fields = ("timestamp", "input_hash", "config_fingerprint", "selected", "orders")
    for index, (before, after) in enumerate(zip(left, right)):
        for field in fields:
            if before.get(field) != after.get(field):
                raise AssertionError(f"decision {index} differs in {field}: {before.get(field)!r} != {after.get(field)!r}")
