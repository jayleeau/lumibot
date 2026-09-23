"""Deterministic policy replay, lifecycle validation, and native/live tests.

The fixtures build their decision snapshot by calling the *real* pure policies
(``rank_scores``, ``select_holdings``, ``target_weights``,
``cap_risk_contributions``, ``apply_leveraged_cap``) so the parity harness is
proven against production arithmetic rather than hand-maintained numbers.
"""
from __future__ import annotations

import copy
import json
import math
import subprocess
import sys
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from hts_production_fixture import (  # noqa: E402
    complete_entry_lifecycle,
    make_production_strategy,
    run_production_rebalance,
)
from strategy_lab.experiment_universes import LEVERAGED_PRODUCTS  # noqa: E402
from strategy_lab.hts_audit import AtomicJsonStore, build_decision_snapshot  # noqa: E402
from strategy_lab.hts_parity import (  # noqa: E402
    ParityMismatch,
    ParityTolerances,
    UnverifiableError,
    assert_decision_parity,
    compare_pnl,
    replay_decision,
    validate_lifecycle,
)
from strategy_lab.hts_policies import (  # noqa: E402
    apply_leveraged_cap,
    cap_risk_contributions,
    rank_scores,
    select_holdings,
    target_weights,
)
from strategy_lab.native_experiments import load_session_end_equity  # noqa: E402

import verify_paper_six_parity as cli  # noqa: E402


def _row(**values: float) -> pd.Series:
    return pd.Series(values, dtype="float64")


def _parity_snapshot() -> dict:
    rank_rows = {
        "AAA": _row(ret=0.10),
        "BBB": _row(ret=0.20),
    }
    mode = "r20"
    scores = rank_scores(mode, rank_rows)
    universe_order = {"AAA": 0, "BBB": 1}
    ranked = sorted(scores.items(), key=lambda item: (-item[1], universe_order[item[0]]))
    selection = select_holdings(
        ranked=ranked,
        held=(),
        top_n=2,
        rank_buffer=None,
        exposure_limit=None,
        exposure_of=None,
        correlation_of=None,
        correlation_cap=None,
    )

    event_time_atr = {"AAA": 1.0, "BBB": 2.0}
    executable_prices = {"AAA": 100.0, "BBB": 50.0}
    volatility_inputs = {"AAA": 0.20, "BBB": 0.30}
    covariance_symbols = ["AAA", "BBB"]
    covariance_matrix = np.array([[0.04, 0.0], [0.0, 0.09]], dtype="float64")
    atr_k = 2.0
    gross_target = 0.995
    per_symbol_cap = None
    risk_contribution_cap = 0.03
    leveraged_cap = None

    selected = list(selection.holdings)
    pre = target_weights(
        "equal-slots",
        selected=selected,
        gross_target=gross_target,
        per_symbol_cap=per_symbol_cap,
        atr_k=atr_k,
        volatility=volatility_inputs,
        atr=event_time_atr,
        price=executable_prices,
        stop_distance_budget=None,
        vol_target=None,
        covariance=covariance_matrix,
    )
    post = cap_risk_contributions(
        pre,
        atr=event_time_atr,
        price=executable_prices,
        atr_k=atr_k,
        risk_contribution_cap=risk_contribution_cap,
    )
    final = apply_leveraged_cap(
        post,
        leveraged_symbols=[symbol for symbol in selected if symbol in LEVERAGED_PRODUCTS],
        cap=leveraged_cap,
    )

    equity = 100_000.0
    cash = 100_000.0
    fee_rate = 0.00035
    planned_orders = []
    budget = cash
    for symbol in selected:
        price = executable_prices[symbol]
        notional = min(float(final[symbol]) * equity, budget)
        effective_price = price * (1.0 + fee_rate)
        quantity = math.floor(notional / effective_price)
        budget -= quantity * effective_price
        planned_orders.append({
            "symbol": symbol,
            "side": "buy",
            "reference_price": price,
            "requested_quantity": quantity,
            "intended_notional": notional,
            "budget_before": None,
            "budget_after": budget,
            "intent_id": f"intent-{symbol.lower()}",
        })
    running = cash
    for order in planned_orders:
        order["budget_before"] = running
        running = order["budget_after"]

    return build_decision_snapshot(
        identity={
            "strategy_name": "w0007",
            "catalog_id": "w0007",
            "implementation_revision": "hts-native-v2-test",
            "resolved_parameters_hash": "params-w0007",
            "feature_hash": "feature-w0007",
            "decision_id": "w0007:2026-09-23:10",
        },
        time={
            "session": "2026-09-23",
            "signal_session": "2026-09-22",
            "signal_observed_at": "2026-09-22T15:00:00-04:00",
            "native_iteration_time": "2026-09-23T10:00:00-04:00",
            "logical_rebalance_time": "2026-09-23T10:00:00-04:00",
            "completed_source_bar_by_symbol": {
                "AAA": "2026-09-23T09:00:00",
                "BBB": "2026-09-23T09:00:00",
            },
            "quote_snapshot_captured_at": "2026-09-23T10:00:05-04:00",
            "timezone": "America/New_York",
            "hourly_convention": "clock-hour 09:00-15:00 ET; bar T known at T+1h",
        },
        selection={
            "rank_mode": mode,
            "rank_input_rows": {symbol: {"ret": float(row["ret"])} for symbol, row in rank_rows.items()},
            "rank_scores": dict(scores),
            "ordinal_ranks": dict(selection.ranks),
            "ranked_symbols": [symbol for symbol, _score in ranked],
            "held_symbols": [],
            "mandatory_held_symbols": [],
            "exposure_groups": {"AAA": "broad", "BBB": "tech"},
            "pairwise_correlations": {"AAA|BBB": 0.10},
            "selected_symbols": list(selection.holdings),
        },
        allocation={
            "selected_order": selected,
            "event_time_atr": event_time_atr,
            "executable_prices": executable_prices,
            "full_live_price_snapshot": dict(executable_prices),
            "volatility_inputs": volatility_inputs,
            "covariance_symbols": covariance_symbols,
            "covariance_matrix": covariance_matrix.tolist(),
            "base_forecast_volatility": 0.15,
            "pre_cap_forecast_volatility": 0.15,
            "post_cap_forecast_volatility": 0.15,
            "pre_risk_contribution_cap_weights": {k: float(v) for k, v in pre.items()},
            "post_risk_contribution_cap_weights": {k: float(v) for k, v in post.items()},
            "final_weights_after_leveraged_cap": {k: float(v) for k, v in final.items()},
            "risk_contribution_cap": risk_contribution_cap,
            "risk_cap_binding_by_symbol": {
                symbol: float(post[symbol]) < float(pre[symbol]) for symbol in selected
            },
            "risk_cap_bound_any": any(float(post[s]) < float(pre[s]) for s in selected),
        },
        account={
            "cash": cash,
            "portfolio_value": equity,
            "balance_observed_at": "2026-09-23T10:00:05-04:00",
            "fee_rate": fee_rate,
        },
        book={
            "existing_positions": [],
            "pending_buys": [],
            "pending_sells": [],
            "occupied_symbols": [],
        },
        planned_orders=planned_orders,
        parameters={
            "weight_mode": "equal-slots",
            "gross_target": gross_target,
            "per_symbol_cap": per_symbol_cap,
            "atr_k": atr_k,
            "stop_distance_budget": None,
            "vol_target": None,
            "risk_contribution_cap": risk_contribution_cap,
            "leveraged_cap": leveraged_cap,
            "top_n": 2,
            "rank_buffer": None,
            "exposure_group_limit": None,
            "correlation_screen": None,
            "universe_symbols": ["AAA", "BBB"],
        },
        fills=[
            {"symbol": "BBB", "side": "buy", "fill_event_quantity": 500.0,
             "cumulative_filled_quantity": 500.0},
            {"symbol": "BBB", "side": "buy", "fill_event_quantity": 249.0,
             "cumulative_filled_quantity": 749.0},
        ],
        protective_stop={
            "symbol": "BBB",
            "expected": True,
            "quantity": 749.0,
            "entry_price": 50.0,
            "level": 46.0,
        },
    )


# --- pure-policy replay -------------------------------------------------------

def test_assert_decision_parity_passes_for_recorded_two_name_decision() -> None:
    snapshot = _parity_snapshot()
    replay = replay_decision(snapshot)
    assert replay["status"] == "pass"
    assert replay["selected_symbols"] == ["BBB", "AAA"]
    assert replay["ordinal_ranks"] == {"BBB": 1, "AAA": 2}
    assert replay["quantities"]["BBB"] == 749
    assert replay["quantities"]["AAA"] == 497
    assert_decision_parity(snapshot)


def test_assert_decision_parity_fails_when_event_time_atr_is_perturbed() -> None:
    snapshot = _parity_snapshot()
    perturbed = copy.deepcopy(snapshot)
    perturbed["allocation"]["event_time_atr"]["BBB"] = 10.0

    with pytest.raises(ParityMismatch) as excinfo:
        assert_decision_parity(perturbed)
    assert excinfo.value.field.startswith("allocation.post_risk_contribution_cap_weights")
    assert excinfo.value.field.endswith("BBB")


@pytest.mark.parametrize("field", ("equity", "quote", "covariance"))
def test_parity_requires_complete_inputs_instead_of_substituting_current_values(field: str) -> None:
    snapshot = _parity_snapshot()
    broken = copy.deepcopy(snapshot)
    if field == "equity":
        del broken["account"]["portfolio_value"]
    elif field == "quote":
        del broken["allocation"]["executable_prices"]
    else:
        del broken["allocation"]["covariance_matrix"]
        del broken["parameters"]["weight_mode"]
        broken["parameters"]["weight_mode"] = "equal-slots"

    with pytest.raises(UnverifiableError):
        assert_decision_parity(broken)
    with pytest.raises(UnverifiableError):
        replay_decision(broken)


@pytest.mark.parametrize(
    "field",
    (
        "pre_risk_contribution_cap_weights",
        "post_risk_contribution_cap_weights",
        "final_weights_after_leveraged_cap",
    ),
)
def test_assert_decision_parity_requires_every_weight_stage(field: str) -> None:
    snapshot = _parity_snapshot()
    broken = copy.deepcopy(snapshot)
    del broken["allocation"][field]
    with pytest.raises(UnverifiableError):
        assert_decision_parity(broken)


# --- production serializer -> CLI ---------------------------------------------

def _production_session_payload():
    strategy = make_production_strategy()
    run_production_rebalance(strategy)

    for symbol in list(strategy._pending_buys):
        pending = strategy._pending_buys[symbol]
        order = pending["order"]
        strategy.on_new_order(order)
        quantity = float(pending["requested_quantity"])
        price = 100.0 if symbol == "AAA" else 50.0
        complete_entry_lifecycle(strategy, symbol, fragments=(quantity,), price=price)

    # A later reactive exit owns a fresh decision.  Reusing the already-processed
    # entry decision would correctly suppress broker contact under fix #3.
    strategy._submit_sell("AAA", "selection_change", 100.0)
    sell_order = strategy._created[-1]
    strategy.on_new_order(sell_order)
    strategy.on_filled_order(None, sell_order, 100.0, 497.0, 1)

    from strategy_lab.hts_audit import build_strategy_event_payload

    return strategy, build_strategy_event_payload(strategy, session="2026-09-23")


def test_production_serializer_output_is_accepted_by_cli_end_to_end(tmp_path: Path) -> None:
    strategy, payload = _production_session_payload()

    path = tmp_path / "2026-09-23.json"
    store = AtomicJsonStore(path)
    store.write_session(payload)

    report = cli.main(["--live-session", str(path), "--json"])
    # cli.main prints the report; call the report builder directly for assertions.
    result = cli.verify_sessions([str(path)], ParityTolerances())

    assert result["status"] == "pass"
    assert result["decision_snapshots_found"] is True
    assert result["mismatches"] == []
    assert result["unverifiable"] == []
    assert result["lifecycle_gaps"] == []
    assert len(payload["decisions"]) == 2


def test_cli_rejects_decision_stub_instead_of_silently_skipping_it(tmp_path: Path) -> None:
    _strategy, payload = _production_session_payload()
    stub = copy.deepcopy(payload)
    stub["decisions"] = [{"decision_id": stub["decisions"][0]["decision_id"]}]

    path = tmp_path / "2026-09-23.json"
    # Write raw so the store does not pre-validate; the CLI must reject the stub.
    AtomicJsonStore(path).write(stub)

    result = cli.verify_sessions([str(path)], ParityTolerances())
    assert result["status"] == "unverifiable"
    assert any("decisions" in item for item in result["unverifiable"])
    assert cli.main(["--live-session", str(path)]) == cli.EXIT_UNVERIFIABLE


@pytest.mark.parametrize("mutation", ("no_new_order", "no_exit_intent", "no_exit_decision", "no_event_time"))
def test_lifecycle_verifier_rejects_missing_timestamp_or_join(mutation: str) -> None:
    _strategy, payload = _production_session_payload()
    broken = copy.deepcopy(payload)
    lifecycle = broken["lifecycle_trace"]

    if mutation == "no_new_order":
        broken["lifecycle_trace"] = [e for e in lifecycle if e["event"] != "order_submitted"]
    elif mutation == "no_exit_intent":
        for event in lifecycle:
            if event["event"] == "exit_fill":
                event["intent_id"] = None
    elif mutation == "no_exit_decision":
        for event in lifecycle:
            if event["event"] == "exit_fill":
                event["decision_id"] = None
    elif mutation == "no_event_time":
        lifecycle[0]["event_time"] = None

    issues = validate_lifecycle(broken)
    assert issues, mutation


@pytest.mark.parametrize(
    "mutation",
    ("ghost_decision", "impossible_cumulative_fill", "final_fill_before_submission"),
)
def test_cli_returns_nonzero_for_adversarial_lifecycle_corruption(
    mutation: str, tmp_path: Path,
) -> None:
    _strategy, payload = _production_session_payload()
    broken = copy.deepcopy(payload)
    lifecycle = broken["lifecycle_trace"]

    if mutation == "ghost_decision":
        lifecycle[0]["decision_id"] = "ghost-decision"
    elif mutation == "impossible_cumulative_fill":
        fill = next(event for event in lifecycle if event["event"] == "final_fill")
        fill["cumulative_filled_quantity"] = float(fill["fill_event_quantity"]) + 1.0
    else:
        fill_index = next(
            index for index, event in enumerate(lifecycle)
            if event["event"] == "final_fill"
        )
        intent_id = lifecycle[fill_index]["intent_id"]
        submitted_index = next(
            index for index, event in enumerate(lifecycle)
            if event["event"] == "order_submitted" and event["intent_id"] == intent_id
        )
        lifecycle[submitted_index], lifecycle[fill_index] = (
            lifecycle[fill_index], lifecycle[submitted_index]
        )

    path = tmp_path / f"{mutation}.json"
    AtomicJsonStore(path).write(broken)

    report = cli.verify_sessions([str(path)], ParityTolerances())
    assert report["status"] != "pass"
    assert cli.main(["--live-session", str(path)]) != cli.EXIT_PASS


# --- native session equity ----------------------------------------------------

def test_native_pnl_reads_actual_session_end_equity_curve(tmp_path: Path) -> None:
    stats = tmp_path / "run_stats.csv"
    stats.write_text(
        "datetime,cash,portfolio_value\n"
        "2026-09-21 09:30:00-04:00,100000,100000\n"
        "2026-09-21 15:59:00-04:00,100000,100500\n"
        "2026-09-22 09:30:00-04:00,100000,100500\n"
        "2026-09-22 15:59:00-04:00,100000,101200\n"
        "2026-09-23 09:30:00-04:00,100000,101200\n"
        "2026-09-23 15:59:00-04:00,100000,102750\n",
        encoding="utf-8",
    )

    curve = load_session_end_equity(stats)
    assert curve == {
        "2026-09-21": 100500.0,
        "2026-09-22": 101200.0,
        "2026-09-23": 102750.0,
    }
    # Three distinct values: never a repeated final equity.
    assert len(set(curve.values())) == 3


def test_cli_option_contract_requires_native_for_pnl(tmp_path: Path, monkeypatch) -> None:
    _strategy, payload = _production_session_payload()
    path = tmp_path / "2026-09-23.json"
    AtomicJsonStore(path).write_session(payload)

    assert cli.main(["--live-session", str(path), "--compare-pnl"]) == cli.EXIT_UNVERIFIABLE
    assert cli.main(["--live-session", str(path), "--run-native"]) == cli.EXIT_UNVERIFIABLE

    calls: list[str] = []

    def _fake_run_native(payload_arg, native_out):
        calls.append("run-native")
        return {"2026-09-23": 100_000.0}, None

    monkeypatch.setattr(cli, "_run_native", _fake_run_native)
    exit_code = cli.main(
        ["--live-session", str(path), "--run-native", "--native-out", str(tmp_path)]
    )
    assert calls == ["run-native"]
    assert exit_code == cli.EXIT_PASS


def test_cli_native_path_builds_declared_parent_and_reaches_pnl_comparison(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The real CLI/native bridge must build W0007 with its H100 lineage."""
    _strategy, base_payload = _production_session_payload()
    paths: list[str] = []
    for session, equity in (("2026-09-22", 100_000.0), ("2026-09-23", 101_000.0)):
        payload = copy.deepcopy(base_payload)
        payload["session"] = session
        payload["session_end_events"] = [
            {"event": "session_end", "session": session, "equity": equity, "cash": equity}
        ]
        path = tmp_path / f"{session}.json"
        AtomicJsonStore(path).write_session(payload)
        paths.append(str(path))

    captured_candidates = []

    def fake_run_candidate(candidate, window, out_dir, *, control_baseline):
        captured_candidates.append(candidate)
        run_dir = Path(out_dir) / candidate.candidate_id / window.label
        run_dir.mkdir(parents=True)
        (run_dir / "run_stats.csv").write_text(
            "datetime,cash,portfolio_value\n"
            "2026-09-22 15:59:00-04:00,100000,100000\n"
            "2026-09-23 15:59:00-04:00,100000,101000\n",
            encoding="utf-8",
        )
        return SimpleNamespace(out_dir=run_dir)

    import strategy_lab.native_experiments as native_experiments

    monkeypatch.setattr(native_experiments, "run_candidate", fake_run_candidate)
    argv: list[str] = []
    for path in paths:
        argv.extend(("--live-session", path))
    argv.extend((
        "--run-native",
        "--native-out",
        str(tmp_path / "native"),
        "--compare-pnl",
        "--json",
    ))

    exit_code = cli.main(argv)
    report = json.loads(capsys.readouterr().out)

    assert exit_code == cli.EXIT_PASS
    assert report["status"] == "pass"
    assert len(captured_candidates) == 1
    assert captured_candidates[0].parent_candidate_id == "H100"
    assert dict(captured_candidates[0].parameters) == base_payload["resolved_parameters"]


def test_cli_missing_native_archive_is_structured_unverifiable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A local archive failure is exit 2 with JSON evidence, never a traceback."""
    _strategy, payload = _production_session_payload()
    path = tmp_path / "2026-09-23.json"
    AtomicJsonStore(path).write_session(payload)

    def missing_archive(*_args, **_kwargs):
        raise FileNotFoundError("required local archive is unavailable")

    import strategy_lab.native_experiments as native_experiments

    monkeypatch.setattr(native_experiments, "run_candidate", missing_archive)
    exit_code = cli.main([
        "--live-session",
        str(path),
        "--run-native",
        "--native-out",
        str(tmp_path / "native"),
        "--json",
    ])
    report = json.loads(capsys.readouterr().out)

    assert exit_code == cli.EXIT_UNVERIFIABLE
    assert report["status"] == "unverifiable"
    assert any(
        "FileNotFoundError: required local archive is unavailable" in reason
        for reason in report["unverifiable"]
    )


def test_cli_unresolvable_native_parent_is_structured_unverifiable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Unknown persisted lineage must fail before the native runner is called."""
    _strategy, payload = _production_session_payload()
    payload["identity"]["catalog_id"] = "UNKNOWN"
    path = tmp_path / "2026-09-23.json"
    AtomicJsonStore(path).write_session(payload)

    def unexpected_runner(*_args, **_kwargs):
        pytest.fail("unresolvable lineage must not start a native run")

    import strategy_lab.native_experiments as native_experiments

    monkeypatch.setattr(native_experiments, "run_candidate", unexpected_runner)
    exit_code = cli.main([
        "--live-session",
        str(path),
        "--run-native",
        "--native-out",
        str(tmp_path / "native"),
        "--json",
    ])
    report = json.loads(capsys.readouterr().out)

    assert exit_code == cli.EXIT_UNVERIFIABLE
    assert report["status"] == "unverifiable"
    assert any("has no declared paper6 native parent" in reason for reason in report["unverifiable"])


def test_cli_json_stdout_is_pure_json_on_the_native_path(tmp_path: Path) -> None:
    """A real subprocess must emit JSON-only stdout even when the native path
    imports LumiBot, whose import-time logging writes a startup line.

    The in-process tests cannot catch this: ``lumibot`` is already imported at
    collection time, so its startup log fires before ``capsys`` starts.  Only a
    fresh subprocess exercises the import during the CLI run.
    """
    _strategy, payload = _production_session_payload()
    path = tmp_path / "2026-09-23.json"
    AtomicJsonStore(path).write_session(payload)

    script = REPO_ROOT / "scripts" / "verify_paper_six_parity.py"
    proc = subprocess.run(
        [
            sys.executable,
            str(script),
            "--live-session",
            str(path),
            "--run-native",
            "--native-out",
            str(tmp_path / "native"),
            "--json",
        ],
        capture_output=True,
        text=True,
    )

    # The native path fails closed on this fixture, but it still imports
    # LumiBot (the source of the stdout pollution) and must return exit 2.
    assert proc.returncode == cli.EXIT_UNVERIFIABLE
    report = json.loads(proc.stdout)  # must not raise on startup log noise
    assert report["status"] == "unverifiable"
    # Nothing outside the JSON document may appear on stdout.
    assert proc.stdout.strip() == json.dumps(report, indent=2, default=str).strip()


def test_pnl_comparison_rejects_missing_sessions_and_reports_first_divergence() -> None:
    tolerances = ParityTolerances(abs_return_tol=1e-4, rel_tol=1e-3)

    inside_live = {"2026-09-21": 100_000.0, "2026-09-22": 101_000.05, "2026-09-23": 102_000.10}
    inside_native = {"2026-09-21": 100_000.0, "2026-09-22": 101_000.0, "2026-09-23": 102_000.0}
    inside = compare_pnl(inside_live, inside_native, tolerances=tolerances)
    assert inside["status"] == "pass"

    outside_live = {"2026-09-21": 100_000.0, "2026-09-22": 101_500.0, "2026-09-23": 103_000.0}
    outside_native = {"2026-09-21": 100_000.0, "2026-09-22": 101_000.0, "2026-09-23": 102_000.0}
    outside = compare_pnl(outside_live, outside_native, tolerances=tolerances)
    assert outside["status"] == "mismatch"
    assert outside["first_divergent_session"] == "2026-09-22"

    missing = compare_pnl(
        {"2026-09-21": 100_000.0, "2026-09-24": 101_000.0},
        {"2026-09-21": 100_000.0, "2026-09-22": 101_000.0},
        tolerances=tolerances,
    )
    assert missing["status"] == "unverifiable"

    single = compare_pnl({"2026-09-21": 100_000.0}, {"2026-09-21": 100_000.0}, tolerances=tolerances)
    assert single["status"] == "unverifiable"
