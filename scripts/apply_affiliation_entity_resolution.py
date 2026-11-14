#!/usr/bin/env python3
"""Resolve exact-name collisions using complete affiliation evidence.

Only real DBLP people are candidates. Exactly one candidate must match every
non-empty service affiliation, through exact ROR organization names/aliases.
The name must match exactly, and no other candidate (including name variants)
may share a service organization. In addition, require either the earlier
complete-competitor rules or corroboration on two venue/year events and two
source pages. Missing competitor affiliations remain unknown, not negatives.

Default: write a review plan only. --apply creates an SQLite backup and applies
the freshly calculated plan. No manual PID list or publication counts are used.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter, defaultdict
from contextlib import closing
from pathlib import Path

from audit_unresolved_entity_resolution import load_audit_groups, require_schema
from apply_high_precision_entity_resolution import proposal_for_group as high_proposal
from apply_ror_entity_resolution import add_ror_evidence, proposal_for_group as ror_proposal
from enrich_reviewers_with_dblp import now_iso, export_resolution, RESOLUTION_CSV
from ror_affiliations import IdentityRorAffiliationIndex, ROR_RELEASE, ensure_ror_index


def proposal_for_group(group, events, ror):
    group = add_ror_evidence(group, ror)
    candidates = group.get("candidates") or []
    positives = [c for c in candidates if c["shared_ror_ids"]]
    if len(positives) != 1:
        return None, "multiple_affiliation_matches" if positives else "no_affiliation_match"
    candidate = positives[0]
    if candidate["best_match_type"] != "exact_alias":
        return None, "affiliation_match_requires_exact_name"

    labels = sorted({label for label in group.get("reviewer_affiliations", []) if label.strip()})
    organizations = set(candidate["ror_matches"])
    coverage = {label: sorted(organizations & set(ror.match_affiliation(label))) for label in labels}
    if not coverage or not all(coverage.values()):
        return None, "incomplete_service_affiliation_coverage"

    matched_events = [e for e in events if organizations & set(ror.match_affiliation(e.get("affiliation") or ""))]
    venue_years = sorted({(e["venue"], e["year"]) for e in matched_events
                         if e.get("venue") and isinstance(e.get("year"), int)})
    urls = sorted({e["source_url"] for e in matched_events if (e.get("source_url") or "").strip()})
    repeated = len(venue_years) >= 2 and len(urls) >= 2

    # Re-evaluate the established narrow rules on real candidates. These
    # functions retain non-person ambiguity blockers in the legacy stages;
    # this stage replaces those blockers with full positive coverage.
    baseline_group = dict(group, nonperson_candidates=[])
    exact = [c for c in candidates if c["best_match_type"] == "exact_alias"]
    old_ror, _ = ror_proposal(baseline_group)
    old_high = high_proposal(baseline_group)
    baseline_pids = {p["candidate_pid"] for p in (old_ror, old_high) if p}
    if len(exact) == 1:
        baseline_pids.add(exact[0]["pid"])
    established = candidate["pid"] in baseline_pids
    if not established and not repeated:
        return None, "insufficient_independent_service_corroboration"

    orcids = candidate.get("verified_orcids") or []
    return {
        "name_key": group["name_key"],
        "reviewer_name": group["representative_name"],
        "status_before": group["status"],
        "candidate_pid": candidate["pid"],
        "candidate_name": candidate["canonical_name"],
        "proposed_status": "resolved_affiliation_exact" if established else "resolved_affiliation_repeated_service",
        "rule": "exact_name_full_affiliation_existing_rule" if established else "exact_name_full_affiliation_repeated_service",
        "best_orcid": orcids[0] if len(orcids) == 1 else None,
        "affiliation_coverage": coverage,
        "matching_venue_years": venue_years,
        "matching_source_urls": urls,
        "unknown_competitor_affiliations": [c["pid"] for c in candidates
                                            if c["pid"] != candidate["pid"] and not c["ror_matches"]],
        "ror_release": ROR_RELEASE,
    }, "proposed"


def build_plan(conn, ror):
    events = defaultdict(list)
    for key, venue, year, affiliation, url in conn.execute(
        "SELECT name_key, venue, year, affiliation, source_url FROM reviewer_observations"
    ):
        events[key].append(dict(venue=venue, year=year, affiliation=affiliation, source_url=url))
    proposals, held = [], []
    for group in load_audit_groups(conn):
        proposal, reason = proposal_for_group(group, events[group["name_key"]], ror)
        if proposal:
            proposals.append(proposal)
        else:
            held.append(dict(name_key=group["name_key"], name=group["representative_name"], reason=reason))
    return proposals, held


def apply_plan(conn, proposals):
    changed = 0
    with conn:
        for proposal in proposals:
            current = conn.execute("SELECT resolved_dblp_pid, status FROM name_resolution WHERE name_key=?",
                                   (proposal["name_key"],)).fetchone()
            if current and current[0] == proposal["candidate_pid"]:
                continue
            if not current or current[0] is not None or current[1] != proposal["status_before"]:
                raise RuntimeError("Identity changed after planning: " + proposal["name_key"])
            if not conn.execute("SELECT 1 FROM dblp_candidates WHERE name_key=? AND dblp_pid=? AND match_type='exact_alias'",
                                (proposal["name_key"], proposal["candidate_pid"])).fetchone():
                raise RuntimeError("Exact-name candidate is no longer present")
            note = "Complete exact-name affiliation resolution: " + json.dumps(proposal, ensure_ascii=False, sort_keys=True)
            conn.execute(
                "UPDATE name_resolution SET status=?, resolved_dblp_pid=?, resolved_dblp_name=?, "
                "best_orcid=?, best_orcid_source=?, notes=?, updated_at=? WHERE name_key=?",
                (proposal["proposed_status"], proposal["candidate_pid"], proposal["candidate_name"],
                 proposal["best_orcid"], "dblp_person_record" if proposal["best_orcid"] else None,
                 note, now_iso(), proposal["name_key"]),
            )
            changed += 1
    return changed


def backup_database(conn, path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as target:
        conn.backup(target)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=Path(__file__).resolve().parents[1] / "data")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    data = args.data_dir.resolve()
    _, index = ensure_ror_index(data / "ror_snapshot", download=False)
    db_path = data / "entity_resolution.sqlite"
    if not db_path.exists():
        raise SystemExit("Run enrich_reviewers_with_dblp.py first")
    with closing(sqlite3.connect(db_path)) as conn, IdentityRorAffiliationIndex(index) as ror:
        require_schema(conn)
        policy = conn.execute("SELECT value FROM local_index_meta WHERE key='identity_policy'").fetchone()
        if not policy or policy[0] != "2":
            raise SystemExit("Rebuild the DBLP candidate index with the current identity policy first")
        proposals, held = build_plan(conn, ror)
        payload = dict(generatedAt=now_iso(), applied=args.apply, proposalCount=len(proposals),
                       reasonCounts=dict(Counter(item["reason"] for item in held)), proposals=proposals, held=held)
        report = data / "reports" / "affiliation_entity_resolution_plan.json"
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"Complete affiliation resolution: {len(proposals)} proposals; {len(held)} held")
        print(f"Plan: {report}")
        if args.apply and proposals:
            stamp = now_iso().replace(":", "").replace("+", "_")
            backup_database(conn, data / "backups" / f"entity_resolution.before_affiliation_{stamp}.sqlite")
            print(f"Applied: {apply_plan(conn, proposals)}")
        if args.apply:
            export_resolution(conn, data / RESOLUTION_CSV)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
