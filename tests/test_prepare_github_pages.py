import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.prepare_github_pages import prepare


class PreparePagesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.source = Path(self.temp.name) / 'source'
        self.output = Path(self.temp.name) / 'output'
        self.write('index.html', '<html></html>')
        self.write('assets/app.js', 'console.log("hello");')
        self.write('data/site_data.js', 'window.CROSSOVER_DATA=' + json.dumps({
            'people': [{'detailsUrl': 'data/people/current.json'}],
            'meta': {'publicationSearchUrl': 'data/publication_search.current.json'},
        }) + ';')
        self.write('data/coverage_summary.json', json.dumps({'recordsUrl': 'data/coverage/current.json'}))
        self.write('data/people/current.json', json.dumps({'evidenceUrl': 'data/people/evidence.json'}))
        for name in ('people/evidence.json', 'coverage/current.json', 'publication_search.current.json',
                     'people/old.json', 'coverage/old.json', 'publication_search.old.json'):
            self.write('data/' + name, '{}')

    def write(self, relative, text):
        path = self.source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')

    def test_keeps_current_and_nested_references_and_leaves_source_intact(self):
        size = prepare(self.source, self.output)
        files = {str(p.relative_to(self.output)) for p in self.output.rglob('*') if p.is_file()}
        self.assertEqual(files, {
            'index.html', 'assets/app.js', 'data/site_data.js', 'data/coverage_summary.json',
            'data/people/current.json', 'data/people/evidence.json',
            'data/coverage/current.json', 'data/publication_search.current.json',
        })
        self.assertTrue((self.source / 'data/people/old.json').exists())
        self.assertEqual(size, sum(p.stat().st_size for p in self.output.rglob('*') if p.is_file()))

    def test_rejects_oversized_bundle_before_copying(self):
        with patch('scripts.prepare_github_pages.PAGES_MAX_BYTES', 1):
            with self.assertRaisesRegex(ValueError, 'less than 1 GB'):
                prepare(self.source, self.output)
        self.assertFalse(self.output.exists())

    def test_rejects_output_overlapping_source(self):
        with self.assertRaisesRegex(ValueError, 'separate'):
            prepare(self.source, self.source / 'staging')

    def test_rejects_missing_current_reference(self):
        (self.source / 'data/people/evidence.json').unlink()
        with self.assertRaisesRegex(ValueError, 'Missing or unsafe'):
            prepare(self.source, self.output)


if __name__ == '__main__':
    unittest.main()
