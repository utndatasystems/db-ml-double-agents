from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from enrich_publication_candidates_with_arxiv import (
    arxiv_id_from_raw_row,
    enrich_candidate_rows,
    extract_arxiv_id,
    load_cache,
    parse_arxiv_atom,
    unresolved_preprint_keys,
)


class ArxivIdExtractionTests(unittest.TestCase):
    def test_extracts_modern_and_legacy_ids_from_dblp_metadata(self) -> None:
        self.assertEqual(
            extract_arxiv_id("https://doi.org/10.48550/arXiv.2510.09655"),
            "2510.09655",
        )
        self.assertEqual(
            extract_arxiv_id("http://arxiv.org/abs/1503.02007v2"),
            "1503.02007",
        )
        self.assertEqual(
            extract_arxiv_id("https://arxiv.org/abs/math.GT/0309136v1"),
            "math/0309136",
        )
        self.assertEqual(
            extract_arxiv_id("https://arxiv.org/abs/cs.IR/0407061"),
            "cs/0407061",
        )

    def test_modern_corr_key_is_a_fallback_when_ee_is_missing(self) -> None:
        row = {
            "dblp_key": "journals/corr/abs-2510-09655",
            "url": "",
            "ees_json": "[]",
        }
        self.assertEqual(arxiv_id_from_raw_row(row), "2510.09655")

    def test_legacy_corr_key_fallback_uses_canonical_archive_id(self) -> None:
        row = {
            "dblp_key": "journals/corr/cs-IR-0407061",
            "url": "",
            "ees_json": "[]",
        }
        self.assertEqual(arxiv_id_from_raw_row(row), "cs/0407061")

    def test_stale_negative_subject_qualified_cache_entry_is_refetched(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cache.json"
            path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "records": {
                            "cs.IR/0407061": {
                                "arxiv_id": "cs.IR/0407061",
                                "status": "not_found",
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            self.assertNotIn("cs/0407061", load_cache(path))


class UnresolvedPreprintSelectionTests(unittest.TestCase):
    def test_selects_only_preprint_components_without_archival_members(self) -> None:
        raw_by_key = {
            "journals/corr/abs-2301-00001": {
                "dblp_key": "journals/corr/abs-2301-00001",
                "journal": "CoRR",
                "record_type": "article",
                "publtype": "informal",
            },
            "journals/corr/abs-2301-00002": {
                "dblp_key": "journals/corr/abs-2301-00002",
                "journal": "CoRR",
                "record_type": "article",
                "publtype": "informal",
            },
            "conf/sigmod/Example24": {
                "dblp_key": "conf/sigmod/Example24",
                "journal": "",
                "record_type": "inproceedings",
                "publtype": "",
            },
            "journals/corr/abs-2301-00003": {
                "dblp_key": "journals/corr/abs-2301-00003",
                "journal": "CoRR",
                "record_type": "article",
                "publtype": "informal",
            },
            "journals/corr/abs-2301-00004": {
                "dblp_key": "journals/corr/abs-2301-00004",
                "journal": "CoRR",
                "record_type": "article",
                "publtype": "informal",
            },
        }
        mapping_rows = [
            {"work_id": "work:unresolved", "dblp_key": "journals/corr/abs-2301-00001"},
            {"work_id": "work:resolved", "dblp_key": "journals/corr/abs-2301-00002"},
            {"work_id": "work:resolved", "dblp_key": "conf/sigmod/Example24"},
            {"work_id": "work:preprints", "dblp_key": "journals/corr/abs-2301-00003"},
            {"work_id": "work:preprints", "dblp_key": "journals/corr/abs-2301-00004"},
        ]

        self.assertEqual(
            unresolved_preprint_keys(raw_by_key, mapping_rows),
            [
                "journals/corr/abs-2301-00001",
                "journals/corr/abs-2301-00003",
                "journals/corr/abs-2301-00004",
            ],
        )


class ArxivAtomParsingTests(unittest.TestCase):
    def test_preserves_doi_journal_ref_comment_and_current_title(self) -> None:
        payload = b"""<?xml version='1.0' encoding='UTF-8'?>
        <feed xmlns='http://www.w3.org/2005/Atom' xmlns:arxiv='http://arxiv.org/schemas/atom'>
          <entry>
            <id>http://arxiv.org/abs/2301.00001v3</id>
            <updated>2024-02-03T00:00:00Z</updated>
            <published>2023-01-02T00:00:00Z</published>
            <title>A Renamed Published Paper</title>
            <summary>Abstract text.</summary>
            <author><name>Alice Example</name></author>
            <author><name>Bob Example</name></author>
            <category term='cs.DB' scheme='http://arxiv.org/schemas/atom'/>
            <arxiv:primary_category term='cs.DB' scheme='http://arxiv.org/schemas/atom'/>
            <arxiv:comment>Accepted at SIGMOD 2024</arxiv:comment>
            <arxiv:journal_ref>Proceedings of SIGMOD 2024</arxiv:journal_ref>
            <arxiv:doi>10.1000/example.1</arxiv:doi>
          </entry>
        </feed>"""
        records = parse_arxiv_atom(payload)
        record = records["2301.00001"]
        self.assertEqual(record["title"], "A Renamed Published Paper")
        self.assertEqual(record["doi"], "10.1000/example.1")
        self.assertEqual(record["journal_ref"], "Proceedings of SIGMOD 2024")
        self.assertEqual(record["comment"], "Accepted at SIGMOD 2024")
        self.assertEqual(record["authors"], ["Alice Example", "Bob Example"])
        self.assertEqual(record["primary_category"], "cs.DB")


class CandidateEnrichmentTests(unittest.TestCase):
    def test_flags_exact_external_doi_and_current_title_matches_without_merging(self) -> None:
        candidates = [
            {
                "preprint_key": "journals/corr/abs-2301-00001",
                "published_key": "conf/sigmod/Example24",
                "preprint_title": "An Earlier Preprint Title",
                "published_title": "A Renamed Published Paper",
                "reason": "review_only_preprint_published_candidate",
            }
        ]
        raw_by_key = {
            "journals/corr/abs-2301-00001": {
                "dblp_key": "journals/corr/abs-2301-00001",
                "url": "https://doi.org/10.48550/arXiv.2301.00001",
                "ees_json": json.dumps(["https://arxiv.org/abs/2301.00001"]),
            },
            "conf/sigmod/Example24": {
                "dblp_key": "conf/sigmod/Example24",
                "venue_label": "SIGMOD",
                "booktitle": "SIGMOD Conference",
                "journal": "",
                "url": "https://doi.org/10.1000/example.1",
                "ees_json": json.dumps(["https://doi.org/10.1000/example.1"]),
            },
        }
        metadata = {
            "2301.00001": {
                "status": "found",
                "title": "A Renamed Published Paper.",
                "authors": ["Alice Example", "Bob Example"],
                "published": "2023-01-02T00:00:00Z",
                "updated": "2024-02-03T00:00:00Z",
                "doi": "10.1000/example.1",
                "journal_ref": "Proceedings of SIGMOD 2024",
                "comment": "Accepted at SIGMOD 2024",
                "primary_category": "cs.DB",
                "categories": ["cs.DB"],
                "abs_url": "http://arxiv.org/abs/2301.00001",
            }
        }

        enriched = enrich_candidate_rows(candidates, raw_by_key, metadata)
        self.assertEqual(len(enriched), 1)
        row = enriched[0]
        self.assertEqual(row["arxiv_id"], "2301.00001")
        self.assertEqual(row["arxiv_doi_matches_published"], "1")
        self.assertEqual(row["arxiv_current_title_matches_published"], "1")
        self.assertEqual(
            row["arxiv_evidence"],
            "arxiv_doi_matches_published;arxiv_current_title_matches_published",
        )
        self.assertEqual(row["reason"], "review_only_preprint_published_candidate")


if __name__ == "__main__":
    unittest.main()
