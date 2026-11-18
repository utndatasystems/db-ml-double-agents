#!/usr/bin/env python3
r"""Plan or apply conservative ROR-backed DB reviewer -> DBLP resolutions.

This is an additional offline entity-resolution stage. It consumes the local,
pinned ROR dump prepared by ``download_ror_snapshot.py`` and never calls the
live ROR API.

Automatic proposal rules are deliberately narrow:

1. Unique first/last-name variant: exactly one DBLP candidate, the first and
   last names agree, and service + DBLP affiliation strings independently map
   to at least one identical, unambiguous ROR organization ID.

2. Ambiguous exact name: among the exact-name DBLP competitors, every candidate
   has at least one affiliation that can be canonicalized to ROR, and exactly
   one candidate shares any ROR organization with the service affiliations.

No fuzzy organization matching is used. Ambiguous ROR aliases are ignored.
The website's displayed affiliation strings are not changed by this stage.

Default mode is DRY RUN. Use ``--apply`` to update ``name_resolution``; a backup
of the entity-resolution SQLite file is created first.
"""

from __future__ import annotations

import argparse
import csv
import json
import shutil
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

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


def _match_payload(matches: dict[str, RorMatch]) -> list[dict[str, str]]:
    return [
        {
            "ror_id": match.ror_id,
            "ror_name": match.display_name,
            "source_affiliation": match.source_affiliation,
            "matched_surface": match.matched_surface,
            "matched_ror_name": match.matched_ror_name,
            "matched_name_type": match.matched_name_type,
        }
        for _ror_id, match in sorted(matches.items())
    ]


def add_ror_evidence(
    group: dict[str, Any],
    ror: RorAffiliationIndex,
) -> dict[str, Any]:
    service_matches = ror.match_affiliations(group.get("reviewer_affiliations") or [])
    enriched = dict(group)
    enriched["service_ror_matches"] = service_matches
    enriched_candidates = []
    for candidate in group.get("candidates") or []:
        item = dict(candidate)
        item["ror_matches"] = ror.match_affiliations(
            candidate.get("candidate_affiliations") or []
        )
        item["shared_ror_ids"] = sorted(
            set(service_matches) & set(item["ror_matches"])
        )
        enriched_candidates.append(item)
    enriched["candidates"] = enriched_candidates
    return enriched


def proposal_for_group(group: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
    from audit_unresolved_entity_resolution import with_nonperson_blockers
    group = with_nonperson_blockers(group)
    candidates = group.get("candidates") or []
    service_ror = group.get("service_ror_matches") or {}
    if not service_ror:
        return None, "service_affiliation_not_canonicalized"
    if not candidates:
        return None, "no_dblp_candidate"

    status = group.get("status", "")

    if status == "candidate_unique_variant" and len(candidates) == 1:
        candidate = candidates[0]
        if (
            candidate.get("best_match_type") == "first_last_variant"
            and bool(candidate.get("first_last_exact"))
            and candidate.get("shared_ror_ids")
        ):
            return (
                _proposal(
                    group,
                    candidate,
                    rule="ror_unique_first_last_variant",
                    status="resolved_ror_unique_variant",
                    explanation=(
                        "Exactly one DBLP PID matches the first/last-name variant and "
                        "service/DBLP affiliations independently canonicalize to the same "
                        "unambiguous ROR organization."
                    ),
                ),
                "proposed",
            )
        return None, "unique_variant_without_shared_ror"

    if status == "ambiguous_exact_name":
        exact_candidates = [
            candidate
            for candidate in candidates
            if candidate.get("best_match_type") == "exact_alias"
        ]
        if len(exact_candidates) < 2:
            return None, "not_multiple_exact_candidates"

        # Missing ROR coverage is treated as unknown evidence, never as evidence
        # against a competitor.
        if not all(candidate.get("ror_matches") for candidate in exact_candidates):
            return None, "exact_competitor_missing_ror_coverage"

        overlapping = [
            candidate
            for candidate in exact_candidates
            if candidate.get("shared_ror_ids")
        ]
        if len(overlapping) != 1:
            return None, (
                "multiple_exact_candidates_share_ror"
                if len(overlapping) > 1
                else "no_exact_candidate_shares_ror"
            )

        candidate = overlapping[0]
        return (
            _proposal(
                group,
                candidate,
                rule="ror_exact_name_unique_affiliation_complete_competitors",
                status="resolved_ror_exact_affiliation",
                explanation=(
                    "Multiple DBLP PIDs share the exact reviewer name, every exact-name "
                    "competitor has ROR-canonicalized affiliation evidence, and exactly "
                    "one candidate shares a canonical ROR organization with the service "
                    "affiliations."
                ),
            ),
            "proposed",
        )

    return None, "status_not_enabled_for_ror_auto_resolution"


def _proposal(
    group: dict[str, Any],
    candidate: dict[str, Any],
    *,
    rule: str,
    status: str,
    explanation: str,
) -> dict[str, Any]:
    orcids = list(candidate.get("verified_orcids") or [])
    shared_ids = list(candidate.get("shared_ror_ids") or [])
    service_matches = group.get("service_ror_matches") or {}
    candidate_matches = candidate.get("ror_matches") or {}
    shared_names = [
        service_matches[ror_id].display_name
        for ror_id in shared_ids
        if ror_id in service_matches
    ]
    exact_candidates = [
        item for item in group.get("candidates") or []
        if item.get("best_match_type") == "exact_alias"
    ]
    return {
        "name_key": group["name_key"],
        "reviewer_name": group["representative_name"],
        "resolution_status_before": group["status"],
        "proposed_status": status,
        "proposed_rule": rule,
        "candidate_pid": candidate["pid"],
        "candidate_name": candidate["canonical_name"],
        "best_match_type": candidate.get("best_match_type", ""),
        "reviewer_observation_count": group.get("observation_count", 0),
        "min_year": group.get("min_year", ""),
        "max_year": group.get("max_year", ""),
        "venues": " | ".join(group.get("venues") or []),
        "reviewer_affiliations": " | ".join(group.get("reviewer_affiliations") or []),
        "all_candidate_count": len(group.get("candidates") or []),
        "exact_candidate_count_considered": len(exact_candidates),
        "candidate_affiliations": " | ".join(candidate.get("candidate_affiliations") or []),
        "shared_ror_ids": " | ".join(shared_ids),
        "shared_ror_names": " | ".join(shared_names),
        "service_ror_evidence_json": json.dumps(
            _match_payload({ror_id: service_matches[ror_id] for ror_id in shared_ids}),
            ensure_ascii=False,
            sort_keys=True,
        ),
        "candidate_ror_evidence_json": json.dumps(
            _match_payload({ror_id: candidate_matches[ror_id] for ror_id in shared_ids}),
            ensure_ascii=False,
            sort_keys=True,
        ),
        "ror_release": ROR_RELEASE,
        "verified_orcids": " | ".join(orcids),
        "best_orcid": orcids[0] if len(orcids) == 1 else "",
        "explanation": explanation,
    }


def build_plan(
    groups: list[dict[str, Any]],
    ror: RorAffiliationIndex,
) -> tuple[list[dict[str, Any]], Counter[str]]:
    proposals: list[dict[str, Any]] = []
    reasons: Counter[str] = Counter()
    for raw_group in groups:
        group = add_ror_evidence(raw_group, ror)
        proposal, reason = proposal_for_group(group)
        reasons[reason] += 1
        if proposal:
            proposals.append(proposal)
    proposals.sort(key=lambda item: item["reviewer_name"].casefold())
    return proposals, reasons


def write_plan(
    plan: list[dict[str, Any]],
    reasons: Counter[str],
    csv_path: Path,
    json_path: Path,
) -> None:
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(plan[0].keys()) if plan else [
        "name_key",
        "reviewer_name",
        "proposed_rule",
        "candidate_pid",
        "candidate_name",
        "shared_ror_ids",
        "shared_ror_names",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(plan)

    payload = {
        "generatedAt": now_iso(),
        "rorRelease": ROR_RELEASE,
        "purpose": "offline ROR-backed high-precision DB reviewer -> DBLP resolution plan",
        "mutatesResolutionDatabase": False,
        "proposalCount": len(plan),
        "decisionReasonCounts": dict(sorted(reasons.items())),
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
            best_orcid = item["best_orcid"] or None
            best_orcid_source = "dblp_person_record" if best_orcid else None
            notes = (
                f"ROR-backed second-stage resolution using {item['ror_release']}: "
                f"{item['proposed_rule']}. Shared ROR IDs: {item['shared_ror_ids']}. "
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
        help="Write all proposals satisfying the guarded ROR rules. Default is dry-run.",
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

    try:
        _snapshot, index_path = ensure_ror_index(ror_dir, download=False)
    except (FileNotFoundError, RuntimeError) as exc:
        raise SystemExit(str(exc)) from exc

    csv_path = report_dir / "ror_entity_resolution_plan.csv"
    json_path = report_dir / "ror_entity_resolution_plan.json"

    conn = sqlite3.connect(db_path)
    try:
        require_schema(conn)
        groups = load_audit_groups(conn)
        with IdentityRorAffiliationIndex(index_path) as ror:
            plan, reasons = build_plan(groups, ror)
        write_plan(plan, reasons, csv_path, json_path)

        by_rule = Counter(item["proposed_rule"] for item in plan)
        mode = "APPLY" if args.apply else "DRY RUN"
        print(f"ROR-backed entity resolution ({ROR_RELEASE}): {mode}")
        print(f"Unresolved groups considered: {len(groups)}")
        print(f"Proposed resolutions: {len(plan)}")
        for rule, count in sorted(by_rule.items()):
            print(f"  {rule:62s} {count:4d}")

        print("\nDecision reasons")
        print("----------------")
        for reason, count in sorted(reasons.items(), key=lambda item: (-item[1], item[0])):
            print(f"  {reason:52s} {count:4d}")

        print("\nSample proposals")
        print("----------------")
        for item in plan[:20]:
            print(
                f"  - {item['reviewer_name']} -> {item['candidate_name']} "
                f"[{item['candidate_pid']}] via {item['shared_ror_names']}"
            )

        print(f"\nPlan CSV:  {csv_path}")
        print(f"Plan JSON: {json_path}")

        if not args.apply:
            print("\nNo database rows changed. Re-run with --apply to apply the guarded plan.")
            return 0

        backup_dir = data_dir / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        backup_path = (
            backup_dir
            / f"entity_resolution.before_ror_{compact_timestamp()}.sqlite"
        )
        conn.commit()
        shutil.copy2(db_path, backup_path)

        changed = apply_plan(conn, plan)
        print(f"\nApplied ROR-backed resolutions: {changed}")
        print(f"Backup: {backup_path}")
        if changed != len(plan):
            print(
                "WARNING: Applied row count differs from plan count. "
                "Some rows may already have been resolved."
            )
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
