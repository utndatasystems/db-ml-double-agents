import csv
import gzip
import sys
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from build_site_data import classify_publication, load_publications
from publication_categories import CategoryMapping, load_category_mappings
from publication_categories import _mapping_index, matching_category_rule
from audit_publication_categories import audit_rows, read_snapshot_evidence


class PublicationCategoryMappingTests(unittest.TestCase):
    def classify(self, booktitle, *, key="conf/example/paper", year=2015, scope="side", journal="", record_type="inproceedings"):
        return classify_publication(scope=scope, dblp_key=key, year=year,
                                    booktitle=booktitle, journal=journal,
                                    record_type=record_type, venue_label=booktitle,
                                    main_venue="ICML" if scope == "main" else "")[0]

    def test_original_named_ml_workshops_are_workshop_context(self):
        self.assertEqual(self.classify("MUD@ICML"), "workshop")
        self.assertEqual(self.classify("EMC2@NeurIPS", year=2019), "workshop")

    def test_workshop_overrides_database_namespace_without_promoting_to_main(self):
        self.assertEqual(self.classify("ICDEW", key="conf/icde/example", year=2024), "workshop")
        self.assertEqual(self.classify("HILDA@SIGMOD", key="conf/sigmod/example", year=2019), "workshop")
        self.assertEqual(self.classify("SIGMOD Conference", key="conf/sigmod/example"), "database")

    def test_protected_main_and_preprints_cannot_be_reclassified(self):
        self.assertEqual(self.classify("MUD@ICML", scope="main"), "main_ml")
        self.assertEqual(self.classify("MUD@ICML", key="journals/corr/example", journal="CoRR", record_type="article"), "preprint")

    def test_exact_matching_and_year_bounds_do_not_spread_to_unreviewed_events(self):
        self.assertEqual(self.classify("  mud@ICML  "), "workshop")
        self.assertEqual(self.classify("MUD@ICML main conference"), "other_conference")
        self.assertEqual(self.classify("ICDEW", key="conf/icde/example", year=2099), "database")
        self.assertEqual(self.classify("ADBIS (Short Papers)", year=2022), "other_conference")

    def test_non_database_journals_are_not_assigned_a_research_area(self):
        self.assertEqual(self.classify("", key="journals/example/test", journal="Example Journal", record_type="article"), "journal")

    def test_reviewed_tracks_in_shared_volumes_keep_their_own_category(self):
        self.assertEqual(self.classify("WISE (2)", year=2001), "workshop")
        self.assertEqual(self.classify("IWANN (2)", year=2009), "workshop")
        self.assertEqual(self.classify("ICMLA (1)", year=2011), "other_conference")
        self.assertEqual(self.classify("ADBIS (Short Papers)", year=2022), "other_conference")
        self.assertEqual(self.classify("CSL", year=2009), "other_conference")

    def test_complete_mapping_file_is_valid(self):
        self.assertTrue(load_category_mappings())

    def test_exact_mapping_can_be_appended_after_database_namespaces(self):
        prefix = CategoryMapping("db", "dblp_key", "prefix", "conf/example/", "database", "Example", "https://dblp.org/", "Database policy")
        exact = CategoryMapping("workshop", "booktitle", "exact", "ExampleW", "workshop", "ExampleW", "https://dblp.org/", "Workshop evidence")
        _mapping_index.cache_clear()
        try:
            with patch("publication_categories.default_category_mappings", return_value=(prefix, exact)):
                self.assertEqual(matching_category_rule(dblp_key="conf/example/paper", booktitle="ExampleW"), exact)
        finally:
            _mapping_index.cache_clear()

    def test_reviewed_exact_exception_can_correct_misleading_workshop_wording(self):
        rule = CategoryMapping("journal", "journal", "exact", "Workshop Transactions", "journal", "Workshop Transactions", "https://dblp.org/", "Reviewed journal")
        with patch("build_site_data.matching_category_rule", return_value=rule):
            self.assertEqual(self.classify("", journal="Workshop Transactions", record_type="article"), "journal")

    def test_invalid_duplicate_overlapping_and_unsafe_rows_are_rejected(self):
        base = CategoryMapping("sample", "booktitle", "exact", "Sample", "workshop", "Sample", "https://dblp.org/rec/conf/sample/2020", "Proceedings evidence", "2020", "2021")
        invalid = [
            [{**asdict(base), "category": "main_ml"}],
            [{**asdict(base), "match_type": "contains"}],
            [{**asdict(base), "match_field": "title"}],
            [{**asdict(base), "match_field": "dblp_key", "match_type": "prefix", "match_text": "conf/sample"}],
            [{**asdict(base), "evidence_url": "javascript:alert(1)"}],
            [asdict(base), asdict(base)],
            [asdict(base), {**asdict(base), "rule_id": "second", "year_from": "2021", "year_to": "2022"}],
            [{**asdict(base), "year_from": "2022", "year_to": "2020"}],
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "mappings.csv"
            for rows in invalid:
                with self.subTest(rows=rows):
                    with path.open("w", encoding="utf-8", newline="") as handle:
                        writer = csv.DictWriter(handle, fieldnames=asdict(base))
                        writer.writeheader()
                        writer.writerows(rows)
                    with self.assertRaises(ValueError):
                        load_category_mappings(path)

    def test_mapping_reason_and_evidence_survive_publication_loading(self):
        row = dict(dblp_pid="1/example", canonical_name="Example", scope="side", year="2015",
                   title="Example paper", dblp_key="conf/icml/example", booktitle="MUD@ICML",
                   record_type="inproceedings", venue_label="MUD@ICML")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "publications.csv"
            with path.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=row)
                writer.writeheader()
                writer.writerow(row)
            people, _, _ = load_publications(path, {"1/example"})
        paper = next(iter(people["1/example"]["papers"].values()))
        self.assertEqual(paper["scope"], "side")
        self.assertEqual(paper["publicationClass"], "workshop")
        self.assertEqual(paper["categoryMapping"]["ruleId"], "workshop_mud_icml")
        self.assertIn("mud2015", paper["categoryMapping"]["evidenceUrl"])

    def test_snapshot_audit_handles_adjacent_records_and_chunk_boundaries(self):
        xml = b'''<dblp><proceedings key="conf/example/2020"><title>A Workshop</title><year>2020</year></proceedings><inproceedings key="conf/example/paper"><title>Paper &amp; Data</title><booktitle>Example</booktitle><year>2020</year><crossref>conf/example/2020</crossref></inproceedings><article key="journals/other/skip"><title>Skip</title></article></dblp>'''
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "dblp-2020-01-01.xml.gz"
            with gzip.open(path, "wb") as handle:
                handle.write(xml)
            for size in (17, 100000):
                with self.subTest(chunk_size=size):
                    evidence = read_snapshot_evidence(path, {"conf/example/paper"}, chunk_size=size)
                    self.assertEqual(evidence["missing_keys"], [])
                    self.assertEqual(evidence["records"]["conf/example/paper"]["title"], "Paper & Data")
                    self.assertEqual(evidence["parents"]["conf/example/2020"]["title"], "A Workshop")
                    self.assertNotIn("journals/other/skip", evidence["records"])

    def test_mixed_proceedings_are_flagged_instead_of_assumed_workshops(self):
        row = dict(dblp_key="conf/example/paper", year="2020", record_type="inproceedings",
                   booktitle="Example (Short Papers)", journal="", venue_label="Example")
        evidence = {"records": {row["dblp_key"]: {"crossref": "conf/example/2020"}},
                    "parents": {"conf/example/2020": {"title": "Conference Short Papers and Workshops"}}}
        result = audit_rows([row], evidence)[0]
        self.assertEqual(result["category"], "other_conference")
        self.assertEqual(result["status"], "review_mixed_or_ambiguous_volume")


if __name__ == "__main__":
    unittest.main()
