from __future__ import annotations

import copy
import gzip
import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from lxml import etree
from apply_affiliation_entity_resolution import proposal_for_group, apply_plan, backup_database
from apply_high_precision_entity_resolution import proposal_for_group as legacy_high
from apply_ror_entity_resolution import proposal_for_group as legacy_ror
from apply_next_tier_entity_resolution import proposal_for_group as legacy_next
from enrich_reviewers_with_dblp import create_schema, scan_snapshot, resolve_names, person_identity_from_www, person_record_from_www
from ror_affiliations import IdentityRorAffiliationIndex, ROR_RELEASE, ROR_ZIP_MD5, build_ror_index_from_records
from scan_dblp_publications import load_targets, interested_person_identifiers


def organization(pid, label, *aliases):
    return {"id": pid, "status": "active", "names": [
        {"value": label, "types": ["ror_display", "label"], "lang": "en"},
        *[{"value": value, "types": ["alias"], "lang": "en"} for value in aliases],
    ]}


class AffiliationEntityResolutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        index = self.root / "ror.sqlite"
        build_ror_index_from_records([
            organization("snow", "Snowflake Inc.", "Snowflake"),
            organization("other", "Other University"),
            organization("system", "University of California System", "University of California"),
            organization("irvine", "University of California, Irvine"),
            organization("berkeley", "University of California, Berkeley"),
            organization("hk", "Hong Kong University of Science and Technology"),
            organization("gz", "Hong Kong University of Science and Technology (Guangzhou)"),
            organization("cuhk", "Chinese University of Hong Kong"),
        ], index, release=ROR_RELEASE, snapshot_md5=ROR_ZIP_MD5)
        self.ror = IdentityRorAffiliationIndex(index)
        self.group = dict(name_key="abhishek roy", representative_name="Abhishek Roy", status="ambiguous_exact_name",
                          reviewer_affiliations=["Snowflake"], recommendation="manual_review",
                          nonperson_candidates=[dict(pid="base", canonical_name="Abhishek Roy", best_match_type="exact_alias",
                              candidate_affiliations=[], verified_orcids=[], ror_matches={}, shared_ror_ids=[],
                              affiliation_safe_equivalence_reason="", first_last_exact=False)], candidates=[
            dict(pid="correct", canonical_name="Abhishek Roy 0008", best_match_type="exact_alias",
                 candidate_affiliations=["Snowflake Inc."], verified_orcids=[]),
            dict(pid="unknown", canonical_name="Abhishek Roy 0009", best_match_type="exact_alias",
                 candidate_affiliations=[], verified_orcids=[]),
        ])
        self.events = [dict(venue="ICDE", year=2024, affiliation="Snowflake", source_url="https://example.org/icde"),
                       dict(venue="VLDB", year=2025, affiliation="Snowflake", source_url="https://example.org/vldb")]

    def tearDown(self):
        self.ror.close()
        self.tmp.cleanup()

    def test_repeated_exact_affiliation_match_with_unknown_competitor(self):
        proposal, reason = proposal_for_group(self.group, self.events, self.ror)
        self.assertEqual(reason, "proposed")
        self.assertEqual(proposal["candidate_pid"], "correct")
        self.assertEqual(proposal["proposed_status"], "resolved_affiliation_repeated_service")
        self.assertEqual(proposal["unknown_competitor_affiliations"], ["unknown"])

    def test_repeated_rows_on_one_event_or_one_page_are_insufficient(self):
        for field, value in [("source_url", "https://example.org/shared"), ("venue", "ICDE")]:
            events = copy.deepcopy(self.events)
            for event in events:
                event[field] = value
                if field == "venue":
                    event["year"] = 2024
            proposal, reason = proposal_for_group(self.group, events, self.ror)
            self.assertIsNone(proposal)
            self.assertEqual(reason, "insufficient_independent_service_corroboration")

    def test_every_recorded_source_affiliation_must_be_covered(self):
        for label in ["Other University", "Unrecognized Institute"]:
            group = copy.deepcopy(self.group)
            group["reviewer_affiliations"].append(label)
            self.assertEqual(proposal_for_group(group, self.events, self.ror)[1], "incomplete_service_affiliation_coverage")

    def test_same_organization_on_two_real_people_stays_ambiguous(self):
        group = copy.deepcopy(self.group)
        group["candidates"][1]["candidate_affiliations"] = ["Snowflake"]
        self.assertEqual(proposal_for_group(group, self.events, self.ror)[1], "multiple_affiliation_matches")

    def test_variant_positive_also_blocks_an_exact_match(self):
        group = copy.deepcopy(self.group)
        group["candidates"][1].update(best_match_type="first_last_variant", candidate_affiliations=["Snowflake"])
        self.assertIsNone(proposal_for_group(group, self.events, self.ror)[0])

    def test_cleanup_alone_does_not_resolve_name_only(self):
        group = copy.deepcopy(self.group)
        group["candidates"] = [group["candidates"][1]]
        self.assertEqual(proposal_for_group(group, self.events, self.ror)[1], "no_affiliation_match")

    def test_unique_real_exact_match_still_requires_affiliation(self):
        group = dict(self.group, candidates=self.group["candidates"][:1])
        proposal, reason = proposal_for_group(group, self.events[:1], self.ror)
        self.assertEqual(reason, "proposed")
        self.assertEqual(proposal["proposed_status"], "resolved_affiliation_exact")

    def test_legacy_stages_cannot_bypass_dedicated_review(self):
        self.assertIsNone(legacy_high(self.group))
        self.assertIsNone(legacy_ror(self.group)[0])
        self.assertIsNone(legacy_next(self.group, {})[0])

    def test_campus_names_survive_trailing_locations(self):
        self.assertEqual(set(self.ror.match_affiliation("University of California, Irvine, CA, USA")), {"irvine"})
        self.assertEqual(set(self.ror.match_affiliation("University of California, Berkeley, CA, USA")), {"berkeley"})
        self.assertEqual(self.ror.match_affiliation("University of California"), {})

    def test_unrecognized_branch_cannot_fall_back_to_parent(self):
        self.assertEqual(self.ror.match_affiliation("Hong Kong University of Science and Technology, Guang Zhou"), {})
        self.assertEqual(set(self.ror.match_affiliation("Hong Kong University of Science and Technology (Guangzhou)")), {"gz"})
        self.assertEqual(self.ror.match_affiliation("Chinese University of Hong Kong, Shenzhen"), {})

    def test_snapshot_scan_retains_collision_but_never_nonperson_as_candidate(self):
        xml = b'<dblp><www key="homepages/base" publtype="disambiguation"><author>Abhishek Roy</author></www><www key="homepages/real"><author>Abhishek Roy 0008</author><note type="affiliation">Snowflake Inc.</note></www></dblp>'
        snapshot = self.root / "snapshot.xml.gz"
        with gzip.open(snapshot, "wb") as handle:
            handle.write(xml)
        dtd = self.root / "dblp.dtd"
        dtd.write_text("", encoding="utf-8")
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        create_schema(conn)
        rows = [("abhishek roy", "Abhishek Roy")]
        scan_snapshot(conn, snapshot, dtd, rows)
        resolve_names(conn, rows)
        self.assertEqual(conn.execute("SELECT dblp_pid FROM dblp_people").fetchall(), [("real",)])
        self.assertEqual(conn.execute("SELECT dblp_pid FROM dblp_nonperson_candidates").fetchall(), [("base",)])
        self.assertEqual(conn.execute("SELECT resolved_dblp_pid,status FROM name_resolution").fetchone(), (None, "ambiguous_exact_name"))
        nonperson = etree.fromstring(b'<www key="homepages/base" publtype="disambiguation"><author>Abhishek Roy</author></www>')
        self.assertIsNone(person_identity_from_www(nonperson))
        self.assertIsNone(person_record_from_www(nonperson, identity={"pid": "base"}))
        self.assertEqual(interested_person_identifiers(nonperson, alias_interest={"Abhishek Roy"}, orcid_interest=set())[1], {"Abhishek Roy"})

    def test_apply_is_idempotent_preserves_existing_decisions_and_backup_includes_wal(self):
        path = self.root / "entity.sqlite"
        with closing(sqlite3.connect(path)) as conn:
            create_schema(conn)
            conn.execute("INSERT INTO name_resolution VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                         ("abhishek roy", "Abhishek Roy", "ambiguous_exact_name", None, None, None, None, 2, 0, 2, "", "now"))
            conn.execute("INSERT INTO dblp_candidates VALUES (?,?,?,?,?)", ("abhishek roy", "correct", "exact_alias", "Abhishek Roy", 0))
            conn.commit()
            backup = self.root / "backup.sqlite"
            backup_database(conn, backup)
            with closing(sqlite3.connect(backup)) as saved:
                self.assertEqual(saved.execute("SELECT COUNT(*) FROM name_resolution").fetchone()[0], 1)
            proposal, _ = proposal_for_group(self.group, self.events, self.ror)
            self.assertEqual(apply_plan(conn, [proposal]), 1)
            self.assertEqual(apply_plan(conn, [proposal]), 0)
            conflict = dict(proposal, candidate_pid="someone_else")
            with self.assertRaisesRegex(RuntimeError, "changed after planning"):
                apply_plan(conn, [conflict])
            self.assertEqual(conn.execute("SELECT resolved_dblp_pid FROM name_resolution").fetchone()[0], "correct")

    def test_publication_scan_rejects_nonperson_target(self):
        path = self.root / "entity.sqlite"
        with closing(sqlite3.connect(path)) as conn:
            create_schema(conn)
            conn.execute("INSERT INTO name_resolution VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                         ("name", "Name", "resolved_unique_exact", "bad", "Name", None, None, 1, 0, 1, "", "now"))
            conn.execute("INSERT INTO dblp_nonperson_candidates VALUES (?,?,?,?)", ("name", "bad", "exact_alias", "Name"))
            conn.commit()
        with self.assertRaisesRegex(RuntimeError, "disambiguation page"):
            load_targets(path)


if __name__ == "__main__":
    unittest.main()
