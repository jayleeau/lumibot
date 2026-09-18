"""Backtest the two paper-trading strategies identified on this machine —
meanrev_ema (LiveMeanrevEma) and meanrev_ema_3d (LiveMeanrevEma3d) — across the
same regime windows used for everything else, through LumiBot's native engine.

Both are daily mean-reversion EMA dip-buys with identical entry
(close > EMA150 and close < EMA10 and RSI3 < 25) that differ only in exit style:
  meanrev_ema     sleeve 2.5x, stop 4%,  RSI2>70 exit, hold 1 day
  meanrev_ema_3d  sleeve 3.0x, stop 2%,  RSI2>80 exit, hold 4 days

Windows (identical to the HTS suite):
  six_year    2020-09-08 -> 2026-09-08
  two_year    2024-09-08 -> 2026-09-08
  pre_2020    2018-05-01 -> 2020-04-30
  early       2018-05-01 -> 2021-12-31
  y2022_2024  2022-01-01 -> 2024-12-31

Writes artifacts to reports/live_pair_backtests/ and a JSONL summary.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path

os.environ["IS_BACKTESTING"] = "true"
os.environ["LUMIBOT_DISABLE_DOTENV"] = "true"
LUMIBOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(LUMIBOT))

import pandas as pd  # noqa: E402

from strategy_lab.daily_fleet_backtest import MEANREV_EMA, MEANREV_EMA_3D  # noqa: E402
from scripts.run_full_lumibot_backtests import (  # noqa: E402
    _prepare_daily, _run_native, NativeDailyFleetStrategy, DailyContext,
)

ROOT = LUMIBOT
REPORT_DIR = ROOT / "reports" / "live_pair_backtests"
REPORT_DIR.mkdir(parents=True, exist_ok=True)
SUMMARY = REPORT_DIR / "summary.jsonl"

WINDOWS = {
    "six_year": ("2020-09-08", "2026-09-08"),
    "two_year": ("2024-09-08", "2026-09-08"),
    "pre_2020": ("2018-05-01", "2020-04-30"),
    "early": ("2018-05-01", "2021-12-31"),
    "y2022_2024": ("2022-01-01", "2024-12-31"),
}
SPECS = {"meanrev_ema": MEANREV_EMA, "meanrev_ema_3d": MEANREV_EMA_3D}


def _daily_sharpe(stats_path: Path) -> float | None:
    """Recompute daily-return Sharpe from run_stats.csv (engine 'sharpe' is CAGR/vol)."""
    try:
        st = pd.read_csv(stats_path, parse_dates=["datetime"])
        st["ts"] = pd.to_datetime(st["datetime"], utc=True)
        st["day"] = st["ts"].dt.strftime("%Y-%m-%d")
        daily = st.sort_values("ts").groupby("day")["portfolio_value"].last()
        r = daily.pct_change().dropna()
        if len(r) < 10 or r.std() == 0:
            return None
        return float(r.mean() / r.std() * 252 ** 0.5)
    except Exception:
        return None


def run_one(spec, window_label: str, start: str, end: str) -> dict:
    from strategy_lab.daily_fleet_backtest import prepare as prepare_daily
    start_dt = datetime.fromisoformat(f"{start}T00:00:00")
    end_dt = datetime.fromisoformat(f"{end}T23:59:59")

    # daily frames for the LIQ universe; the meanrev-ema signal columns are
    # identical for both exit styles (same entry), so the _prepare_daily view
    # (MEANREV_EMA entry + rsi_exit) is correct for the 3d variant too.
    daily_frames, daily_data = _prepare_daily(start, end)
    context_frames = {s: f.copy() for s, f in daily_frames.items()}
    ctx = DailyContext(
        frames=context_frames,
        ordered_symbols=[s for s in daily_frames if s in context_frames],
        leverage=spec.sleeve,
        stop=spec.stop,
        hold_days=spec.hold_days,
        rsi_exit=spec.rsi_exit,
    )
    from scripts.run_full_lumibot_backtests import _DAILY_CONTEXT
    global _DAILY_CONTEXT_ref
    try:
        _set_daily_context(ctx)
    except Exception:
        pass
    payload = None
    # _run_native uses module-global _DAILY_CONTEXT; set it via the module
    import scripts.run_full_lumibot_backtests as m
    old = m._DAILY_CONTEXT
    m._DAILY_CONTEXT = ctx
    try:
        payload = _run_native(
            NativeDailyFleetStrategy,
            name=f"{spec.name}_{window_label}",
            pandas_data=daily_data,
            start=start_dt,
            end=end_dt,
            out_dir=REPORT_DIR,
            sleeptime="1D",
        )
    finally:
        m._DAILY_CONTEXT = old
    return payload


def _set_daily_context(ctx):
    import scripts.run_full_lumibot_backtests as m
    m._DAILY_CONTEXT = ctx


def main() -> int:
    results: list[dict[str, object]] = []
    for wlabel, (start, end) in WINDOWS.items():
        for sname, spec in SPECS.items():
            rec: dict = {"strategy": sname, "window": wlabel, "start": start, "end": end}
            print(f"\n=== {sname} / {wlabel} ({start}->{end}) ===", flush=True)
            try:
                p = run_one(spec, wlabel, start, end)
                analysis = (p or {}).get("analysis", {}) or {}
                rec["runtime_seconds"] = p.get("runtime_seconds")
                rec["total_return"] = analysis.get("total_return")
                rec["cagr"] = analysis.get("cagr")
                rec["volatility"] = analysis.get("volatility")
                rec["sharpe_engine_cagr_over_vol"] = analysis.get("sharpe")
                mdd = analysis.get("max_drawdown")
                if isinstance(mdd, dict):
                    mdd = mdd.get("drawdown")
                rec["max_drawdown"] = mdd
                stats = REPORT_DIR / f"{sname}_{wlabel}_stats.csv"
                rec["sharpe_daily"] = _daily_sharpe(stats)
                rec["status"] = "ok"
            except Exception as exc:  # noqa: BLE001
                rec["status"] = "error"
                rec["error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
                print("  ERROR", rec["error"], flush=True)
            results.append(rec)
            with SUMMARY.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, sort_keys=True, default=str) + "\n")
            print("  ", json.dumps({k: rec[k] for k in
                ("total_return", "cagr", "volatility", "max_drawdown", "sharpe", "sharpe_daily")
                if k in rec}, default=str), flush=True)
    # human summary
    print("\n===== SUMMARY =====")
    ok = [r for r in results if r.get("status") == "ok"]
    for r in ok:
        ret = r.get("total_return"); cagr = r.get("cagr")
        sh = r.get("sharpe_daily") or r.get("sharpe_engine_cagr_over_vol")
        print(f"{r['strategy']:<16} {r['window']:<12} ret={ret*100 if ret else 0:7.1f}% "
              f"cagr={cagr*100 if cagr else 0:5.1f}% sharpe={sh if sh is not None else 'n/a'}")
    print(f"\n{len(results)} runs, {len(ok)} ok -> {SUMMARY}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())