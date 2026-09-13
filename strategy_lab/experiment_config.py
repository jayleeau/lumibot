"""Typed, hashable specifications for the HTS research catalog.

Plan of record: ``docs/HTS_100_VARIATIONS_RESEARCH_PLAN.md``.  This module is the
strategy-owned configuration layer the plan calls for in Phase 1: it names every
registered candidate, resolves its parameters against the frozen control
contract, and produces a stable fingerprint that run artifacts can reference.

Importing this module authorizes nothing.  It does not read the cache, launch a
backtest, start a paper session, submit a broker order, or change a LumiBot
public API.

Design rules enforced here:

* A candidate is addressed by a single stable ID (``HTS_CONTROL_1``, ``H001``..
  ``H100``, ``A01``..``A10``) and a human-readable name.
* Every parameter a candidate changes must be declared by its family, so an
  unregistered knob or an accidental Cartesian product fails loudly.
* Parameters resolve to concrete values; no candidate stores a "delta only"
  state that would make two artifacts look identical when they are not.
"""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from types import MappingProxyType
from typing import Any, Iterable, Mapping, Sequence

CONTRACT_ID = "hts_research_contract_1"

# Execution engine of record. The custom local replay is retired to a read-only
# historical diagnostic and never qualifies a candidate.
EXECUTION_ENGINE = "native-lumibot-backtesting"

KIND_CONTROL = "control"
KIND_HTS = "hts"
KIND_ALTERNATIVE = "alternative"
VALID_KINDS: tuple[str, ...] = (KIND_CONTROL, KIND_HTS, KIND_ALTERNATIVE)

STATUS_REGISTERED = "registered"
STATUS_BLOCKED_DATA = "blocked-data"
VALID_STATUSES: tuple[str, ...] = (STATUS_REGISTERED, STATUS_BLOCKED_DATA)

PRIORITY_STARTING = "starting"
PRIORITY_STANDARD = "standard"
PRIORITY_DEFERRED = "deferred"
VALID_PRIORITIES: tuple[str, ...] = (PRIORITY_STARTING, PRIORITY_STANDARD, PRIORITY_DEFERRED)

_INT = "int"
_FLOAT = "float"
_BOOL = "bool"
_STR = "str"
_SYMBOLS = "symbols"
_INT_LIST = "int-list"
_NULLABLE_INT = "nullable-int"
_NULLABLE_FLOAT = "nullable-float"
_NULLABLE_STR = "nullable-str"
VALID_VALUE_KINDS: tuple[str, ...] = (
    _INT, _FLOAT, _BOOL, _STR, _SYMBOLS, _INT_LIST, _NULLABLE_INT, _NULLABLE_FLOAT, _NULLABLE_STR,
)


class ExperimentConfigError(ValueError):
    """Base class for rejected experiment specifications."""


class UnregisteredParameterError(ExperimentConfigError):
    """A candidate tried to set a knob its family does not declare."""


class InvalidParameterValueError(ExperimentConfigError):
    """A declared knob received a value outside its allowed range or set."""


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


@dataclass(frozen=True)
class ParameterSpec:
    """One declared, typed knob that a family exposes to candidates."""

    name: str
    kind: str
    description: str
    unit: str | None = None
    minimum: float | None = None
    maximum: float | None = None
    allowed_values: tuple[str, ...] | None = None

    def __post_init__(self) -> None:
        if not self.name or not self.name.replace("_", "").isalnum():
            raise ExperimentConfigError(f"invalid parameter name: {self.name!r}")
        if self.kind not in VALID_VALUE_KINDS:
            raise ExperimentConfigError(f"invalid kind for {self.name!r}: {self.kind!r}")
        if self.minimum is not None and self.maximum is not None and self.minimum > self.maximum:
            raise ExperimentConfigError(f"{self.name!r} has minimum > maximum")

    def validate_value(self, value: Any) -> Any:
        """Return the normalized value or raise :class:`InvalidParameterValueError`."""
        kind = self.kind
        if kind in (_NULLABLE_INT, _NULLABLE_FLOAT, _NULLABLE_STR) and value is None:
            return None
        if kind == _BOOL:
            if not isinstance(value, bool):
                raise InvalidParameterValueError(f"{self.name!r} must be a bool")
            return value
        if kind == _INT:
            if not isinstance(value, int) or isinstance(value, bool):
                raise InvalidParameterValueError(f"{self.name!r} must be an int")
        elif kind == _NULLABLE_INT:
            if not isinstance(value, int) or isinstance(value, bool):
                raise InvalidParameterValueError(f"{self.name!r} must be an int or None")
        elif kind == _FLOAT:
            if not _is_number(value):
                raise InvalidParameterValueError(f"{self.name!r} must be a number")
            value = float(value)
        elif kind == _NULLABLE_FLOAT:
            if not _is_number(value):
                raise InvalidParameterValueError(f"{self.name!r} must be a number or None")
            value = float(value)
        elif kind == _STR:
            if not isinstance(value, str) or not value:
                raise InvalidParameterValueError(f"{self.name!r} must be a non-empty string")
        elif kind == _NULLABLE_STR:
            if not isinstance(value, str) or not value:
                raise InvalidParameterValueError(f"{self.name!r} must be a non-empty string or None")
        elif kind == _SYMBOLS:
            if not isinstance(value, (list, tuple)) or not value:
                raise InvalidParameterValueError(f"{self.name!r} must be a non-empty symbol sequence")
            symbols = tuple(str(item) for item in value)
            if len(set(symbols)) != len(symbols):
                raise InvalidParameterValueError(f"{self.name!r} must not repeat a symbol")
            if any(not symbol for symbol in symbols):
                raise InvalidParameterValueError(f"{self.name!r} contains an empty symbol")
            value = symbols
        elif kind == _INT_LIST:
            if not isinstance(value, (list, tuple)) or not value:
                raise InvalidParameterValueError(f"{self.name!r} must be a non-empty integer sequence")
            if any(not isinstance(item, int) or isinstance(item, bool) or item <= 0 for item in value):
                raise InvalidParameterValueError(f"{self.name!r} must contain positive integers")
            if len(set(value)) != len(value):
                raise InvalidParameterValueError(f"{self.name!r} must not repeat a value")
            value = tuple(int(item) for item in value)
        if self.allowed_values is not None and value not in self.allowed_values:
            raise InvalidParameterValueError(
                f"{self.name!r}={value!r} is not one of {sorted(self.allowed_values)}"
            )
        if self.minimum is not None and _is_number(value) and value < self.minimum:
            raise InvalidParameterValueError(f"{self.name!r}={value!r} is below {self.minimum}")
        if self.maximum is not None and _is_number(value) and value > self.maximum:
            raise InvalidParameterValueError(f"{self.name!r}={value!r} is above {self.maximum}")
        return value


@dataclass(frozen=True)
class RuleFamily:
    """A named rule family with its hypothesis and the knobs it may vary."""

    family_id: str
    title: str
    hypothesis: str
    parameters: tuple[ParameterSpec, ...]
    notes: str = ""

    def __post_init__(self) -> None:
        if not self.family_id:
            raise ExperimentConfigError("family_id must not be empty")
        names = [spec.name for spec in self.parameters]
        if len(set(names)) != len(names):
            raise ExperimentConfigError(f"family {self.family_id!r} declares a parameter twice")

    def parameter(self, name: str) -> ParameterSpec | None:
        for spec in self.parameters:
            if spec.name == name:
                return spec
        return None


@dataclass(frozen=True)
class CandidateSpec:
    """A single named, fully resolved experiment configuration.

    ``overrides`` stores only what the candidate changes relative to the shared
    control contract; ``parameters`` is the fully resolved mapping actually fed
    to a runner.  Keeping both makes review easy without introducing ambiguity.
    """

    candidate_id: str
    name: str
    slug: str
    kind: str
    family_id: str
    rule: str
    hypothesis: str
    parameters: tuple[tuple[str, Any], ...]
    overrides: tuple[tuple[str, Any], ...]
    tags: tuple[str, ...] = ()
    data_requirements: tuple[str, ...] = ()
    status: str = STATUS_REGISTERED
    blocked_reasons: tuple[str, ...] = ()
    priority: str = PRIORITY_STANDARD
    research_refs: tuple[str, ...] = ()
    contract_id: str = CONTRACT_ID

    def __post_init__(self) -> None:
        if not self.candidate_id:
            raise ExperimentConfigError("candidate_id must not be empty")
        if not self.name or not self.slug:
            raise ExperimentConfigError(f"{self.candidate_id}: name and slug are required")
        if self.kind not in VALID_KINDS:
            raise ExperimentConfigError(f"{self.candidate_id}: invalid kind {self.kind!r}")
        if self.status not in VALID_STATUSES:
            raise ExperimentConfigError(f"{self.candidate_id}: invalid status {self.status!r}")
        if self.priority not in VALID_PRIORITIES:
            raise ExperimentConfigError(f"{self.candidate_id}: invalid priority {self.priority!r}")
        if not self.rule:
            raise ExperimentConfigError(f"{self.candidate_id}: rule summary is required")
        if not self.hypothesis:
            raise ExperimentConfigError(f"{self.candidate_id}: hypothesis is required")
        if self.status == STATUS_BLOCKED_DATA and not self.blocked_reasons:
            raise ExperimentConfigError(f"{self.candidate_id}: a blocked candidate needs a reason")
        keys = [key for key, _ in self.parameters]
        if len(set(keys)) != len(keys):
            raise ExperimentConfigError(f"{self.candidate_id}: duplicate resolved parameter")
        if len(dict(self.overrides)) != len(self.overrides):
            raise ExperimentConfigError(f"{self.candidate_id}: duplicate override parameter")

    @property
    def parameter_map(self) -> Mapping[str, Any]:
        return MappingProxyType(dict(self.parameters))

    @property
    def override_map(self) -> Mapping[str, Any]:
        return MappingProxyType(dict(self.overrides))

    def parameter(self, name: str) -> Any:
        values = dict(self.parameters)
        if name not in values:
            raise KeyError(f"{self.candidate_id} does not define {name!r}")
        return values[name]

    def fingerprint(self) -> str:
        """Return the stable identity of this resolved configuration."""
        payload = {
            "contract_id": self.contract_id,
            "candidate_id": self.candidate_id,
            "kind": self.kind,
            "family_id": self.family_id,
            "parameters": _jsonable(dict(self.parameters)),
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return sha256(encoded.encode("utf-8")).hexdigest()

    def short_fingerprint(self) -> str:
        return self.fingerprint()[:12]

    def describe(self) -> dict[str, Any]:
        """Return a JSON-serializable description for CLIs and run metadata."""
        return {
            "candidate_id": self.candidate_id,
            "name": self.name,
            "slug": self.slug,
            "kind": self.kind,
            "family_id": self.family_id,
            "priority": self.priority,
            "status": self.status,
            "rule": self.rule,
            "hypothesis": self.hypothesis,
            "tags": list(self.tags),
            "data_requirements": list(self.data_requirements),
            "blocked_reasons": list(self.blocked_reasons),
            "research_refs": list(self.research_refs),
            "overrides": _jsonable(dict(self.overrides)),
            "parameters": _jsonable(dict(self.parameters)),
            "fingerprint": self.fingerprint(),
        }


def _jsonable(value: Any) -> Any:
    """Normalize spec values so :func:`json.dumps` is deterministic."""
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def resolve_parameters(
    control_defaults: Mapping[str, Any],
    overrides: Mapping[str, Any],
    family: RuleFamily,
) -> tuple[tuple[str, Any], ...]:
    """Merge ``overrides`` over the control defaults and validate every knob.

    Raises :class:`UnregisteredParameterError` when a candidate changes a knob the
    family never declared, and :class:`InvalidParameterValueError` when a value
    falls outside the declared range or choice set.
    """
    declared_names = {spec.name for spec in family.parameters}
    unknown = sorted(set(overrides) - set(control_defaults) - declared_names)
    if unknown:
        raise UnregisteredParameterError(
            f"family {family.family_id!r} override(s) {unknown} are neither in the baseline "
            "nor declared by the family"
        )
    undeclared = sorted(set(overrides) - declared_names)
    if undeclared:
        raise UnregisteredParameterError(
            f"family {family.family_id!r} does not declare override(s) {undeclared}"
        )
    if family.family_id == CONTROL_FAMILY_ID and overrides:
        raise ExperimentConfigError("the control candidate must not override any parameter")

    resolved: dict[str, Any] = dict(control_defaults)
    for spec in family.parameters:
        if spec.name in overrides:
            resolved[spec.name] = spec.validate_value(overrides[spec.name])
    return frozen_pairs(resolved)


CONTROL_FAMILY_ID = "HTS_CONTROL_1"


def frozen_pairs(mapping: Mapping[str, Any]) -> tuple[tuple[str, Any], ...]:
    """Return a sorted, hashable view of a parameter mapping."""
    return tuple(sorted(mapping.items(), key=lambda item: item[0]))


def require_unique(ids: Iterable[str]) -> None:
    """Raise when a candidate ID appears more than once."""
    seen: set[str] = set()
    duplicates: set[str] = set()
    for candidate_id in ids:
        if candidate_id in seen:
            duplicates.add(candidate_id)
        seen.add(candidate_id)
    if duplicates:
        raise ExperimentConfigError(f"duplicate candidate IDs: {sorted(duplicates)}")


def all_parameter_specs(families: Sequence[RuleFamily]) -> dict[str, ParameterSpec]:
    """Return every declared parameter across ``families``, rejecting conflicts."""
    declared: dict[str, ParameterSpec] = {}
    for family in families:
        for spec in family.parameters:
            existing = declared.get(spec.name)
            if existing is not None and existing != spec:
                raise ExperimentConfigError(
                    f"parameter {spec.name!r} is declared incompatibly across families"
                )
            declared[spec.name] = spec
    return declared
