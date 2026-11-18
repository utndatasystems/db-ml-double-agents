from __future__ import annotations

from contextlib import closing, redirect_stdout
import io
import csv
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import refresh_affiliations as refresh
from automatic_affiliations import MetadataCache, OUTPUT_FILENAME, POLICY_VERSION


class OfflineAffiliationRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / refresh.DB_ER).touch()
        (self.root / refresh.PUBLICATIONS_CSV).write_text("unchanged publication fixture", encoding="utf-8")
        self.output = self.root / OUTPUT_FILENAME
        self.cache_path = self.root / "affiliation_metadata.sqlite"
        self.person = {"names": {"Alice Example"}, "aliases": {"Alice Example"}, "orcids": set()}
        self.paper = {"dblpKey": "conf/example/A26", "year": 2026, "title": "A publication",
                      "url": "https://doi.org/10.1234/example", "localAuthorName": "Alice Example"}
        self.url = "https://api.crossref.org/works/10.1234%2Fexample"

    def run_offline(self):
        with mock.patch.object(sys, "argv", ["refresh_affiliations.py", "--data-dir", str(self.root), "--offline"]), \
                mock.patch.object(refresh, "load_db_people", return_value=({"alice": self.person}, {})), \
                mock.patch.object(refresh, "latest_papers", return_value={"alice": [self.paper]}), \
                redirect_stdout(io.StringIO()):
            refresh.main()
        return json.loads(self.output.read_text(encoding="utf-8"))

    def cache_response(self, status=200):
        payload = {"message": {"DOI": "10.1234/example", "title": ["A publication"],
                   "author": [{"given": "Alice", "family": "Example", "affiliation": [{"name": "Recovered University"}]}],
                   "published": {"date-parts": [[2026, 4, 12]]}}} if status == 200 else None
        client = MetadataCache(self.cache_path, offline=True)
        try:
            client.conn.execute("INSERT OR REPLACE INTO responses VALUES (?,?,?,?)",
                                (self.url, "2026-10-19T00:00:00+00:00", status, json.dumps(payload)))
            client.conn.commit()
        finally:
            client.close()

    def test_restored_response_is_consumed_without_primary_input_changes(self):
        first = self.run_offline()
        self.assertEqual(first["people"]["alice"]["candidates"], [])
        self.cache_response()
        second = self.run_offline()
        self.assertEqual(first["inputs"], second["inputs"])
        self.assertEqual(second["people"]["alice"]["candidates"][0]["rawAffiliations"], ["Recovered University"])

    def test_missing_supporting_cache_preserves_matching_snapshot_without_creating_empty_cache(self):
        self.cache_response()
        self.run_offline()
        previous = self.output.read_bytes()
        self.cache_path.unlink()
        with mock.patch.object(refresh, "load_db_people", side_effect=AssertionError("snapshot should be reused")):
            # run_offline supplies its own people adapter; direct entry checks no rebuild.
            with mock.patch.object(sys, "argv", ["refresh_affiliations.py", "--data-dir", str(self.root), "--offline"]), redirect_stdout(io.StringIO()):
                refresh.main()
        self.assertEqual(self.output.read_bytes(), previous)
        self.assertFalse(self.cache_path.exists())

    def test_legacy_snapshot_rebuilds_when_supporting_cache_exists(self):
        self.run_offline()
        previous = json.loads(self.output.read_text(encoding="utf-8"))
        previous.pop("metadataCache", None)
        self.output.write_text(json.dumps(previous), encoding="utf-8")
        self.cache_response()
        rebuilt = self.run_offline()
        self.assertIn("metadataCache", rebuilt)
        self.assertEqual(len(rebuilt["people"]["alice"]["candidates"]), 1)

    def test_legacy_snapshot_is_preserved_when_supporting_cache_is_absent(self):
        self.cache_response()
        previous = self.run_offline()
        previous.pop("metadataCache", None)
        self.output.write_text(json.dumps(previous), encoding="utf-8")
        self.cache_path.unlink()
        self.assertEqual(self.run_offline(), previous)
        self.assertFalse(self.cache_path.exists())

    def test_changed_primary_inputs_cannot_preserve_old_evidence_when_cache_missing(self):
        self.cache_response()
        previous = self.run_offline()
        self.cache_path.unlink()
        (self.root / refresh.PUBLICATIONS_CSV).write_text("changed publication fixture", encoding="utf-8")
        rebuilt = self.run_offline()
        self.assertNotEqual(rebuilt["inputs"], previous["inputs"])
        self.assertEqual(rebuilt["people"]["alice"]["candidates"], [])

    def test_explicit_negative_response_invalidates_previous_positive_evidence(self):
        self.cache_response()
        first = self.run_offline()
        self.assertEqual(len(first["people"]["alice"]["candidates"]), 1)
        self.cache_response(status=404)
        second = self.run_offline()
        self.assertNotEqual(first["metadataCache"], second["metadataCache"])
        self.assertEqual(second["people"]["alice"]["candidates"], [])

    def test_logical_fingerprint_includes_committed_wal_and_ignores_vacuum(self):
        self.cache_response()
        before = refresh.metadata_cache_fingerprint(self.cache_path)
        with closing(sqlite3.connect(self.cache_path)) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA wal_autocheckpoint=0")
            conn.execute("UPDATE responses SET payload='null', status=404")
            conn.commit()
            self.assertTrue(Path(str(self.cache_path) + "-wal").exists())
            in_wal = refresh.metadata_cache_fingerprint(self.cache_path)
            self.assertNotEqual(before, in_wal)
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.execute("VACUUM")
            self.assertEqual(in_wal, refresh.metadata_cache_fingerprint(self.cache_path))

    def test_unchanged_cache_reuses_snapshot(self):
        self.cache_response()
        self.run_offline()
        previous = self.output.read_bytes()
        with mock.patch.object(refresh, "load_db_people", side_effect=AssertionError("snapshot should be reused")), \
                mock.patch.object(sys, "argv", ["refresh_affiliations.py", "--data-dir", str(self.root), "--offline"]), redirect_stdout(io.StringIO()):
            refresh.main()
        self.assertEqual(self.output.read_bytes(), previous)

    def test_old_policy_snapshot_is_rebuilt_for_unchanged_inputs(self):
        self.cache_response()
        previous = self.run_offline()
        previous["schemaVersion"] = POLICY_VERSION - 1
        previous["people"]["alice"]["candidates"] = []
        self.output.write_text(json.dumps(previous), encoding="utf-8")
        rebuilt = self.run_offline()
        self.assertEqual(rebuilt["schemaVersion"], POLICY_VERSION)
        self.assertEqual(len(rebuilt["people"]["alice"]["candidates"]), 1)

    def test_csv_ownership_reaches_evidence_collection(self):
        self.cache_response()  # Plausible matching name/title must not bypass ownership.
        fields = ["dblp_pid", "dblp_key", "year", "title", "url", "author_signature",
                  "publication_versions_json", "person_version_dblp_keys"]
        own = "journals/corr/owned"
        row = {"dblp_pid": "alice", "dblp_key": own, "year": 2026,
               "title": "A publication", "url": "https://doi.org/10.48550/arXiv.2601.00001",
               "author_signature": "Alice Example", "person_version_dblp_keys": json.dumps([own]),
               "publication_versions_json": json.dumps([
                   {"dblpKey": "conf/example/unowned", "year": 2026, "url": "https://doi.org/10.1234/example"}])}
        path = self.root / refresh.PUBLICATIONS_CSV
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerow(row)
        papers = refresh.latest_papers(path, {"alice"})
        self.assertEqual(papers["alice"][0]["personVersionDblpKeys"], [own])
        with mock.patch.object(sys, "argv", ["refresh_affiliations.py", "--data-dir", str(self.root), "--offline"]), \
                mock.patch.object(refresh, "load_db_people", return_value=({"alice": self.person}, {})), \
                redirect_stdout(io.StringIO()):
            refresh.main()
        record = json.loads(self.output.read_text(encoding="utf-8"))["people"]["alice"]
        self.assertEqual(record["candidates"], [])
        self.assertEqual(record["checks"], [])

    def test_csv_legacy_and_explicit_empty_ownership_are_distinct(self):
        fields = ["dblp_pid", "dblp_key", "year", "title", "person_version_dblp_keys"]
        path = self.root / refresh.PUBLICATIONS_CSV
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            for pid, ownership in [("legacy", ""), ("empty", "[]")]:
                writer.writerow({"dblp_pid": pid, "dblp_key": pid, "year": 2026,
                                 "title": "A publication", "person_version_dblp_keys": ownership})
        papers = refresh.latest_papers(path, {"legacy", "empty"})
        self.assertNotIn("personVersionDblpKeys", papers["legacy"][0])
        self.assertEqual(papers["empty"][0]["personVersionDblpKeys"], [])


if __name__ == "__main__":
    unittest.main()
