#!/usr/bin/env python3
r"""Plan or apply conservative high-precision DB reviewer -> DBLP resolutions.

This script is intentionally separate from ``enrich_reviewers_with_dblp.py``.
The snapshot enrichment remains strict (unique exact aliases only). This second
stage promotes only two reviewed evidence patterns:

1. Unique first/last-name variant + conservative affiliation equivalence.
2. Ambiguous exact name + exactly one conservative affiliation-equivalent PID,
   but only when *every* competing DBLP PID has non-empty affiliation evidence.

The second condition is important: if a competing PID has no affiliation data,
absence of a match is not evidence against it.

By default this script is DRY-RUN ONLY. Use ``--apply`` to write decisions to
``name_resolution``. An SQLite backup is created automatically before changes.

Outputs under ``data/reports``:
- high_precision_entity_resolution_plan.csv
- high_precision_entity_resolution_plan.json
"""

from __future__ import annotations

import argparse
import csv
import json
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from audit_unresolved_entity_resolution import (
    DEFAULT_DATA_DIR,
    DEFAULT_DB,
    load_audit_groups,
    require_schema,
    with_nonperson_blockers,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REPORT_DIR = "reports"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def compact_timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def proposal_for_group(group: dict[str, Any]) -> dict[str, Any] | None:
    """Return a write proposal only for deliberately narrow evidence patterns."""
    group = with_nonperson_blockers(group)
    candidates = group.get("candidates") or []
    if not candidates:
        return None

    top = candidates[0]
    recommendation = group.get("recommendation", "")

    # Rule A: one relaxed first/last-name candidate exists and the affiliation
    # relationship itself is from the conservative display-cleaning equivalence
    # rules (not merely fuzzy/Jaccard similarity).
    if recommendation == "tier2_candidate":
        if (
            len(candidates) == 1
            and top.get("best_match_type") == "first_last_variant"
            and bool(top.get("first_last_exact"))
            and bool(top.get("affiliation_safe_equivalence_reason"))
        ):
            return _proposal(
                group,
                top,
                rule="unique_first_last_variant_safe_affiliation",
                status="resolved_high_precision_unique_variant",
                explanation=(
                    "Exactly one local DBLP PID matches the first/last-name variant "
                    "and its affiliation is conservatively equivalent to a reviewer affiliation."
                ),
            )
        return None

    # Rule B: multiple DBLP identities share the exact name. Exactly one already
    # has a conservative affiliation-equivalence match (enforced by the audit),
    # AND every competing PID has some affiliation evidence. If any competitor
    # is missing affiliation data, keep the case unresolved rather than treating
    # missing evidence as negative evidence.
    if recommendation == "high_precision_exact_affiliation_review":
        safe_candidates = [
            c for c in candidates if c.get("affiliation_safe_equivalence_reason")
        ]
        every_candidate_has_affiliation = all(
            bool(c.get("candidate_affiliations")) for c in candidates
        )
        if (
            group.get("status") == "ambiguous_exact_name"
            and top.get("best_match_type") == "exact_alias"
            and len(safe_candidates) == 1
            and safe_candidates[0].get("pid") == top.get("pid")
            and every_candidate_has_affiliation
        ):
            return _proposal(
                group,
                top,
                rule="exact_name_unique_safe_affiliation_complete_competitors",
                status="resolved_high_precision_exact_affiliation",
                explanation=(
                    "Multiple DBLP PIDs share the exact reviewer name, but exactly one "
                    "has a conservatively equivalent affiliation and every competing PID "
                    "has non-empty affiliation evidence."
                ),
            )

    return None


def _proposal(
    group: dict[str, Any],
    top: dict[str, Any],
    *,
    rule: str,
    status: str,
    explanation: str,
) -> dict[str, Any]:
    orcids = list(top.get("verified_orcids") or [])
    return {
        "name_key": group["name_key"],
        "reviewer_name": group["representative_name"],
        "resolution_status_before": group["status"],
        "proposed_status": status,
        "proposed_rule": rule,
        "candidate_pid": top["pid"],
        "candidate_name": top["canonical_name"],
        "best_match_type": top.get("best_match_type", ""),
        "reviewer_observation_count": group.get("observation_count", 0),
        "min_year": group.get("min_year", ""),
        "max_year": group.get("max_year", ""),
        "venues": " | ".join(group.get("venues") or []),
        "reviewer_affiliations": " | ".join(group.get("reviewer_affiliations") or []),
        "candidate_count": len(group.get("candidates") or []),
        "candidate_affiliations": " | ".join(top.get("candidate_affiliations") or []),
        "affiliation_safe_reason": top.get("affiliation_safe_equivalence_reason", ""),
        "affiliation_safe_reviewer": top.get("affiliation_safe_reviewer", ""),
        "affiliation_safe_candidate": top.get("affiliation_safe_candidate", ""),
        "candidate_score": top.get("score", ""),
        "score_margin_to_second": top.get("score_margin_to_second", ""),
        "verified_orcids": " | ".join(orcids),
        "best_orcid": orcids[0] if len(orcids) == 1 else "",
        "explanation": explanation,
    }


def build_plan(groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    proposals = []
    for group in groups:
        proposal = proposal_for_group(group)
        if proposal:
            proposals.append(proposal)
    return sorted(proposals, key=lambda x: x["reviewer_name"].casefold())


def write_plan(plan: list[dict[str, Any]], csv_path: Path, json_path: Path) -> None:
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(plan[0].keys()) if plan else [
        "name_key", "reviewer_name", "proposed_rule", "candidate_pid", "candidate_name"
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(plan)

    payload = {
        "generatedAt": now_iso(),
        "mutatesResolutionDatabase": False,
        "proposalCount": len(plan),
        "proposals": plan,
    }
    json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def apply_plan(conn: sqlite3.Connection, plan: list[dict[str, Any]]) -> int:
    changed = 0
    with conn:
        for item in plan:
            best_orcid = item["best_orcid"] or None
            best_orcid_source = "dblp_person_record" if best_orcid else None
            notes = (
                f"High-precision second-stage resolution: {item['proposed_rule']}. "
                f"{item['explanation']}"
            )
            cursor = conn.execute(
                """
                UPDATE name_resolution
                SET status=?, resolved_dblp_pid=?, resolved_dblp_name=?,
                    best_orcid=?, best_orcid_source=?, notes=?, updated_at=?
                WHERE name_key=? AND resolved_dblp_pid IS NULL
                """,
                (
                    item["proposed_status"],
                    item["candidate_pid"],
                    item["candidate_name"],
                    best_orcid,
                    best_orcid_source,
                    notes,
                    now_iso(),
                    item["name_key"],
                ),
            )
            changed += cursor.rowcount
    return changed


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=DEFAULT_DATA_DIR,
        help=f"Data directory (default: {DEFAULT_DATA_DIR})",
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=None,
        help="Entity-resolution SQLite path (default: <data-dir>/entity_resolution.sqlite)",
    )
    parser.add_argument(
        "--report-dir",
        type=Path,
        default=None,
        help="Report directory (default: <data-dir>/reports)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write the proposed high-precision PID resolutions. Default is dry-run only.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    data_dir = args.data_dir.resolve()
    db_path = (args.db or (data_dir / DEFAULT_DB)).resolve()
    report_dir = (args.report_dir or (data_dir / DEFAULT_REPORT_DIR)).resolve()

    if not db_path.exists():
        raise SystemExit(
            f"Entity-resolution database not found: {db_path}\n"
            "Run scripts/enrich_reviewers_with_dblp.py first."
        )

    csv_path = report_dir / "high_precision_entity_resolution_plan.csv"
    json_path = report_dir / "high_precision_entity_resolution_plan.json"

    conn = sqlite3.connect(db_path)
    try:
        require_schema(conn)
        groups = load_audit_groups(conn)
        plan = build_plan(groups)
        write_plan(plan, csv_path, json_path)

        by_rule: dict[str, int] = {}
        for item in plan:
            by_rule[item["proposed_rule"]] = by_rule.get(item["proposed_rule"], 0) + 1

        mode = "APPLY" if args.apply else "DRY RUN"
        print(f"High-precision entity resolution: {mode}")
        print(f"Proposed resolutions: {len(plan)}")
        for rule, count in sorted(by_rule.items()):
            print(f"  {rule:58s} {count:4d}")

        print("\nSample proposals")
        print("----------------")
        for item in plan[:15]:
            print(
                f"  - {item['reviewer_name']} -> {item['candidate_name']} "
                f"[{item['candidate_pid']}] ({item['proposed_rule']})"
            )

        print(f"\nPlan CSV:  {csv_path}")
        print(f"Plan JSON: {json_path}")

        if not args.apply:
            print("\nNo database rows changed. Re-run with --apply after reviewing the plan.")
            return 0

        backup_dir = data_dir / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        backup_path = backup_dir / f"entity_resolution.before_high_precision_{compact_timestamp()}.sqlite"
        conn.commit()
        shutil.copy2(db_path, backup_path)

        changed = apply_plan(conn, plan)
        print(f"\nApplied resolutions: {changed}")
        print(f"Backup: {backup_path}")
        if changed != len(plan):
            print(
                "WARNING: Applied row count differs from the plan count. "
                "Some rows may already have been resolved."
            )
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
