from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from apply_ror_entity_resolution import add_ror_evidence, proposal_for_group
from ror_affiliations import (
    ROR_RELEASE,
    ROR_ZIP_MD5,
    RorAffiliationIndex,
    build_ror_index_from_records,
)


def record(ror_id: str, display: str, *extra_names: tuple[str, str]):
    names = [
        {"value": display, "types": ["ror_display", "label"], "lang": "en"}
    ]
    for value, name_type in extra_names:
        names.append({"value": value, "types": [name_type], "lang": None})
    return {
        "id": ror_id,
        "status": "active",
        "names": names,
    }


class RorEntityResolutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.index_path = Path(self.tmp.name) / "ror.sqlite"
        build_ror_index_from_records(
            [
                record(
                    "https://ror.org/tum000001",
                    "Technical University of Munich",
                    ("Technische Universität München", "label"),
                    ("TUM", "acronym"),
                ),
                record(
                    "https://ror.org/unsw00001",
                    "University of New South Wales",
                    ("UNSW", "acronym"),
                ),
                record(
                    "https://ror.org/other0001",
                    "Other University",
                    ("OU", "acronym"),
                ),
                record(
                    "https://ror.org/hkust0001",
                    "Hong Kong University of Science and Technology",
                    ("HKUST", "acronym"),
                ),
                record(
                    "https://ror.org/ambig0001",
                    "Alpha Biomedical Center",
                    ("ABC", "acronym"),
                ),
                record(
                    "https://ror.org/ambig0002",
                    "Applied Biology Consortium",
                    ("ABC", "acronym"),
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

    def test_ror_acronym_and_explicit_long_name_converge_to_same_id(self):
        service = self.ror.match_affiliation("TUM")
        long_form = self.ror.match_affiliation("Technical University of Munich")
        expected = {"https://ror.org/tum000001"}
        self.assertEqual(set(service), expected)
        self.assertEqual(set(long_form), expected)

    def test_unlisted_short_form_is_not_synthesized(self):
        self.assertEqual(
            self.ror.match_affiliation(
                "TU Munich, Faculty of Computer Science, Germany"
            ),
            {},
        )

    def test_parenthetical_campus_is_not_stripped_to_parent_organization(self):
        self.assertEqual(
            self.ror.match_affiliation(
                "Hong Kong University of Science and Technology (Guangzhou)"
            ),
            {},
        )

    def test_explicit_parenthetical_acronym_can_still_match(self):
        matched = self.ror.match_affiliation(
            "Hong Kong University of Science and Technology (HKUST), Hong Kong"
        )
        self.assertEqual(set(matched), {"https://ror.org/hkust0001"})

    def test_ambiguous_ror_acronym_is_not_identity_evidence(self):
        self.assertEqual(self.ror.match_affiliation("ABC"), {})

    def test_unique_first_last_variant_with_shared_ror_is_proposed(self):
        group = {
            "name_key": "jeffrey naughton",
            "representative_name": "Jeffrey Naughton",
            "status": "candidate_unique_variant",
            "reviewer_affiliations": ["UNSW"],
            "observation_count": 2,
            "min_year": 2023,
            "max_year": 2025,
            "venues": ["SIGMOD"],
            "candidates": [
                {
                    "pid": "n/JeffreyFNaughton",
                    "canonical_name": "Jeffrey F. Naughton",
                    "best_match_type": "first_last_variant",
                    "first_last_exact": True,
                    "candidate_affiliations": [
                        "University of New South Wales, School of Computing"
                    ],
                    "verified_orcids": [],
                }
            ],
        }
        enriched = add_ror_evidence(group, self.ror)
        proposal, reason = proposal_for_group(enriched)
        self.assertEqual(reason, "proposed")
        self.assertIsNotNone(proposal)
        self.assertEqual(proposal["proposed_status"], "resolved_ror_unique_variant")
        self.assertIn("https://ror.org/unsw00001", proposal["shared_ror_ids"])

    def test_ambiguous_exact_name_requires_complete_competitor_ror_coverage(self):
        base = {
            "name_key": "thomas neumann",
            "representative_name": "Thomas Neumann",
            "status": "ambiguous_exact_name",
            "reviewer_affiliations": ["Technical University of Munich"],
            "observation_count": 3,
            "min_year": 2022,
            "max_year": 2025,
            "venues": ["VLDB"],
            "candidates": [
                {
                    "pid": "n/ThomasNeumann",
                    "canonical_name": "Thomas Neumann 0001",
                    "best_match_type": "exact_alias",
                    "first_last_exact": True,
                    "candidate_affiliations": [
                        "Technical University of Munich, Faculty of Computer Science, Germany"
                    ],
                    "verified_orcids": [],
                },
                {
                    "pid": "x/ThomasNeumann",
                    "canonical_name": "Thomas Neumann 0002",
                    "best_match_type": "exact_alias",
                    "first_last_exact": True,
                    "candidate_affiliations": ["Other University"],
                    "verified_orcids": [],
                },
            ],
        }
        enriched = add_ror_evidence(base, self.ror)
        proposal, reason = proposal_for_group(enriched)
        self.assertEqual(reason, "proposed")
        self.assertEqual(proposal["candidate_pid"], "n/ThomasNeumann")
        self.assertEqual(
            proposal["proposed_status"], "resolved_ror_exact_affiliation"
        )

        unsafe = dict(base)
        unsafe["candidates"] = [dict(item) for item in base["candidates"]]
        unsafe["candidates"][1]["candidate_affiliations"] = [
            "Organization Not Present In ROR"
        ]
        enriched = add_ror_evidence(unsafe, self.ror)
        proposal, reason = proposal_for_group(enriched)
        self.assertIsNone(proposal)
        self.assertEqual(reason, "exact_competitor_missing_ror_coverage")

    def test_multiple_exact_candidates_sharing_same_ror_are_not_resolved(self):
        group = {
            "name_key": "common name",
            "representative_name": "Common Name",
            "status": "ambiguous_exact_name",
            "reviewer_affiliations": ["TUM"],
            "candidates": [
                {
                    "pid": "a",
                    "canonical_name": "Common Name 0001",
                    "best_match_type": "exact_alias",
                    "candidate_affiliations": ["TUM"],
                    "verified_orcids": [],
                },
                {
                    "pid": "b",
                    "canonical_name": "Common Name 0002",
                    "best_match_type": "exact_alias",
                    "candidate_affiliations": ["Technical University of Munich"],
                    "verified_orcids": [],
                },
            ],
        }
        enriched = add_ror_evidence(group, self.ror)
        proposal, reason = proposal_for_group(enriched)
        self.assertIsNone(proposal)
        self.assertEqual(reason, "multiple_exact_candidates_share_ror")


if __name__ == "__main__":
    unittest.main()
