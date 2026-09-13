"""The queryable registry of every registered HTS research configuration.

Plan of record: ``docs/HTS_100_VARIATIONS_RESEARCH_PLAN.md`` sections 5-7.  The
registry is the "name them and manage them" layer: one addressable catalog of the
audited control, the 100 fixed HTS variations, and the 10 alternative seeds,
each with a resolved parameter set and a stable fingerprint.

Registry acceptance (plan section 7) is enforced in :func:`validate_registry`:

* exactly 100 unique HTS IDs, ``H001``..``H100``;
* exactly 10 unique alternative IDs, ``A01``..``A10``;
* the control is separate and is not counted as a variation;
* no two candidates share a resolved-spec fingerprint or a human slug;
* every override is declared by the candidate's family, so no unregistered
  parameter value can slip in.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from functools import lru_cache
from types import MappingProxyType
from typing import Mapping

from strategy_lab.alternative_strategies import (
    ALTERNATIVE_FAMILIES,
    build_alternative_candidates,
)
from strategy_lab.experiment_config import (
    EXECUTION_ENGINE,
    KIND_ALTERNATIVE,
    KIND_CONTROL,
    KIND_HTS,
    PRIORITY_DEFERRED,
    PRIORITY_STARTING,
    STATUS_BLOCKED_DATA,
    CandidateSpec,
    ExperimentConfigError,
    RuleFamily,
)
from strategy_lab.hts_variants import (
    CONTROL_FAMILY,
    CONTROL_PARAMETER_SPECS,
    HTS_BASELINE,
    HTS_CONTROL_ID,
    HTS_FAMILIES,
    build_hts_candidates,
)

EXPECTED_HTS_VARIATIONS = 100
EXPECTED_ALTERNATIVES = 10
EXPECTED_TOTAL = 1 + EXPECTED_HTS_VARIATIONS + EXPECTED_ALTERNATIVES


class RegistryValidationError(ExperimentConfigError):
    """Raised when the assembled catalog fails an acceptance rule."""


@dataclass(frozen=True)
class ExperimentRegistry:
    """An immutable, queryable view of the registered experiment catalog."""

    control: CandidateSpec
    hts_variations: tuple[CandidateSpec, ...]
    alternatives: tuple[CandidateSpec, ...]
    families: Mapping[str, RuleFamily]

    def all_candidates(self) -> tuple[CandidateSpec, ...]:
        """Return the control, then H001-H100, then A01-A10."""
        return (self.control, *self.hts_variations, *self.alternatives)

    def candidate_ids(self) -> tuple[str, ...]:
        return tuple(candidate.candidate_id for candidate in self.all_candidates())

    def get(self, candidate_id: str) -> CandidateSpec:
        """Look up one candidate by ID, case-insensitively."""
        target = candidate_id.strip().upper()
        for candidate in self.all_candidates():
            if candidate.candidate_id.upper() == target:
                return candidate
        raise KeyError(f"no registered experiment with ID {candidate_id!r}")

    def family(self, family_id: str) -> RuleFamily:
        try:
            return self.families[family_id]
        except KeyError as error:
            raise KeyError(f"no registered family with ID {family_id!r}") from error

    def list_families(self, kind: str | None = None) -> tuple[RuleFamily, ...]:
        """Return families in catalog order, optionally filtered by candidate kind."""
        ordered: list[RuleFamily] = []
        if kind in (None, KIND_CONTROL, KIND_HTS) and CONTROL_FAMILY.family_id in self.families:
            ordered.append(self.families[CONTROL_FAMILY.family_id])
        for family in HTS_FAMILIES:
            if kind in (None, KIND_HTS):
                ordered.append(self.families[family.family_id])
        for family in ALTERNATIVE_FAMILIES:
            if kind in (None, KIND_ALTERNATIVE):
                ordered.append(self.families[family.family_id])
        return tuple(ordered)

    def search(self, text: str) -> tuple[CandidateSpec, ...]:
        """Case-insensitive substring search across names, rules, and families."""
        needle = text.strip().lower()
        if not needle:
            return self.all_candidates()
        matches: list[CandidateSpec] = []
        for candidate in self.all_candidates():
            family = self.families.get(candidate.family_id)
            haystack = " ".join((
                candidate.candidate_id,
                candidate.name,
                candidate.slug,
                candidate.rule,
                candidate.hypothesis,
                family.title if family else "",
                " ".join(candidate.tags),
            )).lower()
            if needle in haystack:
                matches.append(candidate)
        return tuple(matches)

    def statistics(self) -> dict[str, object]:
        """Return counts used in the catalog header and for quick sanity checks."""
        kinds = {KIND_CONTROL: 0, KIND_HTS: 0, KIND_ALTERNATIVE: 0}
        priorities = {PRIORITY_STARTING: 0, "standard": 0, PRIORITY_DEFERRED: 0}
        statuses: dict[str, int] = {}
        for candidate in self.all_candidates():
            kinds[candidate.kind] += 1
            priorities[candidate.priority] += 1
            statuses[candidate.status] = statuses.get(candidate.status, 0) + 1
        return {
            "total": len(self.all_candidates()),
            "kinds": kinds,
            "priorities": priorities,
            "statuses": statuses,
            "families": len(self.families),
        }

    def blocked_candidates(self) -> tuple[CandidateSpec, ...]:
        return tuple(
            candidate for candidate in self.all_candidates()
            if candidate.status == STATUS_BLOCKED_DATA
        )

    def to_markdown(self, last_updated: str | None = None) -> str:
        """Render the deterministic lookup catalog used by ``docs/``."""
        updated = last_updated or date.today().isoformat()
        stats = self.statistics()
        lines: list[str] = [
            "# Title: HTS Variation Catalog (Control, H001-H100, A01-A10)",
            "",
            "Description: The named, resolved, fingerprinted lookup table for every "
            "registered HTS research configuration.",
            "",
            f"Last Updated: {updated}",
            "",
            "Status: Registered; no candidate has been backtested or qualified",
            "",
            "Audience: Strategy developers and the strategy owner",
            "",
            "## Overview",
            "",
            "This catalog is generated from `strategy_lab/experiment_registry.py`; it is the "
            "authoritative index of the research plan's candidates. Each row has a stable ID, a "
            "human-readable name, a family, and a fingerprint over the fully resolved parameter "
            "set. Address a candidate by ID in any runner, artifact path, or conversation.",
            "",
            f"Execution engine of record: `{EXECUTION_ENGINE}` (`PandasDataBacktesting` plus "
            "`BacktestingBroker`). The retired custom local replay qualifies nothing and is not "
            "part of this catalog's execution path.",
            "",
            "The audited control `HTS_CONTROL_1` is listed separately and is **not** counted as a "
            f"variation. Totals: {stats['total']} configurations = 1 control + 100 HTS "
            "variations + 10 alternative strategies.",
            "",
            "Planning source: `docs/HTS_100_VARIATIONS_RESEARCH_PLAN.md`. Registration is not "
            "authorization to run: no backtest, paper session, or broker order is authorized by "
            "this catalog.",
            "",
            "## How to look these up",
            "",
            "```bash",
            "# Full index (this document)",
            "python scripts/list_strategy_experiments.py --list",
            "",
            "# One candidate, with resolved parameters",
            "python scripts/list_strategy_experiments.py --show H052",
            "",
            "# Filter or search",
            "python scripts/list_strategy_experiments.py --family family-6-concentration",
            "python scripts/list_strategy_experiments.py --kind alternative",
            "python scripts/list_strategy_experiments.py --search correlation",
            "",
            "# Rebuild this document or verify the acceptance rules",
            "python scripts/list_strategy_experiments.py --write-catalog docs/HTS_VARIATIONS_CATALOG.md",
            "python scripts/list_strategy_experiments.py --verify",
            "```",
            "",
            "## Summary",
            "",
            f"- Registered configurations: {stats['total']}",
            f"- Audited control: {stats['kinds'][KIND_CONTROL]}",
            f"- HTS variations: {stats['kinds'][KIND_HTS]}",
            f"- Alternative strategies: {stats['kinds'][KIND_ALTERNATIVE]}",
            f"- Families: {stats['families']}",
            "- Blocked on required data: "
            + ", ".join(c.candidate_id for c in self.blocked_candidates()),
            "",
            "## Master index",
            "",
            "| ID | Name | Kind | Family | Priority | Status | Fingerprint |",
            "|---|---|---|---|---|---|---|",
        ]
        for candidate in self.all_candidates():
            family = self.families[candidate.family_id]
            lines.append(
                f"| {candidate.candidate_id} | {candidate.name} | {candidate.kind} | "
                f"{family.title} | {candidate.priority} | {candidate.status} | "
                f"`{candidate.short_fingerprint()}` |"
            )
        lines.extend(("", "## Families", ""))
        for family in self.list_families():
            lines.extend((f"### {family.title} (`{family.family_id}`)", "", family.hypothesis, ""))
            if family.notes:
                lines.extend((family.notes, ""))
            members = [c for c in self.all_candidates() if c.family_id == family.family_id]
            lines.extend(("| ID | Name | Rule |", "|---|---|---|"))
            for candidate in members:
                lines.append(f"| {candidate.candidate_id} | {candidate.name} | {candidate.rule} |")
            lines.append("")
        blocked = self.blocked_candidates()
        if blocked:
            lines.extend(("## Data prerequisites", "", "| ID | Requirement | Reason |", "|---|---|---|"))
            for candidate in blocked:
                requirements = ", ".join(candidate.data_requirements) or "unspecified"
                reasons = " ".join(candidate.blocked_reasons)
                lines.append(f"| {candidate.candidate_id} | {requirements} | {reasons} |")
            lines.append("")
        deferred = [c for c in self.all_candidates() if c.priority == PRIORITY_DEFERRED]
        if deferred:
            lines.extend(("## Implementation prerequisites", ""))
            lines.append(
                "These candidates are registered but deferred pending implementation of resting "
                "protective stops in the native strategy plus an honest fill-fidelity report: "
                + ", ".join(c.candidate_id for c in deferred) + "."
            )
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"


def _family_index() -> Mapping[str, RuleFamily]:
    families: dict[str, RuleFamily] = {CONTROL_FAMILY.family_id: CONTROL_FAMILY}
    for family in (*HTS_FAMILIES, *ALTERNATIVE_FAMILIES):
        if family.family_id in families:
            raise RegistryValidationError(f"duplicate family ID {family.family_id!r}")
        families[family.family_id] = family
    return MappingProxyType(families)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise RegistryValidationError(message)


def validate_registry(registry: ExperimentRegistry) -> None:
    """Enforce the plan's registry acceptance rules."""
    _require(registry.control.kind == KIND_CONTROL, "the control must use kind='control'")
    _require(
        registry.control.candidate_id == HTS_CONTROL_ID,
        f"the control must be {HTS_CONTROL_ID}",
    )
    hts_ids = [candidate.candidate_id for candidate in registry.hts_variations]
    expected_hts = [f"H{number:03d}" for number in range(1, EXPECTED_HTS_VARIATIONS + 1)]
    _require(
        hts_ids == expected_hts,
        f"HTS variations must be exactly H001-H100 in order; found {hts_ids[:3]}..{hts_ids[-1:]}",
    )
    alt_ids = [candidate.candidate_id for candidate in registry.alternatives]
    expected_alternatives = [f"A{number:02d}" for number in range(1, EXPECTED_ALTERNATIVES + 1)]
    _require(
        alt_ids == expected_alternatives,
        f"alternatives must be exactly A01-A10 in order; found {alt_ids}",
    )
    _require(
        all(candidate.kind == KIND_HTS for candidate in registry.hts_variations),
        "every HTS variation must use kind='hts'",
    )
    _require(
        all(candidate.kind == KIND_ALTERNATIVE for candidate in registry.alternatives),
        "every alternative must use kind='alternative'",
    )
    _require(
        HTS_CONTROL_ID not in hts_ids,
        "the control must not be counted as an HTS variation",
    )
    _require(
        len(registry.all_candidates()) == EXPECTED_TOTAL,
        f"expected {EXPECTED_TOTAL} registered configurations",
    )

    ids = registry.candidate_ids()
    _require(len(set(ids)) == len(ids), "candidate IDs must be unique")
    slugs = [candidate.slug for candidate in registry.all_candidates()]
    _require(len(set(slugs)) == len(slugs), "candidate slugs must be unique")
    fingerprints = [candidate.fingerprint() for candidate in registry.all_candidates()]
    _require(
        len(set(fingerprints)) == len(fingerprints),
        "resolved-spec fingerprints must be unique",
    )

    for candidate in registry.all_candidates():
        family = registry.families.get(candidate.family_id)
        _require(family is not None, f"{candidate.candidate_id}: unknown family {candidate.family_id!r}")
        declared = {spec.name for spec in family.parameters}
        undeclared = sorted(set(dict(candidate.overrides)) - declared)
        _require(
            not undeclared,
            f"{candidate.candidate_id}: unregistered override(s) {undeclared}",
        )
        _require(bool(candidate.name), f"{candidate.candidate_id}: missing name")
        _require(bool(candidate.rule), f"{candidate.candidate_id}: missing rule summary")
        _require(bool(candidate.hypothesis), f"{candidate.candidate_id}: missing hypothesis")

    for spec in CONTROL_PARAMETER_SPECS:
        spec.validate_value(HTS_BASELINE[spec.name])


def build_registry() -> ExperimentRegistry:
    """Assemble and validate the full catalog."""
    hts_candidates = build_hts_candidates()
    control, *variations = hts_candidates
    alternatives = build_alternative_candidates()
    registry = ExperimentRegistry(
        control=control,
        hts_variations=tuple(variations),
        alternatives=tuple(alternatives),
        families=_family_index(),
    )
    validate_registry(registry)
    return registry


@lru_cache(maxsize=1)
def get_registry() -> ExperimentRegistry:
    """Return the process-wide registry singleton."""
    return build_registry()


def render_catalog(last_updated: str | None = None) -> str:
    return get_registry().to_markdown(last_updated=last_updated)
