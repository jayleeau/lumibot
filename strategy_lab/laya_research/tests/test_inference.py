"""Synthetic-only tests for the optional lazy Laya runtime wrapper."""
from __future__ import annotations

import importlib
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from strategy_lab.laya_research import inference


class FakeTokenizer:
    mask_token = "<mask>"
    mask_token_id = 99

    def __call__(self, text: str, *, add_special_tokens: bool = False) -> dict[str, list[int]]:
        del add_special_tokens
        return {"input_ids": list(range(len(text.split())))}


class FakeAgent:
    device = "cpu"
    dtype = "float32"
    cfg = {"max_len": 64, "head_max_len": 32}
    tok = FakeTokenizer()

    def __init__(self, value: float = 0.1234) -> None:
        self.value = value

    def predict(self, state: str, questions: dict[str, dict[str, str]]) -> dict[str, object]:
        del state
        qid = next(iter(questions))
        return {"answers": {qid: {"type": "noul", "noul": self.value}}}


class FakeSDK:
    __version__ = "0.3.5-fake"

    def __init__(self, agent: FakeAgent | None = None) -> None:
        self.agent = agent or FakeAgent()
        self.loaded: list[tuple[str, str]] = []

    def load(self, path: str, *, device: str) -> FakeAgent:
        self.loaded.append((path, device))
        return self.agent


def _model_dir(tmp_path: Path, *, revision: str = inference.MODEL_REVISION) -> Path:
    root = tmp_path / "model"
    (root / "tokenizer").mkdir(parents=True)
    (root / "encoder").mkdir()
    (root / "model.safetensors").write_bytes(b"weights")
    (root / "rl_agent_config.json").write_text("{}", encoding="utf-8")
    (root / "laya_download_manifest.json").write_text(
        json.dumps({"revision": revision}), encoding="utf-8"
    )
    return root


def test_import_is_lazy_and_does_not_require_torch_or_laya() -> None:
    assert "torch" not in sys.modules
    assert "laya" not in sys.modules
    module = importlib.reload(inference)
    assert module.MODEL_REVISION
    assert "torch" not in sys.modules
    assert "laya" not in sys.modules


def test_cache_only_load_and_nested_four_decimal_endpoint(tmp_path: Path) -> None:
    sdk = FakeSDK()
    loaded = inference.load_local(_model_dir(tmp_path), sdk=sdk)
    assert sdk.loaded and sdk.loaded[0][1] == "cpu"
    question = inference.noul_question("Will it be positive?")
    assert inference.predict_noul(loaded, "1,2,3", question) == 0.1234


def test_cache_only_rejects_missing_or_unexpected_revision(tmp_path: Path) -> None:
    with pytest.raises(inference.CacheOnlyError):
        inference.load_local(tmp_path / "missing", sdk=FakeSDK())
    with pytest.raises(inference.CacheOnlyError):
        inference.load_local(_model_dir(tmp_path, revision="wrong"), sdk=FakeSDK())


def test_tokenizer_budget_rejects_silent_truncation(tmp_path: Path) -> None:
    loaded = inference.load_local(_model_dir(tmp_path), sdk=FakeSDK())
    question = inference.noul_question("short")
    assert inference.validate_token_budget(loaded, "one two", question)
    with pytest.raises(inference.TokenBudgetError):
        inference.validate_token_budget(loaded, " ".join(["word"] * 100), question)


def test_backend_drift_is_hard_failure(tmp_path: Path) -> None:
    drifted = FakeAgent()
    drifted.device = "mps"
    with pytest.raises(inference.BackendDriftError):
        inference.load_local(_model_dir(tmp_path), sdk=FakeSDK(drifted))


@pytest.mark.parametrize("value", [float("nan"), -0.1, 1.1, 0.12345, "not-a-number"])
def test_invalid_outputs_are_rejected(value: object) -> None:
    with pytest.raises(inference.InvalidPrediction):
        inference.extract_noul_probability({"answers": {"laya_noul": {"noul": value}}})


def test_duplicate_order_and_resume_hash_are_canonical() -> None:
    base = {
        "symbol": "AAA",
        "source_session": "2022-01-03",
        "decision_session": "2022-01-04",
        **{field: 1.0 for field in inference.FEATURE_FIELDS},
        "probability": 0.1234,
    }
    other = dict(base, symbol="BBB")
    assert inference.canonical_prediction_hash([base, other]) == inference.canonical_prediction_hash([other, base])


def test_missing_nested_result_is_rejected() -> None:
    with pytest.raises(inference.InvalidPrediction):
        inference.extract_noul_probability({"answers": {}})


def test_score_stub_resume_deduplicates_completed_chunks(tmp_path: Path) -> None:
    """The offline plumbing resumes a complete chunk without re-scoring it."""
    import pandas as pd

    script_path = Path(__file__).resolve().parents[3] / "scripts" / "laya_feasibility_probe.py"
    spec = importlib.util.spec_from_file_location("laya_probe_for_test", script_path)
    assert spec and spec.loader
    probe = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(probe)
    snapshots = tmp_path / "snapshots.parquet"
    rows = []
    for index in range(3):
        rows.append(
            {
                "symbol": f"S{index}",
                "source_session": "2022-01-03",
                "decision_session": "2022-01-04",
                **{field: float(index) for field in inference.FEATURE_FIELDS},
            }
        )
    pd.DataFrame(rows).to_parquet(snapshots, index=False)
    gate = tmp_path / "technical.json"
    gate.write_text(json.dumps({"status": "PASS"}), encoding="utf-8")
    output = tmp_path / "predictions"
    arguments = SimpleNamespace(
        protocol=str(tmp_path / "protocol.json"),
        technical_gate=str(gate),
        model_root=str(tmp_path / "models"),
        snapshots=str(snapshots),
        out=str(output),
        max_seconds=900,
        resume=True,
        chunk_size=2,
    )
    (tmp_path / "protocol.json").write_text("{}", encoding="utf-8")
    assert probe.cmd_score(arguments) == 0
    assert probe.cmd_score(arguments) == 0
    manifest = json.loads((output / "score_manifest.json").read_text(encoding="utf-8"))
    assert manifest["complete"] is True
    assert any(chunk["resumed"] for chunk in manifest["chunks"])
