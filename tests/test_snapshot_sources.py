from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from scripts import refresh_snapshots
from scripts.snapshot_sources import LEGACY_SOURCES, MANIFEST_ENV, MANIFEST_FILENAME, load_snapshot_sources, validate_sources


class Response:
    def __init__(self, url, text="", payload=None):
        self.url, self.text, self.content, self.payload = url, text, text.encode(), payload

    def raise_for_status(self):
        pass

    def json(self):
        return self.payload


class SnapshotSourceTests(unittest.TestCase):
    def test_monthly_discovery_ignores_daily_dtd_future_and_invalid_dates(self):
        urls = ["https://dblp.org/xml/dblp.xml.gz",
                "https://doi.org/10.4230/dblp.xml.dtd.2023-06-28",
                *["https://doi.org/10.4230/dblp.xml." + d for d in
                  ["2026-09-01", "2026-10-03", "2027-01-01", "2026-02-30"]]]
        self.assertEqual(refresh_snapshots.monthly_release_dates(urls, date(2026, 10, 19)), ["2026-09-01", "2026-10-03"])

    def test_discovery_uses_monthly_artifact_and_its_referenced_schema(self):
        base = refresh_snapshots.ARTIFACT_BASE
        xml_url = "https://drops.dagstuhl.de/storage/artifacts/dblp/xml/2026/dblp-2026-10-03.xml.gz"
        dtd_url = LEGACY_SOURCES["dtd"]["url"]
        def link(url):
            return f'<a href="{url}">Download</a>'
        responses = {
            refresh_snapshots.DBLP_COLLECTION: link(base + "dblp.xml.2026-09-01") + link(base + "dblp.xml.2026-10-03"),
            base + "dblp.xml.2026-10-03": link(xml_url) + link("https://doi.org/10.4230/dblp.xml.dtd.2023-06-28"),
            xml_url + ".md5": "a" * 32 + "  dblp-2026-10-03.xml.gz",
            base + "dblp.xml.dtd.2023-06-28": link(dtd_url) + link("https://drops.dagstuhl.de/schema/dblp.dtd"),
            dtd_url + ".md5": LEGACY_SOURCES["dtd"]["md5"],
        }
        session = mock.Mock()
        session.get.side_effect = lambda url, **_: Response(url, responses[url])
        with mock.patch.object(refresh_snapshots, "date") as dates:
            dates.today.return_value = date(2026, 10, 19)
            dates.fromisoformat.side_effect = date.fromisoformat
            snapshot, dtd = refresh_snapshots.discover_dblp(session)
        self.assertEqual(snapshot["id"], "dblp-2026-10-03")
        self.assertEqual(snapshot["url"], xml_url)
        self.assertEqual(snapshot["md5"], "a" * 32)
        self.assertEqual(dtd["filename"], "dblp-2023-06-28.dtd")

    def test_discovery_fails_instead_of_falling_back_to_an_old_snapshot(self):
        session = mock.Mock()
        session.get.return_value = Response(refresh_snapshots.DBLP_COLLECTION, "No releases")
        with self.assertRaisesRegex(RuntimeError, "No dated monthly"):
            refresh_snapshots.discover_dblp(session)

    def test_ror_uses_the_concept_record_and_published_checksum(self):
        session = mock.Mock()
        ror = LEGACY_SOURCES["ror"]
        session.get.return_value = Response(refresh_snapshots.ROR_LATEST, payload={
            "conceptrecid": "6347574", "id": int(ror["record"]),
            "files": [{"key": ror["filename"], "checksum": "md5:" + ror["md5"], "links": {"self": ror["url"]}}],
        })
        item = refresh_snapshots.discover_ror(session)
        self.assertEqual(item["release"], ror["release"])
        self.assertEqual(item["md5"], ror["md5"])
        self.assertEqual(session.get.call_args.args[0], refresh_snapshots.ROR_LATEST)

    def test_invalid_manifest_fails_instead_of_silently_using_legacy_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / MANIFEST_FILENAME
            path.write_text('{"schemaVersion": 99}', encoding="utf-8")
            with self.assertRaises(RuntimeError):
                load_snapshot_sources(path)

    def test_manifest_rejects_daily_snapshot_and_unsafe_filename(self):
        for field, value in [("id", "dblp-latest"), ("filename", "../outside.xml.gz"), ("md5", "unknown")]:
            sources = copy.deepcopy(LEGACY_SOURCES)
            sources["dblp"][field] = value
            with self.assertRaises(RuntimeError):
                validate_sources(sources)

    def test_failed_download_keeps_previous_selection(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / MANIFEST_FILENAME
            manifest.write_text("previous selection", encoding="utf-8")
            with mock.patch("scripts.enrich_reviewers_with_dblp.download_file", side_effect=[None, RuntimeError("bad checksum")]):
                with self.assertRaisesRegex(RuntimeError, "bad checksum"):
                    refresh_snapshots.activate_sources(root, copy.deepcopy(LEGACY_SOURCES))
            self.assertEqual(manifest.read_text(encoding="utf-8"), "previous selection")

    def test_successful_download_activates_one_selection_for_all_consumers(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sources = copy.deepcopy(LEGACY_SOURCES)
            sources["dblp"].update(id="dblp-2026-10-03", filename="dblp-2026-10-03.xml.gz", md5="a" * 32)
            sources["ror"].update(release="v2.14-2026-10-06", filename="v2.14-2026-10-06-ror-data.zip", md5="b" * 32)
            with mock.patch("scripts.enrich_reviewers_with_dblp.download_file") as download:
                refresh_snapshots.activate_sources(root, sources)
            self.assertEqual(download.call_count, 3)
            self.assertEqual(load_snapshot_sources(root / MANIFEST_FILENAME), sources)
            env = dict(os.environ, **{MANIFEST_ENV: str(root / MANIFEST_FILENAME)})
            result = subprocess.run([sys.executable, "-c",
                "from scripts.enrich_reviewers_with_dblp import SNAPSHOT_ID; "
                "from scripts.scan_dblp_publications import SNAPSHOT_ID as scan; "
                "from scripts.ror_affiliations import ROR_RELEASE; "
                "from scripts.build_site_data import build_provenance_catalog; "
                "import json; print(json.dumps([SNAPSHOT_ID,scan,ROR_RELEASE,build_provenance_catalog(SNAPSHOT_ID)['sources']['ror_snapshot']['release']]))"],
                env=env, cwd=Path(__file__).resolve().parents[1], text=True, capture_output=True, check=True)
            self.assertEqual(json.loads(result.stdout), ["dblp-2026-10-03", "dblp-2026-10-03", "v2.14-2026-10-06", "v2.14-2026-10-06"])


if __name__ == "__main__":
    unittest.main()
