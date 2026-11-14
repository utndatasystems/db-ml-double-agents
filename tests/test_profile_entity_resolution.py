from __future__ import annotations

import copy
import csv
import gzip
import json
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from lxml import etree

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from apply_profile_entity_resolution import (compatible_full_name, proposal_for_group, apply_plan, build_plan,
                                             load_reviews, load_service_aliases, service_alias_proposal)
from identity_review import sparse_exact_review_groups
from enrich_reviewers_with_dblp import create_schema
from ror_affiliations import IdentityRorAffiliationIndex, build_ror_index_from_records, ROR_RELEASE, ROR_ZIP_MD5
from scan_dblp_publications import TargetPerson, scan, target_fingerprint, load_reviewed_authors, reviewed_authors_in_publication


class ProfileEntityResolutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        index = self.root / "ror.sqlite"
        records = [{"id": key, "status": "active", "names": [{"value": name, "types": ["ror_display", "label"], "lang": "en"}]} for key, name in [
            ("columbia", "Columbia University"), ("other", "Other University"),
            ("bit", "Beijing Institute of Technology"), ("mining", "China University of Mining and Technology, Beijing")]]
        build_ror_index_from_records(records, index, release=ROR_RELEASE, snapshot_md5=ROR_ZIP_MD5)
        self.ror = IdentityRorAffiliationIndex(index)
        self.addCleanup(self.ror.close)
        def candidate(pid, name, affiliations, kind="first_last_variant"):
            return dict(pid=pid, canonical_name=name, matched_aliases=[name], best_match_type=kind,
                        candidate_affiliations=affiliations, verified_orcids=[])
        self.group = dict(name_key="kenneth ross", representative_name="Kenneth Ross", status="resolved_unique_exact",
                          resolved_dblp_pid="short", reviewer_affiliations=["Columbia University"], candidates=[
            candidate("short", "Kenneth Ross", [], "exact_alias"),
            candidate("full", "Kenneth A. Ross", ["Columbia University"])])
        self.events = [dict(venue="SIGMOD", year=2024, affiliation="Columbia University", source_url="https://example.test/2024"),
                       dict(venue="VLDB", year=2025, affiliation="Columbia University", source_url="https://example.test/2025")]
        self.review = dict(previous_pid="short", target_pid="full", affiliations={"Columbia University"},
                           evidence_urls=["https://example.test/official"], reviewed_on="2026-11-02", reason="Official page confirms name and institution.")

    def test_general_rule_corrects_sparse_exact_profile(self):
        p, reason = proposal_for_group(self.group, self.events, self.ror)
        self.assertEqual(reason, "proposed_automatic")
        self.assertEqual(p["candidate_pid"], "full")
        self.assertEqual(p["previous_pid"], "short")
        self.assertEqual(p["evidence"]["unknown_competitor_affiliations"], ["short"])

    def test_different_full_given_name_cannot_win_by_institution(self):
        group = copy.deepcopy(self.group)
        group["candidates"][1].update(canonical_name="Karl Ross", matched_aliases=["Karl Ross"])
        self.assertEqual(proposal_for_group(group, self.events, self.ror)[1], "incompatible_full_name")
        for left, right in [("Seung-won Hwang", "Seung-Eon Hwang"), ("A. Smith", "Anthony Smith"), ("Kenneth B. Ross", "Kenneth A. Ross")]:
            self.assertFalse(compatible_full_name(left, right))
        self.assertTrue(compatible_full_name("Anthony Tung", "Anthony K. H. Tung"))
        self.assertTrue(compatible_full_name("Bjorn T. Jonsson", "Bjorn Thor Jonsson"))

    def test_similar_institution_words_are_not_organization_evidence(self):
        group = copy.deepcopy(self.group)
        group["reviewer_affiliations"] = ["Beijing Institute of Technology"]
        group["candidates"][1]["candidate_affiliations"] = ["China University of Mining and Technology, Beijing"]
        self.assertEqual(proposal_for_group(group, self.events, self.ror)[1], "no_affiliation_match")

    def test_same_institution_collision_requires_reviewed_evidence(self):
        group = copy.deepcopy(self.group)
        group["candidates"].append(dict(group["candidates"][1], pid="second"))
        self.assertEqual(proposal_for_group(group, self.events, self.ror)[1], "multiple_affiliation_matches")
        self.assertEqual(proposal_for_group(group, self.events, self.ror, self.review)[0]["candidate_pid"], "full")

    def test_single_source_or_repeated_rows_do_not_prove_identity(self):
        for events in [self.events[:1] * 2, [dict(e, source_url="https://example.test/shared") for e in self.events]]:
            self.assertEqual(proposal_for_group(self.group, events, self.ror)[1], "insufficient_independent_service_corroboration")

    def test_complete_service_affiliation_coverage_is_required(self):
        group = dict(self.group, reviewer_affiliations=["Columbia University", "Other University"])
        self.assertEqual(proposal_for_group(group, self.events, self.ror)[1], "incomplete_service_affiliation_coverage")

    def test_general_rule_preserves_evidenced_existing_profile(self):
        for field, value in [("candidate_affiliations", ["Other University"]), ("verified_orcids", ["0000-0000-0000-0001"])]:
            group = copy.deepcopy(self.group)
            group["candidates"][0][field] = value
            self.assertEqual(proposal_for_group(group, self.events, self.ror)[1], "current_has_identity_evidence")
        group = dict(self.group, status="resolved_affiliation_repeated_service")
        self.assertIsNone(proposal_for_group(group, self.events, self.ror)[0])

    def test_reviewed_single_service_can_use_primary_evidence(self):
        p, reason = proposal_for_group(self.group, self.events[:1], self.ror, self.review)
        self.assertEqual(reason, "proposed_reviewed")
        self.assertEqual(p["proposed_status"], "resolved_reviewed_profile")
        self.assertEqual(p["evidence"]["evidence_urls"], ["https://example.test/official"])

    def test_reviews_do_not_overwrite_new_affiliations_or_unexpected_assignments(self):
        cases = [(dict(self.group, reviewer_affiliations=["Other University"]), "reviewed_affiliations_changed"),
                 (dict(self.group, resolved_dblp_pid="unexpected"), "reviewed_assignment_changed"),
                 (dict(self.group, candidates=self.group["candidates"][:1]), "reviewed_candidate_missing"),
                 (dict(self.group, resolved_dblp_pid="full"), "already_applied")]
        for group, expected in cases:
            self.assertEqual(proposal_for_group(group, self.events, self.ror, self.review)[1], expected)

    def test_apply_is_atomic_idempotent_and_does_not_mutate_candidate_aliases(self):
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        create_schema(conn)
        conn.execute("INSERT INTO name_resolution VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                     ("kenneth ross", "Kenneth Ross", "resolved_unique_exact", "short", "Kenneth Ross", None, None, 1, 1, 2, "", "now"))
        conn.execute("INSERT INTO dblp_people VALUES (?,?,?,?,?,?,?,?,?)", ("full", "Kenneth A. Ross", '["Kenneth A. Ross"]', '[]', '[]', '[]', '[]', 'homepages/full', 'snapshot'))
        conn.execute("INSERT INTO dblp_candidates VALUES (?,?,?,?,?)", ("kenneth ross", "full", "first_last_variant", "Kenneth A. Ross", 1))
        conn.commit()
        p, _ = proposal_for_group(self.group, self.events, self.ror, self.review)
        self.assertEqual(apply_plan(conn, [p]), 1)
        self.assertEqual(apply_plan(conn, [p]), 0)
        self.assertEqual(conn.execute("SELECT aliases_json FROM dblp_people").fetchone()[0], '["Kenneth A. Ross"]')
        with self.assertRaisesRegex(RuntimeError, "changed after planning"):
            apply_plan(conn, [dict(p, candidate_pid="unrelated")])

    def test_changed_review_scope_cannot_restore_the_known_bad_exact_profile(self):
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        create_schema(conn)
        conn.execute("INSERT INTO name_resolution VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                     ("kenneth ross", "Kenneth Ross", "resolved_unique_exact", "short", "Kenneth Ross", None, None, 1, 1, 2, "", "now"))
        conn.commit()
        group = dict(self.group, reviewer_affiliations=["Other University"])
        with mock.patch("apply_profile_entity_resolution.load_audit_groups", return_value=[group]):
            proposals, _ = build_plan(conn, self.ror, {"kenneth ross": self.review})
        self.assertEqual(len(proposals), 1)
        self.assertEqual(apply_plan(conn, proposals), 1)
        self.assertEqual(conn.execute("SELECT resolved_dblp_pid,status FROM name_resolution").fetchone(),
                         (None, "ambiguous_reviewed_profile"))

    def test_record_level_exception_never_turns_into_a_global_alias(self):
        path = self.root / "snapshot.xml.gz"
        # The short signature is globally ambiguous; only the explicitly reviewed
        # paper may be attributed to the full researcher.
        with gzip.open(path, "wb") as f:
            f.write(b'<dblp><inproceedings key="conf/test/verified"><author>Farhana Choudhury</author><title>Verified paper</title><year>2024</year></inproceedings><inproceedings key="conf/test/unverified"><author>Farhana Choudhury</author><title>Unverified paper</title><year>2024</year></inproceedings><www key="homepages/short"><author>Farhana Choudhury</author></www><www key="homepages/other"><author>Farhana Choudhury</author></www><www key="homepages/full"><author>Farhana Murtaza Choudhury</author></www></dblp>')
        dtd = self.root / "dblp.dtd"
        dtd.write_text("", encoding="utf-8")
        original = TargetPerson("full", "Farhana Murtaza Choudhury", ("Farhana Murtaza Choudhury",), ())
        reviewed = TargetPerson("full", original.canonical_name, original.aliases, (), (("conf/test/verified", "short", "Farhana Choudhury"),))
        self.assertNotEqual(target_fingerprint([original]), target_fingerprint([reviewed]))
        rows, _, _ = scan(path, dtd, [reviewed])
        self.assertEqual([(r["dblp_key"], r["dblp_pid"], r["match_basis"]) for r in rows], [("conf/test/verified", "full", "reviewed_publication_author")])

    def test_reviewed_paper_rejects_conflicting_pid_and_duplicate_author_names(self):
        for xml in [b'<inproceedings><author pid="other">Farhana Choudhury</author></inproceedings>',
                    b'<inproceedings><author>Farhana Choudhury</author><author>Farhana Choudhury</author></inproceedings>']:
            self.assertEqual(reviewed_authors_in_publication(etree.fromstring(xml), [("full", "short", "Farhana Choudhury")]), [])
        author = etree.fromstring(b'<inproceedings><author orcid="0000-0001-2345-6789">Farhana Choudhury</author></inproceedings>')
        target = TargetPerson("full", "Farhana Murtaza Choudhury", (), ("0000-0001-1111-1111",))
        self.assertEqual(reviewed_authors_in_publication(author, [("full", "short", "Farhana Choudhury")], {"full": target}), [])

    def test_review_queue_rejects_different_names_and_generic_institution_overlap(self):
        from coverage_candidates import CandidateReview
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        self.addCleanup(conn.close)
        create_schema(conn)
        conn.execute("INSERT INTO dblp_people VALUES (?,?,?,?,?,?,?,?,?)",
                     ("other", "Zhiwei Zhang 0023", '["Zhiwei Zhang 0023"]', '["China University of Mining and Technology, Beijing"]', '[]', '[]', '[]', 'homepages/other', 'snapshot'))
        review = CandidateReview(conn, self.ror)
        self.assertFalse(review.credible_alternative(dict(representative_name="Zhiwei Zhang", affiliations_json='["Beijing Institute of Technology"]'),
                                                    dict(dblp_pid="other", canonical_name="Zhiwei Zhang 0023")))
        conn.execute("UPDATE dblp_people SET canonical_name='Seung-Eon Hwang',aliases_json='[]',affiliations_json='[\"Columbia University\"]'")
        self.assertFalse(review.credible_alternative(dict(representative_name="Seung-won Hwang", affiliations_json='["Columbia University"]'),
                                                    dict(dblp_pid="other", canonical_name="Seung-Eon Hwang")))

    def test_reviewed_publication_cannot_be_assigned_to_two_people(self):
        path = self.root / "authors.csv"
        with path.open("w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["dblp_key", "source_pid", "target_pid", "author_signature", "source_url", "verified_on", "reason"])
            w.writeheader()
            for pid in ["one", "two"]:
                w.writerow(dict(dblp_key="paper", source_pid="short", target_pid=pid, author_signature="Short Name", source_url="https://example.test", verified_on="2026-11-02", reason="Verified"))
        with self.assertRaisesRegex(ValueError, "conflicting"):
            load_reviewed_authors(path, {"one", "two"})

    def test_review_file_rejects_duplicate_names(self):
        path = self.root / "people.csv"
        with path.open("w", encoding="utf-8", newline="") as f:
            fields = ["service_name", "previous_pid", "target_pid", "service_affiliations_json", "evidence_urls_json", "reviewed_on", "reason"]
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            row = dict(zip(fields, ["Kenneth Ross", "short", "full", '["Columbia University"]', '["https://example.test"]', "2026-11-02", "Verified"]))
            w.writerow(row); w.writerow(row)
        with self.assertRaisesRegex(ValueError, "duplicate"):
            load_reviews(path)

    def alias_database(self, database_name="identities.sqlite"):
        conn = sqlite3.connect(self.root / database_name)
        self.addCleanup(conn.close)
        create_schema(conn)
        for key, name, pid, status in [("ken ross", "Ken Ross", None, "unresolved_no_local_dblp_candidate"),
                                       ("kenneth ross", "Kenneth Ross", "full", "resolved_unique_exact")]:
            conn.execute("INSERT INTO name_groups VALUES (?,?,?,?,?,?,?,?,?)",
                         (key, name, 1, 2024, 2024, '["SIGMOD"]', '["pc_member"]', '["Columbia University"]', "now"))
            conn.execute("INSERT INTO name_resolution VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                         (key, name, status, pid, "Kenneth A. Ross" if pid else None, None, None,
                          1 if pid else 0, 0, 1 if pid else 0, "", "now"))
        conn.execute("INSERT INTO dblp_people VALUES (?,?,?,?,?,?,?,?,?)",
                     ("full", "Kenneth A. Ross", '["Kenneth A. Ross"]', '["Columbia University"]',
                      '[]', '[]', '[]', 'homepages/full', 'snapshot'))
        conn.commit()
        group = dict(name_key="ken ross", representative_name="Ken Ross", resolved_dblp_pid=None,
                     status="unresolved_no_local_dblp_candidate", reviewer_affiliations=["Columbia University"], candidates=[])
        review = dict(self.review, anchor_name_key="kenneth ross", target_pid="full")
        return conn, group, review

    def test_reviewed_service_alias_adds_local_candidate_without_publication_alias(self):
        from scan_dblp_publications import load_targets
        conn, group, review = self.alias_database()
        before = target_fingerprint(load_targets(self.root / "identities.sqlite"))
        p, reason = service_alias_proposal(conn, group, review)
        self.assertEqual(reason, "proposed_reviewed_alias")
        self.assertEqual(apply_plan(conn, [p]), 1)
        self.assertEqual(apply_plan(conn, [p]), 0)
        row = conn.execute("SELECT status,resolved_dblp_pid,exact_candidate_count,variant_candidate_count,all_candidate_count "
                           "FROM name_resolution WHERE name_key='ken ross'").fetchone()
        self.assertEqual(row, ("resolved_reviewed_service_alias", "full", 0, 1, 1))
        self.assertEqual(conn.execute("SELECT match_type FROM dblp_candidates WHERE name_key='ken ross'").fetchone()[0],
                         "reviewed_service_alias")
        self.assertEqual(conn.execute("SELECT aliases_json FROM dblp_people").fetchone()[0], '["Kenneth A. Ross"]')
        self.assertEqual(before, target_fingerprint(load_targets(self.root / "identities.sqlite")))

    def test_reviewed_service_alias_requires_stable_anchor_and_affiliation_scope(self):
        conn, group, review = self.alias_database()
        self.assertEqual(service_alias_proposal(conn, dict(group, reviewer_affiliations=["Other University"]), review)[1],
                         "reviewed_alias_affiliations_changed")
        self.assertEqual(service_alias_proposal(conn, dict(group, resolved_dblp_pid="other"), review)[1],
                         "reviewed_alias_assignment_changed")
        self.assertEqual(service_alias_proposal(conn, dict(group, resolved_dblp_pid="full"), review)[1], "already_applied")
        conn.execute("UPDATE name_resolution SET resolved_dblp_pid=NULL WHERE name_key='kenneth ross'")
        self.assertEqual(service_alias_proposal(conn, group, review)[1], "reviewed_alias_anchor_changed")

    def test_reviewed_service_alias_rechecks_anchor_before_apply(self):
        conn, group, review = self.alias_database()
        p, _ = service_alias_proposal(conn, group, review)
        conn.execute("UPDATE name_resolution SET resolved_dblp_pid=NULL WHERE name_key='kenneth ross'")
        conn.commit()
        with self.assertRaisesRegex(RuntimeError, "anchor changed"):
            apply_plan(conn, [p])
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM dblp_candidates").fetchone()[0], 0)

    def test_reviewed_alias_can_correct_only_the_explicit_previous_exact_assignment(self):
        conn, group, review = self.alias_database()
        review = dict(review, previous_pid='short')
        group = dict(group, resolved_dblp_pid='short', status='resolved_unique_exact')
        item, reason = service_alias_proposal(conn, group, review)
        self.assertEqual(reason, 'proposed_reviewed_alias')
        self.assertEqual(item['previous_pid'], 'short')
        self.assertEqual(item['candidate_pid'], 'full')
        for changed in [dict(group, resolved_dblp_pid='unrelated'),
                        dict(group, status='resolved_ror_exact_affiliation')]:
            self.assertEqual(service_alias_proposal(conn, changed, review)[1], 'reviewed_alias_assignment_changed')
        self.assertEqual(service_alias_proposal(conn, group, dict(review, previous_pid=''))[1], 'reviewed_alias_assignment_changed')

    def test_invalid_alias_correction_defers_previous_exact_profile(self):
        conn, group, review = self.alias_database()
        review = dict(review, previous_pid='short')
        group = dict(group, resolved_dblp_pid='short', status='resolved_unique_exact',
                     reviewer_affiliations=['Unreviewed institution'])
        with mock.patch('apply_profile_entity_resolution.load_audit_groups', return_value=[group]):
            plan, _ = build_plan(conn, self.ror, {}, {'ken ross': review})
        self.assertEqual(plan[0]['candidate_pid'], None)
        self.assertEqual(plan[0]['proposed_status'], 'ambiguous_reviewed_profile')

    def test_reviewed_service_alias_is_reapplied_after_fresh_candidate_lookup(self):
        conn, group, review = self.alias_database()
        with mock.patch("apply_profile_entity_resolution.load_audit_groups", return_value=[group]):
            proposals, _ = build_plan(conn, self.ror, {}, {"ken ross": review})
        self.assertEqual(len(proposals), 1)
        self.assertEqual(apply_plan(conn, proposals), 1)
        with mock.patch("apply_profile_entity_resolution.load_audit_groups",
                        return_value=[dict(group, resolved_dblp_pid="full", status="resolved_reviewed_service_alias")]):
            self.assertEqual(build_plan(conn, self.ror, {}, {"ken ross": review})[0], [])

    def test_applied_profile_is_deferred_when_review_scope_or_candidate_changes(self):
        for missing_candidate in (False, True):
            conn = sqlite3.connect(":memory:")
            self.addCleanup(conn.close)
            create_schema(conn)
            conn.execute("INSERT INTO name_resolution VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                         ("kenneth ross", "Kenneth Ross", "resolved_reviewed_profile", "full",
                          "Kenneth A. Ross", None, None, 1, 1, 2, "", "now"))
            conn.commit()
            group = dict(self.group, status="resolved_reviewed_profile", resolved_dblp_pid="full")
            if missing_candidate:
                group["candidates"] = group["candidates"][:1]
            else:
                group["reviewer_affiliations"] = ["Other University"]
            with mock.patch("apply_profile_entity_resolution.load_audit_groups", return_value=[group]):
                plan, _ = build_plan(conn, self.ror, {"kenneth ross": self.review})
            self.assertEqual(apply_plan(conn, plan), 1)
            self.assertEqual(conn.execute("SELECT resolved_dblp_pid,status FROM name_resolution").fetchone(),
                             (None, "ambiguous_reviewed_profile"))

    def test_stale_review_does_not_demote_unrelated_resolution(self):
        conn, _, _ = self.alias_database()
        group = dict(self.group, status="resolved_ror_exact_affiliation", resolved_dblp_pid="full",
                     reviewer_affiliations=["Other University"])
        with mock.patch("apply_profile_entity_resolution.load_audit_groups", return_value=[group]):
            plan, held = build_plan(conn, self.ror, {"kenneth ross": self.review})
        self.assertEqual(plan, [])
        self.assertEqual(held[0]["reason"], "reviewed_affiliations_changed")

    def test_removed_profile_review_defers_only_its_owned_assignment(self):
        conn, _, _ = self.alias_database()
        conn.execute("UPDATE name_resolution SET status='resolved_reviewed_profile' WHERE name_key='kenneth ross'")
        conn.commit()
        reviewed = dict(self.group, status="resolved_reviewed_profile", resolved_dblp_pid="full")
        unrelated = dict(reviewed, name_key="unrelated", status="resolved_ror_exact_affiliation")
        with mock.patch("apply_profile_entity_resolution.load_audit_groups", return_value=[reviewed, unrelated]):
            plan, _ = build_plan(conn, self.ror, {})
        self.assertEqual([p["name_key"] for p in plan], ["kenneth ross"])
        self.assertEqual(plan[0]["evidence"]["reason"], "reviewed_decision_removed")
        apply_plan(conn, plan)
        self.assertIsNone(conn.execute("SELECT resolved_dblp_pid FROM name_resolution WHERE name_key='kenneth ross'").fetchone()[0])

    def test_removed_alias_review_clears_only_review_added_candidate(self):
        conn, group, review = self.alias_database()
        apply_plan(conn, [service_alias_proposal(conn, group, review)[0]])
        conn.execute("INSERT INTO dblp_candidates VALUES (?,?,?,?,?)",
                     ("ken ross", "full", "first_last_variant", "Kenneth A. Ross", 0))
        conn.commit()
        applied = dict(group, status="resolved_reviewed_service_alias", resolved_dblp_pid="full")
        with mock.patch("apply_profile_entity_resolution.load_audit_groups", return_value=[applied]):
            plan, _ = build_plan(conn, self.ror, {})
        apply_plan(conn, plan)
        self.assertEqual(conn.execute("SELECT match_type FROM dblp_candidates WHERE name_key='ken ross'").fetchall(),
                         [("first_last_variant",)])
        with mock.patch("apply_profile_entity_resolution.load_audit_groups",
                        return_value=[dict(applied, status="ambiguous_reviewed_profile", resolved_dblp_pid=None)]):
            self.assertEqual(build_plan(conn, self.ror, {})[0], [])

    def test_applied_alias_is_deferred_and_only_reviewed_candidate_removed(self):
        conn, group, review = self.alias_database()
        apply_plan(conn, [service_alias_proposal(conn, group, review)[0]])
        conn.execute("INSERT INTO dblp_candidates VALUES (?,?,?,?,?)",
                     ("ken ross", "full", "first_last_variant", "Kenneth A. Ross", 0))
        conn.commit()
        changed = dict(group, resolved_dblp_pid="full", status="resolved_reviewed_service_alias",
                       reviewer_affiliations=["Other University"])
        with mock.patch("apply_profile_entity_resolution.load_audit_groups", return_value=[changed]):
            plan, _ = build_plan(conn, self.ror, {}, {"ken ross": review})
        self.assertEqual(apply_plan(conn, plan), 1)
        self.assertEqual(apply_plan(conn, plan), 0)
        self.assertEqual(conn.execute("SELECT resolved_dblp_pid,status,all_candidate_count FROM name_resolution "
                                      "WHERE name_key='ken ross'").fetchone(),
                         (None, "ambiguous_reviewed_profile", 1))
        self.assertEqual(conn.execute("SELECT match_type FROM dblp_candidates WHERE name_key='ken ross'").fetchall(),
                         [("first_last_variant",)])

    def test_removed_profile_anchor_invalidates_retained_alias_review(self):
        for already_applied in (False, True):
            for alias_first in (False, True):
                with self.subTest(already_applied=already_applied, alias_first=alias_first):
                    conn, alias, review = self.alias_database(f"removed-anchor-{already_applied}-{alias_first}.sqlite")
                    if already_applied:
                        apply_plan(conn, [service_alias_proposal(conn, alias, review)[0]])
                        alias = dict(alias, status="resolved_reviewed_service_alias", resolved_dblp_pid="full")
                        conn.execute("INSERT INTO dblp_candidates VALUES (?,?,?,?,?)",
                                     ("ken ross", "full", "first_last_variant", "Kenneth A. Ross", 0))
                    conn.execute("UPDATE name_resolution SET status='resolved_reviewed_profile' WHERE name_key='kenneth ross'")
                    conn.commit()
                    anchor = dict(self.group, status="resolved_reviewed_profile", resolved_dblp_pid="full")
                    groups = [alias, anchor] if alias_first else [anchor, alias]
                    # The profile review was deleted; the dependent alias review remains.
                    with mock.patch("apply_profile_entity_resolution.load_audit_groups", return_value=groups):
                        plan, held = build_plan(conn, self.ror, {}, {"ken ross": review})
                    by_key = {item["name_key"]: item for item in plan}
                    self.assertIn("kenneth ross", by_key)
                    self.assertEqual(by_key["kenneth ross"]["evidence"]["reason"], "reviewed_decision_removed")
                    if already_applied:
                        self.assertEqual(by_key["ken ross"]["evidence"]["reason"], "reviewed_alias_anchor_changed")
                    else:
                        self.assertEqual([item["reason"] for item in held], ["reviewed_alias_anchor_changed"])
                    self.assertEqual(apply_plan(conn, plan), 2 if already_applied else 1)
                    self.assertEqual(conn.execute("SELECT DISTINCT resolved_dblp_pid FROM name_resolution").fetchall(), [(None,)])
                    self.assertEqual(conn.execute("SELECT match_type FROM dblp_candidates WHERE name_key='ken ross'").fetchall(),
                                     [("first_last_variant",)] if already_applied else [])
                    after = [dict(group, resolved_dblp_pid=None,
                                  status="ambiguous_reviewed_profile" if group is anchor or already_applied else group["status"])
                             for group in groups]
                    with mock.patch("apply_profile_entity_resolution.load_audit_groups", return_value=after):
                        self.assertEqual(build_plan(conn, self.ror, {}, {"ken ross": review})[0], [])

    def test_alias_uses_final_anchor_plan_independent_of_group_order(self):
        for already_applied in (False, True):
            for alias_first in (False, True):
                with self.subTest(already_applied=already_applied, alias_first=alias_first):
                    conn, group, review = self.alias_database(f"identities-{already_applied}-{alias_first}.sqlite")
                    if already_applied:
                        apply_plan(conn, [service_alias_proposal(conn, group, review)[0]])
                        group = dict(group, resolved_dblp_pid="full", status="resolved_reviewed_service_alias")
                    conn.execute("UPDATE name_resolution SET status='resolved_reviewed_profile' WHERE name_key='kenneth ross'")
                    conn.commit()
                    anchor = dict(self.group, status="resolved_reviewed_profile", resolved_dblp_pid="full",
                                  reviewer_affiliations=["Other University"])
                    groups = [group, anchor] if alias_first else [anchor, group]
                    with mock.patch("apply_profile_entity_resolution.load_audit_groups", return_value=groups):
                        plan, held = build_plan(conn, self.ror, {"kenneth ross": self.review}, {"ken ross": review})
                    self.assertEqual(len(plan), 2 if already_applied else 1)
                    if not already_applied:
                        self.assertEqual(held[0]["reason"], "reviewed_alias_anchor_changed")
                    apply_plan(conn, plan)
                    self.assertTrue(all(row[0] is None for row in conn.execute("SELECT resolved_dblp_pid FROM name_resolution")))

    def test_apply_rejects_alias_whose_anchor_the_same_plan_clears(self):
        conn, group, review = self.alias_database()
        alias = service_alias_proposal(conn, group, review)[0]
        anchor = dict(name_key="kenneth ross", previous_pid="full", status_before="resolved_unique_exact",
                      candidate_pid=None, candidate_name=None, best_orcid=None,
                      proposed_status="ambiguous_reviewed_profile", evidence={"rule": "stale_review_requires_verification"})
        for plan in ([alias, anchor], [anchor, alias]):
            with self.assertRaisesRegex(RuntimeError, "anchor changed"):
                apply_plan(conn, plan)
        self.assertEqual(conn.execute("SELECT resolved_dblp_pid FROM name_resolution WHERE name_key='kenneth ross'").fetchone(), ("full",))
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM dblp_candidates").fetchone()[0], 0)

    def test_applied_alias_is_deferred_if_target_person_disappears(self):
        conn, group, review = self.alias_database()
        apply_plan(conn, [service_alias_proposal(conn, group, review)[0]])
        conn.execute("DELETE FROM dblp_people WHERE dblp_pid='full'")
        conn.commit()
        group = dict(group, resolved_dblp_pid="full", status="resolved_reviewed_service_alias")
        with mock.patch("apply_profile_entity_resolution.load_audit_groups", return_value=[group]):
            plan, _ = build_plan(conn, self.ror, {}, {"ken ross": review})
        self.assertEqual(plan[0]["evidence"]["reason"], "reviewed_alias_person_missing")
        apply_plan(conn, plan)
        self.assertEqual(conn.execute("SELECT resolved_dblp_pid,all_candidate_count FROM name_resolution "
                                      "WHERE name_key='ken ross'").fetchone(), (None, 0))

    def test_reviewed_alias_chain_can_apply_in_reverse_dependency_order(self):
        conn, group, review = self.alias_database()
        conn.execute("INSERT INTO name_resolution SELECT 'k ross','K Ross',status,resolved_dblp_pid,"
                     "resolved_dblp_name,best_orcid,best_orcid_source,exact_candidate_count,"
                     "variant_candidate_count,all_candidate_count,notes,updated_at FROM name_resolution WHERE name_key='ken ross'")
        conn.commit()
        dependent = dict(group, name_key="k ross", representative_name="K Ross")
        dependent_review = dict(review, anchor_name_key="ken ross")
        with mock.patch("apply_profile_entity_resolution.load_audit_groups", return_value=[dependent, group]):
            plan, _ = build_plan(conn, self.ror, {}, {"k ross": dependent_review, "ken ross": review})
        self.assertEqual([p["name_key"] for p in plan], ["k ross", "ken ross"])
        self.assertEqual(apply_plan(conn, plan), 2)
        self.assertEqual(conn.execute("SELECT DISTINCT resolved_dblp_pid FROM name_resolution").fetchall(), [("full",)])

    def test_reviewed_alias_cycle_cannot_corroborate_applied_decisions(self):
        conn, group, review = self.alias_database()
        apply_plan(conn, [service_alias_proposal(conn, group, review)[0]])
        conn.execute("UPDATE name_resolution SET status='resolved_reviewed_service_alias' WHERE name_key='kenneth ross'")
        conn.commit()
        group = dict(group, resolved_dblp_pid="full", status="resolved_reviewed_service_alias")
        other = dict(group, name_key="kenneth ross", representative_name="Kenneth Ross")
        aliases = {"ken ross": review, "kenneth ross": dict(review, anchor_name_key="ken ross")}
        with mock.patch("apply_profile_entity_resolution.load_audit_groups", return_value=[group, other]):
            plan, _ = build_plan(conn, self.ror, {}, aliases)
        self.assertEqual(len(plan), 2)
        self.assertEqual(plan[0]["evidence"]["reason"], "reviewed_alias_anchor_cycle")
        apply_plan(conn, plan)
        self.assertEqual(conn.execute("SELECT DISTINCT resolved_dblp_pid FROM name_resolution").fetchall(), [(None,)])

    def test_service_alias_file_preserves_reviewed_scope_and_rejects_duplicates(self):
        path = self.root / "service_aliases.csv"
        fields = ["service_name", "target_service_name", "target_pid", "service_affiliations_json",
                  "evidence_urls_json", "reviewed_on", "reason"]
        row = dict(zip(fields, ["Ken Ross", "Kenneth Ross", "full", '["Columbia University"]',
                                '["https://example.test/official"]', "2026-11-03", "Reviewed official name form"]))
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader(); writer.writerow(row)
        loaded = load_service_aliases(path)
        self.assertEqual(loaded["ken ross"]["anchor_name_key"], "kenneth ross")
        self.assertEqual(loaded["ken ross"]["affiliations"], {"Columbia University"})
        with path.open("a", encoding="utf-8", newline="") as handle:
            csv.DictWriter(handle, fieldnames=fields).writerow(row)
        with self.assertRaisesRegex(ValueError, "duplicate"):
            load_service_aliases(path)

    def test_sparse_review_queue_never_changes_identity_or_uses_stored_variant_count(self):
        conn, _, _ = self.alias_database()
        conn.execute("UPDATE dblp_people SET affiliations_json='[]'")
        conn.execute("INSERT INTO dblp_people VALUES (?,?,?,?,?,?,?,?,?)",
                     ("variant", "Kenneth B. Ross", '["Kenneth B. Ross"]', '[]', '[]', '[]', '[]', 'homepages/variant', 'snapshot'))
        conn.execute("INSERT INTO dblp_candidates VALUES (?,?,?,?,?)",
                     ("kenneth ross", "variant", "first_last_variant", "Kenneth B. Ross", 0))
        conn.commit()
        before = conn.total_changes
        groups = sparse_exact_review_groups(conn)
        self.assertEqual([(g["name_key"], g["resolved_dblp_pid"], g["real_variant_count"]) for g in groups],
                         [("kenneth ross", "full", 1)])
        self.assertEqual(conn.total_changes, before)
        conn.execute("UPDATE dblp_people SET verified_orcids_json='[\"verified\"]' WHERE dblp_pid='full'")
        self.assertEqual(sparse_exact_review_groups(conn), [])
        conn.execute("UPDATE dblp_people SET verified_orcids_json='[]' WHERE dblp_pid='full'")
        conn.execute("DELETE FROM dblp_people WHERE dblp_pid='variant'")
        self.assertEqual(sparse_exact_review_groups(conn), [])

    def test_legacy_inputs_without_review_evidence_do_not_break_coverage_export(self):
        legacy = sqlite3.connect(":memory:")
        self.addCleanup(legacy.close)
        legacy.execute("CREATE TABLE name_resolution (name_key TEXT,status TEXT,resolved_dblp_pid TEXT)")
        legacy.execute("INSERT INTO name_resolution VALUES ('person','resolved_unique_exact','pid')")
        before = legacy.total_changes
        self.assertEqual(sparse_exact_review_groups(legacy), [])
        self.assertEqual(legacy.total_changes, before)


if __name__ == "__main__":
    unittest.main()
