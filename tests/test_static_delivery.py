"""Publishing must reject missing assets and mixed or damaged data generations."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from scripts.build_matrix_stats import build_stats
from scripts.home_population import home_population_summary
from scripts.validate_static_site import validate_static_site


class StaticDeliveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for page in ('index.html', 'people.html', 'all.html'):
            self.write(page, '<link href="assets/styles.css" rel="stylesheet">'
                       '<script src="assets/app.js"></script><a href="people.html">People</a>')
        self.write('assets/styles.css', 'body {}')
        self.write('assets/app.js', '// static app')
        evidence = self.detail('people/evidence-example-', {'source': 'example'}, include_newline=False)
        coverage = self.detail('coverage/records.', {'records': []}, include_newline=True)
        event = {'venue': 'SIGMOD', 'year': 2025, 'role': 'pc_member'}
        self.person = {'id': 'dblp:1/2', 'dblpPid': '1/2', 'evidenceUrl': evidence,
                       'focus': {'included': True}, 'db': {'events': [event]},
                       'ml': {'mainPaperCount': 1, 'venues': ['NeurIPS'], 'papers': [
                           {'scope': 'main', 'venue': 'NeurIPS', 'year': 2025, 'workId': 'work'}]}}
        self.payload = {'meta': {'dataReady': True, 'populationFocusPolicyVersion': 3},
                        'people': [self.person],
                        'mainMlVenueYearTotals': {v: {'2025': 100} for v in ('NeurIPS', 'ICML', 'ICLR')}}
        self.home = home_population_summary(self.payload['people'], self.payload['mainMlVenueYearTotals'])
        self.matrix = build_stats(self.payload, [('SIGMOD', '1/2', 2025)])
        self.save_data()
        self.write('data/coverage_summary.json', json.dumps({'detailUrl': coverage}))

    def write(self, name, text):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode('utf-8'))
        return path

    def detail(self, prefix, payload, *, include_newline):
        body = json.dumps(payload, separators=(',', ':'))
        hashed = body + ('\n' if include_newline else '')
        digest = hashlib.sha256(hashed.encode('utf-8')).hexdigest()[:12]
        name = f'data/{prefix}{digest}.json'
        self.write(name, body + '\r\n')  # Windows checkout must validate on Linux too.
        return name

    def save_data(self):
        self.write('data/site_data.js', 'window.CROSSOVER_DATA=' + json.dumps(self.payload) + ';')
        self.write('data/matrix_stats.js', 'window.CROSSOVER_MATRIX_STATS=' + json.dumps(self.matrix) + ';')
        self.write('data/home_summary.json', json.dumps(self.home))

    def test_complete_site_supports_both_content_hash_conventions_and_windows_newlines(self):
        files, details = validate_static_site(self.root)
        self.assertGreater(files, 8)
        self.assertEqual(details, 2)

    def test_missing_lazy_evidence_is_rejected(self):
        (self.root / self.person['evidenceUrl']).unlink()
        with self.assertRaisesRegex(ValueError, 'Missing or unsafe'):
            validate_static_site(self.root)

    def test_modified_detail_cannot_reuse_an_immutable_filename(self):
        self.write(self.person['evidenceUrl'], '{"source":"changed"}')
        with self.assertRaisesRegex(ValueError, 'Content hash mismatch'):
            validate_static_site(self.root)

    def test_old_home_summary_cannot_be_published_with_new_researcher_data(self):
        self.home['withMainMlPaper'] = 0
        self.save_data()
        with self.assertRaisesRegex(ValueError, 'Home summary'):
            validate_static_site(self.root)

    def test_old_matrix_cannot_be_published_with_new_researcher_data(self):
        self.matrix['matrix']['SIGMOD']['NeurIPS']['people'] = 0
        self.save_data()
        with self.assertRaisesRegex(ValueError, 'Matrix does not match'):
            validate_static_site(self.root)

    def test_missing_dynamically_loaded_methodology_script_is_rejected(self):
        self.write('index.html', '<button data-methodology-app-src="missing.js">Methodology</button>')
        with self.assertRaisesRegex(ValueError, 'missing.js'):
            validate_static_site(self.root)

    def test_local_only_export_inside_published_folder_is_rejected(self):
        self.write('data/site_data.json', '{}')
        with self.assertRaisesRegex(ValueError, 'Private build/documentation'):
            validate_static_site(self.root)

    def test_root_relative_links_that_break_subdirectory_hosting_are_rejected(self):
        self.write('index.html', '<a href="/people.html">People</a>')
        with self.assertRaisesRegex(ValueError, 'Missing or unsafe'):
            validate_static_site(self.root)

    def test_invalid_shared_service_reference_is_rejected(self):
        self.payload['browserServiceEvents'] = self.person['db']['events']
        self.person['db'] = {'eventRefs': [-1]}
        self.save_data()
        with self.assertRaisesRegex(ValueError, 'Invalid shared service'):
            validate_static_site(self.root)


if __name__ == '__main__':
    unittest.main()
