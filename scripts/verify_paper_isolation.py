#!/usr/bin/env python3
"""Guard the paper-trading isolation boundary.

The live paper fleet (oracle_live_strategies/) runs strategies that import
several modules from this repo's strategy_lab/ using the main .venv interpreter.
This script is the standing invariant that keeps the offline Laya research (and
any future research) from ever leaking into the live/paper execution path.

What it guarantees (the invariant we committed to jayleeau 2026-09-22):

1. LIVE-IMPORTED strategy_lab modules must NOT import torch, laya, or
   strategy_lab.laya_research. They must stay the unchanged, provider-generic
   signal/feature code the paper bots already run.
2. The main .venv interpreter used by the paper bots must NOT contain torch/laya.
   (Laya lives only in the isolated .venv-laya.)
3. The live runner entrypoints (oracle_live_strategies/{run_all,run_paper_six}.py
   and the launchd wrapper run_live.sh) must not reference laya.

If any of these BREAKS, the script exits non-zero -- call it in CI/pre-commit so a
change cannot silently reach the paper path.

NOTE: this does NOT freeze the strategy_lab modules byte-for-byte. Legitimate
strategy work may intentionally alter them; when that happens, the operator must
explicitly acknowledge the change and re-qualify the paper path. The hashes here
are a recorded baseline for review, not a hard block.
"""
from __future__ import annotations

import hashlib
import importlib.util
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STRATEGY_LAB = ROOT / "strategy_lab"
# oracle_live_strategies lives at the profile root (peer of the lumibot repo's
# parent), e.g. <profile>/oracle_live_strategies. Resolve it from the repo layout
# or allow an explicit override -- never hardcode an absolute profile path.
_PRIVATE = Path(os.environ.get("GLITCH_ORACLE_DIR", ROOT.parent.parent / "oracle_live_strategies"))
PRIVATE = _PRIVATE

# strategy_lab modules the live paper bots import (see run_paper_six.py and
# run_all.py). Kept in a known list so an accidental import from a candidate that
# reads strategy_lab.** does not fly under the radar.
LIVE_IMPORTED_MODULES = (
    "native_experiments.py",
    "feature_store.py",
    "hts_variants.py",
    "experiment_universes.py",
    "experiment_config.py",
)

# Symbols that must never appear in a live-imported module's source.
FORBIDDEN_SYMBOLS = ("import torch", "from torch", "import laya", "from laya", "laya_research")

# Baseline SHA-256 recorded 2026-09-22 (Laya Task 0b, commit 8a1ab60f). These are
# reviewable evidence, not a freeze.
BASELINE_HASHES = {
    "native_experiments.py": "cbc5e4e4dcb893c4713ad626517a0e781156a2a51ec4fd018e30222611a982c6",
    "feature_store.py": "0fe41ce8633acdc861af3603b691939baa1a6623c977b2125e04c3bbc3f4cee8",
    "hts_variants.py": "65ea7301934346e9a44cf7b5691a2558ffd8a9bdfe85182ba6016057bbe0f248",
    "experiment_universes.py": "92f9e18d8704917fa90f6e1068d7fa4f5e7fec6836bb0b6f1be4b07b9d209416",
    "experiment_config.py": "da1ca6d5313541a6611c24f5e9b438bb361db4a9ef32f868f1e1dbdf2b8b1569",
}

LIVE_RUNNERS = ("run_all.py", "run_paper_six.py", "run_live.sh")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def module_forbidden(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8", errors="replace")
    return [sym for sym in FORBIDDEN_SYMBOLS if sym in text]


def interpreter_has_pkg(name: str) -> bool:
    return importlib.util.find_spec(name) is not None


def main() -> int:
    errors: list[str] = []
    changed: list[str] = []

    print(f"[paper-isolation] repo root: {ROOT}")
    print(f"[paper-isolation] live runner dir: {PRIVATE}")

    # 1. Live-imported strategy_lab modules: no torch/laya/laya_research.
    for name in LIVE_IMPORTED_MODULES:
        path = STRATEGY_LAB / name
        if not path.exists():
            errors.append(f"module missing: strategy_lab/{name}")
            continue
        forbidden = module_forbidden(path)
        if forbidden:
            errors.append(f"{name} references forbidden symbol(s): {forbidden}")
        digest = sha256(path)
        baseline = BASELINE_HASHES.get(name)
        if baseline is not None and digest != baseline:
            changed.append(f"{name}: hash changed ({baseline[:12]} -> {digest[:12]})")
        print(f"[paper-isolation] {name:32s} sha256={digest[:16]}" + ("  (CHANGED)" if digest != baseline else ""))

    # 2. Main .venv must not have torch/laya (paper bots use this interpreter).
    for pkg in ("torch", "laya"):
        if interpreter_has_pkg(pkg):
            errors.append(f"main .venv unexpectedly has {pkg} -- paper bots MUST NOT load it")

    # 3. Live runner entrypoints must not reference laya.
    if PRIVATE.exists():
        for name in LIVE_RUNNERS:
            path = PRIVATE / name
            if path.exists() and "laya" in path.read_text(encoding="utf-8", errors="replace").lower():
                errors.append(f"live runner {name} references 'laya'")
    else:
        print(f"[paper-isolation] (oracle_live_strategies not found at {PRIVATE} -- skipping runner scan)")

    if changed:
        # Legitimate strategy work may change these; this is informational, not
        # a block. The operator must re-qualify the paper path on acknowledge.
        print("\n[paper-isolation] WARNING: live-imported module hash(es) changed since the 2026-09-22\nbaseline recorded in this script. This may be legitimate strategy work, but the paper\npath must be re-qualified and the new hashes recorded before anything relying on it runs.")
        for c in changed:
            print(f"    - {c}")

    if errors:
        print("\n[paper-isolation] FAIL: the isolation boundary is violated:")
        for e in errors:
            print(f"    - {e}")
        print("\nSTOP: do not proceed until the paper path is proved unaffected.")
        return 1

    print("\n[paper-isolation] PASS: paper-trading isolation boundary intact.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())