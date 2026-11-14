#!/usr/bin/env python3
r"""Plan or apply the next conservative DB reviewer -> DBLP resolution tier.

This stage deliberately does *not* relax the existing ROR resolver. It adds two
separate evidence patterns on top of already-reviewed identity decisions:

1. Existing-PID alias propagation (apply-eligible): an unresolved service-name
   spelling points to a DBLP PID that is already resolved elsewhere in the same
   entity-resolution database. The unresolved spelling must either have exactly
   one compatible local DBLP candidate, or exactly one already-resolved PID
   candidate that also shares an explicit ROR organization with the service
   affiliation while no competing candidate shares any ROR organization.

2. Two independent ROR anchors (apply-eligible): for an ambiguous exact name,
   one exact-name DBLP candidate shares at least two distinct ROR organization
   IDs with the service history, using at least two distinct affiliation
   observations on both sides. No competitor may share two or more anchors.

No fuzzy organization matching and no synthesized ROR organization names are
introduced here. Default mode is DRY RUN. ``--apply`` writes only proposals
explicitly marked apply-eligible and creates an SQLite backup first.
"""

from __future__ import annotations

import argparse
import csv
import json
import shutil
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from apply_ror_entity_resolution import add_ror_evidence
from audit_unresolved_entity_resolution import (
    DEFAULT_DATA_DIR,
    DEFAULT_DB,
    load_audit_groups,
    require_schema,
)
from ror_affiliations import (
    DEFAULT_ROR_DIRNAME,
    ROR_RELEASE,
    RorAffiliationIndex,
    IdentityRorAffiliationIndex,
    RorMatch,
    ensure_ror_index,
)

DEFAULT_REPORT_DIR = "reports"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def compact_timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def load_resolved_pid_owners(
    conn: sqlite3.Connection,
) -> dict[str, list[dict[str, str]]]:
    owners: dict[str, list[dict[str, str]]] = defaultdict(list)
    rows = conn.execute(
        """
        SELECT name_key, representative_name, status,
               resolved_dblp_pid, resolved_dblp_name
        FROM name_resolution
        WHERE resolved_dblp_pid IS NOT NULL
          AND TRIM(resolved_dblp_pid) <> ''
        ORDER BY representative_name COLLATE NOCASE, name_key
        """
    ).fetchall()
    for name_key, representative_name, status, pid, resolved_name in rows:
        owners[str(pid)].append(
            {
                "name_key": str(name_key),
                "representative_name": str(representative_name),
                "status": str(status),
                "resolved_dblp_name": str(resolved_name or ""),
            }
        )
    return dict(owners)


def _name_compatible(candidate: dict[str, Any]) -> bool:
    match_type = candidate.get("best_match_type")
    if match_type == "exact_alias":
        return True
    if match_type == "first_last_variant":
        return bool(candidate.get("first_last_exact"))
    if match_type == "initial_last_variant":
        return bool(candidate.get("initial_compatible")) and bool(
            candidate.get("last_name_exact")
        )
    return False


def _distinct_match_sources(
    matches: dict[str, RorMatch], ror_ids: list[str]
) -> set[str]:
    return {
        matches[ror_id].source_affiliation.casefold()
        for ror_id in ror_ids
        if ror_id in matches and matches[ror_id].source_affiliation
    }


def _match_payload(matches: dict[str, RorMatch], ror_ids: list[str]) -> list[dict[str, str]]:
    payload = []
    for ror_id in sorted(ror_ids):
        match = matches.get(ror_id)
        if not match:
            continue
        payload.append(
            {
                "ror_id": match.ror_id,
                "ror_name": match.display_name,
                "source_affiliation": match.source_affiliation,
                "matched_surface": match.matched_surface,
                "matched_ror_name": match.matched_ror_name,
                "matched_name_type": match.matched_name_type,
            }
        )
    return payload


def _base_proposal(
    group: dict[str, Any],
    candidate: dict[str, Any],
    *,
    rule: str,
    proposed_status: str,
    apply_eligible: bool,
    explanation: str,
    resolved_owners: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    shared_ids = list(candidate.get("shared_ror_ids") or [])
    service_matches = group.get("service_ror_matches") or {}
    candidate_matches = candidate.get("ror_matches") or {}
    shared_names = [
        service_matches[ror_id].display_name
        for ror_id in shared_ids
        if ror_id in service_matches
    ]
    orcids = list(candidate.get("verified_orcids") or [])
    owners = resolved_owners or []
    return {
        "name_key": group["name_key"],
        "reviewer_name": group["representative_name"],
        "resolution_status_before": group["status"],
        "proposed_status": proposed_status,
        "proposed_rule": rule,
        "apply_eligible": int(apply_eligible),
        "candidate_pid": candidate["pid"],
        "candidate_name": candidate["canonical_name"],
        "best_match_type": candidate.get("best_match_type", ""),
        "reviewer_observation_count": group.get("observation_count", 0),
        "min_year": group.get("min_year", ""),
        "max_year": group.get("max_year", ""),
        "venues": " | ".join(group.get("venues") or []),
        "reviewer_affiliations": " | ".join(group.get("reviewer_affiliations") or []),
        "candidate_count": len(group.get("candidates") or []),
        "candidate_affiliations": " | ".join(candidate.get("candidate_affiliations") or []),
        "shared_ror_count": len(shared_ids),
        "shared_ror_ids": " | ".join(shared_ids),
        "shared_ror_names": " | ".join(shared_names),
        "service_shared_ror_source_count": len(
            _distinct_match_sources(service_matches, shared_ids)
        ),
        "candidate_shared_ror_source_count": len(
            _distinct_match_sources(candidate_matches, shared_ids)
        ),
        "service_ror_evidence_json": json.dumps(
            _match_payload(service_matches, shared_ids),
            ensure_ascii=False,
            sort_keys=True,
        ),
        "candidate_ror_evidence_json": json.dumps(
            _match_payload(candidate_matches, shared_ids),
            ensure_ascii=False,
            sort_keys=True,
        ),
        "existing_resolved_names": " | ".join(
            owner["representative_name"] for owner in owners
        ),
        "existing_resolution_statuses": " | ".join(
            sorted({owner["status"] for owner in owners})
        ),
        "verified_orcids": " | ".join(orcids),
        "best_orcid": orcids[0] if len(orcids) == 1 else "",
        "ror_release": ROR_RELEASE,
        "explanation": explanation,
    }


def proposal_for_group(
    group: dict[str, Any],
    resolved_pid_owners: dict[str, list[dict[str, str]]],
) -> tuple[dict[str, Any] | None, str]:
    from audit_unresolved_entity_resolution import with_nonperson_blockers
    group = with_nonperson_blockers(group)
    candidates = group.get("candidates") or []
    if not candidates:
        return None, "no_dblp_candidate"

    # Tier A1: the unresolved spelling has one compatible local candidate and
    # that exact PID is already resolved elsewhere. This propagates an existing
    # identity decision; it does not discover a new DBLP person.
    if len(candidates) == 1:
        candidate = candidates[0]
        owners = resolved_pid_owners.get(candidate.get("pid"), [])
        if owners and _name_compatible(candidate):
            return (
                _base_proposal(
                    group,
                    candidate,
                    rule="unique_candidate_existing_resolved_pid",
                    proposed_status="resolved_existing_pid_alias",
                    apply_eligible=True,
                    resolved_owners=owners,
                    explanation=(
                        "The unresolved service-name spelling has exactly one compatible "
                        "local DBLP candidate, and that PID is already resolved elsewhere "
                        "in the same entity-resolution database."
                    ),
                ),
                "proposed_existing_pid_unique_candidate",
            )

    # Tier A2: more than one local candidate exists, but exactly one candidate
    # is an already-resolved PID *and* is the only candidate sharing any explicit
    # ROR organization with the service affiliations.
    candidates_with_shared_ror = [
        candidate for candidate in candidates if candidate.get("shared_ror_ids")
    ]
    resolved_ror_candidates = [
        candidate
        for candidate in candidates_with_shared_ror
        if resolved_pid_owners.get(candidate.get("pid")) and _name_compatible(candidate)
    ]
    if len(candidates_with_shared_ror) == 1 and len(resolved_ror_candidates) == 1:
        candidate = resolved_ror_candidates[0]
        owners = resolved_pid_owners[candidate["pid"]]
        return (
            _base_proposal(
                group,
                candidate,
                rule="unique_ror_overlap_existing_resolved_pid",
                proposed_status="resolved_existing_pid_ror_alias",
                apply_eligible=True,
                resolved_owners=owners,
                explanation=(
                    "Exactly one local candidate shares an explicit ROR organization "
                    "with the service affiliation, that candidate's PID is already "
                    "resolved elsewhere, and the name relation is compatible."
                ),
            ),
            "proposed_existing_pid_ror_alias",
        )

    # Tier B: two independent ROR anchors. This deliberately relaxes *only*
    # the competitor-coverage requirement, while strengthening positive evidence
    # from one to two independent canonical organizations. The rule was first
    # deployed read-only and promoted only after reviewing the real proposal set.
    if group.get("status") == "ambiguous_exact_name":
        exact_candidates = [
            candidate
            for candidate in candidates
            if candidate.get("best_match_type") == "exact_alias"
        ]
        qualifying: list[dict[str, Any]] = []
        for candidate in exact_candidates:
            shared_ids = list(candidate.get("shared_ror_ids") or [])
            if len(shared_ids) < 2:
                continue
            service_source_count = len(
                _distinct_match_sources(group.get("service_ror_matches") or {}, shared_ids)
            )
            candidate_source_count = len(
                _distinct_match_sources(candidate.get("ror_matches") or {}, shared_ids)
            )
            if service_source_count >= 2 and candidate_source_count >= 2:
                qualifying.append(candidate)

        if len(qualifying) == 1:
            candidate = qualifying[0]
            return (
                _base_proposal(
                    group,
                    candidate,
                    rule="ror_two_independent_anchors",
                    proposed_status="resolved_ror_two_anchor_exact_name",
                    apply_eligible=True,
                    explanation=(
                        "One exact-name candidate shares at least two distinct ROR "
                        "organizations with the service history, supported by at least "
                        "two distinct affiliation observations on both sides. No other "
                        "exact-name candidate reaches two anchors. This stronger positive "
                        "evidence is apply-eligible even when some competitors lack ROR coverage."
                    ),
                ),
                "proposed_two_anchor_resolution",
            )
        if len(qualifying) > 1:
            return None, "multiple_exact_candidates_have_two_ror_anchors"

    return None, "no_next_tier_rule"


def build_plan(
    groups: list[dict[str, Any]],
    ror: RorAffiliationIndex,
    resolved_pid_owners: dict[str, list[dict[str, str]]],
) -> tuple[list[dict[str, Any]], Counter[str]]:
    plan: list[dict[str, Any]] = []
    reasons: Counter[str] = Counter()
    for group in groups:
        enriched = add_ror_evidence(group, ror)
        proposal, reason = proposal_for_group(enriched, resolved_pid_owners)
        reasons[reason] += 1
        if proposal:
            plan.append(proposal)
    return sorted(plan, key=lambda x: x["reviewer_name"].casefold()), reasons


def write_plan(plan: list[dict[str, Any]], csv_path: Path, json_path: Path) -> None:
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(plan[0].keys()) if plan else [
        "name_key",
        "reviewer_name",
        "proposed_rule",
        "apply_eligible",
        "candidate_pid",
        "candidate_name",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(plan)

    payload = {
        "generatedAt": now_iso(),
        "mutatesResolutionDatabase": False,
        "proposalCount": len(plan),
        "applyEligibleCount": sum(bool(item["apply_eligible"]) for item in plan),
        "reviewOnlyCount": sum(not bool(item["apply_eligible"]) for item in plan),
        "proposals": plan,
    }
    json_path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def apply_plan(conn: sqlite3.Connection, plan: list[dict[str, Any]]) -> int:
    changed = 0
    with conn:
        for item in plan:
            if not bool(item.get("apply_eligible")):
                continue
            best_orcid = item.get("best_orcid") or None
            best_orcid_source = "dblp_person_record" if best_orcid else None
            notes = (
                f"Next-tier entity resolution: {item['proposed_rule']}. "
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
        "--ror-dir",
        type=Path,
        default=None,
        help="ROR snapshot/index directory (default: <data-dir>/ror_snapshot)",
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
        help=(
            "Apply all proposals marked apply-eligible, including the reviewed "
            "two-independent-ROR-anchor rule."
        ),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    data_dir = args.data_dir.resolve()
    db_path = (args.db or (data_dir / DEFAULT_DB)).resolve()
    ror_dir = (args.ror_dir or (data_dir / DEFAULT_ROR_DIRNAME)).resolve()
    report_dir = (args.report_dir or (data_dir / DEFAULT_REPORT_DIR)).resolve()

    if not db_path.exists():
        raise SystemExit(
            f"Entity-resolution database not found: {db_path}\n"
            "Run scripts/enrich_reviewers_with_dblp.py first."
        )

    _snapshot, index_path = ensure_ror_index(ror_dir, download=False)
    csv_path = report_dir / "next_tier_entity_resolution_plan.csv"
    json_path = report_dir / "next_tier_entity_resolution_plan.json"

    conn = sqlite3.connect(db_path)
    ror = IdentityRorAffiliationIndex(index_path)
    try:
        require_schema(conn)
        groups = load_audit_groups(conn)
        owners = load_resolved_pid_owners(conn)
        plan, reasons = build_plan(groups, ror, owners)
        write_plan(plan, csv_path, json_path)

        by_rule = Counter(item["proposed_rule"] for item in plan)
        apply_eligible = [item for item in plan if bool(item["apply_eligible"])]
        review_only = [item for item in plan if not bool(item["apply_eligible"])]

        mode = "APPLY" if args.apply else "DRY RUN"
        print(f"Next-tier entity resolution ({ROR_RELEASE}): {mode}")
        print(f"Unresolved groups considered: {len(groups)}")
        print(f"Total proposals: {len(plan)}")
        print(f"  apply-eligible: {len(apply_eligible)}")
        print(f"  review-only:    {len(review_only)}")
        for rule, count in sorted(by_rule.items()):
            print(f"  {rule:52s} {count:4d}")

        print("\nDecision reasons")
        print("----------------")
        for reason, count in reasons.most_common():
            print(f"  {reason:52s} {count:4d}")

        alias_apply = [
            item
            for item in apply_eligible
            if item["proposed_rule"]
            in {
                "unique_candidate_existing_resolved_pid",
                "unique_ror_overlap_existing_resolved_pid",
            }
        ]
        two_anchor_apply = [
            item
            for item in apply_eligible
            if item["proposed_rule"] == "ror_two_independent_anchors"
        ]

        print("\nSample apply-eligible existing-PID aliases")
        print("------------------------------------------")
        for item in alias_apply[:15]:
            owners_text = item.get("existing_resolved_names") or "existing resolved PID"
            print(
                f"  - {item['reviewer_name']} -> {item['candidate_name']} "
                f"[{item['candidate_pid']}] via {owners_text}"
            )

        print("\nSample apply-eligible two-anchor proposals")
        print("------------------------------------------")
        for item in two_anchor_apply[:15]:
            print(
                f"  - {item['reviewer_name']} -> {item['candidate_name']} "
                f"[{item['candidate_pid']}] via {item['shared_ror_names']}"
            )

        if review_only:
            print("\nOther review-only proposals")
            print("---------------------------")
            for item in review_only[:15]:
                print(
                    f"  - {item['reviewer_name']} -> {item['candidate_name']} "
                    f"[{item['candidate_pid']}]"
                )

        print(f"\nPlan CSV:  {csv_path}")
        print(f"Plan JSON: {json_path}")

        if not args.apply:
            print(
                "\nNo database rows changed. Review the plan first; --apply will write "
                "all rows currently marked apply-eligible."
            )
            return 0

        if not apply_eligible:
            print("\nNo apply-eligible rows; database unchanged.")
            return 0

        backup_dir = data_dir / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        backup_path = (
            backup_dir
            / f"entity_resolution.before_next_tier_{compact_timestamp()}.sqlite"
        )
        conn.commit()
        shutil.copy2(db_path, backup_path)

        changed = apply_plan(conn, plan)
        print(f"\nApplied next-tier resolutions: {changed}")
        if review_only:
            print(f"Review-only proposals skipped: {len(review_only)}")
        print(f"Backup: {backup_path}")
        if changed != len(apply_eligible):
            print(
                "WARNING: Applied row count differs from the apply-eligible plan count. "
                "Some rows may already have been resolved."
            )
        return 0
    finally:
        ror.close()
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
