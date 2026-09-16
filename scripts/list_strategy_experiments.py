#!/usr/bin/env python3
"""Look up, filter, and render the HTS research catalog.

This is the operator-facing companion to ``docs/HTS_100_VARIATIONS_RESEARCH_PLAN.md``.
It reads the registry in ``strategy_lab/experiment_registry.py`` and prints,
filters, or re-renders the named configurations.  It never runs a backtest.

Examples
--------
    python scripts/list_strategy_experiments.py --list
    python scripts/list_strategy_experiments.py --show H052 --show H098
    python scripts/list_strategy_experiments.py --family family-6-concentration
    python scripts/list_strategy_experiments.py --kind alternative
    python scripts/list_strategy_experiments.py --search "volatility target"
    python scripts/list_strategy_experiments.py --write-catalog docs/HTS_VARIATIONS_CATALOG.md
    python scripts/list_strategy_experiments.py --verify
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path
from typing import Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from strategy_lab.experiment_config import (  # noqa: E402
    PRIORITY_DEFERRED,
    PRIORITY_STANDARD,
    PRIORITY_STARTING,
    STATUS_BLOCKED_DATA,
    STATUS_REGISTERED,
    CandidateSpec,
)
from strategy_lab.experiment_registry import (  # noqa: E402
    ExperimentRegistry,
    RegistryValidationError,
    get_registry,
    validate_registry,
)


def _index_table(registry: ExperimentRegistry, candidates: Sequence[CandidateSpec]) -> str:
    if not candidates:
        return "No matching candidates."
    rows = [("ID", "Name", "Kind", "Family", "Parent", "Priority", "Status")]
    for candidate in candidates:
        rows.append((
            candidate.candidate_id,
            candidate.name,
            candidate.kind,
            registry.families[candidate.family_id].title,
            candidate.parent_candidate_id or "",
            candidate.priority,
            candidate.status,
        ))
    widths = [max(len(row[column]) for row in rows) for column in range(len(rows[0]))]
    lines = []
    for position, row in enumerate(rows):
        lines.append("  ".join(cell.ljust(widths[column]) for column, cell in enumerate(row)).rstrip())
        if position == 0:
            lines.append("  ".join("-" * width for width in widths))
    lines.append("")
    lines.append(f"{len(candidates)} candidate(s).")
    return "\n".join(lines)


def _detail(registry: ExperimentRegistry, candidate: CandidateSpec) -> str:
    family = registry.families[candidate.family_id]
    lines = [
        f"{candidate.candidate_id}  {candidate.name}",
        f"  slug        {candidate.slug}",
        f"  kind        {candidate.kind}",
        f"  family      {family.title} ({family.family_id})",
        f"  parent      {candidate.parent_candidate_id or 'none'}",
        f"  priority    {candidate.priority}",
        f"  status      {candidate.status}",
        f"  rule        {candidate.rule}",
        f"  hypothesis  {candidate.hypothesis}",
        f"  fingerprint {candidate.fingerprint()}",
    ]
    if candidate.overrides:
        lines.append("  overrides")
        for key, value in candidate.overrides:
            lines.append(f"    {key} = {value!r}")
    else:
        lines.append("  overrides   none (inherits the control contract)")
    lines.append("  resolved parameters")
    for key, value in candidate.parameters:
        lines.append(f"    {key} = {value!r}")
    if candidate.data_requirements:
        lines.append(f"  requirements {', '.join(candidate.data_requirements)}")
    if candidate.blocked_reasons:
        lines.append("  blocked")
        for reason in candidate.blocked_reasons:
            lines.append(f"    {reason}")
    if candidate.research_refs:
        lines.append(f"  research    {'; '.join(candidate.research_refs)}")
    return "\n".join(lines)


def _filtered(
    registry: ExperimentRegistry,
    args: argparse.Namespace,
) -> list[CandidateSpec]:
    candidates: Sequence[CandidateSpec] = registry.search(" ".join(args.search)) if args.search else registry.all_candidates()
    if args.kind:
        candidates = [candidate for candidate in candidates if candidate.kind == args.kind]
    if args.family:
        candidates = [candidate for candidate in candidates if candidate.family_id == args.family]
    if args.priority:
        candidates = [candidate for candidate in candidates if candidate.priority == args.priority]
    if args.status:
        candidates = [candidate for candidate in candidates if candidate.status == args.status]
    return list(candidates)


def _json_payload(registry: ExperimentRegistry, candidates: Sequence[CandidateSpec]) -> str:
    return json.dumps(
        {
            "statistics": registry.statistics(),
            "candidates": [candidate.describe() for candidate in candidates],
        },
        indent=2,
        sort_keys=True,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--list", action="store_true", help="print the filtered index table (default action)")
    parser.add_argument("--show", metavar="ID", action="append", default=[], help="print full detail for a candidate ID")
    parser.add_argument("--family", metavar="FAMILY_ID", help="filter by family ID")
    parser.add_argument("--kind", choices=("control", "hts", "hts-v2", "alternative"), help="filter by candidate kind")
    parser.add_argument(
        "--priority", choices=(PRIORITY_STARTING, PRIORITY_STANDARD, PRIORITY_DEFERRED),
        help="filter by priority",
    )
    parser.add_argument(
        "--status", choices=(STATUS_REGISTERED, STATUS_BLOCKED_DATA), help="filter by registration status",
    )
    parser.add_argument("--search", metavar="TEXT", nargs="+", help="case-insensitive substring search")
    parser.add_argument("--families", action="store_true", help="print the family list with hypotheses")
    parser.add_argument("--json", action="store_true", help="emit JSON instead of a table")
    parser.add_argument("--write-catalog", metavar="PATH", help="write the Markdown catalog to PATH")
    parser.add_argument(
        "--date", metavar="YYYY-MM-DD", default=date.today().isoformat(),
        help="catalog header date (default: today)",
    )
    parser.add_argument("--verify", action="store_true", help="re-check the registry acceptance rules and exit")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    registry = get_registry()

    if args.verify:
        try:
            validate_registry(registry)
        except RegistryValidationError as error:
            print(f"registry verification FAILED: {error}", file=sys.stderr)
            return 1
        stats = registry.statistics()
        print(
            "registry OK: "
            f"{stats['total']} configurations "
            f"({stats['kinds']['control']} control, {stats['kinds']['hts']} hts, "
            f"{stats['kinds']['hts-v2']} hts-v2, "
            f"{stats['kinds']['alternative']} alternative), {stats['families']} families"
        )
        return 0

    if args.write_catalog:
        path = Path(args.write_catalog)
        if not path.is_absolute():
            path = ROOT / path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(registry.to_markdown(last_updated=args.date), encoding="utf-8")
        print(f"wrote {path.relative_to(ROOT)} ({len(registry.all_candidates())} candidates)")
        return 0

    if args.families:
        for family in registry.list_families(kind=args.kind):
            print(f"{family.family_id}  {family.title}")
            print(f"    {family.hypothesis}")
        return 0

    if args.show:
        output: list[str] = []
        for candidate_id in args.show:
            try:
                candidate = registry.get(candidate_id)
            except KeyError as error:
                print(str(error), file=sys.stderr)
                return 2
            output.append(_detail(registry, candidate))
        print("\n\n".join(output))
        return 0

    candidates = _filtered(registry, args)
    if args.json:
        print(_json_payload(registry, candidates))
    else:
        print(_index_table(registry, candidates))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
