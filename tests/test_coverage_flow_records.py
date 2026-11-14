import csv
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

import tests.test_data_coverage as coverage_fixture
from scripts.coverage_flow_records import matching_note
from scripts.data_coverage import export_coverage
from scripts.publication_category_reviews import REVIEW_FIELDS


class FlowEvidenceTests(unittest.TestCase):
    def fixture(self, directory):
        return coverage_fixture.CoverageTests().fixture(directory)

    def records(self, summary, output, component):
        group = next(g for g in summary['flows']['groups'] if g['id'] == component['groupId'])
        return json.loads((output / Path(group['detailUrl']).relative_to('data')).read_text(encoding='utf-8'))['records']

    def test_exact_failed_ribbon_preserves_candidate_evidence_and_service_sources(self):
        with tempfile.TemporaryDirectory() as directory:
            data, output, people, cohort, policy = self.fixture(directory)
            with closing(sqlite3.connect(data / 'entity_resolution.sqlite')) as conn, conn:
                conn.execute('ALTER TABLE dblp_candidates ADD COLUMN match_type TEXT')
                conn.execute('ALTER TABLE dblp_candidates ADD COLUMN matched_alias TEXT')
                conn.executemany('INSERT INTO dblp_candidates VALUES (?,?,?,?,?)', [
                    ('d', 'a', 0, 'exact_alias', 'D'), ('d', 'b', 0, 'exact_alias', 'D')])
            summary = export_coverage(data, output, people, cohort, policy)
            flow = summary['flows']['people']
            failed = next(n for n in flow['nodes'] if n['id'] == 'ambiguous')
            edge = next(e for e in flow['links'] if e['source'] == 'lookup-several' and e['target'] == 'ambiguous')
            node_records, edge_records = self.records(summary, output, failed), self.records(summary, output, edge)
            self.assertEqual([r['id'] for r in edge_records], ['d'])
            self.assertEqual(edge_records[0]['identityReview'], node_records[0]['identityReview'])
            self.assertEqual(len(edge_records[0]['identityReview']['candidates']), 2)
            self.assertEqual(edge_records[0]['sources'], ['https://example.test/service'])
            self.assertEqual(edge_records[0]['evidence']['Identity status'], 'ambiguous_exact_name')
            names = next(n for n in flow['nodes'] if n['id'] == 'name-groups')
            name = next(r for r in self.records(summary, output, names) if r['id'] == 'd')
            self.assertEqual(name['identityReview'], edge_records[0]['identityReview'])
            with closing(sqlite3.connect(data / 'entity_resolution.sqlite')) as conn:
                self.assertIsNone(conn.execute("SELECT resolved_dblp_pid FROM name_resolution WHERE name_key='d'").fetchone()[0])

    def test_reviewed_identity_is_readable_without_losing_review_or_service_provenance(self):
        note = 'Service identity profile correction: ' + json.dumps({'evidence': {
            'rule': 'reviewed_service_identity', 'reviewed_on': '2026-11-09',
            'reason': 'An institutional page links to the selected DBLP profile.',
            'evidence_urls': ['https://example.test/institution']}})
        with tempfile.TemporaryDirectory() as directory:
            data, output, people, cohort, policy = self.fixture(directory)
            with closing(sqlite3.connect(data / 'entity_resolution.sqlite')) as conn, conn:
                conn.execute('ALTER TABLE name_resolution ADD COLUMN notes TEXT')
                conn.execute("UPDATE name_resolution SET notes=? WHERE name_key='a'", (note,))
            summary = export_coverage(data, output, people, cohort, policy)
            node = next(n for n in summary['flows']['people']['nodes'] if n['id'] == 'matched')
            record = next(r for r in self.records(summary, output, node) if r['id'] == 'a')
            self.assertEqual(record['detail'], 'An institutional page links to the selected DBLP profile.')
            self.assertEqual(record['evidence']['Matching reason'], record['detail'])
            self.assertEqual(record['evidence']['Identity reviewed on'], '2026-11-09')
            self.assertEqual(record['reviewSources'], ['https://example.test/institution'])
            self.assertEqual(record['sources'], ['https://example.test/service'])
            self.assertEqual(record['evidence']['Assigned DBLP PID'], 'a')

    def test_unknown_or_malformed_matching_notes_remain_verbatim(self):
        for note in ['Unstructured evidence', 'Unknown: {"reason":"unknown format"}',
                     'Service identity profile correction: {broken',
                     'Service identity profile correction: [1]',
                     'Service identity profile correction: {"evidence":{"reason":"x","evidence_urls":"bad"}}']:
            self.assertEqual(matching_note(note), (note, {}, []))

    def test_paper_stage_evidence_carries_exact_version_reviews_without_changing_use(self):
        with tempfile.TemporaryDirectory() as directory:
            data, output, people, cohort, policy = self.fixture(directory)
            coverage_fixture.write_rows(data / 'publication_entity_resolution.csv', [
                {'dblp_key': key, 'work_id': work, 'is_canonical': str(int(key != 'r4')),
                 'resolution_basis': 'explicit_version' if key == 'r4' else 'single_record'}
                for key, work in [('r1', 'w1'), ('r2', 'w2'), ('r3', 'w3'), ('r4', 'w2')]])
            reviews = []
            for key, status in [('r2', 'confirmed'), ('r3', 'pending')]:
                row = dict.fromkeys(REVIEW_FIELDS, '')
                row.update(dblp_key=key, title=key, year='2024', record_type='inproceedings',
                           category='workshop', status=status, evidence_url='https://example.test/' + key,
                           source_title='Proceedings', verification_method='live_primary_source',
                           reason='Exact track evidence.' if status == 'confirmed' else 'Track remains uncertain.',
                           reviewed_on='2026-11-09', snapshot_id='test-snapshot')
                reviews.append(row)
            with (data / 'publication_category_reviews.csv').open('w', encoding='utf-8', newline='') as handle:
                writer = csv.DictWriter(handle, fieldnames=REVIEW_FIELDS)
                writer.writeheader(); writer.writerows(reviews)
            summary = export_coverage(data, output, people, cohort, policy)
            flow = summary['flows']['papers']
            outside = next(n for n in flow['nodes'] if n['id'] == 'outside')
            outside_record = self.records(summary, output, outside)[0]
            self.assertEqual(outside_record['id'], 'w3')
            self.assertEqual(outside_record['evidence']['Display-category review'], 'r3: pending')
            self.assertIn('no DB-focused author', outside_record['evidence']['Stage decision'])
            self.assertIn('independent of DB-focus', outside_record['evidence']['Category review scope'])
            context = next(n for n in flow['nodes'] if n['id'] == 'context')
            record = next(r for r in self.records(summary, output, context) if r['id'] == 'w2')
            self.assertEqual(record['evidence']['Display-category review'], 'r2: confirmed')
            self.assertIn('Retained profile context', record['evidence']['Stage decision'])
            edge = next(e for e in flow['links'] if e['target'] == 'context')
            ribbon_record = next(r for r in self.records(summary, output, edge) if r['id'] == 'w2')
            self.assertEqual(ribbon_record['evidence'], record['evidence'])


if __name__ == '__main__':
    unittest.main()
