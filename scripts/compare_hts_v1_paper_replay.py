"""Compare a recorded paper decision journal with its offline replay journal."""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from strategy_lab.hts_v1_core import DecisionJournal, compare_decision_journals


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("expected", type=Path, help="paper-session JSONL journal")
    parser.add_argument("actual", type=Path, help="offline replay JSONL journal")
    args = parser.parse_args(argv)
    compare_decision_journals(DecisionJournal.load(args.expected), DecisionJournal.load(args.actual))
    print("HTS v1 decision parity passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
