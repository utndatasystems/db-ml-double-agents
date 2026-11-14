from __future__ import annotations

import csv
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from build_site_data import (
    apply_publication_affiliation_evidence,
    choose_primary_affiliation_evidence,
    load_publication_affiliation_evidence,
)


class PublicationAffiliationTests(unittest.TestCase):
    def setUp(self):
        self.primary = choose_primary_affiliation_evidence(
            [("Old University", "Old University")],
            [{"year": 2024, "displayAffiliation": "New University"}],
            [],
        )
        self.paper = {"dblpKey": "conf/example/Paper25", "year": 2025}
        self.evidence = {
            "personName": "Alice Example", "dblpKey": self.paper["dblpKey"],
            "year": 2025, "affiliation": "New University",
            "sourceUrl": "https://example.org/paper.pdf", "verifiedOn": "2026-10-18",
            "reason": "Author-specific affiliation checked in the paper.",
        }
        self.catalog = {"sources": {}}

    def choose(self, evidence=None, papers=None):
        return apply_publication_affiliation_evidence(
            self.primary, evidence if evidence is not None else [self.evidence],
            papers if papers is not None else [self.paper], self.catalog,
        )

    def test_recent_verified_author_affiliation_corrects_stale_dblp_with_source(self):
        result = self.choose()
        self.assertEqual(result["affiliation"], "New University")
        self.assertEqual(result["decisionKey"], "verified_publication_affiliation")
        self.assertEqual(result["publicationAffiliationYear"], 2025)
        self.assertEqual(result["currentAffiliations"], ["Old University"])
        self.assertEqual(self.primary["affiliation"], "Old University")
        self.assertTrue(result["hasConflict"])
        source = next(iter(self.catalog["sources"].values()))
        self.assertEqual(source["url"], "https://example.org/paper.pdf")
        self.assertEqual(source["personName"], "Alice Example")

    def test_older_paper_cannot_replace_newer_service_evidence(self):
        self.primary["latestServiceYear"] = 2026
        self.assertEqual(self.choose()["affiliation"], "Old University")
        self.assertEqual(self.catalog["sources"], {})

    def test_no_reviewed_publication_does_not_swap_service_ahead_of_dblp(self):
        self.assertEqual(self.choose(evidence=[]), self.primary)

    def test_wrong_person_paper_or_year_fails_instead_of_importing_coauthor_affiliation(self):
        with self.assertRaisesRegex(ValueError, "not a matching publication/year"):
            self.choose(papers=[{"dblpKey": "conf/example/OtherPerson25", "year": 2025}])
        with self.assertRaises(ValueError):
            self.choose(papers=[{**self.paper, "year": 2024}])

    def test_two_affiliations_in_same_newest_year_remain_flagged_without_picking_one(self):
        second = {**self.evidence, "affiliation": "Other University"}
        result = self.choose(evidence=[self.evidence, second])
        self.assertEqual(result["affiliation"], "Old University")
        self.assertTrue(result["hasConflict"])
        self.assertEqual(result["confidence"], "low")

    def test_newest_reviewed_year_wins_independent_of_csv_order(self):
        old = {**self.evidence, "dblpKey": "conf/example/Paper24", "year": 2024, "affiliation": "Other University"}
        papers = [self.paper, {"dblpKey": old["dblpKey"], "year": 2024}]
        for evidence in [[old, self.evidence], [self.evidence, old]]:
            self.assertEqual(self.choose(evidence=evidence, papers=papers)["affiliation"], "New University")

    def test_explicit_merged_version_can_supply_affiliation_with_its_own_year(self):
        paper = {"dblpKey": "conf/example/Paper26", "year": 2026,
                 "personVersionDblpKeys": ["conf/example/Paper26", self.evidence["dblpKey"]],
                 "resolvedVersions": [{"dblpKey": self.evidence["dblpKey"], "year": 2025}]}
        self.assertEqual(self.choose(papers=[paper])["affiliation"], "New University")

    def test_nonowned_component_cannot_supply_reviewed_affiliation(self):
        paper = {"dblpKey": "journals/corr/Own26", "year": 2026,
                 "resolvedVersions": [{"dblpKey": self.evidence["dblpKey"], "year": 2025}]}
        for ownership in [None, [], [paper["dblpKey"]]]:
            with self.subTest(ownership=ownership):
                candidate = dict(paper)
                if ownership is not None:
                    candidate["personVersionDblpKeys"] = ownership
                with self.assertRaisesRegex(ValueError, "not a matching publication/year"):
                    self.choose(papers=[candidate])
                self.assertEqual(self.catalog["sources"], {})

    def test_explicit_empty_ownership_rejects_even_the_selected_record(self):
        with self.assertRaisesRegex(ValueError, "not a matching publication/year"):
            self.choose(papers=[{**self.paper, "personVersionDblpKeys": []}])

    def test_csv_validates_dates_urls_and_required_fields(self):
        fields = ["dblp_pid", "person_name", "dblp_key", "publication_year", "affiliation", "source_url", "verified_on", "reason"]
        row = ["12/345", "Alice Example", "conf/example/Paper25", "2025", "New University", "https://example.org/paper.pdf", "2026-10-18", "Checked author block"]
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "publication_affiliation_evidence.csv"
            self.assertEqual(load_publication_affiliation_evidence(path), {})
            for index, value in [(None, None), (5, "not-a-url"), (6, "yesterday"), (3, "2027"), (4, "")]:
                candidate = row.copy()
                if index is not None:
                    candidate[index] = value
                with path.open("w", encoding="utf-8", newline="") as handle:
                    writer = csv.writer(handle)
                    writer.writerow(fields)
                    writer.writerow(candidate)
                if index is None:
                    loaded = load_publication_affiliation_evidence(path)
                    self.assertEqual(loaded["12/345"][0]["affiliation"], "New University")
                else:
                    with self.assertRaises(ValueError):
                        load_publication_affiliation_evidence(path)


if __name__ == "__main__":
    unittest.main()
