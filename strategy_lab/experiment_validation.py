"""Independent audit of native HTS result artifacts.

Plan of record: ``docs/HTS_REMAINING_IMPLEMENTATION_PLAN.md`` sections 2 and 7.

This module deliberately does **not** import the strategy's feature or metric
functions.  It rebuilds daily equity, returns, arithmetic Sharpe, total return,
CAGR, volatility, drawdown, cash, fees, and terminal positions straight from the
emitted ``run_stats.csv`` and ``run_trades.csv`` files, then compares them to the
metrics the runner recorded.  Reusing the runner's own code would only prove a
calculation agrees with itself.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from typing import Any, Mapping

import pandas as pd

TRADING_DAYS = 252.0
INITIAL_CASH = 100_000.0


@dataclass
class RunAudit:
    """The independent view of one stored run."""

    run_dir: Path
    candidate_id: str
    window: str
    ok: bool
    findings: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    recorded: dict[str, Any] = field(default_factory=dict)
    fills: int = 0
    fees: float = 0.0
    net_positions: dict[str, float] = field(default_factory=dict)


def independent_metrics(stats_path: Path, *, initial_cash: float = INITIAL_CASH) -> dict[str, Any]:
    """Recompute the standard daily metrics with an independent implementation."""
    frame = pd.read_csv(stats_path)
    stamps = pd.to_datetime(frame["datetime"], utc=True)
    daily = (
        frame.assign(_session=stamps.dt.strftime("%Y-%m-%d"), _stamp=stamps)
        .sort_values("_stamp")
        .groupby("_session", sort=True)["portfolio_value"]
        .last()
        .astype("float64")
    )
    sessions = int(len(daily))
    start_equity = float(initial_cash)
    final_equity = float(daily.iloc[-1]) if sessions else float("nan")
    if sessions > 1:
        returns = daily.pct_change().to_numpy()[1:]
        mean = float(returns.mean())
        stdev = float(returns.std(ddof=1))
        volatility = stdev * math.sqrt(TRADING_DAYS)
        sharpe = mean / stdev * math.sqrt(TRADING_DAYS) if stdev else 0.0
        span_years = (
            datetime.fromisoformat(daily.index[-1]).toordinal()
            - datetime.fromisoformat(daily.index[0]).toordinal()
        ) / 365.25
        span_years = max(span_years, 1e-9)
        cagr = (final_equity / start_equity) ** (1.0 / span_years) - 1.0
        running_max = daily.cummax()
        drawdown = float(-(daily / running_max - 1.0).min())
    else:
        volatility = 0.0
        sharpe = 0.0
        cagr = float("nan")
        drawdown = float("nan")
    return {
        "sessions": sessions,
        "first_session": daily.index[0] if sessions else None,
        "last_session": daily.index[-1] if sessions else None,
        "final_equity": final_equity,
        "total_return": final_equity / start_equity - 1.0 if sessions else float("nan"),
        "cagr": cagr,
        "volatility": volatility,
        "sharpe": sharpe,
        "max_drawdown": drawdown,
        "min_cash": float(frame["cash"].min()) if "cash" in frame else None,
    }


def reconcile_trades(trades_path: Path) -> tuple[dict[str, float], float, int, list[str]]:
    """Net filled position per symbol, total fees, fill count, and any problems."""
    if not trades_path.exists():
        return {}, 0.0, 0, ["trades file missing"]
    frame = pd.read_csv(trades_path)
    problems: list[str] = []
    if "status" not in frame.columns:
        return {}, 0.0, 0, ["trades file has no status column"]
    filled = frame[frame["status"].astype(str).str.lower().eq("fill")].copy()
    if filled.empty:
        return {}, 0.0, 0, problems
    prices = pd.to_numeric(filled["price"], errors="coerce")
    if not prices.notna().all():
        problems.append("nonfinite fill price")
    quantities = pd.to_numeric(filled["filled_quantity"], errors="coerce").fillna(0.0)
    sides = filled["side"].astype(str).str.lower()
    net: dict[str, float] = {}
    for symbol, side, quantity in zip(filled["symbol"], sides, quantities):
        signed = float(quantity) if side == "buy" else -float(quantity)
        net[str(symbol)] = net.get(str(symbol), 0.0) + signed
    fees = float(pd.to_numeric(filled["trade_cost"], errors="coerce").fillna(0.0).sum())
    return net, fees, int(len(filled)), problems


def stable_hash(run_dir: Path) -> str:
    """Hash the decision/fill/equity artifacts that a repeated run must reproduce."""
    digest = sha256()
    for name in ("run_trades.csv", "run_stats.csv"):
        path = run_dir / name
        if not path.exists():
            digest.update(f"missing:{name}".encode())
            continue
        frame = pd.read_csv(path)
        digest.update(name.encode())
        digest.update(pd.util.hash_pandas_object(frame, index=False).values.tobytes())
    return digest.hexdigest()


def audit_run(run_dir: Path, *, rtol: float = 1e-10, atol: float = 1e-12) -> RunAudit:
    """Audit one candidate/window directory."""
    candidate_id = run_dir.parent.name
    window = run_dir.name
    audit = RunAudit(run_dir=run_dir, candidate_id=candidate_id, window=window, ok=True)
    result_path = run_dir / "run_result.json"
    if not result_path.exists():
        audit.ok = False
        audit.findings.append("run_result.json missing")
        return audit
    payload = json.loads(result_path.read_text(encoding="utf-8"))
    recorded = dict(payload.get("metrics") or {})
    audit.recorded = recorded
    problems = list(payload.get("problems") or [])
    if problems:
        audit.ok = False
        audit.findings.extend(f"runner problem: {item}" for item in problems)
    stats_path = run_dir / "run_stats.csv"
    if not stats_path.exists():
        audit.ok = False
        audit.findings.append("run_stats.csv missing")
        return audit
    metrics = independent_metrics(stats_path)
    audit.metrics = metrics
    for key in ("sessions", "final_equity", "total_return", "cagr", "volatility", "sharpe", "max_drawdown"):
        recomputed = metrics.get(key)
        stored = recorded.get(key)
        if recomputed is None or stored is None:
            audit.findings.append(f"metric {key} missing on one side")
            audit.ok = False
            continue
        if not math.isclose(float(recomputed), float(stored), rel_tol=rtol, abs_tol=atol):
            audit.findings.append(f"metric {key} mismatch recomputed={recomputed!r} recorded={stored!r}")
            audit.ok = False
    net, fees, fills, trade_problems = reconcile_trades(run_dir / "run_trades.csv")
    audit.net_positions = net
    audit.fees = fees
    audit.fills = fills
    for problem in trade_problems:
        audit.findings.append(problem)
        audit.ok = False
    if metrics.get("min_cash") is not None and float(metrics["min_cash"]) < -1.0:
        audit.findings.append(f"cash went negative: {metrics['min_cash']}")
        audit.ok = False
    terminal = payload.get("terminal_positions") or {}
    for symbol, quantity in net.items():
        if abs(quantity) < 1e-9:
            continue
        if symbol not in terminal:
            audit.findings.append(f"open position {symbol} x{quantity} not in terminal state")
            audit.ok = False
    if not audit.findings:
        audit.warnings.extend(payload.get("warnings") or [])
    return audit


def discover_runs(out_dir: Path) -> list[Path]:
    """Every candidate/window directory that holds a result artifact."""
    return sorted(
        path.parent
        for path in out_dir.glob("*/*/run_result.json")
    )


def audit_suite(out_dir: Path) -> dict[str, Any]:
    """Audit every stored run and return a summary plus a per-run table."""
    runs = discover_runs(out_dir)
    audits = [audit_run(run_dir) for run_dir in runs]
    accepted = [audit for audit in audits if audit.ok]
    rejected = [audit for audit in audits if not audit.ok]
    return {
        "out_dir": str(out_dir),
        "runs": len(audits),
        "accepted": len(accepted),
        "rejected": len(rejected),
        "failed": [
            {"candidate_id": audit.candidate_id, "window": audit.window, "findings": audit.findings}
            for audit in rejected
        ],
        "table": [
            {
                "candidate_id": audit.candidate_id,
                "window": audit.window,
                "ok": audit.ok,
                "sharpe": audit.metrics.get("sharpe"),
                "total_return": audit.metrics.get("total_return"),
                "cagr": audit.metrics.get("cagr"),
                "volatility": audit.metrics.get("volatility"),
                "max_drawdown": audit.metrics.get("max_drawdown"),
                "final_equity": audit.metrics.get("final_equity"),
                "sessions": audit.metrics.get("sessions"),
                "fills": audit.fills,
                "fees": audit.fees,
                "warnings": audit.warnings,
            }
            for audit in audits
        ],
    }


def compare_runs(first: Path, second: Path) -> dict[str, Any]:
    """Determinism check: do two runs of the same job share decision/equity hashes?"""
    return {
        "first": str(first),
        "second": str(second),
        "first_hash": stable_hash(first),
        "second_hash": stable_hash(second),
        "identical": stable_hash(first) == stable_hash(second),
    }
