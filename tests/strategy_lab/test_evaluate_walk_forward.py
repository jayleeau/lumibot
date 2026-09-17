"""Regression coverage for walk-forward evaluator accounting."""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from scripts.evaluate_walk_forward import PRIMARY_COST_BPS, _chained_metrics


def _write_stats(run_dir: Path, equities: list[float]) -> None:
    """Write one synthetic daily native equity curve for a fresh-cash block."""
    run_dir.mkdir(parents=True)
    pd.DataFrame(
        {
            "datetime": pd.date_range("2024-01-02T21:00:00Z", periods=len(equities), freq="D"),
            "portfolio_value": equities,
        }
    ).to_csv(run_dir / "run_stats.csv", index=False)


def test_chained_metrics_include_each_from_cash_block_first_session_return(tmp_path: Path) -> None:
    """First sessions contribute once; the transition charge remains separate."""
    candidate = "known-returns"
    _write_stats(tmp_path / candidate / "b01", [95_000.0, 104_500.0])  # -5%, then +10%
    _write_stats(tmp_path / candidate / "b02", [90_000.0, 99_000.0])  # -10%, then +10%

    metrics = _chained_metrics(tmp_path, candidate, ["b01", "b02"])
    boundary_factor = 1.0 - 2.0 * PRIMARY_COST_BPS / 10_000.0
    expected_factor = (0.95 * 1.10) * boundary_factor * (0.90 * 1.10)
    assert metrics["total_return"] == pytest.approx(expected_factor - 1.0)
    assert metrics["max_drawdown"] == pytest.approx(1.0 - boundary_factor * 0.90)

    # A block containing only its first-session loss must not be treated as flat.
    _write_stats(tmp_path / "loss-only" / "b01", [90_000.0])
    loss_only = _chained_metrics(tmp_path, "loss-only", ["b01"])
    assert loss_only["total_return"] == pytest.approx(-0.10)
    assert loss_only["total_return"] != 0.0
