"""Read-only production checks and temporary boundary fixtures for specialist 3."""
import csv
import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT / "scripts"), str(ROOT / "tests")]
from apply_profile_entity_resolution import build_plan, load_reviews, load_service_aliases
from automatic_affiliations import publication_dois
from build_site_data import apply_publication_affiliation_evidence, load_publication_affiliation_evidence
from ror_affiliations import IdentityRorAffiliationIndex
from test_profile_entity_resolution import ProfileEntityResolutionTests


def main():
    out = {}
    with sqlite3.connect((ROOT / "data/entity_resolution.sqlite").as_uri() + "?mode=ro", uri=True) as conn:
        with IdentityRorAffiliationIndex(ROOT / "data/ror_snapshot/ror_affiliation_index.sqlite") as ror:
            ror.conn.close()
            ror.conn = sqlite3.connect((ROOT / "data/ror_snapshot/ror_affiliation_index.sqlite").as_uri() + "?mode=ro", uri=True)
            ror.conn.row_factory = sqlite3.Row
            reviews = load_reviews(ROOT / "data/reviewed_person_identities.csv")
            aliases = load_service_aliases(ROOT / "data/reviewed_service_aliases.csv")
            plan, held = build_plan(conn, ror, reviews, aliases)
            out["production_reviews"] = {"reviews": len(reviews), "aliases": len(aliases), "plan": plan,
                                         "reasons": dict(Counter(p["reason"] for p in held))}
            # Counterfactual only: remove the review inputs, retain the applied database.
            plan, held = build_plan(conn, ror, {}, {})
            owned = list(conn.execute("SELECT name_key,status,resolved_dblp_pid FROM name_resolution WHERE status IN ('resolved_reviewed_profile','resolved_reviewed_service_alias')"))
            out["deleted_review_counterfactual"] = {"applied_rows": owned, "plan_for_rows": [p for p in plan if p["name_key"] in {r[0] for r in owned}]}
    reviewed = load_publication_affiliation_evidence(ROOT / "data/publication_affiliation_evidence.csv")
    partial, referenced_nonowned, doi_nonowned = [], [], []
    with (ROOT / "data/dblp_publication_entities_for_db_people.csv").open(encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            versions = json.loads(row.get("publication_versions_json") or "[]")
            owned = set(json.loads(row.get("person_version_dblp_keys") or "[]"))
            unowned = [v for v in versions if v.get("dblpKey") not in owned]
            if not unowned:
                continue
            partial.append({"pid": row["dblp_pid"], "key": row["dblp_key"], "owned": sorted(owned), "unowned": [v.get("dblpKey") for v in unowned]})
            for review in reviewed.get(row["dblp_pid"], []):
                if review["dblpKey"] in [v.get("dblpKey") for v in unowned]:
                    referenced_nonowned.append(review)
            paper = {"year": int(row["year"]), "url": row["url"], "resolvedVersions": versions}
            without = {**paper, "resolvedVersions": [v for v in versions if v.get("dblpKey") in owned]}
            extra = set(publication_dois(paper)) - set(publication_dois(without))
            if extra:
                doi_nonowned.append({**partial[-1], "extra_dois": sorted(extra)})
    out["production_ownership"] = {"partial_rows": partial, "review_rows_nonowned": referenced_nonowned, "dois_nonowned": doi_nonowned}
    automatic = json.loads((ROOT / "data/automatic_affiliation_evidence.json").read_text(encoding="utf-8"))
    out["production_ownership"]["automatic_records_for_unowned_doi_cases"] = {row["pid"]: automatic["people"].get(row["pid"]) for row in doi_nonowned}
    primary = {"affiliation": "Own University", "sourceIds": [], "notes": []}
    paper = {"dblpKey": "own-preprint", "year": 2026, "personVersionDblpKeys": ["own-preprint"], "resolvedVersions": [{"dblpKey": "own-preprint", "year": 2026}, {"dblpKey": "others-conference", "year": 2025}]}
    review = {"dblpKey": "others-conference", "year": 2025, "personName": "Alice Example", "affiliation": "Coauthor University", "sourceUrl": "https://example.org/paper.pdf", "verifiedOn": "2026-11-07"}
    out["nonowned_manual_affiliation_fixture"] = apply_publication_affiliation_evidence(primary, [review], [paper], {"sources": {}})
    test = ProfileEntityResolutionTests()
    test.setUp()
    try:
        from unittest import mock
        conn, group, alias_review = test.alias_database()
        from apply_profile_entity_resolution import apply_plan, service_alias_proposal
        apply_plan(conn, [service_alias_proposal(conn, group, alias_review)[0]])
        applied = dict(group, resolved_dblp_pid="full", status="resolved_reviewed_service_alias")
        with mock.patch("apply_profile_entity_resolution.load_audit_groups", return_value=[applied]):
            plan, held = build_plan(conn, test.ror, {}, {})
        out["deleted_alias_fixture"] = {"plan": plan, "held": held, "persisted": list(conn.execute("SELECT status,resolved_dblp_pid FROM name_resolution WHERE name_key='ken ross'"))}
    finally:
        test.doCleanups()
    rendered = json.dumps(out, indent=2, ensure_ascii=False)
    (Path(__file__).parent / "affiliation_review_evidence.json").write_text(rendered + "\n", encoding="utf-8")
    print("Wrote affiliation_review_evidence.json; reviewed assignments: 17; partial ownership rows: " + str(len(partial)))


if __name__ == "__main__":
    main()
