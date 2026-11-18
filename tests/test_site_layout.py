"""Keep private build exports out of the directory uploaded to static hosting."""
from contextlib import closing, redirect_stdout
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

from scripts import build_matrix_stats, build_site_data
from scripts.site_paths import WEBSITE_DIR


class SiteLayoutTests(unittest.TestCase):
    def test_matrix_consumes_private_site_export_and_only_publishes_browser_wrapper(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = root / "data"
            data.mkdir()
            entity = data / "entity_resolution.sqlite"
            with closing(sqlite3.connect(entity)) as conn, conn:
                conn.executescript("""
                    CREATE TABLE name_resolution (name_key TEXT, resolved_dblp_pid TEXT);
                    CREATE TABLE reviewer_observations (name_key TEXT, venue TEXT, year INTEGER);
                    INSERT INTO name_resolution VALUES ('example', '1/2');
                    INSERT INTO reviewer_observations VALUES ('example', 'SIGMOD', 2025);
                """)
            private = root / "website" / "build"
            private.mkdir(parents=True)
            full_export = private / "site_data.json"
            full_export.write_text(json.dumps({"meta": {"populationFocusPolicyVersion": 3},
                "people": [{"dblpPid": "1/2", "db": {"events": [{"venue": "SIGMOD"}]},
                            "ml": {"papers": [{"scope": "main", "venue": "NeurIPS", "year": 2025}]}}]}),
                encoding="utf-8")
            browser = root / "website" / "deploy" / "data"
            arguments = ["build_matrix_stats.py", "--entity-db", str(entity),
                         "--site-data", str(full_export), "--output-dir", str(browser),
                         "--audit-output-dir", str(private)]
            with mock.patch("sys.argv", arguments), redirect_stdout(io.StringIO()):
                self.assertEqual(build_matrix_stats.main(), 0)
            readable = json.loads((private / "matrix_stats.json").read_text(encoding="utf-8"))
            script = (browser / "matrix_stats.js").read_text(encoding="utf-8")
            published = json.loads(script.removeprefix("window.CROSSOVER_MATRIX_STATS=").strip().removesuffix(";"))
            self.assertEqual(published, readable)
            self.assertEqual(published["matrix"]["SIGMOD"]["NeurIPS"]["people"], 1)
            self.assertEqual(sorted(p.name for p in browser.iterdir()), ["matrix_stats.js"])

    def test_builders_reject_private_exports_anywhere_inside_published_site(self):
        for module in (build_site_data, build_matrix_stats):
            with self.subTest(builder=module.__name__):
                arguments = ["builder", "--audit-output-dir", str(WEBSITE_DIR / "private")]
                with mock.patch("sys.argv", arguments), self.assertRaisesRegex(SystemExit, "outside"):
                    module.main()


if __name__ == "__main__":
    unittest.main()
