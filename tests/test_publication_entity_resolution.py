from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

from lxml import etree

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from resolve_publication_entities import resolve_rows
from scan_dblp_publications import publication_fields


BASE_FIELDS = {
    "canonical_name": "Alice Example",
    "venue": "",
    "venue_label": "CoRR",
    "record_type": "article",
    "publtype": "informal",
    "booktitle": "",
    "journal": "CoRR",
    "streams": "",
    "url": "",
    "author_signature": "Alice Example",
    "author_orcid": "",
    "author_position": "1",
    "author_count": "2",
    "match_basis": "dblp_author_pid",
    "snapshot_id": "dblp-test",
}


def row(
    *,
    pid: str,
    key: str,
    title: str,
    year: int,
    authors: list[str],
    scope: str = "side",
    venue: str = "",
    venue_label: str = "CoRR",
    journal: str = "CoRR",
    record_type: str = "article",
    publtype: str = "informal",
    ees: list[str] | None = None,
    rels: list[dict[str, str]] | None = None,
) -> dict[str, str]:
    result = dict(BASE_FIELDS)
    result.update(
        {
            "dblp_pid": pid,
            "scope": scope,
            "venue": venue,
            "venue_label": venue_label,
            "year": str(year),
            "title": title,
            "record_type": record_type,
            "publtype": publtype,
            "journal": journal,
            "dblp_key": key,
            "authors_json": json.dumps(authors),
            "ees_json": json.dumps(ees or []),
            "rels_json": json.dumps(rels or []),
        }
    )
    return result


class ScannerMetadataTests(unittest.TestCase):
    def test_publication_fields_preserves_authors_ees_and_version_relation(self) -> None:
        elem = etree.fromstring(
            b"""
            <inproceedings key="conf/iclr/Example24">
              <author>Alice Example</author>
              <author>Bob Example</author>
              <title>A Final Paper.</title>
              <year>2024</year>
              <booktitle>ICLR</booktitle>
              <ee>https://doi.org/10.1000/example</ee>
              <rel type="versionOf" uri="https://dblp.org/rec/journals/corr/abs-2312-00001" label="2" sort="2" />
              <stream>streams/conf/iclr</stream>
            </inproceedings>
            """
        )
        record = publication_fields(elem)
        self.assertIsNotNone(record)
        assert record is not None
        self.assertEqual(record["authors"], ["Alice Example", "Bob Example"])
        self.assertEqual(record["ees"], ["https://doi.org/10.1000/example"])
        self.assertEqual(record["rels"][0]["type"], "versionOf")
        self.assertEqual(
            record["rels"][0]["target"],
            "https://dblp.org/rec/journals/corr/abs-2312-00001",
        )


class PublicationEntityResolutionTests(unittest.TestCase):
    def test_title_fallbacks_cannot_bridge_native_archival_components(self) -> None:
        for marker in ("", " (Extended Version)"):
            with self.subTest(marker=marker):
                rows = [
                    row(pid="p1", key="journals/corr/abs-2401-00001",
                        title="Article B" + marker, year=2024,
                        authors=["Alice Example"],
                        rels=[{"type": "versionOf", "target": "conf/sigmod/A24"}]),
                    row(pid="p1", key="conf/sigmod/A24", title="Article A", year=2024,
                        authors=["Alice Example"], journal="", publtype="",
                        record_type="inproceedings"),
                    row(pid="p1", key="conf/sigmod/B24", title="Article B", year=2024,
                        authors=["Alice Example"], journal="", publtype="",
                        record_type="inproceedings"),
                ]
                resolved, mapping, _, stats = resolve_rows(rows)
                work_ids = {item["dblp_key"]: item["work_id"] for item in mapping}
                self.assertEqual(stats["work_entities"], 2)
                self.assertEqual(work_ids[rows[0]["dblp_key"]], work_ids["conf/sigmod/A24"])
                self.assertNotEqual(work_ids["conf/sigmod/A24"], work_ids["conf/sigmod/B24"])
                self.assertEqual(len(resolved), 2)

    def test_arxiv_links_recheck_archival_membership_after_root_changes(self) -> None:
        rows = [
            row(pid="p1", key=f"journals/corr/abs-2401-0000{i}",
                title=f"Preprint {i}", year=2024, authors=["Alice Example"],
                rels=[{"type": "versionOf", "target": "abstract/shared"}])
            for i in (1, 2, 3)
        ]
        rows.extend([
            row(pid="p1", key=f"conf/sigmod/{letter}24", title=f"Article {letter}",
                year=2024, authors=["Alice Example"], journal="", publtype="",
                record_type="inproceedings", ees=[f"https://doi.org/10.1000/{letter}"])
            for letter in ("A", "B")
        ])
        metadata = {
            "2401.00001": {"status": "found", "title": "Article A", "doi": "10.1000/A"},
            "2401.00002": {"status": "found", "title": "Article B", "doi": "10.1000/B"},
            "2401.00003": {"status": "found", "title": "Article A", "doi": "10.1000/A"},
        }
        _, mapping, _, stats = resolve_rows(rows, arxiv_metadata=metadata)
        work_ids = {item["dblp_key"]: item["work_id"] for item in mapping}
        self.assertEqual(stats["work_entities"], 2)
        self.assertNotEqual(work_ids["conf/sigmod/A24"], work_ids["conf/sigmod/B24"])
        for preprint in rows[:3]:
            self.assertEqual(work_ids[preprint["dblp_key"]], work_ids["conf/sigmod/A24"])

    def test_native_evidence_can_still_merge_archival_records(self) -> None:
        for evidence in ("relation", "doi"):
            with self.subTest(evidence=evidence):
                rows = [
                    row(pid="p1", key=f"conf/sigmod/{letter}24", title=f"Article {letter}",
                        year=2024, authors=["Alice Example"], journal="", publtype="",
                        record_type="inproceedings")
                    for letter in ("A", "B")
                ]
                if evidence == "relation":
                    rows[0]["rels_json"] = json.dumps([
                        {"type": "versionOf", "target": "conf/sigmod/B24"}])
                else:
                    for item in rows:
                        item["ees_json"] = json.dumps(["https://doi.org/10.1000/shared"])
                self.assertEqual(resolve_rows(rows)[3]["work_entities"], 1)

    def test_exact_title_and_author_set_merge_preprint_into_archival_record(self) -> None:
        rows = [
            row(
                pid="p1",
                key="journals/corr/abs-2301-00001",
                title="Learned Cardinality Estimation: A Practical Study.",
                year=2023,
                authors=["Alice Example", "Bob Example"],
                ees=["https://arxiv.org/abs/2301.00001"],
            ),
            row(
                pid="p1",
                key="conf/sigmod/Example24",
                title="Learned Cardinality Estimation - A Practical Study",
                year=2024,
                authors=["Bob Example", "Alice Example"],
                venue_label="SIGMOD",
                journal="",
                record_type="inproceedings",
                publtype="",
                ees=["https://doi.org/10.1000/example24"],
            ),
        ]

        resolved, mapping, candidates, stats = resolve_rows(rows)
        self.assertEqual(stats["work_entities"], 1)
        self.assertEqual(stats["merged_records"], 1)
        self.assertEqual(stats["exact_title_author_components"], 1)
        self.assertEqual(candidates, [])
        self.assertEqual(len(resolved), 1)
        self.assertEqual(resolved[0]["dblp_key"], "conf/sigmod/Example24")
        versions = json.loads(resolved[0]["publication_versions_json"])
        self.assertEqual([item["dblpKey"] for item in versions], [
            "conf/sigmod/Example24",
            "journals/corr/abs-2301-00001",
        ])
        self.assertFalse(versions[0]["isPreprint"])
        self.assertTrue(versions[1]["isPreprint"])
        self.assertEqual(versions[0]["ees"], ["https://doi.org/10.1000/example24"])
        self.assertEqual(versions[1]["ees"], ["https://arxiv.org/abs/2301.00001"])
        self.assertEqual(versions[1]["arxivId"], "2301.00001")
        self.assertTrue(
            all(
                item["resolution_basis"]
                == "exact_title_authors_preprint_published"
                for item in mapping
            )
        )

    def test_multiple_preprints_can_merge_into_one_archival_destination(self) -> None:
        rows = [
            row(
                pid="p1",
                key="journals/corr/abs-2201-00001",
                title="One Scientific Work",
                year=2022,
                authors=["Alice Example", "Bob Example"],
            ),
            row(
                pid="p1",
                key="journals/corr/abs-2301-00001",
                title="One Scientific Work",
                year=2023,
                authors=["Alice Example", "Bob Example"],
            ),
            row(
                pid="p1",
                key="conf/sigmod/Example24",
                title="One Scientific Work",
                year=2024,
                authors=["Bob Example", "Alice Example"],
                venue_label="SIGMOD",
                journal="",
                record_type="inproceedings",
                publtype="",
            ),
        ]

        resolved, mapping, candidates, stats = resolve_rows(rows)
        self.assertEqual(stats["work_entities"], 1)
        self.assertEqual(stats["merged_records"], 2)
        self.assertEqual(stats["exact_title_author_components"], 1)
        self.assertEqual(candidates, [])
        self.assertEqual(len(resolved), 1)
        self.assertEqual(resolved[0]["dblp_key"], "conf/sigmod/Example24")
        self.assertTrue(all(item["component_size"] == "3" for item in mapping))

    def test_exact_title_multiple_archival_destinations_are_review_only(self) -> None:
        rows = [
            row(
                pid="p1",
                key="journals/corr/abs-2301-00001",
                title="Bridging Should Not Collapse Archival Papers",
                year=2023,
                authors=["Alice Example", "Bob Example"],
            ),
            row(
                pid="p1",
                key="conf/nips/Example24",
                title="Bridging Should Not Collapse Archival Papers",
                year=2024,
                authors=["Alice Example", "Bob Example"],
                scope="main",
                venue="NeurIPS",
                venue_label="NeurIPS",
                journal="",
                record_type="inproceedings",
                publtype="",
            ),
            row(
                pid="p1",
                key="journals/example/Example25",
                title="Bridging Should Not Collapse Archival Papers",
                year=2025,
                authors=["Alice Example", "Bob Example"],
                venue_label="Example Journal",
                journal="Example Journal",
                record_type="article",
                publtype="",
            ),
        ]

        resolved, mapping, candidates, stats = resolve_rows(rows)
        self.assertEqual(stats["work_entities"], 3)
        self.assertEqual(stats["merged_records"], 0)
        self.assertEqual(stats["exact_title_author_components"], 0)
        self.assertEqual(stats["ambiguous_exact_title_candidates"], 2)
        self.assertEqual(stats["renamed_title_candidates"], 0)
        self.assertEqual(len(resolved), 3)
        self.assertTrue(all(item["component_size"] == "1" for item in mapping))
        self.assertEqual(
            {item["reason"] for item in candidates},
            {"ambiguous_exact_title_multiple_archival_publications"},
        )
        self.assertEqual(
            {item["published_key"] for item in candidates},
            {"conf/nips/Example24", "journals/example/Example25"},
        )

    def test_repeated_signature_far_apart_keeps_unambiguous_local_merge(self) -> None:
        rows = [
            row(
                pid="p1",
                key="journals/corr/abs-2001-00001",
                title="A Reused Exact Title",
                year=2020,
                authors=["Alice Example", "Bob Example"],
            ),
            row(
                pid="p1",
                key="conf/sigmod/Example21",
                title="A Reused Exact Title",
                year=2021,
                authors=["Alice Example", "Bob Example"],
                venue_label="SIGMOD",
                journal="",
                record_type="inproceedings",
                publtype="",
            ),
            row(
                pid="p1",
                key="journals/example/Example30",
                title="A Reused Exact Title",
                year=2030,
                authors=["Alice Example", "Bob Example"],
                venue_label="Example Journal",
                journal="Example Journal",
                record_type="article",
                publtype="",
            ),
        ]

        resolved, mapping, candidates, stats = resolve_rows(rows)
        self.assertEqual(stats["work_entities"], 2)
        self.assertEqual(stats["merged_records"], 1)
        self.assertEqual(stats["exact_title_author_components"], 1)
        self.assertEqual(stats["ambiguous_exact_title_candidates"], 0)
        self.assertEqual(candidates, [])
        component_sizes = sorted(int(item["component_size"]) for item in mapping)
        self.assertEqual(component_sizes, [1, 2, 2])
        self.assertEqual(len(resolved), 2)

    def test_literal_preprint_version_marker_is_automatic_one_to_one(self) -> None:
        rows = [
            row(
                pid="p1",
                key="journals/corr/abs-2301-00001",
                title="A Paper Title (Extended Version)",
                year=2023,
                authors=["Alice Example", "Bob Example"],
            ),
            row(
                pid="p1",
                key="conf/sigmod/Example24",
                title="A Paper Title.",
                year=2024,
                authors=["Alice Example", "Bob Example"],
                venue_label="SIGMOD",
                journal="",
                record_type="inproceedings",
                publtype="",
            ),
        ]

        resolved, mapping, candidates, stats = resolve_rows(rows)
        self.assertEqual(stats["work_entities"], 1)
        self.assertEqual(stats["merged_records"], 1)
        self.assertEqual(stats["explicit_preprint_marker_components"], 1)
        self.assertEqual(candidates, [])
        self.assertEqual(len(resolved), 1)
        self.assertEqual(resolved[0]["dblp_key"], "conf/sigmod/Example24")
        self.assertTrue(
            all(
                item["resolution_basis"] == "explicit_preprint_version_marker"
                for item in mapping
            )
        )

    def test_preprint_version_marker_requires_one_to_one_pairing(self) -> None:
        rows = [
            row(
                pid="p1",
                key="journals/corr/abs-2301-00001",
                title="A Paper Title (full version)",
                year=2023,
                authors=["Alice Example", "Bob Example"],
            ),
            row(
                pid="p1",
                key="conf/sigmod/Example24",
                title="A Paper Title",
                year=2024,
                authors=["Alice Example", "Bob Example"],
                venue_label="SIGMOD",
                journal="",
                record_type="inproceedings",
                publtype="",
            ),
            row(
                pid="p1",
                key="journals/example/Example25",
                title="A Paper Title",
                year=2025,
                authors=["Alice Example", "Bob Example"],
                venue_label="Example Journal",
                journal="Example Journal",
                record_type="article",
                publtype="",
            ),
        ]

        resolved, _mapping, candidates, stats = resolve_rows(rows)
        self.assertEqual(stats["work_entities"], 3)
        self.assertEqual(stats["merged_records"], 0)
        self.assertEqual(stats["explicit_preprint_marker_components"], 0)
        self.assertEqual(len(resolved), 3)
        self.assertEqual(len(candidates), 2)
        self.assertTrue(
            all(
                item["reason"] == "review_only_preprint_published_candidate"
                for item in candidates
            )
        )

    def test_marker_followed_by_punctuation_remains_review_only(self) -> None:
        rows = [
            row(
                pid="p1",
                key="journals/corr/abs-2301-00001",
                title="A Paper Title (Extended Version).",
                year=2023,
                authors=["Alice Example", "Bob Example"],
            ),
            row(
                pid="p1",
                key="conf/sigmod/Example24",
                title="A Paper Title.",
                year=2024,
                authors=["Alice Example", "Bob Example"],
                venue_label="SIGMOD",
                journal="",
                record_type="inproceedings",
                publtype="",
            ),
        ]

        resolved, _mapping, candidates, stats = resolve_rows(rows)
        self.assertEqual(stats["work_entities"], 2)
        self.assertEqual(stats["merged_records"], 0)
        self.assertEqual(stats["explicit_preprint_marker_components"], 0)
        self.assertEqual(len(resolved), 2)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(
            candidates[0]["reason"], "review_only_preprint_published_candidate"
        )

    def test_arxiv_doi_merges_unique_archival_target_with_shared_author(self) -> None:
        rows = [
            row(
                pid="p1",
                key="journals/corr/abs-2301-00001",
                title="An Early Preprint Title",
                year=2023,
                authors=["Alice Example", "Bob Example"],
                ees=["https://arxiv.org/abs/2301.00001"],
            ),
            row(
                pid="p1",
                key="conf/sigmod/Final24",
                title="A Completely Renamed Final Paper",
                year=2024,
                authors=["Alice Example", "Carol Example"],
                venue_label="SIGMOD",
                journal="",
                record_type="inproceedings",
                publtype="",
                ees=["https://doi.org/10.1000/final.1"],
            ),
        ]
        metadata = {
            "2301.00001": {
                "status": "found",
                "title": "Still Another Title",
                "doi": "10.1000/final.1",
            }
        }

        resolved, mapping, candidates, stats = resolve_rows(
            rows, arxiv_metadata=metadata
        )
        self.assertEqual(stats["work_entities"], 1)
        self.assertEqual(stats["arxiv_evidence_components"], 1)
        self.assertEqual(stats["arxiv_doi_components"], 1)
        self.assertEqual(stats["arxiv_current_title_components"], 0)
        self.assertEqual(candidates, [])
        self.assertEqual(len(resolved), 1)
        self.assertTrue(all("arxiv_doi" in item["resolution_basis"] for item in mapping))

    def test_arxiv_doi_rejects_unrelated_zero_author_overlap(self) -> None:
        rows = [
            row(
                pid="p1",
                key="journals/corr/abs-1807-09320",
                title="Constant-Delay Enumeration for Nondeterministic Document Spanners",
                year=2018,
                authors=["Alice Example", "Bob Example"],
                ees=["https://arxiv.org/abs/1807.09320"],
            ),
            row(
                pid="p1",
                key="conf/icdt/Unrelated19",
                title="Additive First-Order Queries",
                year=2019,
                authors=["Carol Example", "Dave Example"],
                venue_label="ICDT",
                journal="",
                record_type="inproceedings",
                publtype="",
                ees=["https://doi.org/10.1000/unrelated"],
            ),
        ]
        metadata = {
            "1807.09320": {
                "status": "found",
                "title": rows[0]["title"],
                "doi": "10.1000/unrelated",
            }
        }

        _resolved, _mapping, _candidates, stats = resolve_rows(
            rows, arxiv_metadata=metadata
        )
        self.assertEqual(stats["work_entities"], 2)
        self.assertEqual(stats["arxiv_evidence_components"], 0)

    def test_arxiv_doi_rejects_small_subset_of_large_author_target(self) -> None:
        preprint_authors = [
            "Fabian Lehmann",
            "Jonathan Bader",
            "Lauritz Thamsen",
            "Ulf Leser",
        ]
        target_authors = preprint_authors + [f"Collaborator {i}" for i in range(30)]
        rows = [
            row(
                pid="p1",
                key="journals/corr/abs-2311-15929",
                title="The Common Workflow Scheduler Interface: Status Quo and Future Plans.",
                year=2023,
                authors=preprint_authors,
                ees=["https://arxiv.org/abs/2311.15929"],
            ),
            row(
                pid="p1",
                key="conf/sc/Workflow23",
                title="Novel Approaches Toward Scalable Composable Workflows in Hyper-Heterogeneous Computing Environments.",
                year=2023,
                authors=target_authors,
                venue_label="SC Workshops",
                journal="",
                record_type="inproceedings",
                publtype="",
                ees=["https://doi.org/10.1145/3624062.3626283"],
            ),
        ]
        metadata = {
            "2311.15929": {
                "status": "found",
                "title": rows[0]["title"],
                "doi": "10.1145/3624062.3626283",
            }
        }

        _resolved, _mapping, _candidates, stats = resolve_rows(
            rows, arxiv_metadata=metadata
        )
        self.assertEqual(stats["work_entities"], 2)
        self.assertEqual(stats["arxiv_evidence_components"], 0)
        self.assertEqual(stats["arxiv_doi_components"], 0)

    def test_arxiv_doi_exact_current_title_can_override_author_overlap_guard(self) -> None:
        rows = [
            row(
                pid="p1",
                key="journals/corr/abs-2301-00009",
                title="Earlier Working Title",
                year=2023,
                authors=["Alice Example"],
                ees=["https://arxiv.org/abs/2301.00009"],
            ),
            row(
                pid="p1",
                key="conf/sigmod/ExactTitle24",
                title="Final Exact Current Title",
                year=2024,
                authors=["Alice Example", "Bob Example", "Carol Example"],
                venue_label="SIGMOD",
                journal="",
                record_type="inproceedings",
                publtype="",
                ees=["https://doi.org/10.1000/exact-title"],
            ),
        ]
        metadata = {
            "2301.00009": {
                "status": "found",
                "title": "Final Exact Current Title",
                "doi": "10.1000/exact-title",
            }
        }

        _resolved, _mapping, candidates, stats = resolve_rows(
            rows, arxiv_metadata=metadata
        )
        self.assertEqual(stats["work_entities"], 1)
        self.assertEqual(stats["arxiv_doi_components"], 1)
        self.assertEqual(candidates, [])

    def test_arxiv_current_title_exact_merges_unique_global_title_author_target(self) -> None:
        rows = [
            row(
                pid="p1",
                key="journals/corr/abs-2301-00001",
                title="Earlier Working Title",
                year=2023,
                authors=["Alice Example", "Bob Example"],
                ees=["https://arxiv.org/abs/2301.00001"],
            ),
            row(
                pid="p1",
                key="conf/icml/Final24",
                title="Final Published Title",
                year=2024,
                authors=["Bob Example", "Alice Example"],
                scope="main",
                venue="ICML",
                venue_label="ICML",
                journal="",
                record_type="inproceedings",
                publtype="",
            ),
        ]
        metadata = {
            "2301.00001": {
                "status": "found",
                "title": "Final Published Title.",
                "doi": "",
            }
        }

        resolved, mapping, candidates, stats = resolve_rows(
            rows, arxiv_metadata=metadata
        )
        self.assertEqual(stats["work_entities"], 1)
        self.assertEqual(stats["arxiv_doi_components"], 0)
        self.assertEqual(stats["arxiv_current_title_components"], 1)
        self.assertEqual(candidates, [])
        self.assertEqual(resolved[0]["dblp_key"], "conf/icml/Final24")
        self.assertTrue(
            all("arxiv_current_title_exact" in item["resolution_basis"] for item in mapping)
        )

    def test_arxiv_current_title_requires_globally_unique_archival_target(self) -> None:
        rows = [
            row(
                pid="p1",
                key="journals/corr/abs-2301-00001",
                title="Earlier Working Title",
                year=2023,
                authors=["Alice Example", "Bob Example"],
                ees=["https://arxiv.org/abs/2301.00001"],
            ),
            row(
                pid="p1",
                key="conf/icml/Final24",
                title="Final Published Title",
                year=2024,
                authors=["Alice Example", "Bob Example"],
                venue_label="ICML",
                journal="",
                record_type="inproceedings",
                publtype="",
            ),
            row(
                pid="p1",
                key="journals/example/Final25",
                title="Final Published Title",
                year=2025,
                authors=["Alice Example", "Bob Example"],
                venue_label="Example Journal",
                journal="Example Journal",
                record_type="article",
                publtype="",
            ),
        ]
        metadata = {
            "2301.00001": {
                "status": "found",
                "title": "Final Published Title",
                "doi": "",
            }
        }

        _resolved, _mapping, _candidates, stats = resolve_rows(
            rows, arxiv_metadata=metadata
        )
        self.assertEqual(stats["work_entities"], 3)
        self.assertEqual(stats["arxiv_current_title_components"], 0)

    def test_arxiv_doi_and_current_title_conflict_abstains(self) -> None:
        rows = [
            row(
                pid="p1",
                key="journals/corr/abs-2301-00001",
                title="Earlier Working Title",
                year=2023,
                authors=["Alice Example", "Bob Example"],
                ees=["https://arxiv.org/abs/2301.00001"],
            ),
            row(
                pid="p1",
                key="conf/sigmod/Doi24",
                title="DOI Target",
                year=2024,
                authors=["Alice Example", "Carol Example"],
                venue_label="SIGMOD",
                journal="",
                record_type="inproceedings",
                publtype="",
                ees=["https://doi.org/10.1000/doi-target"],
            ),
            row(
                pid="p1",
                key="conf/icml/Title24",
                title="Current arXiv Title",
                year=2024,
                authors=["Alice Example", "Bob Example"],
                venue_label="ICML",
                journal="",
                record_type="inproceedings",
                publtype="",
            ),
        ]
        metadata = {
            "2301.00001": {
                "status": "found",
                "title": "Current arXiv Title",
                "doi": "10.1000/doi-target",
            }
        }

        _resolved, _mapping, _candidates, stats = resolve_rows(
            rows, arxiv_metadata=metadata
        )
        self.assertEqual(stats["work_entities"], 3)
        self.assertEqual(stats["arxiv_evidence_components"], 0)

    def test_arxiv_evidence_does_not_override_existing_dblp_native_merge(self) -> None:
        rows = [
            row(
                pid="p1",
                key="journals/corr/abs-2301-00001",
                title="Native Match",
                year=2023,
                authors=["Alice Example", "Bob Example"],
                ees=["https://arxiv.org/abs/2301.00001"],
            ),
            row(
                pid="p1",
                key="conf/sigmod/Native24",
                title="Native Match",
                year=2024,
                authors=["Alice Example", "Bob Example"],
                venue_label="SIGMOD",
                journal="",
                record_type="inproceedings",
                publtype="",
            ),
            row(
                pid="p1",
                key="journals/example/Other24",
                title="Other Publication",
                year=2024,
                authors=["Alice Example", "Carol Example"],
                venue_label="Example Journal",
                journal="Example Journal",
                record_type="article",
                publtype="",
                ees=["https://doi.org/10.1000/other"],
            ),
        ]
        metadata = {
            "2301.00001": {
                "status": "found",
                "title": "Other Publication",
                "doi": "10.1000/other",
            }
        }

        _resolved, mapping, _candidates, stats = resolve_rows(
            rows, arxiv_metadata=metadata
        )
        self.assertEqual(stats["work_entities"], 2)
        self.assertEqual(stats["exact_title_author_components"], 1)
        self.assertEqual(stats["arxiv_evidence_components"], 0)
        native = [item for item in mapping if item["dblp_key"] != "journals/example/Other24"]
        self.assertTrue(
            all("exact_title_authors_preprint_published" in item["resolution_basis"] for item in native)
        )

    def test_exact_title_does_not_merge_when_author_sets_differ(self) -> None:
        rows = [
            row(
                pid="p1",
                key="journals/corr/abs-2301-00001",
                title="Exactly the Same Paper Title",
                year=2023,
                authors=["Alice Example", "Bob Example"],
            ),
            row(
                pid="p1",
                key="conf/sigmod/Example24",
                title="Exactly the Same Paper Title",
                year=2024,
                authors=["Alice Example"],
                venue_label="SIGMOD",
                journal="",
                record_type="inproceedings",
                publtype="",
            ),
        ]

        resolved, _mapping, candidates, stats = resolve_rows(rows)
        self.assertEqual(stats["work_entities"], 2)
        self.assertEqual(stats["exact_title_author_components"], 0)
        self.assertEqual(len(resolved), 2)
        self.assertEqual(candidates, [])

    def test_version_relation_merges_but_does_not_propagate_canonical_authorship(self) -> None:
        rel = [{"type": "versionOf", "target": "conf/nips/Final24"}]
        rows = [
            row(
                pid="p1",
                key="journals/corr/abs-2312-00001",
                title="Old Preprint Title",
                year=2023,
                authors=["Alice Example", "Bob Example"],
                rels=rel,
            ),
            row(
                pid="p2",
                key="journals/corr/abs-2312-00001",
                title="Old Preprint Title",
                year=2023,
                authors=["Alice Example", "Bob Example"],
                rels=rel,
            ),
            row(
                pid="p1",
                key="conf/nips/Final24",
                title="Renamed Final Paper",
                year=2024,
                authors=["Alice Example"],
                scope="main",
                venue="NeurIPS",
                venue_label="NeurIPS",
                journal="",
                record_type="inproceedings",
                publtype="",
            ),
        ]

        resolved, mapping, candidates, stats = resolve_rows(rows)
        self.assertEqual(stats["work_entities"], 1)
        self.assertEqual(stats["merged_records"], 1)
        self.assertEqual(stats["version_relation_components"], 1)
        self.assertEqual(candidates, [])

        by_pid = {item["dblp_pid"]: item for item in resolved}
        self.assertEqual(by_pid["p1"]["dblp_key"], "conf/nips/Final24")
        self.assertEqual(by_pid["p1"]["scope"], "main")
        # p2 never appears on the final record, so the resolver must not claim
        # that p2 authored the NeurIPS version.
        self.assertEqual(by_pid["p2"]["dblp_key"], "journals/corr/abs-2312-00001")
        self.assertEqual(by_pid["p2"]["scope"], "side")
        self.assertEqual(by_pid["p1"]["work_id"], by_pid["p2"]["work_id"])
        self.assertTrue(all(item["component_size"] == "2" for item in mapping))

    def test_shared_doi_is_an_automatic_merge(self) -> None:
        rows = [
            row(
                pid="p1",
                key="journals/corr/abs-2401-00001",
                title="Same DOI Draft",
                year=2024,
                authors=["Alice Example"],
                ees=["https://doi.org/10.5555/SAME"],
            ),
            row(
                pid="p1",
                key="journals/example/Final24",
                title="Same DOI Final",
                year=2024,
                authors=["Alice Example"],
                journal="Example Journal",
                publtype="",
                ees=["https://doi.org/10.5555/same"],
            ),
        ]
        resolved, _mapping, _candidates, stats = resolve_rows(rows)
        self.assertEqual(stats["work_entities"], 1)
        self.assertEqual(stats["shared_doi_components"], 1)
        self.assertEqual(len(resolved), 1)
        self.assertEqual(resolved[0]["dblp_key"], "journals/example/Final24")

    def test_title_author_similarity_is_review_only(self) -> None:
        rows = [
            row(
                pid="p1",
                key="journals/corr/abs-2301-00001",
                title="Learning Cardinality Estimation with Deep Models",
                year=2023,
                authors=["Alice Example", "Bob Example"],
            ),
            row(
                pid="p1",
                key="conf/sigmod/Example24",
                title="Deep Models for Cardinality Estimation",
                year=2024,
                authors=["Alice Example", "Bob Example"],
                venue_label="SIGMOD",
                journal="",
                record_type="inproceedings",
                publtype="",
            ),
        ]
        resolved, _mapping, candidates, stats = resolve_rows(rows)
        self.assertEqual(stats["work_entities"], 2)
        self.assertEqual(stats["merged_records"], 0)
        self.assertEqual(len(resolved), 2)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(stats["ambiguous_exact_title_candidates"], 0)
        self.assertEqual(stats["renamed_title_candidates"], 1)
        self.assertEqual(candidates[0]["reason"], "review_only_preprint_published_candidate")

    def test_review_candidate_requires_exact_author_set(self) -> None:
        rows = [
            row(
                pid="p1",
                key="journals/corr/abs-2301-00001",
                title="Learning Cardinality Estimation with Deep Models",
                year=2023,
                authors=["Alice Example", "Bob Example"],
            ),
            row(
                pid="p1",
                key="conf/sigmod/Example24",
                title="Deep Models for Cardinality Estimation",
                year=2024,
                authors=["Alice Example"],
                venue_label="SIGMOD",
                journal="",
                record_type="inproceedings",
                publtype="",
            ),
        ]

        _resolved, _mapping, candidates, stats = resolve_rows(rows)
        self.assertEqual(stats["work_entities"], 2)
        self.assertEqual(candidates, [])

    def test_review_candidate_requires_publication_within_two_years(self) -> None:
        rows = [
            row(
                pid="p1",
                key="journals/corr/abs-2201-00001",
                title="Learning Cardinality Estimation with Deep Models",
                year=2022,
                authors=["Alice Example", "Bob Example"],
            ),
            row(
                pid="p1",
                key="conf/sigmod/Example25",
                title="Deep Models for Cardinality Estimation",
                year=2025,
                authors=["Alice Example", "Bob Example"],
                venue_label="SIGMOD",
                journal="",
                record_type="inproceedings",
                publtype="",
            ),
        ]

        _resolved, _mapping, candidates, stats = resolve_rows(rows)
        self.assertEqual(stats["work_entities"], 2)
        self.assertEqual(candidates, [])


if __name__ == "__main__":
    unittest.main()
