#!/usr/bin/env python3
"""Correct sparse exact-name assignments using reviewed or corroborated evidence.

Runs after the unresolved-name rules. Reviewed decisions are scoped to a name,
the previous PID and the observed affiliations; new affiliations or a missing
candidate require review again. The general rule requires a compatible fuller
name, complete exact-organization coverage and two independent service pages
and venue/year events. It never uses publication counts to choose a person.

Correcting a service identity does not merge the old PID's publications. Specific
verified paper links live separately in reviewed_publication_authors.csv.
Reviewed roster spellings in reviewed_service_aliases.csv can reuse an already
matched service identity without adding aliases to publication matching.
"""
from __future__ import annotations

import argparse
import csv
import json
import sqlite3
from collections import Counter, defaultdict
from contextlib import closing
from datetime import date
from pathlib import Path

from apply_affiliation_entity_resolution import backup_database
from apply_ror_entity_resolution import add_ror_evidence
from audit_unresolved_entity_resolution import compatible_full_name, load_audit_groups, normalize_name, require_schema
from enrich_reviewers_with_dblp import CANDIDATES_CSV, RESOLUTION_CSV, export_candidates, export_resolution, now_iso
from identity_review import sparse_exact_review_groups
from ror_affiliations import IdentityRorAffiliationIndex, ROR_RELEASE, ensure_ror_index

REVIEW_FILE = "reviewed_person_identities.csv"
SERVICE_ALIAS_FILE = "reviewed_service_aliases.csv"


def load_reviews(path):
    if not path.exists():
        return {}
    reviews = {}
    with path.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            key = normalize_name(row["service_name"])
            labels = json.loads(row["service_affiliations_json"])
            urls = json.loads(row["evidence_urls_json"])
            date.fromisoformat(row["reviewed_on"])
            if (not key or key in reviews or not row["target_pid"]
                    or not isinstance(labels, list) or not labels or not all(isinstance(x, str) and x.strip() for x in labels)
                    or not isinstance(urls, list) or not urls or not all(isinstance(x, str) and x.startswith("https://") for x in urls)
                    or not row["reason"].strip()):
                raise ValueError("Invalid or duplicate reviewed identity: " + key)
            reviews[key] = row | {"affiliations": set(labels), "evidence_urls": urls}
    return reviews


def load_service_aliases(path):
    """Read reviewed roster spellings, not aliases for publication matching."""
    if not path.exists():
        return {}
    reviews = {}
    with path.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            key = normalize_name(row["service_name"])
            anchor = normalize_name(row["target_service_name"])
            labels = json.loads(row["service_affiliations_json"])
            urls = json.loads(row["evidence_urls_json"])
            date.fromisoformat(row["reviewed_on"])
            if (not key or key == anchor or key in reviews or not anchor or not row["target_pid"]
                    or row.get("previous_pid") == row["target_pid"]
                    or not isinstance(labels, list) or not labels
                    or not all(isinstance(x, str) and x.strip() for x in labels)
                    or not isinstance(urls, list) or not urls
                    or not all(isinstance(x, str) and x.startswith("https://") for x in urls)
                    or not row["reason"].strip()):
                raise ValueError("Invalid or duplicate reviewed service alias: " + key)
            reviews[key] = row | {"anchor_name_key": anchor, "affiliations": set(labels), "evidence_urls": urls}
    return reviews


def service_alias_proposal(conn, group, review, *, anchor_pids=None):
    labels = {x.strip() for x in group.get("reviewer_affiliations", []) if x.strip()}
    if not labels or not labels <= review["affiliations"]:
        return None, "reviewed_alias_affiliations_changed"
    if anchor_pids is None:
        anchor = conn.execute("SELECT resolved_dblp_pid FROM name_resolution WHERE name_key=?",
                              (review["anchor_name_key"],)).fetchone()
        anchor_pid = anchor[0] if anchor else None
    else:
        anchor_pid = anchor_pids.get(review["anchor_name_key"])
    if anchor_pid != review["target_pid"]:
        return None, "reviewed_alias_anchor_changed"
    current = group.get("resolved_dblp_pid")
    reviewed_previous = review.get("previous_pid") or None
    allowed_previous = (reviewed_previous and current == reviewed_previous
                        and group["status"] == "resolved_unique_exact")
    if current not in (None, review["target_pid"]) and not allowed_previous:
        return None, "reviewed_alias_assignment_changed"
    row = conn.execute("SELECT canonical_name,verified_orcids_json FROM dblp_people WHERE dblp_pid=?",
                       (review["target_pid"],)).fetchone()
    if not row:
        return None, "reviewed_alias_person_missing"
    if current == review["target_pid"]:
        return None, "already_applied"
    target = dict(pid=review["target_pid"], canonical_name=row[0], verified_orcids=json.loads(row[1] or "[]"))
    return proposal(group, target, status="resolved_reviewed_service_alias", evidence=dict(
        rule="reviewed_service_alias", anchor_name_key=review["anchor_name_key"],
        service_affiliations=sorted(labels), evidence_urls=review["evidence_urls"],
        reviewed_on=review["reviewed_on"], reason=review["reason"])), "proposed_reviewed_alias"


def proposal(group, candidate, *, status, evidence):
    orcids = candidate.get("verified_orcids") or []
    return dict(name_key=group["name_key"], reviewer_name=group["representative_name"],
                status_before=group["status"], previous_pid=group.get("resolved_dblp_pid"),
                candidate_pid=candidate["pid"], candidate_name=candidate["canonical_name"],
                proposed_status=status, best_orcid=orcids[0] if len(orcids) == 1 else None,
                evidence=evidence)


def proposal_for_group(group, events, ror, review=None):
    candidates = group.get("candidates") or []
    current_pid = group.get("resolved_dblp_pid")
    labels = {x.strip() for x in group.get("reviewer_affiliations", []) if x.strip()}
    if review:
        target = next((c for c in candidates if c["pid"] == review["target_pid"]), None)
        if not target:
            return None, "reviewed_candidate_missing"
        if not labels or not labels <= review["affiliations"]:
            return None, "reviewed_affiliations_changed"
        if current_pid not in (None, review["previous_pid"] or None, review["target_pid"]):
            return None, "reviewed_assignment_changed"
        if current_pid == review["target_pid"]:
            return None, "already_applied"
        return proposal(group, target, status="resolved_reviewed_profile", evidence=dict(
            rule="reviewed_service_identity", service_affiliations=sorted(labels),
            previous_pid=review["previous_pid"], evidence_urls=review["evidence_urls"],
            reviewed_on=review["reviewed_on"], reason=review["reason"])), "proposed_reviewed"

    # Never replace a previously evidenced/manual decision with this heuristic.
    if group["status"] != "resolved_unique_exact" or not current_pid:
        return None, "not_sparse_exact_assignment"
    current = next((c for c in candidates if c["pid"] == current_pid), None)
    if not current or current["candidate_affiliations"] or current["verified_orcids"]:
        return None, "current_has_identity_evidence"
    enriched = add_ror_evidence(group, ror)
    positives = [c for c in enriched["candidates"] if c["shared_ror_ids"]]
    if len(positives) != 1:
        return None, "multiple_affiliation_matches" if positives else "no_affiliation_match"
    target = positives[0]
    if target["pid"] == current_pid or not any(compatible_full_name(group["representative_name"], name)
            for name in target["matched_aliases"] + [target["canonical_name"]]):
        return None, "incompatible_full_name"
    coverage = {label: sorted(set(target["ror_matches"]) & set(ror.match_affiliation(label))) for label in labels}
    if not coverage or not all(coverage.values()):
        return None, "incomplete_service_affiliation_coverage"
    matched = [e for e in events if set(target["ror_matches"]) & set(ror.match_affiliation(e.get("affiliation") or ""))]
    venue_years = sorted({(e["venue"], e["year"]) for e in matched if e.get("venue") and isinstance(e.get("year"), int)})
    urls = sorted({e["source_url"] for e in matched if e.get("source_url")})
    if len(venue_years) < 2 or len(urls) < 2:
        return None, "insufficient_independent_service_corroboration"
    return proposal(group, target, status="resolved_profile_affiliation", evidence=dict(
        rule="compatible_full_name_repeated_service", affiliation_coverage=coverage,
        matching_venue_years=venue_years, matching_source_urls=urls, ror_release=ROR_RELEASE,
        unknown_competitor_affiliations=[c["pid"] for c in enriched["candidates"]
                                         if c["pid"] != target["pid"] and not c["ror_matches"]])), "proposed_automatic"


def build_plan(conn, ror, reviews, service_aliases=None):
    events = defaultdict(list)
    for key, venue, year, affiliation, url in conn.execute(
            "SELECT name_key, venue, year, affiliation, source_url FROM reviewer_observations"):
        events[key].append(dict(venue=venue, year=year, affiliation=affiliation, source_url=url))
    groups = load_audit_groups(conn, include_resolved=True)
    aliases = service_aliases or {}
    outcomes = {}
    projected = dict(conn.execute("SELECT name_key,resolved_dblp_pid FROM name_resolution"))

    def defer_stale(group, review, reason, *, alias=False):
        # Revalidate our own applied decisions, while preserving assignments
        # made by unrelated automatic/manual rules.
        applied_status = "resolved_reviewed_service_alias" if alias else "resolved_reviewed_profile"
        owned = (group["status"] == applied_status
                 and group.get("resolved_dblp_pid") == review["target_pid"])
        original_bad_exact = (not alias and group["status"] == "resolved_unique_exact"
                              and group.get("resolved_dblp_pid") == review["previous_pid"])
        if alias:
            original_bad_exact = (bool(review.get("previous_pid")) and group["status"] == "resolved_unique_exact"
                                  and group.get("resolved_dblp_pid") == review["previous_pid"])
        if not (owned or original_bad_exact):
            return None
        return dict(name_key=group["name_key"], reviewer_name=group["representative_name"],
                    status_before=group["status"], previous_pid=group.get("resolved_dblp_pid"),
                    candidate_pid=None, candidate_name=None, best_orcid=None,
                    proposed_status="ambiguous_reviewed_profile", evidence=dict(
                        rule="stale_review_requires_verification", reason=reason,
                        reviewed_target=review["target_pid"], stale_service_alias=alias,
                        recorded_service_affiliations=group["reviewer_affiliations"]))

    def record(group, item, reason):
        outcomes[group["name_key"]] = (item, reason)
        if item:
            projected[group["name_key"]] = item["candidate_pid"]

    for group in groups:
        key = group["name_key"]
        if key in reviews or key in aliases:
            continue
        if group["status"] in {"resolved_reviewed_profile", "resolved_reviewed_service_alias"}:
            # Removing a reviewed decision also removes its authority. Do not
            # restore a previously rejected identity or retain an orphaned alias.
            reason = "reviewed_decision_removed"
            review = {"target_pid": group.get("resolved_dblp_pid")}
            is_alias = group["status"] == "resolved_reviewed_service_alias"
            record(group, defer_stale(group, review, reason, alias=is_alias), reason)

    for group in groups:
        review = reviews.get(group["name_key"])
        if group["name_key"] in aliases or group["name_key"] in outcomes:
            continue
        item, reason = proposal_for_group(group, events[group["name_key"]], ror, review)
        if not item and review and reason in {"reviewed_candidate_missing", "reviewed_affiliations_changed"}:
            item = defer_stale(group, review, reason)
        record(group, item, reason)

    by_key = {g["name_key"]: g for g in groups}
    visiting = set()

    def plan_alias(key):
        if key in outcomes or key not in by_key:
            return
        group, review = by_key[key], aliases[key]
        if key in visiting:
            # A cycle cannot independently corroborate either roster spelling.
            reason = "reviewed_alias_anchor_cycle"
            record(group, defer_stale(group, review, reason, alias=True), reason)
            projected[key] = None
            return
        visiting.add(key)
        anchor = review["anchor_name_key"]
        if anchor in aliases:
            plan_alias(anchor)
        visiting.remove(key)
        if key in outcomes:
            return
        item, reason = service_alias_proposal(conn, group, review, anchor_pids=projected)
        if not item and reason in {"reviewed_alias_affiliations_changed", "reviewed_alias_anchor_changed",
                                   "reviewed_alias_person_missing"}:
            item = defer_stale(group, review, reason, alias=True)
        record(group, item, reason)

    for group in groups:
        if group["name_key"] in aliases:
            plan_alias(group["name_key"])
    proposals, held = [], []
    for group in groups:
        item, reason = outcomes[group["name_key"]]
        if item:
            proposals.append(item)
        else:
            held.append(dict(name_key=group["name_key"], name=group["representative_name"], reason=reason))
    return proposals, held


def apply_plan(conn, proposals):
    changed = 0
    with conn:
        projected = dict(conn.execute("SELECT name_key,resolved_dblp_pid FROM name_resolution"))
        for item in proposals:
            projected[item["name_key"]] = item["candidate_pid"]
        # Validate the final plan, so an alias cannot depend on an anchor that
        # this same atomic plan clears later in the iteration.
        for item in proposals:
            if item["evidence"].get("rule") == "reviewed_service_alias":
                anchor_pid = projected.get(item["evidence"]["anchor_name_key"])
                person = conn.execute("SELECT 1 FROM dblp_people WHERE dblp_pid=?", (item["candidate_pid"],)).fetchone()
                if anchor_pid != item["candidate_pid"] or not person:
                    raise RuntimeError("Reviewed service alias anchor changed after planning")
        for item in proposals:
            current = conn.execute("SELECT resolved_dblp_pid,status FROM name_resolution WHERE name_key=?",
                                   (item["name_key"],)).fetchone()
            if current and tuple(current) == (item["candidate_pid"], item["proposed_status"]):
                continue
            if not current or tuple(current) != (item["previous_pid"], item["status_before"]):
                raise RuntimeError("Identity changed after planning: " + item["name_key"])
            deferred = item["candidate_pid"] is None and item["proposed_status"] == "ambiguous_reviewed_profile"
            if deferred and item["evidence"].get("stale_service_alias"):
                conn.execute("DELETE FROM dblp_candidates WHERE name_key=? AND match_type='reviewed_service_alias'",
                             (item["name_key"],))
            if item["evidence"].get("rule") == "reviewed_service_alias":
                conn.execute("INSERT OR IGNORE INTO dblp_candidates VALUES (?,?,?,?,?)",
                             (item["name_key"], item["candidate_pid"], "reviewed_service_alias", item["candidate_name"], 0))
            if item["evidence"].get("rule") == "reviewed_service_alias" or (deferred and item["evidence"].get("stale_service_alias")):
                exact, total = conn.execute("SELECT COUNT(DISTINCT CASE WHEN match_type='exact_alias' THEN dblp_pid END),"
                                            "COUNT(DISTINCT dblp_pid) FROM dblp_candidates WHERE name_key=?",
                                            (item["name_key"],)).fetchone()
                conn.execute("UPDATE name_resolution SET exact_candidate_count=?,variant_candidate_count=?,all_candidate_count=? WHERE name_key=?",
                             (exact, total - exact, total, item["name_key"]))
            if not deferred and not conn.execute("SELECT 1 FROM dblp_candidates c JOIN dblp_people p USING(dblp_pid) WHERE c.name_key=? AND c.dblp_pid=?",
                                                (item["name_key"], item["candidate_pid"])).fetchone():
                raise RuntimeError("Reviewed profile is no longer a real candidate")
            conn.execute("UPDATE name_resolution SET status=?, resolved_dblp_pid=?, resolved_dblp_name=?, best_orcid=?, "
                         "best_orcid_source=?, notes=?, updated_at=? WHERE name_key=?",
                         (item["proposed_status"], item["candidate_pid"], item["candidate_name"], item["best_orcid"],
                          "dblp_person_record" if item["best_orcid"] else None,
                          "Service identity profile correction: " + json.dumps(item, ensure_ascii=False, sort_keys=True),
                          now_iso(), item["name_key"]))
            changed += 1
    return changed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=Path(__file__).resolve().parents[1] / "data")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    data = args.data_dir.resolve()
    reviews = load_reviews(data / REVIEW_FILE)
    aliases = load_service_aliases(data / SERVICE_ALIAS_FILE)
    if reviews.keys() & aliases.keys():
        raise ValueError("A service name cannot have both a profile correction and a service-alias review")
    _, index = ensure_ror_index(data / "ror_snapshot", download=False)
    with closing(sqlite3.connect(data / "entity_resolution.sqlite")) as conn, IdentityRorAffiliationIndex(index) as ror:
        require_schema(conn)
        proposals, held = build_plan(conn, ror, reviews, aliases)
        report = data / "reports" / "profile_entity_resolution_plan.json"
        report.parent.mkdir(parents=True, exist_ok=True)
        review_groups = sparse_exact_review_groups(conn)
        payload = dict(generatedAt=now_iso(), applied=False,
            proposalCount=len(proposals), reasonCounts=dict(Counter(x["reason"] for x in held)),
            proposals=proposals, held=held,
            sparseExactReview=dict(total=len(review_groups), shown=min(50, len(review_groups)),
                                   notConfirmedErrors=True, records=review_groups[:50]))
        report.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"Service identity profile corrections: {len(proposals)} proposals; plan: {report}")
        if args.apply:
            changed = 0
            if proposals:
                backup_database(conn, data / "backups" / f"entity_resolution.before_profiles_{now_iso().replace(':', '').replace('+', '_')}.sqlite")
                changed = apply_plan(conn, proposals)
                print(f"Applied: {changed}")
            export_resolution(conn, data / RESOLUTION_CSV)
            export_candidates(conn, data / CANDIDATES_CSV)
            review_groups = sparse_exact_review_groups(conn)
            payload["sparseExactReview"] = dict(total=len(review_groups), shown=min(50, len(review_groups)),
                                               notConfirmedErrors=True, records=review_groups[:50])
            payload.update(applied=True, appliedCount=changed)
            report.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
