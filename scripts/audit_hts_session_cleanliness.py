"""P1 session-cleanliness audit for the HTS 15-minute archive.

Classifies every bar into premarket / regular trading hours / post-market using
the NYSE calendar (exchange_calendars), computes true early-close sessions from
the calendar itself, checks DST handling and non-session dates, and writes a
compact evidence artifact.  Read-only on the archive.

Run: .venv/bin/python scripts/audit_hts_session_cleanliness.py
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import duckdb
import exchange_calendars as xc
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "short" / "hts_xnas_itch_15m_split_adjusted.duckdb"
OUT = ROOT / "reports" / "hts_session_cleanliness_audit.json"


def main() -> int:
    con = duckdb.connect(str(DB), read_only=True)
    df = con.execute(
        "SELECT symbol, ts, (ts AT TIME ZONE 'America/New_York') AS et FROM bars_15m_raw"
    ).fetchdf()
    con.close()
    df["et"] = pd.to_datetime(df["et"])
    t = df["et"].dt.time

    pre = (t >= dt.time(4, 0)) & (t < dt.time(9, 30))
    rth = (t >= dt.time(9, 30)) & (t < dt.time(16, 0))
    post = (t >= dt.time(16, 0)) & (t <= dt.time(20, 0))
    other = ~(pre | rth | post)
    total = len(df)

    cal = xc.get_calendar("XNYS")
    # per-day last RTH bar (bar START); expected close = calendar close; bar END = start+15m
    rth_bars = df[rth].copy()
    rth_bars["day"] = rth_bars["et"].dt.normalize()
    last_start = rth_bars.groupby("day")["et"].max()

    session_dates = pd.Series(sorted(df["et"].dt.normalize().unique()))
    nonsession_dates = [s for s in session_dates if not cal.is_session(s)]

    early_close = []
    for d, last in last_start.items():
        if not cal.is_session(d):
            continue
        expected_close = cal.session_close(d).tz_convert("America/New_York").tz_localize(None)
        last_naive = pd.Timestamp(last)
        if last_naive.tzinfo is not None:
            last_naive = last_naive.tz_convert("America/New_York").tz_localize(None)
        bar_end = last_naive + pd.Timedelta(minutes=15)
        # tolerance 1 min for bar-end rounding
        if bar_end < expected_close - pd.Timedelta(minutes=1):
            early_close.append({
                "date": str(d.date()),
                "expected_close_et": expected_close.strftime("%H:%M"),
                "last_bar_end_et": bar_end.strftime("%H:%M"),
            })

    # DST: UTC offset of the 09:30 ET bar by month
    dst = {}
    nine30 = df[df["et"].dt.time == dt.time(9, 30)].copy()
    offs = nine30.groupby(nine30["et"].dt.month)["ts"].apply(
        lambda s: sorted({x.strftime("%z") for x in s})
    )
    dst = {int(m): v for m, v in offs.items()}

    report = {
        "archive": str(DB.relative_to(ROOT)),
        "total_bars": int(total),
        "sessions": int(df["et"].dt.date.nunique()),
        "session_classification": {
            "premarket_0400_0930_et": int(pre.sum()),
            "premarket_pct": round(float(pre.mean()), 4),
            "rth_0930_1600_et": int(rth.sum()),
            "rth_pct": round(float(rth.mean()), 4),
            "postmarket_1600_2000_et": int(post.sum()),
            "postmarket_pct": round(float(post.mean()), 4),
            "outside_0400_2000_et": int(other.sum()),
        },
        "regular_session_only": bool(pre.sum() == 0 and post.sum() == 0),
        "non_session_dates_present": len(nonsession_dates),
        "non_session_examples": [str(x.date()) for x in nonsession_dates[:5]],
        "early_close_sessions": early_close,
        "dst_utc_offset_of_0930_et_by_month": dst,
        "finding": (
            "The 15-minute archive is NOT regular-session-only: it carries "
            "premarket (04:00-09:30 ET) and post-market (16:00-20:00 ET) bars. "
            "Relabelling cannot remove this; any regular-session candidate must "
            "explicitly filter to 09:30-16:00 ET, and A10 (opening-range) must "
            "use genuine 09:30-10:00 ET one-minute bars, not a relabelled proxy."
        ),
    }
    OUT.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({k: report[k] for k in (
        "total_bars", "sessions", "session_classification", "regular_session_only",
        "non_session_dates_present", "early_close_sessions")}, indent=2))
    print(f"\nwrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())