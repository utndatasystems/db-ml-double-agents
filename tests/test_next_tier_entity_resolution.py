from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from apply_next_tier_entity_resolution import apply_plan, proposal_for_group
from apply_ror_entity_resolution import add_ror_evidence
from ror_affiliations import (
    ROR_RELEASE,
    ROR_ZIP_MD5,
    RorAffiliationIndex,
    build_ror_index_from_records,
)


def record(ror_id: str, display: str, *extra_names: tuple[str, str]):
    names = [{"value": display, "types": ["ror_display", "label"], "lang": "en"}]
    for value, name_type in extra_names:
        names.append({"value": value, "types": [name_type], "lang": None})
    return {"id": ror_id, "status": "active", "names": names}


class NextTierEntityResolutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.index_path = Path(self.tmp.name) / "ror.sqlite"
        build_ror_index_from_records(
            [
                record(
                    "https://ror.org/a00000001",
                    "Alpha University",
                    ("ALU", "acronym"),
                ),
                record(
                    "https://ror.org/b00000002",
                    "Beta University",
                    ("BEU", "acronym"),
                ),
                record(
                    "https://ror.org/c00000003",
                    "Gamma University",
                    ("GAU", "acronym"),
                ),
            ],
            self.index_path,
            release=ROR_RELEASE,
            snapshot_md5=ROR_ZIP_MD5,
        )
        self.ror = RorAffiliationIndex(self.index_path)

    def tearDown(self):
        self.ror.close()
        self.tmp.cleanup()

    @staticmethod
    def _owners(pid: str):
        return {
            pid: [
                {
                    "name_key": "resolved person",
                    "representative_name": "Resolved Person",
                    "status": "resolved_unique_exact",
                    "resolved_dblp_name": "Resolved Person",
                }
            ]
        }

    def test_unique_candidate_existing_pid_alias_is_apply_eligible(self):
        group = {
            "name_key": "ryan c marcus",
            "representative_name": "Ryan C Marcus",
            "status": "candidate_unique_variant",
            "reviewer_affiliations": [],
            "candidates": [
                {
                    "pid": "m/RyanMarcus",
                    "canonical_name": "Ryan Marcus",
                    "best_match_type": "first_last_variant",
                    "first_last_exact": True,
                    "initial_compatible": True,
                    "last_name_exact": True,
                    "candidate_affiliations": [],
                    "verified_orcids": [],
                }
            ],
        }
        enriched = add_ror_evidence(group, self.ror)
        proposal, reason = proposal_for_group(
            enriched, self._owners("m/RyanMarcus")
        )
        self.assertEqual(reason, "proposed_existing_pid_unique_candidate")
        self.assertEqual(proposal["apply_eligible"], 1)
        self.assertEqual(proposal["proposed_status"], "resolved_existing_pid_alias")

    def test_existing_pid_alias_requires_name_compatibility(self):
        group = {
            "name_key": "venkata meduri",
            "representative_name": "Venkata Meduri",
            "status": "candidate_unique_variant",
            "reviewer_affiliations": [],
            "candidates": [
                {
                    "pid": "m/VamsiMeduri",
                    "canonical_name": "Vamsi Meduri",
                    "best_match_type": "first_last_variant",
                    "first_last_exact": False,
                    "initial_compatible": False,
                    "last_name_exact": True,
                    "candidate_affiliations": [],
                    "verified_orcids": [],
                }
            ],
        }
        enriched = add_ror_evidence(group, self.ror)
        proposal, reason = proposal_for_group(
            enriched, self._owners("m/VamsiMeduri")
        )
        self.assertIsNone(proposal)
        self.assertEqual(reason, "no_next_tier_rule")

    def test_multiple_candidates_can_propagate_only_resolved_pid_with_unique_ror_overlap(self):
        group = {
            "name_key": "steven e whang",
            "representative_name": "Steven E Whang",
            "status": "ambiguous_variant",
            "reviewer_affiliations": ["Alpha University"],
            "candidates": [
                {
                    "pid": "w/Resolved",
                    "canonical_name": "Steven Whang",
                    "best_match_type": "first_last_variant",
                    "first_last_exact": True,
                    "initial_compatible": True,
                    "last_name_exact": True,
                    "candidate_affiliations": ["ALU"],
                    "verified_orcids": [],
                },
                {
                    "pid": "w/Other",
                    "canonical_name": "Steven Whang 0002",
                    "best_match_type": "first_last_variant",
                    "first_last_exact": True,
                    "initial_compatible": True,
                    "last_name_exact": True,
                    "candidate_affiliations": ["GAU"],
                    "verified_orcids": [],
                },
            ],
        }
        enriched = add_ror_evidence(group, self.ror)
        proposal, reason = proposal_for_group(enriched, self._owners("w/Resolved"))
        self.assertEqual(reason, "proposed_existing_pid_ror_alias")
        self.assertEqual(proposal["apply_eligible"], 1)
        self.assertEqual(proposal["candidate_pid"], "w/Resolved")

    def test_two_independent_ror_anchors_are_apply_eligible(self):
        group = {
            "name_key": "common researcher",
            "representative_name": "Common Researcher",
            "status": "ambiguous_exact_name",
            "reviewer_affiliations": ["Alpha University", "Beta University"],
            "candidates": [
                {
                    "pid": "c/Correct",
                    "canonical_name": "Common Researcher 0001",
                    "best_match_type": "exact_alias",
                    "first_last_exact": True,
                    "initial_compatible": True,
                    "last_name_exact": True,
                    "candidate_affiliations": ["ALU", "BEU"],
                    "verified_orcids": [],
                },
                {
                    "pid": "c/Other",
                    "canonical_name": "Common Researcher 0002",
                    "best_match_type": "exact_alias",
                    "first_last_exact": True,
                    "initial_compatible": True,
                    "last_name_exact": True,
                    "candidate_affiliations": ["GAU"],
                    "verified_orcids": [],
                },
            ],
        }
        enriched = add_ror_evidence(group, self.ror)
        proposal, reason = proposal_for_group(enriched, {})
        self.assertEqual(reason, "proposed_two_anchor_resolution")
        self.assertEqual(proposal["candidate_pid"], "c/Correct")
        self.assertEqual(proposal["shared_ror_count"], 2)
        self.assertEqual(proposal["apply_eligible"], 1)
        self.assertEqual(
            proposal["proposed_status"], "resolved_ror_two_anchor_exact_name"
        )

    def test_two_anchor_rule_requires_independent_affiliation_observations(self):
        group = {
            "name_key": "common researcher",
            "representative_name": "Common Researcher",
            "status": "ambiguous_exact_name",
            "reviewer_affiliations": ["Alpha University / Beta University"],
            "candidates": [
                {
                    "pid": "c/Correct",
                    "canonical_name": "Common Researcher 0001",
                    "best_match_type": "exact_alias",
                    "candidate_affiliations": ["ALU", "BEU"],
                    "verified_orcids": [],
                },
                {
                    "pid": "c/Other",
                    "canonical_name": "Common Researcher 0002",
                    "best_match_type": "exact_alias",
                    "candidate_affiliations": ["GAU"],
                    "verified_orcids": [],
                },
            ],
        }
        enriched = add_ror_evidence(group, self.ror)
        proposal, reason = proposal_for_group(enriched, {})
        self.assertIsNone(proposal)
        self.assertEqual(reason, "no_next_tier_rule")

    def test_two_anchor_rule_rejects_multiple_qualifying_exact_candidates(self):
        group = {
            "name_key": "common researcher",
            "representative_name": "Common Researcher",
            "status": "ambiguous_exact_name",
            "reviewer_affiliations": ["Alpha University", "Beta University"],
            "candidates": [
                {
                    "pid": "c/One",
                    "canonical_name": "Common Researcher 0001",
                    "best_match_type": "exact_alias",
                    "candidate_affiliations": ["ALU", "BEU"],
                    "verified_orcids": [],
                },
                {
                    "pid": "c/Two",
                    "canonical_name": "Common Researcher 0002",
                    "best_match_type": "exact_alias",
                    "candidate_affiliations": ["Alpha University", "Beta University"],
                    "verified_orcids": [],
                },
            ],
        }
        enriched = add_ror_evidence(group, self.ror)
        proposal, reason = proposal_for_group(enriched, {})
        self.assertIsNone(proposal)
        self.assertEqual(reason, "multiple_exact_candidates_have_two_ror_anchors")

    def test_apply_plan_applies_two_anchor_resolutions(self):
        conn = sqlite3.connect(":memory:")
        try:
            conn.execute(
                """
                CREATE TABLE name_resolution (
                    name_key TEXT PRIMARY KEY,
                    status TEXT,
                    resolved_dblp_pid TEXT,
                    resolved_dblp_name TEXT,
                    best_orcid TEXT,
                    best_orcid_source TEXT,
                    notes TEXT,
                    updated_at TEXT
                )
                """
            )
            conn.executemany(
                "INSERT INTO name_resolution (name_key, status) VALUES (?, ?)",
                [("auto", "candidate_unique_variant"), ("review", "ambiguous_exact_name")],
            )
            plan = [
                {
                    "name_key": "auto",
                    "proposed_status": "resolved_existing_pid_alias",
                    "candidate_pid": "p/1",
                    "candidate_name": "Person One",
                    "best_orcid": "",
                    "proposed_rule": "unique_candidate_existing_resolved_pid",
                    "explanation": "test",
                    "apply_eligible": 1,
                },
                {
                    "name_key": "review",
                    "proposed_status": "resolved_ror_two_anchor_exact_name",
                    "candidate_pid": "p/2",
                    "candidate_name": "Person Two",
                    "best_orcid": "",
                    "proposed_rule": "ror_two_independent_anchors",
                    "explanation": "test",
                    "apply_eligible": 1,
                },
            ]
            changed = apply_plan(conn, plan)
            self.assertEqual(changed, 2)
            auto = conn.execute(
                "SELECT resolved_dblp_pid FROM name_resolution WHERE name_key='auto'"
            ).fetchone()[0]
            review = conn.execute(
                "SELECT resolved_dblp_pid FROM name_resolution WHERE name_key='review'"
            ).fetchone()[0]
            self.assertEqual(auto, "p/1")
            self.assertEqual(review, "p/2")
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
