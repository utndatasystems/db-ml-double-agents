from __future__ import annotations

import csv
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))
import build_site_data as site_builder

from build_site_data import (
    artifact_fingerprint,
    combined_artifact_fingerprint,
    arxiv_cache_refreshed_at,
    latest_reviewer_fetch_at,
    browser_payload_and_detail_files,
    BROWSER_DETAIL_SCHEMA_VERSION,
    BROWSER_SEARCH_SCHEMA_VERSION,
    assert_pipeline_freshness,
    build_payload,
    choose_name,
    choose_primary_affiliation,
    choose_primary_affiliation_evidence,
    primary_affiliation_review_rows,
    PUBLICATION_CLASS_LABELS,
    PROVENANCE_SCHEMA_VERSION,
    SCHEMA_VERSION,
    classify_publication,
    load_db_people,
    load_publications,
)


class BrowserExportRecoveryTests(unittest.TestCase):
    def generation(self, title):
        person = {"id": "alice", "dblpPid": "alice", "provenance": {"identity": {"note": title}},
                  "ml": {"papers": [{"scope": "side", "title": title}]}}
        return browser_payload_and_detail_files({"people": [person]})

    def references(self, root, payload):
        urls = [person["ml"]["detailUrl"] for person in payload["people"] if person["ml"].get("detailUrl")]
        urls.extend(person["evidenceUrl"] for person in payload["people"] if person.get("evidenceUrl"))
        if payload.get("meta", {}).get("publicationSearchUrl"):
            urls.append(payload["meta"]["publicationSearchUrl"])
        return [root / url.removeprefix("data/") for url in urls]

    def test_failed_asset_or_manifest_promotion_preserves_previous_manifest_and_references(self):
        for failure_stage in ("detail", "search", "manifest"):
            with self.subTest(stage=failure_stage), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                old, old_details, old_search = self.generation("Old publication title")
                new, new_details, new_search = self.generation("Corrected publication title")
                site_builder.write_browser_export(root, old, old_details, old_search)
                old_js = (root / "site_data.js").read_bytes()
                original_replace = Path.replace

                def fail_selected_promotion(path, target):
                    is_detail = path.parent.name == "people"
                    is_search = path.name.startswith("publication_search.")
                    is_manifest = path.name == "site_data.js.tmp"
                    if {"detail": is_detail, "search": is_search, "manifest": is_manifest}[failure_stage]:
                        raise OSError("simulated interrupted " + failure_stage + " promotion")
                    return original_replace(path, target)

                with patch.object(Path, "replace", fail_selected_promotion):
                    with self.assertRaisesRegex(OSError, "simulated interrupted"):
                        site_builder.write_browser_export(root, new, new_details, new_search)
                self.assertEqual((root / "site_data.js").read_bytes(), old_js)
                self.assertTrue(all(path.exists() for path in self.references(root, old)))

                site_builder.write_browser_export(root, new, new_details, new_search)
                self.assertTrue(all(path.exists() for path in self.references(root, new)))
                self.assertTrue(all(path.exists() for path in self.references(root, old)))
                self.assertNotEqual((root / "site_data.js").read_bytes(), old_js)

    def test_success_retains_one_previous_generation_then_prunes_older_unreferenced_assets(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            first = self.generation("First publication title")
            second = self.generation("Second publication title")
            third = self.generation("Third publication title")
            for generation in (first, second):
                site_builder.write_browser_export(root, *generation)
            site_builder.write_browser_export(root, *second)
            self.assertTrue(all(path.exists() for path in self.references(root, first[0])),
                            "Repeating the same generation must preserve its prior-generation assets")
            site_builder.write_browser_export(root, *third)
            self.assertTrue(all(path.exists() for path in self.references(root, second[0])))
            self.assertTrue(all(path.exists() for path in self.references(root, third[0])))
            self.assertTrue(all(not path.exists() for path in self.references(root, first[0])))

    def test_unreadable_previous_manifest_preserves_assets_instead_of_guessing_references(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            old = self.generation("Old publication title")
            new = self.generation("Corrected publication title")
            site_builder.write_browser_export(root, *old)
            (root / "site_data.js").write_text("window.CROSSOVER_DATA=broken;", encoding="utf-8")
            site_builder.write_browser_export(root, *new)
            self.assertTrue(all(path.exists() for path in self.references(root, old[0])))
            self.assertTrue(all(path.exists() for path in self.references(root, new[0])))


class PublicationVersionOwnershipTests(unittest.TestCase):
    def test_loader_preserves_person_version_keys_and_complete_component_metadata(self):
        versions = [{'dblpKey': 'conf/icde/paper', 'booktitle': 'ICDE', 'title': 'Research', 'year': 2020}]
        base = {'dblp_pid': 'a', 'dblp_key': 'journals/corr/preprint', 'title': 'Preprint', 'year': '2020',
                'scope': 'side', 'work_id': 'work', 'publication_versions_json': json.dumps(versions)}
        for ownership in [None, '', '[]', '["journals/corr/preprint"]',
                          '["journals/corr/preprint", "conf/icde/paper"]']:
            with self.subTest(ownership=ownership), tempfile.TemporaryDirectory() as td:
                row = dict(base)
                if ownership is not None:
                    row['person_version_dblp_keys'] = ownership
                path = Path(td) / 'publications.csv'
                with path.open('w', encoding='utf-8', newline='') as handle:
                    writer = csv.DictWriter(handle, fieldnames=list(row))
                    writer.writeheader()
                    writer.writerow(row)
                people, _, _ = load_publications(path, {'a'})
                paper = people['a']['papers']['work']
                self.assertEqual(paper['resolvedVersions'], versions)
                if ownership:
                    self.assertEqual(paper['personVersionDblpKeys'], json.loads(ownership))
                else:
                    self.assertNotIn('personVersionDblpKeys', paper)


class BuildFreshnessTests(unittest.TestCase):
    def test_service_only_edit_reuses_verified_scan_but_changed_targets_or_bytes_do_not(self):
        from scan_dblp_publications import SNAPSHOT_FILENAME, TargetPerson, write_scan_cache

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            entity = root / "entity_resolution.sqlite"
            raw = root / "raw.csv"
            totals = root / "totals.csv"
            resolved = root / "resolved.csv"
            snapshot = root / "dblp_snapshot" / SNAPSHOT_FILENAME
            snapshot.parent.mkdir()
            snapshot.write_bytes(b"pinned test snapshot")
            for path in (entity, raw, totals, resolved):
                path.write_text("original", encoding="utf-8")
            for path in (raw, totals):
                os.utime(path, (100, 100))
            os.utime(entity, (200, 200))
            os.utime(resolved, (300, 300))
            targets = [TargetPerson("1/2", "Person", ("Person",), ())]
            write_scan_cache(raw, totals, snapshot, targets, row_count=1, stats={})

            def check():
                assert_pipeline_freshness(
                    root, entity_db=entity, publications_csv=resolved,
                    raw_publications_csv=raw, main_totals_csv=totals,
                )

            with patch("scan_dblp_publications.load_targets", return_value=targets):
                check()
            changed = [TargetPerson("3/4", "Person", ("Person",), ())]
            with patch("scan_dblp_publications.load_targets", return_value=changed):
                with self.assertRaisesRegex(RuntimeError, "publication scan is older"):
                    check()
            # Same length and restored mtime relative to the entity still need
            # content verification when the recorded scan mtime changes.
            raw.write_text("modified", encoding="utf-8")
            os.utime(raw, (150, 150))
            with patch("scan_dblp_publications.load_targets", return_value=targets):
                with self.assertRaisesRegex(RuntimeError, "publication scan is older"):
                    check()

    def test_newer_reviewer_db_rejects_build(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            entity = root / "entity_resolution.sqlite"
            publications = root / "dblp_publications_for_db_people.csv"
            reviewer = root / "icde_reviewers.sqlite"
            for path in (entity, publications, reviewer):
                path.write_text("x", encoding="utf-8")
            os.utime(entity, (100, 100))
            os.utime(publications, (200, 200))
            os.utime(reviewer, (300, 300))
            with self.assertRaisesRegex(RuntimeError, "Entity resolution is older"):
                assert_pipeline_freshness(
                    root, entity_db=entity, publications_csv=publications
                )

    def test_newer_entity_resolution_rejects_old_publication_scan(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            entity = root / "entity_resolution.sqlite"
            publications = root / "dblp_publications_for_db_people.csv"
            entity.write_text("x", encoding="utf-8")
            publications.write_text("x", encoding="utf-8")
            os.utime(publications, (100, 100))
            os.utime(entity, (200, 200))
            with self.assertRaisesRegex(RuntimeError, "publication scan is older"):
                assert_pipeline_freshness(
                    root, entity_db=entity, publications_csv=publications
                )

    def test_newer_raw_publication_scan_rejects_old_entity_resolution(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            entity = root / "entity_resolution.sqlite"
            raw = root / "dblp_publications_for_db_people.csv"
            resolved = root / "dblp_publication_entities_for_db_people.csv"
            for path in (entity, raw, resolved):
                path.write_text("x", encoding="utf-8")
            os.utime(entity, (100, 100))
            os.utime(resolved, (200, 200))
            os.utime(raw, (300, 300))
            with self.assertRaisesRegex(RuntimeError, "Publication entity resolution is older"):
                assert_pipeline_freshness(
                    root,
                    entity_db=entity,
                    publications_csv=resolved,
                    raw_publications_csv=raw,
                )

    def test_newer_arxiv_cache_rejects_old_publication_entity_resolution(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            entity = root / "entity_resolution.sqlite"
            raw = root / "dblp_publications_for_db_people.csv"
            resolved = root / "dblp_publication_entities_for_db_people.csv"
            cache = root / "arxiv_candidate_metadata_cache.json"
            for path in (entity, raw, resolved, cache):
                path.write_text("x", encoding="utf-8")
            os.utime(entity, (100, 100))
            os.utime(raw, (200, 200))
            os.utime(resolved, (300, 300))
            os.utime(cache, (400, 400))
            with self.assertRaisesRegex(RuntimeError, "arXiv evidence cache"):
                assert_pipeline_freshness(
                    root,
                    entity_db=entity,
                    publications_csv=resolved,
                    raw_publications_csv=raw,
                    arxiv_cache=cache,
                )


class ArtifactFingerprintTests(unittest.TestCase):
    def test_fingerprint_is_content_based_and_does_not_expose_local_path(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "input.csv"
            path.write_bytes(b"abc")
            fingerprint = artifact_fingerprint(path)
            parent = str(path.parent)

        self.assertEqual(fingerprint["filename"], "input.csv")
        self.assertEqual(fingerprint["sizeBytes"], 3)
        self.assertEqual(
            fingerprint["sha256"],
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
        )
        self.assertNotIn(parent, json.dumps(fingerprint))


    def test_combined_fingerprint_is_single_identifier_without_file_metadata(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            first = root / "first.csv"
            second = root / "second.sqlite"
            first.write_text("alpha", encoding="utf-8")
            second.write_text("beta", encoding="utf-8")
            fingerprint = combined_artifact_fingerprint({"a": first, "b": second})

        self.assertRegex(fingerprint, r"^sha256:[0-9a-f]{64}$")
        self.assertNotIn("first.csv", fingerprint)
        self.assertNotIn("second.sqlite", fingerprint)

    def test_latest_reviewer_fetch_at_uses_newest_scrape_timestamp(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db = root / "sigmod_reviewers.sqlite"
            conn = sqlite3.connect(db)
            try:
                conn.execute("CREATE TABLE reviewer_records (fetched_at TEXT NOT NULL)")
                conn.executemany(
                    "INSERT INTO reviewer_records(fetched_at) VALUES (?)",
                    [("2026-09-30T09:00:00+00:00",), ("2026-10-04T15:39:00+00:00",)],
                )
                conn.commit()
            finally:
                conn.close()

            self.assertEqual(
                latest_reviewer_fetch_at(root),
                "2026-10-04T15:39:00+00:00",
            )


class ArxivRefreshMetadataTests(unittest.TestCase):
    def test_explicit_refresh_timestamp_wins_over_file_mtime(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "arxiv_candidate_metadata_cache.json"
            path.write_text(
                json.dumps({
                    "version": 1,
                    "refreshedAt": "2026-10-05T07:45:00+00:00",
                    "records": {},
                }),
                encoding="utf-8",
            )
            os.utime(path, (100, 100))
            self.assertEqual(
                arxiv_cache_refreshed_at(path),
                "2026-10-05T07:45:00+00:00",
            )

    def test_legacy_cache_falls_back_to_file_mtime(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "arxiv_candidate_metadata_cache.json"
            path.write_text(json.dumps({"version": 1, "records": {}}), encoding="utf-8")
            os.utime(path, (100, 100))
            self.assertEqual(
                arxiv_cache_refreshed_at(path),
                "1970-01-01T00:01:40+00:00",
            )


class BrowserPayloadSplitTests(unittest.TestCase):
    def test_service_sharing_and_lazy_evidence_preserve_the_original_records(self):
        import copy
        event = {"venue": "VLDB", "year": 2025, "role": "review_board_member",
                 "sourceUrl": "https://example.org/reviewers", "rounds": [1, 2],
                 "affiliation": "Example University", "provenance": {"sourceIds": ["source-1"]}}
        organizer = {**event, "role": "organizer"}
        person = {"id": "alice", "dblpPid": "a", "provenance": {"identity": {"sourceIds": ["source-1"]}},
                  "db": {"events": [event, organizer]}, "ml": {"papers": [{"scope": "main", "workId": "main"}]}}
        payload = {"people": [person], "populationFocus": {"candidates": [
            {"id": "alice", "dblpPid": "a", "db": {"events": [event]}}]}}
        original = copy.deepcopy(payload)
        browser, files, search = browser_payload_and_detail_files(payload)
        self.assertEqual(payload, original, "Export must not mutate the full research dataset")
        self.assertEqual(len(browser["browserServiceEvents"]), 2)
        self.assertEqual(browser["people"][0]["db"]["eventRefs"], [0, 1])
        self.assertEqual(browser["populationFocus"]["candidates"][0]["db"]["eventRefs"], [0])
        self.assertNotIn("provenance", browser["people"][0])
        evidence_url = browser["people"][0]["evidenceUrl"]
        evidence = json.loads(files[Path(evidence_url).name])
        self.assertEqual(evidence["personId"], "alice")
        self.assertEqual(evidence["provenance"], original["people"][0]["provenance"])
        self.assertEqual(evidence["events"], original["people"][0]["db"]["events"])
        expanded = site_builder.expand_browser_service_events(browser)
        self.assertEqual([e["role"] for e in expanded["people"][0]["db"]["events"]], ["review_board_member", "organizer"])
        self.assertEqual(len(expanded["populationFocus"]["candidates"][0]["db"]["events"]), 1)
        self.assertEqual(expanded["people"][0]["ml"]["papers"], original["people"][0]["ml"]["papers"])
        self.assertEqual(expanded["people"][0]["db"]["events"][0]["rounds"], [1, 2])
        self.assertIsNot(expanded["people"][0]["db"]["events"][0], expanded["populationFocus"]["candidates"][0]["db"]["events"][0])
        self.assertIsNone(search)

    def test_excluded_pc_profiles_are_searchable_without_entering_the_study_population(self):
        selected = {"id": "selected", "focus": {"included": True}, "ml": {"papers": [
            {"workId": "selected-main", "scope": "main"},
            {"workId": "selected-side", "scope": "side", "title": "Selected context"}]}}
        excluded = {"id": "excluded", "focus": {"included": False, "label": "Mixed"}, "ml": {"papers": [
            {"workId": "excluded-main", "scope": "main"},
            {"workId": "excluded-side", "scope": "side", "title": "Excluded searchable context"}]}}
        payload = {"summary": {"sitePeople": 1}, "people": [selected]}
        browser, details, search = browser_payload_and_detail_files(payload, [excluded, selected])
        self.assertEqual([p["id"] for p in browser["people"]], ["selected"])
        self.assertEqual([p["id"] for p in browser["additionalExplorerPeople"]], ["excluded"])
        self.assertEqual(browser["summary"]["sitePeople"], 1)
        self.assertFalse(browser["additionalExplorerPeople"][0]["focus"]["included"])
        self.assertEqual([p["workId"] for p in browser["additionalExplorerPeople"][0]["ml"]["papers"]], ["excluded-main"])
        self.assertEqual(len(details), 2)
        self.assertEqual(len(excluded["ml"]["papers"]), 2)
        self.assertIn("Excluded searchable context", json.loads(search[1])["people"]["excluded"])

    def test_context_publications_are_deferred_per_person(self):
        payload = {
            "meta": {"schemaVersion": SCHEMA_VERSION},
            "people": [
                {
                    "id": "dblp:12/3456",
                    "ml": {
                        "papers": [
                            {"workId": "main", "scope": "main", "publicationClass": "main_ml"},
                            {"workId": "workshop", "scope": "side", "publicationClass": "workshop", "title": "Deferred workshop paper"},
                            {"workId": "db", "scope": "side", "publicationClass": "database", "title": "Deferred database paper"},
                        ],
                        "sidePaperCount": 2,
                    },
                }
            ],
        }

        browser, detail_files, search_file = browser_payload_and_detail_files(payload)

        self.assertEqual(len(payload["people"][0]["ml"]["papers"]), 3)
        browser_ml = browser["people"][0]["ml"]
        self.assertEqual([paper["workId"] for paper in browser_ml["papers"]], ["main"])
        self.assertEqual(browser_ml["deferredPaperCount"], 2)
        self.assertTrue(browser_ml["detailUrl"].startswith("data/people/"))
        self.assertEqual(browser["meta"]["browserDetailSchemaVersion"], BROWSER_DETAIL_SCHEMA_VERSION)
        self.assertEqual(browser["meta"]["browserSearchSchemaVersion"], BROWSER_SEARCH_SCHEMA_VERSION)
        self.assertTrue(browser["meta"]["publicationSearchUrl"].startswith("data/publication_search."))
        self.assertEqual(len(detail_files), 1)
        detail = json.loads(next(iter(detail_files.values())))
        self.assertEqual(detail["personId"], "dblp:12/3456")
        self.assertEqual({paper["workId"] for paper in detail["papers"]}, {"workshop", "db"})
        self.assertIsNotNone(search_file)
        search_name, search_content = search_file
        self.assertTrue(search_name.startswith("publication_search."))
        search_payload = json.loads(search_content)
        self.assertEqual(search_payload["schemaVersion"], BROWSER_SEARCH_SCHEMA_VERSION)
        self.assertIn("dblp:12/3456", search_payload["people"])


class PublicationClassificationTests(unittest.TestCase):
    def test_publication_schema_has_no_satellite_tier(self):
        self.assertEqual(SCHEMA_VERSION, 20)
        self.assertEqual(PROVENANCE_SCHEMA_VERSION, 5)
        self.assertEqual(PUBLICATION_CLASS_LABELS["main_ml"], "Main ML")
        self.assertNotIn("satellite", PUBLICATION_CLASS_LABELS)

    def test_target_workshop_is_context_workshop_not_main_ml(self):
        self.assertEqual(
            classify_publication(
                scope="side",
                dblp_key="conf/iclr/example",
                year=2024,
                record_type="inproceedings",
                booktitle="ICLR (Workshop)",
                journal="",
                venue_label="ICLR (Workshop)",
                main_venue="",
            )[0],
            "workshop",
        )

    def test_named_non_main_ml_event_is_context_only(self):
        self.assertEqual(
            classify_publication(
                scope="side",
                dblp_key="conf/ml4h/example",
                year=2023,
                record_type="inproceedings",
                booktitle="ML4H@NeurIPS",
                journal="",
                venue_label="ML4H@NeurIPS",
                main_venue="",
            )[0],
            "other_conference",
        )

    def test_non_ml_workshop_remains_workshop(self):
        self.assertEqual(
            classify_publication(
                scope="side",
                dblp_key="conf/icde/example",
                year=2024,
                record_type="inproceedings",
                booktitle="ICDE Workshops",
                journal="",
                venue_label="ICDE Workshops",
                main_venue="",
            )[0],
            "workshop",
        )

    def test_corr_with_workshop_note_remains_preprint(self):
        self.assertEqual(
            classify_publication(
                scope="side",
                dblp_key="journals/corr/example",
                year=2024,
                record_type="article",
                booktitle="ICLR (Workshop)",
                journal="CoRR",
                venue_label="CoRR",
                main_venue="",
            )[0],
            "preprint",
        )


class DisplayNameTests(unittest.TestCase):
    def test_identity_name_wins_equal_detail_tie(self):
        self.assertEqual(
            choose_name(
                {"MingJie Tang", "mingjie tang", "Mingjie Tang"},
                "01/7213",
                preferred_names={"MingJie Tang"},
            ),
            "MingJie Tang",
        )

    def test_more_detailed_existing_name_still_beats_shorter_canonical(self):
        self.assertEqual(
            choose_name(
                {"Venkata Vamsikrishna Meduri", "Vamsi Meduri"},
                "117/2614",
                preferred_names={"Vamsi Meduri"},
            ),
            "Venkata Vamsikrishna Meduri",
        )

    def test_name_choice_is_deterministic_without_preferred_name(self):
        self.assertEqual(
            choose_name({"mingjie tang", "MingJie Tang"}, "fallback"),
            "MingJie Tang",
        )

    def test_existing_pid_alias_does_not_take_over_display_name_candidates(self):
        conn = sqlite3.connect(":memory:")
        conn.executescript(
            """
            CREATE TABLE name_resolution (
                name_key TEXT PRIMARY KEY, representative_name TEXT, status TEXT,
                resolved_dblp_pid TEXT, resolved_dblp_name TEXT, best_orcid TEXT
            );
            CREATE TABLE reviewer_observations (
                name_key TEXT, name TEXT, venue TEXT, year INTEGER, role TEXT,
                review_round TEXT, review_cycle TEXT, affiliation TEXT, source_url TEXT
            );
            CREATE TABLE dblp_people (
                dblp_pid TEXT PRIMARY KEY, canonical_name TEXT, aliases_json TEXT,
                affiliations_json TEXT, current_affiliations_json TEXT,
                former_affiliations_json TEXT, verified_orcids_json TEXT
            );
            """
        )
        rows = [
            ("abdul quamar", "Abdul Quamar", "resolved_unique_exact"),
            ("abdul h quamar", "Abdul H Quamar", "resolved_existing_pid_alias"),
        ]
        for name_key, representative, status in rows:
            conn.execute(
                "INSERT INTO name_resolution VALUES (?, ?, ?, ?, ?, ?)",
                (name_key, representative, status, "127/6195", "Abdul Quamar", ""),
            )
            conn.execute(
                "INSERT INTO reviewer_observations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (name_key, representative, "ICDE", 2022, "pc_member", "", "", "IBM", "x"),
            )
        conn.execute(
            "INSERT INTO dblp_people VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("127/6195", "Abdul Quamar", json.dumps(["Abdul Quamar"]), "[]", "[]", "[]", "[]"),
        )

        people, _ = load_db_people(conn)
        person = people["127/6195"]
        self.assertIn("Abdul Quamar", person["names"])
        self.assertNotIn("Abdul H Quamar", person["names"])
        self.assertIn("Abdul H Quamar", person["aliases"])


class PrimaryAffiliationTests(unittest.TestCase):
    class StubRorMatcher:
        def __init__(self, mapping: dict[str, set[str]]):
            self.mapping = mapping

        def match_affiliation(self, affiliation: str) -> dict[str, object]:
            return {ror_id: object() for ror_id in self.mapping.get(affiliation, set())}

    def test_latest_service_affiliation_canonicalizes_to_matching_dblp_current(self):
        matcher = self.StubRorMatcher(
            {
                "MIT": {"https://ror.org/042nb2s44"},
                "MIT Cambridge, MA, USA": {"https://ror.org/042nb2s44"},
            }
        )
        primary = choose_primary_affiliation(
            [
                ("Brown University, Providence, RI, USA", "Brown University, Providence, RI, USA"),
                ("ETH Zurich, Switzerland", "ETH Zurich, Switzerland"),
                ("MIT Cambridge, MA, USA", "MIT Cambridge, MA, USA"),
            ],
            [
                {
                    "year": 2025,
                    "displayAffiliation": "MIT",
                    "provenance": {
                        "affiliations": [
                            {"raw": "MIT", "display": "MIT"},
                        ]
                    },
                }
            ],
            [],
            affiliation_matcher=matcher,
        )
        self.assertEqual(primary, "MIT Cambridge, MA, USA")
        evidence = choose_primary_affiliation_evidence(
            [("MIT Cambridge, MA, USA", "MIT Cambridge, MA, USA")],
            [{"year": 2025, "displayAffiliation": "MIT"}],
            [],
            affiliation_matcher=matcher,
        )
        self.assertIn("ror_affiliation_reconciliation", evidence["methodIds"])

    def test_unmatched_service_affiliation_does_not_override_dblp_current(self):
        primary = choose_primary_affiliation(
            [("University of Technology Nuremberg, Nuremberg, Germany",
              "University of Technology Nuremberg, Nuremberg, Germany")],
            [
                {
                    "year": 2025,
                    "displayAffiliation": "Amazon Web Services",
                    "rawAffiliations": ["Amazon Web Services"],
                    "displayAffiliations": ["Amazon Web Services"],
                },
            ],
            [],
        )
        self.assertEqual(
            primary,
            "University of Technology Nuremberg, Nuremberg, Germany",
        )

    def test_latest_service_affiliation_is_used_when_dblp_has_no_current_affiliation(self):
        primary = choose_primary_affiliation(
            [],
            [
                {
                    "year": 2025,
                    "displayAffiliation": "New University",
                    "rawAffiliations": ["New University"],
                    "displayAffiliations": ["New University"],
                },
                {
                    "year": 2024,
                    "displayAffiliation": "Old University",
                    "rawAffiliations": ["Old University"],
                    "displayAffiliations": ["Old University"],
                },
            ],
            [],
        )
        self.assertEqual(primary, "New University")

    def test_dblp_current_order_is_fallback_when_no_dated_service_exists(self):
        primary = choose_primary_affiliation(
            [
                ("MIT Cambridge, MA, USA", "MIT Cambridge, MA, USA"),
                ("Brown University, Providence, RI, USA", "Brown University, Providence, RI, USA"),
            ],
            [],
            [],
        )
        self.assertEqual(primary, "MIT Cambridge, MA, USA")

    def test_matching_service_affiliation_flags_other_same_year_disagreement(self):
        matcher = self.StubRorMatcher(
            {
                "MIT": {"https://ror.org/042nb2s44"},
                "MIT Cambridge, MA, USA": {"https://ror.org/042nb2s44"},
            }
        )
        evidence = choose_primary_affiliation_evidence(
            [("MIT Cambridge, MA, USA", "MIT Cambridge, MA, USA")],
            [
                {
                    "year": 2025,
                    "displayAffiliation": "MIT",
                    "rawAffiliations": ["MIT"],
                    "displayAffiliations": ["MIT"],
                },
                {
                    "year": 2025,
                    "displayAffiliation": "Old Employer",
                    "rawAffiliations": ["Old Employer"],
                    "displayAffiliations": ["Old Employer"],
                },
            ],
            [],
            affiliation_matcher=matcher,
        )
        self.assertEqual(evidence["affiliation"], "MIT Cambridge, MA, USA")
        self.assertEqual(evidence["confidence"], "medium")
        self.assertTrue(evidence["hasConflict"])
        self.assertEqual(evidence["latestServiceAffiliations"], ["MIT", "Old Employer"])

    def test_service_only_same_year_conflict_prefers_repeated_observation(self):
        evidence = choose_primary_affiliation_evidence(
            [],
            [
                {"year": 2025, "displayAffiliation": "University A"},
                {"year": 2025, "displayAffiliation": "University B"},
                {"year": 2025, "displayAffiliation": "University B"},
            ],
            [],
        )
        self.assertEqual(evidence["affiliation"], "University B")
        self.assertEqual(evidence["decisionKey"], "latest_service_only_conflict")
        self.assertEqual(evidence["confidence"], "low")
        self.assertTrue(evidence["hasConflict"])

    def test_affiliation_review_rows_only_include_non_high_confidence_decisions(self):
        people = [
            {
                "dblpPid": "1/1",
                "name": "High Confidence",
                "primaryAffiliation": "University A",
                "provenance": {
                    "primaryAffiliation": {
                        "confidence": "high",
                        "hasConflict": False,
                        "decisionKey": "single_dblp_current",
                    }
                },
            },
            {
                "dblpPid": "2/2",
                "name": "Needs Review",
                "primaryAffiliation": "University B",
                "provenance": {
                    "primaryAffiliation": {
                        "confidence": "medium",
                        "hasConflict": True,
                        "decisionKey": "dblp_current_over_stale_service",
                        "currentAffiliations": ["University B"],
                        "latestServiceYear": 2025,
                        "latestServiceAffiliations": ["Old Employer"],
                        "notes": ["Sources disagree."],
                    }
                },
            },
        ]
        rows = primary_affiliation_review_rows(people)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["personName"], "Needs Review")
        self.assertEqual(rows[0]["hasConflict"], "yes")
        self.assertEqual(rows[0]["latestServiceYear"], 2025)

    def test_load_db_people_preserves_dblp_current_affiliation_order(self):
        conn = sqlite3.connect(":memory:")
        conn.executescript(
            """
            CREATE TABLE name_resolution (
                name_key TEXT PRIMARY KEY, representative_name TEXT, status TEXT,
                resolved_dblp_pid TEXT, resolved_dblp_name TEXT, best_orcid TEXT
            );
            CREATE TABLE reviewer_observations (
                name_key TEXT, name TEXT, venue TEXT, year INTEGER, role TEXT,
                review_round TEXT, review_cycle TEXT, affiliation TEXT, source_url TEXT
            );
            CREATE TABLE dblp_people (
                dblp_pid TEXT PRIMARY KEY, canonical_name TEXT, aliases_json TEXT,
                affiliations_json TEXT, current_affiliations_json TEXT,
                former_affiliations_json TEXT, verified_orcids_json TEXT
            );
            """
        )
        conn.execute(
            "INSERT INTO name_resolution VALUES (?, ?, ?, ?, ?, ?)",
            ("tim kraska", "Tim Kraska", "resolved_unique_exact", "26/6037", "Tim Kraska", ""),
        )
        conn.execute(
            "INSERT INTO reviewer_observations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("tim kraska", "Tim Kraska", "SIGMOD", 2025, "industrial_pc_member", "", "", "MIT", "x"),
        )
        ordered = [
            "MIT Cambridge, MA, USA",
            "Brown University, Providence, RI, USA",
            "ETH Zurich, Switzerland",
        ]
        conn.execute(
            "INSERT INTO dblp_people VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "26/6037",
                "Tim Kraska",
                json.dumps(["Tim Kraska"]),
                json.dumps(ordered),
                json.dumps(ordered),
                "[]",
                "[]",
            ),
        )

        people, _ = load_db_people(conn)
        self.assertEqual(people["26/6037"]["current_affiliations_ordered"], ordered)


class ProvenanceTests(unittest.TestCase):
    def test_site_payload_keeps_source_method_and_decision_provenance(self):
        conn = sqlite3.connect(":memory:")
        conn.executescript(
            """
            CREATE TABLE name_resolution (
                name_key TEXT PRIMARY KEY, representative_name TEXT, status TEXT,
                resolved_dblp_pid TEXT, resolved_dblp_name TEXT, best_orcid TEXT,
                best_orcid_source TEXT, notes TEXT
            );
            CREATE TABLE reviewer_observations (
                name_key TEXT, name TEXT, venue TEXT, year INTEGER, role TEXT,
                review_round TEXT, review_cycle TEXT, affiliation TEXT,
                source_url TEXT, source_db TEXT
            );
            CREATE TABLE dblp_people (
                dblp_pid TEXT PRIMARY KEY, canonical_name TEXT, aliases_json TEXT,
                affiliations_json TEXT, current_affiliations_json TEXT,
                former_affiliations_json TEXT, verified_orcids_json TEXT
            );
            CREATE TABLE local_index_meta (key TEXT PRIMARY KEY, value TEXT);
            """
        )
        conn.execute(
            "INSERT INTO name_resolution VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "alice example",
                "Alice Example",
                "resolved_ror_unique_variant",
                "12/3456",
                "Alice Example",
                "0000-0000-0000-0001",
                "dblp_person_record",
                "ROR-backed second-stage resolution using v2.13-2026-09-22.",
            ),
        )
        service_url = "https://2025.sigmod.org/org_sigmod_industry_pc.shtml"
        conn.execute(
            "INSERT INTO reviewer_observations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "alice example",
                "Alice Example",
                "SIGMOD",
                2025,
                "industrial_pc_member",
                "",
                "",
                "Example University",
                service_url,
                "sigmod_reviewers.sqlite",
            ),
        )
        conn.execute(
            "INSERT INTO dblp_people VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "12/3456",
                "Alice Example",
                json.dumps(["Alice Example"]),
                json.dumps(["Example University"]),
                json.dumps(["Example University"]),
                "[]",
                json.dumps(["0000-0000-0000-0001"]),
            ),
        )
        conn.execute(
            "INSERT INTO local_index_meta VALUES (?, ?)",
            ("snapshot_id", "dblp-2026-09-01"),
        )

        with tempfile.TemporaryDirectory() as td:
            publications = Path(td) / "dblp_publication_entities_for_db_people.csv"
            fields = [
                "dblp_pid", "canonical_name", "scope", "venue", "venue_label",
                "year", "title", "record_type", "publtype", "booktitle",
                "journal", "dblp_key", "url", "author_signature",
                "author_position", "author_count", "match_basis", "snapshot_id",
                "work_id", "canonical_dblp_key", "version_count",
                "version_dblp_keys", "person_version_count",
                "publication_entity_basis",
            ]
            with publications.open("w", encoding="utf-8", newline="") as fh:
                writer = csv.DictWriter(fh, fieldnames=fields)
                writer.writeheader()
                writer.writerow(
                    {
                        "dblp_pid": "12/3456",
                        "canonical_name": "Alice Example",
                        "scope": "main",
                        "venue": "NeurIPS",
                        "venue_label": "NeurIPS",
                        "year": "2025",
                        "title": "A Provenance Example",
                        "record_type": "inproceedings",
                        "booktitle": "NeurIPS",
                        "dblp_key": "conf/nips/Example25",
                        "url": "https://dblp.org/rec/conf/nips/Example25",
                        "author_signature": "Alice Example",
                        "author_position": "1",
                        "author_count": "2",
                        "match_basis": "dblp_author_pid",
                        "snapshot_id": "dblp-2026-09-01",
                        "work_id": "dblp-work:conf/nips/Example25",
                        "canonical_dblp_key": "conf/nips/Example25",
                        "version_count": "2",
                        "version_dblp_keys": json.dumps(
                            ["journals/corr/abs-2501-00001", "conf/nips/Example25"]
                        ),
                        "person_version_count": "2",
                        "publication_entity_basis": "arxiv_doi",
                    }
                )

            people, summary, _, provenance = build_payload(conn, publications)

        self.assertEqual(summary["sitePeople"], 1)
        self.assertEqual(provenance["schemaVersion"], 5)
        self.assertEqual(
            provenance["sources"]["dblp_snapshot"]["release"],
            "dblp-2026-09-01",
        )
        self.assertIn("arxiv_metadata_cache", provenance["sources"])
        self.assertIn("crossover_matrix", provenance["metrics"])
        self.assertEqual(
            provenance["recordTypes"]["publication"]["decisionFields"]["workResolution"],
            "publicationEntityBasis",
        )

        person = people[0]
        identity = person["provenance"]["identity"]
        self.assertIn("resolved_ror_unique_variant", identity["decisionKeys"])
        self.assertIn("ror_snapshot", identity["sourceIds"])

        primary_affiliation = person["provenance"]["primaryAffiliation"]
        self.assertEqual(primary_affiliation["affiliation"], "Example University")
        self.assertEqual(primary_affiliation["decisionKey"], "service_corroborates_dblp_current")
        self.assertEqual(primary_affiliation["confidence"], "high")
        self.assertFalse(primary_affiliation["hasConflict"])
        self.assertNotIn("ror_affiliation_reconciliation", primary_affiliation["methodIds"])
        self.assertIn("primaryAffiliation", provenance["decisionRules"])
        self.assertIn("primary_affiliation_selection", provenance["methods"])

        event = person["db"]["events"][0]
        self.assertIn("scrape_sigmod_reviewers", event["provenance"]["extractionMethodIds"])
        self.assertEqual(len(event["provenance"]["sourceIds"]), 1)
        event_source = provenance["sources"][event["provenance"]["sourceIds"][0]]
        self.assertEqual(event_source["url"], service_url)

        paper = person["ml"]["papers"][0]
        self.assertEqual(paper["matchBasis"], "dblp_author_pid")
        self.assertEqual(paper["publicationEntityBasis"], "arxiv_doi")
        self.assertEqual(paper["canonicalDblpKey"], "conf/nips/Example25")
        self.assertEqual(paper["arxivIds"], ["2501.00001"])
        exact_arxiv_sources = [
            source_id
            for source_id in paper["provenance"]["sourceIds"]
            if source_id.startswith("arxiv_record:")
        ]
        self.assertEqual(len(exact_arxiv_sources), 1)
        exact_arxiv_source = provenance["sources"][exact_arxiv_sources[0]]
        self.assertEqual(exact_arxiv_source["recordId"], "2501.00001")
        self.assertEqual(
            exact_arxiv_source["url"], "https://arxiv.org/abs/2501.00001"
        )


if __name__ == "__main__":
    unittest.main()
