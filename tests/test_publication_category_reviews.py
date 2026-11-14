import csv
import contextlib
import io
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from publication_category_reviews import REVIEW_FIELDS, annotate_category_reviews, load_category_reviews
import audit_publication_categories


class PublicationCategoryReviewTests(unittest.TestCase):
    def review(self, **changes):
        return dict(dblp_key='conf/example/paper', title='Exact paper title', year='2025',
                    booktitle='Example', journal='', record_type='inproceedings',
                    category='workshop', status='confirmed', evidence_url='https://example.org/program',
                    source_title='Workshop program', reason='Paper is listed under Workshop A.',
                    verification_method='live_primary_source',
                    reviewed_on='2026-11-09', snapshot_id='dblp-2026-11-01', **changes)

    def row(self, review):
        return {**review, 'status': 'review_mixed_or_ambiguous_volume'}

    def test_confirmed_review_clears_only_matching_record_and_category(self):
        review = self.review()
        rows = [self.row(review)]
        annotate_category_reviews(rows, {review['dblp_key']: review})
        self.assertEqual(rows[0]['status'], 'reviewed_assignment')
        self.assertEqual(rows[0]['reviewed_on'], '2026-11-09')
        for change, expected in [({'title': 'Different paper'}, 'review_metadata_changed'),
                                 ({'year': '2026'}, 'review_metadata_changed'),
                                 ({'category': 'other_conference'}, 'review_assignment_conflict')]:
            rows = [{**self.row(review), **change}]
            annotate_category_reviews(rows, {review['dblp_key']: review})
            self.assertEqual(rows[0]['status'], expected)

    def test_pending_review_does_not_certify_a_fallback(self):
        review = {**self.review(), 'status': 'pending'}
        rows = [self.row(review)]
        annotate_category_reviews(rows, {review['dblp_key']: review})
        self.assertEqual(rows[0]['status'], 'review_pending')

    def test_review_is_not_tied_to_the_current_snapshot_identifier(self):
        review = self.review()
        rows = [{**self.row(review), 'snapshot_id': 'dblp-2027-01-01'}]
        annotate_category_reviews(rows, {review['dblp_key']: review})
        self.assertEqual(rows[0]['status'], 'reviewed_assignment')

    def test_review_of_one_version_does_not_clear_a_related_versions_flag(self):
        review = self.review()
        rows = [self.row(review), {**self.row(review), 'dblp_key': 'journals/corr/other',
                                 'category': 'preprint', 'status': 'review_version_specific_category'}]
        annotate_category_reviews(rows, {review['dblp_key']: review})
        self.assertEqual(rows[1]['status'], 'review_version_specific_category')

    def test_invalid_or_duplicate_review_rows_are_rejected(self):
        base = self.review()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'publication_category_reviews.csv'
            for rows in [[base, base], [{**base, 'status': 'guessed'}],
                         [{**base, 'reviewed_on': 'yesterday'}], [{**base, 'evidence_url': 'file:///local'}]]:
                with self.subTest(rows=rows):
                    with path.open('w', encoding='utf-8', newline='') as target:
                        writer = csv.DictWriter(target, fieldnames=REVIEW_FIELDS)
                        writer.writeheader(); writer.writerows(rows)
                    with self.assertRaises(ValueError):
                        load_category_reviews(path)

    def test_persistent_ledger_loads_independently_of_working_directory(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            relocated = Path(directory) / 'different checkout'
            (relocated / 'scripts').mkdir(parents=True)
            (relocated / 'data').mkdir()
            for name in ('publication_categories.py', 'publication_category_reviews.py'):
                shutil.copyfile(root / 'scripts' / name, relocated / 'scripts' / name)
            for name in ('publication_category_mappings.csv', 'publication_category_reviews.csv'):
                shutil.copyfile(root / 'data' / name, relocated / 'data' / name)
            output = subprocess.check_output([sys.executable, '-c',
                'from scripts.publication_category_reviews import load_category_reviews; '
                'from scripts.publication_categories import load_category_mappings; '
                'print(len(load_category_reviews()),len(load_category_mappings()))'],
                cwd=relocated, text=True)
            self.assertEqual(int(output.split()[0]), len(load_category_reviews()))
            self.assertGreater(int(output.split()[1]), 0)

    def test_conflicting_confirmed_review_stops_the_audit_after_writing_evidence(self):
        review = self.review()
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            row = {**review, 'scope': 'side', 'venue_label': 'Example', 'work_id': 'example'}
            with (data / 'dblp_publication_entities_for_db_people.csv').open('w', encoding='utf-8', newline='') as target:
                writer = csv.DictWriter(target, fieldnames=list(row))
                writer.writeheader(); writer.writerow(row)
            evidence = {'snapshot': 'example', 'records': {review['dblp_key']: {}}, 'parents': {}}
            with patch.object(sys, 'argv', ['audit_publication_categories.py', '--data-dir', str(data)]), \
                 patch.object(audit_publication_categories, 'read_snapshot_evidence', return_value=evidence), \
                 patch.object(audit_publication_categories, 'load_category_reviews', return_value={review['dblp_key']: review}), \
                 contextlib.redirect_stdout(io.StringIO()), \
                 self.assertRaisesRegex(RuntimeError, 'confirmed publication review'):
                audit_publication_categories.main()
            with (data / 'reports/publication_category_review.csv').open(encoding='utf-8', newline='') as source:
                flagged = list(csv.DictReader(source))
            self.assertEqual(flagged[0]['status'], 'review_assignment_conflict')


if __name__ == '__main__':
    unittest.main()
