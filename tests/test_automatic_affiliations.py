from datetime import date, datetime, timezone
from contextlib import closing
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from automatic_affiliations import (
    MetadataCache, apply_automatic_evidence, crossref_evidence, matched_author,
    organization, publication_dois, published_date, select_automatic_affiliation, evidence_refreshed_at,
)
from build_site_data import apply_publication_affiliation_evidence, artifact_fingerprint, build_payload, unique


class AutomaticAffiliationTests(unittest.TestCase):
    def test_crossref_refresh_uses_retrieval_time_not_offline_build_time(self):
        snapshot = {"generatedAt": "2026-10-21T09:00:00+00:00", "people": {
            "one": {"candidates": [{"retrievedAt": "2026-10-19T06:46:10+00:00"}]},
            "two": {"checks": [{"retrievedAt": "2026-10-19T08:45:00+02:00"}]},
        }}
        self.assertEqual(evidence_refreshed_at(snapshot), "2026-10-19T06:46:10+00:00")
        snapshot["people"]["two"]["checks"].append({"retrievedAt": "2026-10-19T06:47:00Z"})
        self.assertEqual(evidence_refreshed_at(snapshot), "2026-10-19T06:47:00+00:00")

    def test_unknown_crossref_refresh_is_not_invented_from_build_date(self):
        self.assertEqual(evidence_refreshed_at({"generatedAt": "2026-10-21T09:00:00+00:00", "people": {
            "one": {"checks": [{"retrievedAt": ""}, {"retrievedAt": "invalid"}, {"retrievedAt": "2026-10-19T06:46:10"}]},
        }}), "")

    def test_forced_refresh_rechecks_cached_dois_once_including_previous_misses(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = MetadataCache(Path(tmp) / "cache.sqlite", refresh=True)
            try:
                urls = ["https://api.crossref.org/works/10.1234%2Fone", "https://api.crossref.org/works/10.1234%2Fmissing"]
                stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
                client.conn.executemany("INSERT INTO responses VALUES (?,?,?,?)", [
                    (urls[0], stamp, 200, '{"message":{"old":true}}'), (urls[1], stamp, 404, 'null'),
                ])
                client.conn.commit()
                response = mock.Mock(status_code=200)
                response.json.return_value = {"message": {"items": [{"DOI": "10.1234/one", "title": ["Fresh metadata"]}], "total-results": 1}}
                with mock.patch.object(client.session, "get", return_value=response) as fetch:
                    client.prefetch(["10.1234/one", "10.1234/missing"])
                    self.assertEqual(client.get(urls[0])[0]["message"]["title"], ["Fresh metadata"])
                    self.assertIsNone(client.get(urls[1])[0])
                self.assertEqual(fetch.call_count, 1)
            finally:
                client.close()

    def setUp(self):
        self.paper = {"dblpKey": "conf/example/Paper26", "year": 2026, "title": "A publication."}
        self.author = {"given": "Alice", "family": "Example", "affiliation": [{"name": "New University"}]}
        self.message = {"DOI": "10.1234/example", "title": ["A publication"], "author": [self.author],
                        "published": {"date-parts": [[2026, 4, 12]]}}
        self.primary = {"affiliation": "Old University", "decisionKey": "single_dblp_current", "sourceIds": [],
                        "latestServiceYear": 2025, "latestServiceAffiliations": ["Old University"],
                        "currentAffiliations": ["Old University"], "notes": [], "hasConflict": False}
        self.item, _ = crossref_evidence(self.message, "10.1234/example", self.paper, ["Alice Example"], [], "2026-10-18T00:00:00+00:00")

    def record(self, *items):
        return {"candidates": list(items) or [self.item], "searchCapped": False}

    def test_matches_only_the_specific_author_not_a_coauthor(self):
        other = {"given": "Bob", "family": "Other", "affiliation": [{"name": "Wrong University"}]}
        self.message["author"] = [other, self.author]
        item, status = crossref_evidence(self.message, "10.1234/example", self.paper, ["Alice Example"], [], "now")
        self.assertEqual(status, "matched")
        self.assertEqual(item["rawAffiliations"], ["New University"])

    def test_duplicate_full_names_and_initial_only_matches_are_rejected(self):
        self.assertIsNone(matched_author([self.author, self.author], ["Alice Example"], [])[0])
        self.assertIsNone(matched_author([{**self.author, "given": "A."}], ["Alice Example"], [])[0])

    def test_orcid_disambiguates_but_conflicting_orcid_blocks_name_fallback(self):
        author = {**self.author, "ORCID": "https://orcid.org/0000-0001"}
        self.assertEqual(matched_author([author], ["A different name"], ["0000-0001"])[1], "exact_orcid")
        self.assertEqual(matched_author([author], ["Alice Example"], ["0000-0002"])[1], "conflicting_orcid")

    def test_work_doi_title_and_year_are_checked(self):
        for change, expected in [({"DOI": "10.1234/wrong"}, "doi_mismatch"), ({"title": ["Another publication"]}, "title_mismatch"),
                                 ({"published": {"date-parts": [[2020]]}}, "publication_year_mismatch")]:
            result, status = crossref_evidence({**self.message, **change}, "10.1234/example", self.paper, ["Alice Example"], [], "now")
            self.assertIsNone(result)
            self.assertEqual(status, expected)

    def test_new_metadata_deposit_does_not_make_an_old_paper_current(self):
        self.assertIsNone(published_date({"indexed": {"date-time": "2026-10-18"}, "deposited": {"date-parts": [[2026, 10, 18]]}}))
        self.assertIsNone(published_date({"published": {"date-parts": [[2027, 1, 1]]}}, today=date(2026, 10, 18)))

    def test_latest_date_wins_independently_of_input_order(self):
        older = {**self.item, "sourceDate": "2026-02-01", "rawAffiliations": ["Old University"]}
        for items in [(older, self.item), (self.item, older)]:
            result = select_automatic_affiliation(self.primary, self.record(*items))
            self.assertEqual(result["affiliation"], "New University")

    def test_partial_year_date_does_not_get_invented_january_precedence(self):
        uncertain = {**self.item, "sourceDate": "2026", "rawAffiliations": ["Other University"]}
        result = select_automatic_affiliation(self.primary, self.record(self.item, uncertain))
        self.assertEqual(result["automaticAffiliationStatus"], "conflicting_latest_publications")
        self.assertEqual(result["affiliation"], "Old University")

    def test_newer_service_and_same_year_disagreement_are_preserved(self):
        for service_year, status in [(2027, "publication_older_than_service"), (2026, "same_year_service_conflict")]:
            result = select_automatic_affiliation({**self.primary, "latestServiceYear": service_year}, self.record())
            self.assertEqual(result["affiliation"], "Old University")
            self.assertEqual(result["automaticAffiliationStatus"], status)

    def test_same_year_corroboration_can_replace_an_undated_dblp_note(self):
        primary = {**self.primary, "latestServiceYear": 2026, "latestServiceAffiliations": ["New University"]}
        self.assertEqual(select_automatic_affiliation(primary, self.record())["affiliation"], "New University")

    def test_multiple_listed_affiliations_are_not_silently_reduced_to_one(self):
        item = {**self.item, "rawAffiliations": ["New University", "Second University"]}
        result = select_automatic_affiliation(self.primary, self.record(item))
        self.assertIn("New University", result["affiliation"])
        self.assertIn("Second University", result["affiliation"])
        self.assertTrue(result["hasConflict"])

    def test_known_label_and_ror_surface_share_the_same_organization_key(self):
        class Matcher:
            def match_affiliation(self, raw):
                if raw == "University X":
                    return {"ror:one": SimpleNamespace(matched_surface="University X")}
                return {}
        self.assertEqual(organization("University X Via Road", Matcher(), ["University X"]), ("ror:one", "University X"))

    def test_case_variant_source_labels_have_reproducible_order(self):
        values = ["Microsoft research", "Microsoft Research", "Snowflake"]
        self.assertEqual(unique(values), unique(reversed(values)))
        self.assertEqual(set(unique(values)), set(values))

    def test_other_pid_or_missing_publication_evidence_cannot_change_affiliation(self):
        result = apply_automatic_evidence(self.primary, self.record(), [{**self.paper, "dblpKey": "other"}], {"sources": {}})
        self.assertEqual(result["affiliation"], "Old University")
        result = select_automatic_affiliation(self.primary, {**self.record(), "searchCapped": True})
        self.assertEqual(result["automaticAffiliationStatus"], "incomplete_search")

    def test_older_manual_review_cannot_overwrite_newer_automatic_evidence(self):
        primary = select_automatic_affiliation(self.primary, self.record())
        manual = {"dblpKey": "old", "year": 2025, "personName": "Alice Example", "affiliation": "Old University"}
        result = apply_publication_affiliation_evidence(primary, [manual], [{"dblpKey": "old", "year": 2025}], {"sources": {}})
        self.assertEqual(result["affiliation"], "New University")

    def test_merged_old_version_doi_is_not_new_affiliation_evidence(self):
        paper = {**self.paper, "url": "https://doi.org/10.1234/current", "resolvedVersions": [
            {"year": 2025, "url": "https://doi.org/10.1234/old"},
            {"year": 2026, "url": "https://doi.org/10.48550/arXiv.2601.00001"}]}
        self.assertEqual(publication_dois(paper), ["10.1234/current"])

    def test_dois_are_limited_to_same_year_person_owned_versions(self):
        paper = {**self.paper, "url": "https://doi.org/10.1234/selected",
                 "personVersionDblpKeys": [self.paper["dblpKey"], "owned"],
                 "resolvedVersions": [
                     {"dblpKey": "owned", "year": 2026, "ees": ["https://doi.org/10.1234/owned"]},
                     {"dblpKey": "unowned", "year": 2026, "url": "https://doi.org/10.1234/coauthor"},
                     {"dblpKey": "old", "year": 2025, "url": "https://doi.org/10.1234/old"}]}
        self.assertEqual(publication_dois(paper), ["10.1234/owned", "10.1234/selected"])
        self.assertEqual(publication_dois({**paper, "personVersionDblpKeys": []}), [])
        self.assertEqual(publication_dois({**paper, "personVersionDblpKeys": ["owned"]}), ["10.1234/owned"])

    def test_legacy_ownership_allows_only_selected_record_dois(self):
        paper = {**self.paper, "url": "https://doi.org/10.48550/arXiv.2601.00001",
                 "resolvedVersions": [
                     {"dblpKey": self.paper["dblpKey"], "year": 2026, "ees": ["https://doi.org/10.1234/selected"]},
                     {"dblpKey": "other", "year": 2026, "url": "https://doi.org/10.1234/other"}]}
        self.assertEqual(publication_dois(paper), ["10.1234/selected"])

    def test_application_rejects_evidence_from_an_unowned_version_doi(self):
        paper = {**self.paper, "url": "https://doi.org/10.1234/own",
                 "personVersionDblpKeys": [self.paper["dblpKey"]],
                 "resolvedVersions": [{"dblpKey": "coauthor", "year": 2026,
                                       "url": "https://doi.org/10.1234/example"}]}
        catalog = {"sources": {}}
        result = apply_automatic_evidence(self.primary, self.record(), [paper], catalog)
        self.assertEqual(result["affiliation"], "Old University")
        self.assertEqual(catalog["sources"], {})
        owned = {**self.item, "sourceUrl": "https://doi.org/10.1234/own"}
        result = apply_automatic_evidence(self.primary, self.record(owned), [paper], catalog)
        self.assertEqual(result["affiliation"], "New University")

    def test_offline_cache_never_calls_network_and_reuses_results(self):
        with tempfile.TemporaryDirectory() as td:
            client = MetadataCache(Path(td) / "cache.sqlite", offline=True)
            client.conn.execute("INSERT INTO responses VALUES (?,?,?,?)", ("url", "2026-01-01T00:00:00+00:00", 200, '{"value":1}'))
            with mock.patch.object(client.session, "get", side_effect=AssertionError("network call")):
                self.assertEqual(client.get("url")[0], {"value": 1})
                self.assertIsNone(client.get("missing")[0])
            client.close()

    def test_outage_keeps_previous_successful_evidence_in_cache(self):
        with tempfile.TemporaryDirectory() as td:
            client = MetadataCache(Path(td) / "cache.sqlite", refresh_days=0)
            client.conn.execute("INSERT INTO responses VALUES (?,?,?,?)", ("url", "2026-01-01T00:00:00+00:00", 200, '{"value":1}'))
            response = SimpleNamespace(status_code=503, headers={})
            with mock.patch.object(client.session, "get", return_value=response), mock.patch("automatic_affiliations.time.sleep"):
                self.assertEqual(client.get("url")[0], {"value": 1})
            self.assertEqual(client.errors, 1)
            client.close()

    def test_stale_affiliation_snapshot_cannot_attach_to_changed_publications(self):
        import json
        import sqlite3
        from automatic_affiliations import POLICY_VERSION
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            publications = root / "papers.csv"
            publications.write_text("new publication corpus", encoding="utf-8")
            snapshot = {"schemaVersion": POLICY_VERSION, "inputs": {"publications": {"sha256": "old"}}, "people": {}}
            (root / "automatic_affiliation_evidence.json").write_text(json.dumps(snapshot), encoding="utf-8")
            with closing(sqlite3.connect(":memory:")) as conn, mock.patch("build_site_data.load_db_people", return_value=({}, {})), mock.patch("build_site_data.load_publications", return_value=({}, {}, set())):
                with self.assertRaisesRegex(ValueError, "older than the publication corpus"):
                    build_payload(conn, publications)

    def test_changed_researcher_resolution_invalidates_a_snapshot(self):
        import json
        import sqlite3
        from automatic_affiliations import POLICY_VERSION
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            publications = root / "papers.csv"
            publications.write_text("publication corpus", encoding="utf-8")
            snapshot = {"schemaVersion": POLICY_VERSION,
                        "inputs": {"publications": artifact_fingerprint(publications), "entityResolution": {"sha256": "old"}}, "people": {}}
            (root / "automatic_affiliation_evidence.json").write_text(json.dumps(snapshot), encoding="utf-8")
            with closing(sqlite3.connect(root / "entities.sqlite")) as conn, mock.patch("build_site_data.load_db_people", return_value=({}, {})), mock.patch("build_site_data.load_publications", return_value=({}, {}, set())):
                with self.assertRaisesRegex(ValueError, "older than researcher resolution"):
                    build_payload(conn, publications)


if __name__ == "__main__":
    unittest.main()
