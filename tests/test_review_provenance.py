"""Review labels must follow the exact decision, not spread to unrelated data."""
from __future__ import annotations

import csv
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from review_provenance import (
    ATTRIBUTIONS_FILE, annotate_project_reviews, decision_fingerprint,
    load_project_reviews, review_key,
)
from build_site_data import browser_payload_and_detail_files


class ReviewProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def write(self, filename, rows):
        with (self.root / filename).open('w', encoding='utf-8', newline='') as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)

    def identity(self, filename='reviewed_person_identities.csv', method=None):
        row = dict(service_name='Alice Smith', target_pid='alice',
                   reviewed_on='2026-11-09', reason='Institutional profile confirms this match.',
                   evidence_urls_json=json.dumps(['https://example.org/alice']))
        self.write(filename, [row])
        if method:
            self.write(ATTRIBUTIONS_FILE, [dict(
                review_file=filename, review_key=review_key(filename, row),
                review_method=method, decision_sha256=decision_fingerprint(row))])
        return row

    def person(self, **extra):
        return dict(id='dblp:alice', dblpPid='alice', name='Alice Smith',
                    aliases={'db': ['Alice Smith']}, ml={'papers': []}, **extra)

    def test_unknown_origin_is_not_automatically_ai_assisted(self):
        self.identity()
        review = load_project_reviews(self.root)['identity']['alice'][0]
        self.assertEqual(review['method'], 'project_review')

    def test_attribution_binds_to_the_entire_decision_and_requires_recheck_after_edit(self):
        row = self.identity(method='ai_assisted')
        self.assertEqual(load_project_reviews(self.root)['identity']['alice'][0]['method'], 'ai_assisted')
        row['reason'] = 'Changed evidence or decision.'
        self.write('reviewed_person_identities.csv', [row])
        with self.assertRaisesRegex(ValueError, 'needs rechecking'):
            load_project_reviews(self.root)

    def test_unsafe_evidence_links_are_rejected(self):
        row = self.identity()
        row['evidence_urls_json'] = '["javascript:alert(1)"]'
        self.write('reviewed_person_identities.csv', [row])
        with self.assertRaisesRegex(ValueError, 'HTTPS'):
            load_project_reviews(self.root)

    def test_identity_review_does_not_transfer_to_other_people_or_papers(self):
        self.identity(method='human')
        alice = self.person()
        alice['ml']['papers'] = [{'dblpKey': 'conf/test/unreviewed', 'scope': 'main'}]
        bob = dict(dblpPid='bob', name='Alice Smith', ml={'papers': []})
        unobserved = self.person()
        unobserved.update(name='Another Name', aliases={'db': []})
        annotate_project_reviews([alice, bob, unobserved], load_project_reviews(self.root))
        self.assertEqual(alice['provenance']['identity']['projectReviews'][0]['method'], 'human')
        self.assertNotIn('provenance', bob)
        self.assertNotIn('provenance', unobserved)
        self.assertNotIn('provenance', alice['ml']['papers'][0])

    def test_authorship_review_follows_owned_version_not_coauthor_only_version(self):
        self.write('reviewed_publication_authors.csv', [dict(
            dblp_key='journals/corr/preprint', target_pid='alice', source_pid='wrong',
            author_signature='Alice Smith', verified_on='2026-11-09',
            source_url='https://example.org/paper', reason='Author block confirms Alice.')])
        alice = self.person()
        alice['ml']['papers'] = [
            dict(dblpKey='conf/icml/published', personVersionDblpKeys=['journals/corr/preprint']),
            dict(dblpKey='conf/icml/unrelated', versionDblpKeys=['journals/corr/preprint'], personVersionDblpKeys=[]),
        ]
        annotate_project_reviews([alice], load_project_reviews(self.root))
        owned, unrelated = alice['ml']['papers']
        self.assertEqual(owned['provenance']['projectReviews'][0]['personName'], 'Alice Smith')
        self.assertNotIn('provenance', unrelated)

    def test_restored_display_accents_do_not_hide_an_applied_identity_review(self):
        row = self.identity()
        row['service_name'] = 'Renee Miller'
        self.write('reviewed_person_identities.csv', [row])
        person = self.person()
        person.update(name='Renée J. Miller', aliases={'db': ['Renée Miller']})
        annotate_project_reviews([person], load_project_reviews(self.root))
        self.assertEqual(person['provenance']['identity']['projectReviews'][0]['serviceName'], 'Renee Miller')

    def test_category_requires_confirmed_matching_metadata_not_just_the_same_key(self):
        row = dict(dblp_key='conf/test/paper', title='A paper.', year='2025',
                   booktitle='Test Workshop', journal='', record_type='inproceedings',
                   category='workshop', status='confirmed', evidence_url='https://example.org/venue',
                   reason='Proceedings identify this workshop.', reviewed_on='2026-11-09')
        self.write('publication_category_reviews.csv', [row])
        paper = dict(dblpKey=row['dblp_key'], title=row['title'], year=2025, booktitle=row['booktitle'],
                     journal='', recordType='inproceedings', publicationClass='workshop', scope='side')
        alice = self.person()
        alice['ml']['papers'] = [paper]
        annotate_project_reviews([alice], load_project_reviews(self.root))
        self.assertEqual(paper['provenance']['projectReviews'][0]['kind'], 'category')
        for field, value in [('title', 'Changed title.'), ('year', 2026), ('publicationClass', 'journal')]:
            changed = {**paper, field: value}
            changed.pop('provenance')
            alice['ml']['papers'] = [changed]
            annotate_project_reviews([alice], load_project_reviews(self.root))
            self.assertNotIn('provenance', changed)
        row['status'] = 'pending'
        self.write('publication_category_reviews.csv', [row])
        paper.pop('provenance')
        alice['ml']['papers'] = [paper]
        annotate_project_reviews([alice], load_project_reviews(self.root))
        self.assertNotIn('provenance', paper)

    def test_affiliation_distinguishes_applied_review_from_older_evidence(self):
        self.write('publication_affiliation_evidence.csv', [dict(
            dblp_pid='alice', dblp_key='conf/test/paper', affiliation='Example University',
            verified_on='2026-11-09', source_url='https://example.org/paper', reason='Author-specific address.')])
        primary = dict(decisionKey='verified_publication_affiliation', publicationAffiliationYear=2025,
                       publicationAffiliationEvidence=[dict(dblpKey='conf/test/paper', year=2025)])
        alice = self.person(provenance={'primaryAffiliation': primary})
        annotate_project_reviews([alice], load_project_reviews(self.root))
        self.assertTrue(primary['projectReviews'][0]['usedForDisplay'])
        primary['decisionKey'] = 'automatic_publication_affiliation'
        annotate_project_reviews([alice], load_project_reviews(self.root))
        self.assertFalse(primary['projectReviews'][0]['usedForDisplay'])

    def test_review_evidence_survives_lazy_browser_export(self):
        self.identity(method='ai_assisted')
        alice = self.person()
        annotate_project_reviews([alice], load_project_reviews(self.root))
        payload, details, _ = browser_payload_and_detail_files({'people': [alice]})
        self.assertNotIn('provenance', payload['people'][0])
        evidence = json.loads(next(value for key, value in details.items() if key.startswith('evidence-')))
        review = evidence['provenance']['identity']['projectReviews'][0]
        self.assertEqual(review['evidenceUrls'], ['https://example.org/alice'])
        self.assertEqual(review['reviewedOn'], '2026-11-09')


if __name__ == '__main__':
    unittest.main()
