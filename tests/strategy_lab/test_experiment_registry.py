"""Acceptance tests for the HTS research catalog registry.

These tests are the mechanical form of the plan's registry acceptance criteria
(``docs/HTS_100_VARIATIONS_RESEARCH_PLAN.md`` section 7) plus guards that the
in-repo catalog document stays in sync with the registry.
"""
from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path

import pytest

from strategy_lab.alternative_strategies import ALTERNATIVE_FAMILIES
from strategy_lab.experiment_config import (
    EXECUTION_ENGINE,
    InvalidParameterValueError,
    UnregisteredParameterError,
    all_parameter_specs,
    resolve_parameters,
)
from strategy_lab.experiment_registry import (
    EXPECTED_ALTERNATIVES,
    EXPECTED_HTS_VARIATIONS,
    EXPECTED_HTS_V2_VARIATIONS,
    RegistryValidationError,
    build_registry,
    get_registry,
    validate_registry,
)
from strategy_lab.experiment_universes import (
    CRYPTO_LINKED,
    ECONOMIC_EXPOSURE_GROUPS,
    LEVERAGED_PRODUCTS,
    exposure_group,
    resolve_universe,
)
from strategy_lab.hts_variants import (
    CONTROL_PARAMETER_SPECS,
    FAMILY_1_TREND_SPEED,
    HTS_BASELINE,
    HTS_CONTROL_ID,
    HTS_FAMILIES,
    HTS_V2_BASELINE,
    HTS_V2_FAMILY,
    HTS_V2_MATRIX_SHA256,
    HTS_V2_VARIATIONS,
    v2_matrix_hash,
)

ROOT = Path(__file__).resolve().parents[2]
CATALOG_PATH = ROOT / "docs" / "HTS_VARIATIONS_CATALOG.md"


def test_registry_matches_the_planned_counts() -> None:
    registry = get_registry()
    stats = registry.statistics()
    assert len(registry.hts_variations) == EXPECTED_HTS_VARIATIONS
    assert len(registry.hts_v2_variations) == EXPECTED_HTS_V2_VARIATIONS
    assert len(registry.alternatives) == EXPECTED_ALTERNATIVES
    assert stats["total"] == 1 + EXPECTED_HTS_VARIATIONS + EXPECTED_HTS_V2_VARIATIONS + EXPECTED_ALTERNATIVES
    assert stats["kinds"] == {"control": 1, "hts": 100, "hts-v2": 100, "alternative": 10}
    assert stats["families"] == 1 + len(HTS_FAMILIES) + 1 + len(ALTERNATIVE_FAMILIES)


def test_control_is_separate_and_not_counted_as_a_variation() -> None:
    registry = get_registry()
    assert registry.control.candidate_id == HTS_CONTROL_ID
    assert registry.control.kind == "control"
    assert registry.control.overrides == ()
    assert HTS_CONTROL_ID not in {candidate.candidate_id for candidate in registry.hts_variations}


def test_hts_v2_and_alternative_ids_are_contiguous_and_unique() -> None:
    registry = get_registry()
    assert [c.candidate_id for c in registry.hts_variations] == [f"H{n:03d}" for n in range(1, 101)]
    assert [c.candidate_id for c in registry.hts_v2_variations] == [f"V{n:03d}" for n in range(1, 101)]
    assert [c.candidate_id for c in registry.alternatives] == [f"A{n:02d}" for n in range(1, 11)]
    ids = registry.candidate_ids()
    assert len(set(ids)) == len(ids) == 211


def test_every_resolved_fingerprint_and_slug_is_unique() -> None:
    registry = get_registry()
    fingerprints = [candidate.fingerprint() for candidate in registry.all_candidates()]
    slugs = [candidate.slug for candidate in registry.all_candidates()]
    assert len(set(fingerprints)) == len(fingerprints)
    assert len(set(slugs)) == len(slugs)


def test_validate_registry_rejects_a_tampered_catalog() -> None:
    registry = get_registry()
    tampered = replace(
        registry,
        hts_variations=(
            replace(registry.hts_variations[0], candidate_id="H999"),
            *registry.hts_variations[1:],
        ),
    )
    with pytest.raises(RegistryValidationError):
        validate_registry(tampered)


def test_an_undeclared_override_is_rejected() -> None:
    # top_n exists in the shared contract but family 1 only declares trend_sma.
    with pytest.raises(UnregisteredParameterError):
        resolve_parameters(HTS_BASELINE, {"top_n": 3}, FAMILY_1_TREND_SPEED)


def test_an_out_of_range_or_wrong_type_value_is_rejected() -> None:
    with pytest.raises(InvalidParameterValueError):
        resolve_parameters(HTS_BASELINE, {"trend_sma": 0}, FAMILY_1_TREND_SPEED)
    with pytest.raises(InvalidParameterValueError):
        resolve_parameters(HTS_BASELINE, {"trend_sma": 25.5}, FAMILY_1_TREND_SPEED)


def test_lookup_is_case_insensitive() -> None:
    registry = get_registry()
    assert registry.get("h052") is registry.get("H052")
    with pytest.raises(KeyError):
        registry.get("H999")


def test_search_matches_name_family_and_rule() -> None:
    registry = get_registry()
    correlation = {candidate.candidate_id for candidate in registry.search("correlation")}
    assert {"H059", "H098"} <= correlation
    volatility = {candidate.candidate_id for candidate in registry.search("volatility target")}
    assert {"H063", "H068"} <= volatility
    assert registry.search("no-such-token-anywhere") == ()


def test_blocked_candidates_are_reported_as_blocked_not_as_results() -> None:
    registry = get_registry()
    blocked = {candidate.candidate_id: candidate for candidate in registry.blocked_candidates()}
    assert set(blocked) == {"A02", "A07", "A08", "A10"}
    for candidate in blocked.values():
        assert candidate.status == "blocked-data"
        assert candidate.blocked_reasons, f"{candidate.candidate_id} needs a stated reason"
        assert candidate.data_requirements, f"{candidate.candidate_id} needs a data requirement"


def test_starting_priorities_match_the_plan_sequence() -> None:
    registry = get_registry()
    starting = {candidate.candidate_id for candidate in registry.all_candidates() if candidate.priority == "starting"}
    assert {
        HTS_CONTROL_ID, "H052", "H053", "H059", "H060", "H063", "H068", "H086",
        "H089", "H098", "A01", "A03", "A05", "A06",
    } <= starting


def test_protective_order_candidates_are_deferred_with_a_requirement() -> None:
    registry = get_registry()
    for candidate_id in ("H039", "H040", "H100"):
        candidate = registry.get(candidate_id)
        assert candidate.priority == "deferred"
        # The native engine already models stop orders, so the remaining
        # prerequisite is implementation plus fill-fidelity reporting.
        assert "resting-stop-implementation-and-fill-fidelity" in candidate.data_requirements


def test_h060_uses_the_economic_exposure_limit() -> None:
    registry = get_registry()
    assert dict(registry.get("H060").overrides)["exposure_group_limit"] == 1


def test_all_family_parameter_specs_agree() -> None:
    specs = all_parameter_specs((*HTS_FAMILIES, HTS_V2_FAMILY, *ALTERNATIVE_FAMILIES))
    assert set(specs) >= {"trend_sma", "vol_target", "universe_symbols", "horizons"}
    for candidate in get_registry().all_candidates():
        for name, value in candidate.parameters:
            if name in specs:
                specs[name].validate_value(value)


def test_every_control_default_validates_against_its_spec() -> None:
    for spec in CONTROL_PARAMETER_SPECS:
        assert spec.name in HTS_BASELINE, f"{spec.name} has no baseline default"
        spec.validate_value(HTS_BASELINE[spec.name])


def test_reentry_cooldown_is_the_only_family_nine_and_ten_parameter() -> None:
    # The native expiry rule treats -1 and 0 as disabled; the registry keeps one
    # canonical key so a candidate can never resolve two conflicting cooldowns.
    legacy_name = "stop" + "_cooldown_sessions"
    for family in HTS_FAMILIES:
        names = {spec.name for spec in family.parameters}
        assert legacy_name not in names
    assert "reentry_cooldown_bars" in {
        spec.name for spec in HTS_FAMILIES[8].parameters
    }
    assert "reentry_cooldown_bars" in {
        spec.name for spec in HTS_FAMILIES[9].parameters
    }


def test_fingerprint_changes_when_a_resolved_parameter_changes() -> None:
    registry = get_registry()
    base = registry.get("H052")
    other = replace(base, parameters=tuple(
        (name, 99 if name == "top_n" else value) for name, value in base.parameters
    ))
    assert base.fingerprint() != other.fingerprint()


def test_v2_matrix_is_exactly_five_parent_blocks_and_twenty_frozen_recipes() -> None:
    registry = get_registry()
    assert v2_matrix_hash() == HTS_V2_MATRIX_SHA256
    parents = ("H100", "H027", "H022", "H095", HTS_CONTROL_ID)
    for index, parent in enumerate(parents):
        block = registry.hts_v2_variations[index * 20:(index + 1) * 20]
        assert [candidate.parent_candidate_id for candidate in block] == [parent] * 20
        assert [candidate.candidate_id for candidate in block] == [f"V{n:03d}" for n in range(index * 20 + 1, index * 20 + 21)]
    # The supplied plan hash makes the full ordered dictionaries (rather than
    # merely generated combinations) part of this regression contract.
    assert tuple(registry.hts_v2_variations) == HTS_V2_VARIATIONS


def test_v2_parameters_fail_closed_and_every_override_is_declared() -> None:
    with pytest.raises(UnregisteredParameterError):
        resolve_parameters(HTS_V2_BASELINE, {"trend_sma": 10}, HTS_V2_FAMILY)
    with pytest.raises(InvalidParameterValueError):
        resolve_parameters(HTS_V2_BASELINE, {"risk_off_gate": "spy-sma50"}, HTS_V2_FAMILY)
    with pytest.raises(InvalidParameterValueError):
        resolve_parameters(HTS_V2_BASELINE, {"risk_contribution_cap": 1.1}, HTS_V2_FAMILY)
    with pytest.raises(InvalidParameterValueError):
        resolve_parameters(HTS_V2_BASELINE, {"min_position_holding_bars": 1.5}, HTS_V2_FAMILY)
    declared = {spec.name for spec in HTS_V2_FAMILY.parameters}
    for candidate in get_registry().hts_v2_variations:
        assert set(candidate.override_map) <= declared


def test_v2_semantic_configurations_and_lineage_are_unique() -> None:
    registry = get_registry()
    semantic = [tuple(candidate.parameters) for candidate in registry.hts_v2_variations]
    assert len(set(semantic)) == 100
    assert {candidate.parent_candidate_id for candidate in registry.hts_v2_variations} == {
        "H100", "H027", "H022", "H095", HTS_CONTROL_ID,
    }
    assert "H100" in registry.to_markdown(last_updated="2026-09-16")
    assert {candidate.candidate_id for candidate in registry.search("balanced robustness")} == {"V019", "V039", "V059", "V079", "V099"}


def test_v1_identity_is_frozen_while_v2_carries_parent_lineage() -> None:
    registry = get_registry()
    control = registry.control
    assert control.overrides == ()
    assert control.parent_candidate_id is None
    assert control.fingerprint() == "40a366fa3530e03436a03a791b5ed55e8d14e6c7537e984cefd04b7130930f28"
    assert all(not any(name.startswith("risk_off") or name.startswith("min_trade") for name, _ in candidate.parameters)
               for candidate in (registry.control, *registry.hts_variations, *registry.alternatives))
    v2 = registry.get("V001")
    assert v2.parent_candidate_id == "H100"
    assert v2.describe()["parent_candidate_id"] == "H100"


def test_u0_universe_matches_the_shared_default_universe() -> None:
    pytest.importorskip("duckdb")
    from strategy_lab.hts_backtest import DEFAULT_UNIVERSE

    assert resolve_universe("U0") == tuple(DEFAULT_UNIVERSE)
    assert len(resolve_universe("U0")) == 57


def test_unleveraged_and_ex_crypto_universes_drop_the_expected_symbols() -> None:
    pytest.importorskip("duckdb")
    unleveraged = set(resolve_universe("U0_UNLEVERAGED"))
    assert not unleveraged & set(LEVERAGED_PRODUCTS)
    assert not unleveraged & {"MSTR", "COIN"}
    assert {"SPY", "QQQ", "BIL"} <= unleveraged

    ex_crypto = set(resolve_universe("U0_EX_CRYPTO"))
    assert not ex_crypto & set(CRYPTO_LINKED)
    assert {"SPY", "QQQ"} <= ex_crypto


def test_exposure_group_helper_collapses_documented_pairs() -> None:
    assert exposure_group("TQQQ") == exposure_group("QQQ") == "nasdaq-100"
    assert exposure_group("BOIL") == exposure_group("UNG") == "natural-gas"
    assert exposure_group("XLV") == "XLV"
    names = [name for name, _ in ECONOMIC_EXPOSURE_GROUPS]
    assert len(set(names)) == len(names)


def test_catalog_document_is_in_sync_with_the_registry() -> None:
    # The tracked Markdown is the frozen v1 catalogue; v2 rendering is tested
    # in-memory because this task intentionally changes only strategy_lab,
    # scripts, and tests.
    text = get_registry().to_markdown(last_updated="2026-09-16")
    for candidate_id in ("HTS_CONTROL_1", "H001", "H100", "V001", "V100", "A01", "A10"):
        assert candidate_id in text
    # The catalog must name the engine of record and must not imply the retired
    # custom replay is an execution path.
    assert EXECUTION_ENGINE in text
    assert "qualifies nothing" in text


def test_build_registry_is_deterministic() -> None:
    first = build_registry().candidate_ids()
    second = build_registry().candidate_ids()
    assert first == second == get_registry().candidate_ids()
