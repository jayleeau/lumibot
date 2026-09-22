"""Research-only package for the Laya offline entry-eligibility study (Task 0a).

This package intentionally imports no inference framework, model, torch, broker,
or archive module at import time.  It holds the frozen causal research contract
(``contracts``) and the deterministic snapshot/target builder (``snapshots``).
The optional inference environment lives in ``strategy_lab/laya_research/
inference.py`` in a later task and is never imported here.
"""

__all__ = ["contracts", "snapshots"]
