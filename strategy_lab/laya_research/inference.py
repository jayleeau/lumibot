"""Lazy, cache-only access to the pinned Laya inference runtime.

This module is intentionally safe to import from the ordinary LumiBot
environment: neither :mod:`torch` nor :mod:`laya` is imported until
``load_local`` is called.  The wrapper keeps the model process separate from
the native engine and validates the small part of the SDK contract used by
the frozen research protocol.
"""
from __future__ import annotations

import hashlib
import importlib
import json
import math
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any, Callable, Iterable, Mapping, Sequence

from strategy_lab.laya_research.contracts import FEATURE_FIELDS, Snapshot, serialize_features

MODEL_ID = "convaiinnovations/laya"
MODEL_REVISION = "1c5edc17a7acd8701df6fc341c0d179f1c62c982"
PACKAGE_VERSION = "0.3.5"
DEFAULT_MAX_LEN = 512
DEFAULT_HEAD_MAX_LEN = 192


class InferenceError(RuntimeError):
    """Base class for technical inference-contract failures."""


class CacheOnlyError(InferenceError):
    """Raised when a local checkpoint is absent or would require a download."""


class TokenBudgetError(InferenceError):
    """Raised when the assembled question and state cannot fit without truncation."""


class InvalidPrediction(InferenceError):
    """Raised for malformed, nonfinite, out-of-range, or non-canonical output."""


class BackendDriftError(InferenceError):
    """Raised when the loaded SDK reports a device/dtype other than the pin."""


@dataclass(frozen=True)
class BackendSpec:
    """Reference backend settings recorded in every technical run."""

    device: str = "cpu"
    dtype: str = "float32"
    seed: int = 20260922
    threads: int = 1


@dataclass
class LoadedAgent:
    """A loaded SDK agent plus immutable load-time identity metadata."""

    raw: Any
    model_dir: Path
    sdk_version: str | None
    device: str
    dtype: str
    config: Mapping[str, Any]

    def predict(self, state: Any, questions: Mapping[str, Mapping[str, Any]]) -> Any:
        """Run the SDK prediction method without changing its result schema."""
        method = getattr(self.raw, "predict", None) or getattr(self.raw, "system_one", None)
        if method is None or not callable(method):
            raise InferenceError("loaded Laya agent has no predict/system_one method")
        return method(state, dict(questions))


def noul_question(instructions: str) -> dict[str, dict[str, str]]:
    """Build the single frozen binary question definition."""
    if not isinstance(instructions, str) or not instructions.strip():
        raise ValueError("noul question instructions must be a non-empty string")
    return {"laya_noul": {"type": "noul", "instructions": instructions}}


def _normalise_device(value: Any) -> str:
    """Return a stable lower-case device name from SDK/torch values."""
    if value is None:
        return "unknown"
    return str(getattr(value, "type", value)).lower()


def _normalise_dtype(value: Any) -> str:
    """Return a stable dtype name from SDK/torch values."""
    if value is None:
        return "unknown"
    text = str(value).lower().replace("torch.", "")
    return {"fp32": "float32", "float": "float32", "fp16": "float16"}.get(text, text)


def _agent_config(agent: Any) -> Mapping[str, Any]:
    cfg = getattr(agent, "cfg", getattr(agent, "config", {}))
    return cfg if isinstance(cfg, Mapping) else {}


def _backend_values(agent: Any) -> tuple[str, str]:
    device = _normalise_device(getattr(agent, "device", None))
    dtype = _normalise_dtype(getattr(agent, "dtype", None))
    if dtype == "unknown":
        model = getattr(agent, "model", None)
        try:
            dtype = _normalise_dtype(next(model.parameters()).dtype)
        except (AttributeError, StopIteration, TypeError):
            pass
    return device, dtype


def configure_reference_backend(spec: BackendSpec = BackendSpec()) -> None:
    """Set deterministic CPU seeds and thread count using a lazy torch import."""
    torch = importlib.import_module("torch")
    torch.manual_seed(spec.seed)
    torch.set_num_threads(spec.threads)
    if hasattr(torch, "set_num_interop_threads"):
        try:
            torch.set_num_interop_threads(spec.threads)
        except RuntimeError:
            # PyTorch permits this only before parallel work starts.
            pass
    torch.use_deterministic_algorithms(True)


def _verify_local_files(model_dir: Path, expected_revision: str) -> Mapping[str, Any]:
    if not model_dir.is_dir():
        raise CacheOnlyError(f"local model directory does not exist: {model_dir}")
    required = ("rl_agent_config.json", "model.safetensors", "tokenizer", "encoder")
    missing = [name for name in required if not (model_dir / name).exists()]
    if missing:
        raise CacheOnlyError(f"checkpoint is incomplete; missing {missing}")
    metadata_path = model_dir / "laya_download_manifest.json"
    if not metadata_path.exists():
        raise CacheOnlyError("checkpoint has no pinned laya_download_manifest.json")
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CacheOnlyError("invalid laya download manifest") from exc
    if metadata.get("revision") != expected_revision:
        raise CacheOnlyError(
            f"unexpected model revision {metadata.get('revision')!r}; expected {expected_revision}"
        )
    return metadata


def load_local(
    model_dir: str | Path,
    *,
    device: str = "cpu",
    expected_revision: str = MODEL_REVISION,
    sdk: ModuleType | Any | None = None,
    backend: BackendSpec = BackendSpec(),
) -> LoadedAgent:
    """Load a pinned checkpoint from disk without allowing hub/network access.

    ``sdk`` is injectable solely for synthetic tests.  Production calls import
    the installed ``laya`` package lazily and pass it a local directory.
    """
    path = Path(model_dir)
    metadata = _verify_local_files(path, expected_revision)
    if device != backend.device:
        raise BackendDriftError(f"requested device {device!r} differs from reference {backend.device!r}")
    if sdk is None:
        sdk = importlib.import_module("laya")
    load = getattr(sdk, "load", None)
    if not callable(load):
        raise InferenceError("installed laya package has no callable load()")
    # Synthetic SDK fixtures intentionally run in the ordinary venv where
    # torch is absent; the real path configures torch immediately before load.
    if sdk is None:
        configure_reference_backend(backend)
    raw = load(str(path), device=device)
    actual_device, actual_dtype = _backend_values(raw)
    if actual_device != backend.device or actual_dtype != backend.dtype:
        raise BackendDriftError(
            f"loaded backend drifted to device={actual_device!r}, dtype={actual_dtype!r}; "
            f"expected {backend.device}/{backend.dtype}"
        )
    return LoadedAgent(
        raw=raw,
        model_dir=path,
        sdk_version=getattr(sdk, "__version__", None),
        device=actual_device,
        dtype=actual_dtype,
        config=dict(_agent_config(raw)),
    )


# Descriptive alias used by callers and tests.
load_agent = load_local


def _token_ids(tokenizer: Any, text: str) -> list[int]:
    encoded = tokenizer(text, add_special_tokens=False)
    ids = encoded.get("input_ids") if isinstance(encoded, Mapping) else getattr(encoded, "input_ids", None)
    if ids is None:
        raise TokenBudgetError("tokenizer did not return input_ids")
    if ids and isinstance(ids[0], list):
        ids = ids[0]
    return [int(item) for item in ids]


def validate_token_budget(
    agent: LoadedAgent | Any,
    state: Any,
    questions: Mapping[str, Mapping[str, Any]],
) -> dict[str, int]:
    """Prove the frozen question and state fit without tokenizer truncation.

    The calculation mirrors the SDK's English ``build_sequence`` layout.  A
    missing tokenizer or an over-budget assembled sequence is a hard failure;
    this function never silently truncates the state.
    """
    raw = agent.raw if isinstance(agent, LoadedAgent) else agent
    tokenizer = getattr(raw, "tok", getattr(raw, "tokenizer", None))
    cfg = agent.config if isinstance(agent, LoadedAgent) else _agent_config(raw)
    if tokenizer is None:
        raise TokenBudgetError("loaded agent exposes no tokenizer for budget verification")
    max_len = int(cfg.get("max_len", DEFAULT_MAX_LEN))
    head_max_len = int(cfg.get("head_max_len", DEFAULT_HEAD_MAX_LEN))
    if max_len <= 0 or head_max_len <= 0 or head_max_len >= max_len:
        raise TokenBudgetError(f"invalid tokenizer budgets max_len={max_len}, head_max_len={head_max_len}")
    results: dict[str, int] = {}
    for qid, question in questions.items():
        if question.get("type") != "noul":
            raise TokenBudgetError("Task 0b permits one noul question only")
        instructions = str(question.get("instructions", ""))
        head = 1 + len(_token_ids(tokenizer, f"noul question: {instructions}")) + 1
        option_ids = []
        mask_id = getattr(tokenizer, "mask_token_id", None)
        if mask_id is None:
            raise TokenBudgetError("tokenizer has no mask_token_id")
        for option in ("false: no, the statement does not hold", "true: yes, the statement holds"):
            option_ids.append([int(mask_id)] + _token_ids(tokenizer, " " + option)[:48])
        head_tokens = head + sum(len(item) for item in option_ids) + 1
        if head_tokens > head_max_len:
            raise TokenBudgetError(
                f"question {qid!r} needs {head_tokens} head tokens > head_max_len={head_max_len}"
            )
        state_text = state if isinstance(state, str) else json.dumps(state, ensure_ascii=False, separators=(",", ":"))
        state_tokens = len(_token_ids(tokenizer, str(state_text).replace(getattr(tokenizer, "mask_token", "<mask>"), " ")))
        room = max_len - head_tokens - 1
        if state_tokens > room:
            raise TokenBudgetError(
                f"state needs {state_tokens} tokens but only {room} remain; truncation is forbidden"
            )
        results[qid] = head_tokens + state_tokens + 1
    return results


def extract_noul_probability(result: Mapping[str, Any], question_id: str = "laya_noul") -> float:
    """Extract and validate the SDK's nested four-decimal ``noul`` value."""
    try:
        answer = result["answers"][question_id]
        value = answer["noul"]
    except (KeyError, TypeError) as exc:
        raise InvalidPrediction(f"missing answers[{question_id!r}]['noul']") from exc
    try:
        probability = float(value)
    except (TypeError, ValueError) as exc:
        raise InvalidPrediction(f"noul value is not numeric: {value!r}") from exc
    if not math.isfinite(probability) or not 0.0 <= probability <= 1.0:
        raise InvalidPrediction(f"noul value is outside [0, 1]: {probability!r}")
    rounded = round(probability, 4)
    if abs(probability - rounded) > 1e-12:
        raise InvalidPrediction(f"SDK noul value is not four-decimal canonical: {value!r}")
    return rounded


def predict_noul(
    agent: LoadedAgent | Any,
    state: Any,
    questions: Mapping[str, Mapping[str, Any]],
    *,
    question_id: str = "laya_noul",
    validate_budget: bool = True,
) -> float:
    """Run one frozen question and return its validated raw SDK probability."""
    if validate_budget:
        validate_token_budget(agent, state, questions)
    result = agent.predict(state, questions) if isinstance(agent, LoadedAgent) else agent.predict(state, questions)
    if not isinstance(result, Mapping):
        raise InvalidPrediction("SDK result is not a mapping")
    return extract_noul_probability(result, question_id)


def canonical_snapshot_hash(snapshot: Snapshot | Mapping[str, Any]) -> str:
    """Hash canonical decoded row values, independent of Parquet metadata."""
    if isinstance(snapshot, Snapshot):
        text = snapshot.text
        identity = f"{snapshot.symbol}|{snapshot.source_session.isoformat()}|{snapshot.decision_session.isoformat()}"
    else:
        features = tuple(float(snapshot[name]) for name in FEATURE_FIELDS)
        text = serialize_features(features)
        identity = f"{snapshot['symbol']}|{snapshot['source_session']}|{snapshot['decision_session']}"
    return hashlib.sha256(f"{identity}|{text}".encode("utf-8")).hexdigest()


def canonical_prediction_hash(rows: Iterable[Mapping[str, Any]]) -> str:
    """Hash sorted canonical row/probability pairs for repeat-load comparison."""
    values = []
    for row in rows:
        values.append(f"{canonical_snapshot_hash(row)}|{float(row['probability']):.4f}")
    return hashlib.sha256("\n".join(sorted(values)).encode("ascii")).hexdigest()


__all__ = [
    "BackendDriftError", "BackendSpec", "CacheOnlyError", "InferenceError",
    "InvalidPrediction", "LoadedAgent", "MODEL_ID", "MODEL_REVISION",
    "PACKAGE_VERSION", "TokenBudgetError", "canonical_prediction_hash",
    "canonical_snapshot_hash", "configure_reference_backend", "extract_noul_probability",
    "load_agent", "load_local", "noul_question", "predict_noul", "validate_token_budget",
]
