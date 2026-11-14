import csv
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.focus_population import db_paper_decision, page_count, publication_profile, apply_focus_population, exclusions, cohort_summary
from scripts.home_population import home_population_summary
from scripts.build_matrix_stats import build_stats


class FocusPopulationTests(unittest.TestCase):
    def test_ratio_boundaries_and_evidence_minimum(self):
        self.assertEqual(publication_profile(4, 2, [2020, 2021, 2022])['label'], 'DB-focused')
        self.assertEqual(publication_profile(2, 4, [2020, 2021, 2022])['label'], 'ML-focused')
        self.assertEqual(publication_profile(3, 2, [2020, 2021, 2022])['label'], 'Mixed')
        self.assertEqual(publication_profile(5, 0, [2020, 2021, 2022])['label'], 'DB-focused')
        self.assertEqual(publication_profile(4, 0, [2020, 2021, 2022])['label'], 'Limited evidence')
        self.assertEqual(publication_profile(50, 0, [2020, 2020])['label'], 'Limited evidence')
        self.assertEqual(publication_profile(0, 0, [])['label'], 'Limited evidence')

    def test_page_ranges_and_conference_publication_journals(self):
        self.assertEqual(page_count('42:1-42:18'), 18)
        self.assertEqual(page_count('100-105'), 6)
        self.assertIsNone(page_count('abstract'))
        for key, journal in [('journals/pacmmod/a', 'Proc. ACM Manag. Data'), ('journals/pvldb/a', 'Proc. VLDB Endow.')]:
            p = {'dblpKey': key, 'journal': journal, 'title': 'A research paper'}
            self.assertTrue(db_paper_decision(p, {'pages': '1-12'})[0])
            self.assertFalse(db_paper_decision(p, {'pages': '1-4'})[0])
            self.assertFalse(db_paper_decision(p, None)[0])

    def test_service_fallback_requires_ratio_and_two_distinct_years_of_each_kind(self):
        profile = publication_profile(2, 1, [2020, 2021], db_years=[2020, 2021], service_years=[2022, 2023])
        self.assertEqual(profile['label'], 'DB-focused')
        self.assertEqual(profile['basis'], 'publication_and_service')
        for D, M, db_years, service_years in [
            (1, 0, [2020], [2022, 2023]), (2, 2, [2020, 2021], [2022, 2023]),
            (2, 0, [2020, 2020], [2022, 2023]), (2, 0, [2020, 2021], [2022, 2022]),
        ]:
            with self.subTest(D=D, M=M, db_years=db_years, service_years=service_years):
                self.assertEqual(publication_profile(D, M, [2020, 2021], db_years=db_years, service_years=service_years)['label'], 'Limited evidence')

    def test_service_fallback_uses_qualifying_pc_years_and_deduplicated_db_works(self):
        def paper(key, year):
            return {'dblpKey': key, 'workId': key, 'title': 'A research paper', 'booktitle': 'ICDE', 'scope': 'side', 'year': year}
        papers = [paper('conf/icde/a', 2020), paper('conf/icde/b', 2021)]
        person = {'id': 'dblp:a', 'dblpPid': 'a', 'name': 'A', 'ml': {'papers': papers}, 'db': {'events': [
            {'venue': 'ICDE', 'year': 2022, 'role': 'program_committee_member'},
            {'venue': 'VLDB', 'year': 2022, 'role': 'review_board_member'},
            {'venue': 'ICDE', 'year': 2023, 'role': 'general_chair'},
            {'venue': 'ICDE', 'year': 2023, 'role': 'shadow_pc_member'},
            {'venue': 'ICDE', 'year': 2026, 'role': 'program_committee_member'},
        ]}}
        records = {p['dblpKey']: {'pages': '1-12'} for p in papers}
        with tempfile.TemporaryDirectory() as directory, patch('scripts.focus_population.read_metadata', return_value=records):
            included, _ = apply_focus_population([person], Path(directory))
            self.assertEqual(included, [])
            person['db']['events'].append({'venue': 'ICDE', 'year': 2023, 'role': 'program_committee_member'})
            included, policy = apply_focus_population([person], Path(directory))
            self.assertEqual(len(included), 1)
            self.assertEqual(policy['serviceFallbackCount'], 1)
            self.assertEqual(person['focus']['serviceYears'], 2)
            # Multiple dated versions of one work cannot satisfy the two-paper requirement.
            papers[1]['workId'] = papers[0]['workId']
            included, _ = apply_focus_population([person], Path(directory))
            self.assertEqual(included, [])

    def test_workshops_journals_companions_and_nonpapers_excluded(self):
        for key, booktitle, journal in [
            ('conf/sigmod/a', 'HILDA@SIGMOD', ''),
            ('conf/sigmod/a', 'SIGMOD Companion', ''),
            ('conf/icde/a', 'ICDE Workshops', ''),
            ('journals/vldb/a', '', 'VLDB J.'),
            ('journals/tods/a', '', 'ACM Trans. Database Syst.'),
        ]:
            self.assertFalse(db_paper_decision({'dblpKey': key, 'booktitle': booktitle, 'journal': journal}, {'pages': '1-20'})[0])
        for title in ['Editorial', 'Front matter', 'A tutorial on joins', 'A demonstration of a system', 'Reproducibility Report for Paper X']:
            self.assertFalse(db_paper_decision({'dblpKey': 'conf/sigmod/a', 'booktitle': 'SIGMOD Conference', 'title': title}, {'pages': '1-12'})[0])

    def test_unpaginated_cidr_and_unknown_lengths(self):
        p = {'dblpKey': 'conf/cidr/a', 'booktitle': 'CIDR', 'title': 'A new system'}
        self.assertTrue(db_paper_decision(p, {'pages': ''})[0])
        self.assertFalse(db_paper_decision(dict(p, dblpKey='conf/icde/a', booktitle='ICDE'), {'pages': ''})[0])

    def test_reviewed_nonresearch_records_are_excluded_by_exact_key(self):
        reviewed = [row for row in exclusions() if row['field'] == 'dblp_key']
        self.assertGreaterEqual(len(reviewed), 33)
        for row in reviewed:
            key = row['value']
            paper = {'dblpKey': key, 'title': 'Title need not identify the track'}
            if key.startswith('conf/sigmod/'):
                paper['booktitle'] = 'SIGMOD Conference'
            elif key.startswith('conf/pods/'):
                paper['booktitle'] = 'PODS'
            elif key.startswith('conf/cidr/'):
                paper['booktitle'] = 'CIDR'
            elif key.startswith('conf/icde/'):
                paper['booktitle'] = 'ICDE'
            else:
                paper['journal'] = 'Proc. ACM Manag. Data' if '/pacmmod/' in key else 'Proc. VLDB Endow.'
            with self.subTest(key=key):
                pages = '' if key.startswith('conf/cidr/') else '1-8'
                self.assertEqual(db_paper_decision(paper, {'pages': pages}), (False, row['reason']))
                # A key-specific review must not reject another research record.
                control = dict(paper, dblpKey=key + 'Research')
                self.assertTrue(db_paper_decision(control, {'pages': '1-6'})[0])
                self.assertTrue(row['evidence_url'].startswith('https://'))

    def test_reviewed_sigmod_2022_tutorial_is_not_six_page_research_evidence(self):
        tutorial = {'dblpKey': 'conf/sigmod/NargesianAJ22', 'booktitle': 'SIGMOD Conference',
                    'title': 'Responsible Data Integration: Next-generation Challenges.'}
        self.assertEqual(db_paper_decision(tutorial, {'pages': '2458-2464'}),
                         (False, 'Tutorial (official SIGMOD 2022 track)'))
        # Generic title heuristics miss this tutorial. A key review must not
        # suppress a research record with similarly worded motivation.
        self.assertTrue(db_paper_decision(dict(tutorial, dblpKey='conf/sigmod/research'),
                                         {'pages': '2458-2464'})[0])
        papers = [dict(tutorial, workId='tutorial', scope='side', year=2022)]
        for i, year in enumerate([2019, 2020, 2021]):
            papers.append({'dblpKey': f'conf/icde/research{i}', 'workId': f'db{i}',
                           'booktitle': 'ICDE', 'title': 'Research paper', 'scope': 'side', 'year': year})
        for i, year in enumerate([2023, 2024]):
            papers.append({'dblpKey': f'conf/icml/research{i}', 'workId': f'ml{i}',
                           'venue': 'ICML', 'title': 'ML research paper', 'scope': 'main', 'year': year})
        person = {'id': 'fixture', 'dblpPid': 'fixture', 'name': 'Fixture',
                  'ml': {'papers': papers}, 'db': {'events': [
                      {'venue': 'SIGMOD', 'year': 2023, 'role': 'pc_member'}]}}
        metadata = {p['dblpKey']: {'pages': '1-12'} for p in papers[:4]}
        with tempfile.TemporaryDirectory() as directory, patch('scripts.focus_population.read_metadata', return_value=metadata):
            included, _ = apply_focus_population([person], Path(directory))
        self.assertEqual(included, [])
        self.assertEqual((person['focus']['dbPapers'], person['focus']['mlPapers']), (3, 2))
        self.assertEqual(person['focus']['label'], 'Mixed')

    def test_six_page_research_and_numeric_article_ids_remain_distinct(self):
        for title in ['A six-page research paper', 'Demonstration-guided query optimization',
                      'Optimizing tutorial recommendation queries']:
            with self.subTest(title=title):
                paper = {'dblpKey': 'conf/icde/research', 'booktitle': 'ICDE', 'title': title}
                self.assertTrue(db_paper_decision(paper, {'pages': '2051-2056'})[0])
        paper = {'dblpKey': 'journals/pacmmod/PadmanabhaSS24', 'journal': 'Proc. ACM Manag. Data',
                 'title': 'A Dichotomy in the Complexity of Consistent Query Answering for Two Atom Queries With Self-Join.'}
        self.assertEqual(db_paper_decision(paper, {'pages': '74'}),
                         (False, 'Full-paper length cannot be verified'))

    def test_verified_sigmod_2017_tutorial_is_not_db_research_evidence(self):
        paper = {'dblpKey': 'conf/sigmod/Kumar0017', 'booktitle': 'SIGMOD Conference',
                 'title': 'Data Management in Machine Learning: Challenges, Techniques, and Systems.'}
        self.assertFalse(db_paper_decision(paper, {'pages': '1717-1722'})[0])
        self.assertTrue(db_paper_decision({**paper, 'dblpKey': 'conf/sigmod/unrelated'}, {'pages': '1717-1722'})[0])

    def test_reviewed_exclusions_require_provenance_and_override_generic_reasons(self):
        reviewed = next(row for row in exclusions() if row['value'] == 'conf/sigmod/Omidvar-Tehrani19')
        paper = {'dblpKey': reviewed['value'], 'booktitle': 'SIGMOD Conference', 'title': 'Tutorial: changed title'}
        self.assertEqual(db_paper_decision(paper, {'pages': '1-6'}), (False, reviewed['reason']))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'exclusions.csv'
            path.write_text('field,match_type,value,reason,evidence_url,verified_on\n'
                            'dblp_key,exact,conf/sigmod/test,Tutorial,,\n', encoding='utf-8')
            with patch('scripts.focus_population.EXCLUSIONS_PATH', path):
                with self.assertRaisesRegex(ValueError, 'needs evidence and a review date'):
                    exclusions()

    def test_reviewed_exclusions_reject_duplicate_keys_and_invalid_provenance(self):
        original = {'field': 'dblp_key', 'match_type': 'exact', 'value': 'conf/sigmod/reviewed',
                    'reason': 'Tutorial', 'evidence_url': 'https://sigmod2019.org/sigmod_tutorials',
                    'verified_on': '2026-11-03'}
        invalid = [
            ([original, dict(original)], 'Duplicate or conflicting'),
            ([original, dict(original, reason='Editorial')], 'Duplicate or conflicting'),
            ([dict(original, verified_on='2026-02-30')], 'ISO review date'),
            ([dict(original, verified_on='20261103')], 'ISO review date'),
            ([dict(original, verified_on='03/11/2026')], 'ISO review date'),
            ([dict(original, evidence_url='http://sigmod2019.org/sigmod_tutorials')], 'HTTPS evidence URL'),
            ([dict(original, evidence_url='https:///missing-host')], 'HTTPS evidence URL'),
            ([dict(original, evidence_url='https://bad host/source')], 'HTTPS evidence URL'),
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'exclusions.csv'
            for rows, message in invalid:
                with self.subTest(rows=rows):
                    with path.open('w', encoding='utf-8', newline='') as handle:
                        writer = csv.DictWriter(handle, fieldnames=original)
                        writer.writeheader()
                        writer.writerows(rows)
                    with patch('scripts.focus_population.EXCLUSIONS_PATH', path):
                        with self.assertRaisesRegex(ValueError, message):
                            exclusions()

    def test_reviewed_tutorial_changes_boundary_profile_and_preserves_audit_evidence(self):
        # The real boundary example: four DB works previously included a six-page tutorial.
        db_keys = ['journals/pvldb/SeleznovaOAS20', 'conf/edbt/Amer-YahiaEOBF19',
                   'conf/sigmod/Omidvar-Tehrani19', 'conf/edbt/Amer-YahiaTRS15']
        papers = []
        for key, year in zip(db_keys, [2020, 2019, 2019, 2015]):
            papers.append({'dblpKey': key, 'workId': 'dblp-work:' + key, 'year': year,
                           'scope': 'side', 'title': 'Data Pipelines for User Group Analytics.' if key == db_keys[2] else 'Research paper',
                           'journal': 'Proc. VLDB Endow.' if key.startswith('journals/') else '',
                           'booktitle': 'SIGMOD Conference' if key.startswith('conf/sigmod/') else 'EDBT'})
        for key, venue, year in [('conf/icml/GuinetODC24', 'ICML', 2024), ('conf/iclr/LingamOSGGLHD25', 'ICLR', 2025)]:
            papers.append({'dblpKey': key, 'workId': 'dblp-work:' + key, 'year': year,
                           'scope': 'main', 'venue': venue, 'title': 'ML research paper'})
        person = {'id': 'dblp:200/8321', 'dblpPid': '200/8321', 'name': 'Behrooz Omidvar-Tehrani',
                  'db': {'events': [{'venue': 'SIGMOD', 'year': 2023, 'role': 'pc_member'}]}, 'ml': {'papers': papers}}
        records = {key: {'pages': '2048-2053' if key == db_keys[2] else '1-12'} for key in db_keys}
        title_rules = tuple(row for row in exclusions() if row['field'] == 'title')
        with tempfile.TemporaryDirectory() as directory, patch('scripts.focus_population.read_metadata', return_value=records):
            with patch('scripts.focus_population.exclusions', return_value=title_rules):
                included, previous_policy = apply_focus_population([person], Path(directory))
                self.assertEqual(len(included), 1)
                self.assertEqual((person['focus']['dbPapers'], person['focus']['mlPapers']), (4, 2))
            included, policy = apply_focus_population([person], Path(directory))
            self.assertEqual(included, [])
            self.assertEqual((person['focus']['dbPapers'], person['focus']['mlPapers']), (3, 2))
            self.assertEqual(person['focus']['label'], 'Mixed')
            self.assertEqual(person['focus']['publicationYears'], 5)
            self.assertEqual(policy['candidateCount'], previous_policy['candidateCount'])
            with (Path(directory) / 'reports' / 'focus_publication_audit.csv').open(encoding='utf-8-sig', newline='') as handle:
                tutorial = next(row for row in csv.DictReader(handle) if row['key'] == db_keys[2])
            self.assertEqual(tutorial['counted'], 'False')
            self.assertEqual(tutorial['exclusion_evidence_url'], 'https://sigmod2019.org/sigmod_tutorials')
            self.assertEqual(tutorial['exclusion_verified_on'], '2026-11-03')

    def test_work_deduplication_version_evidence_and_committee_qualification(self):
        papers = [{'dblpKey': f'conf/icde/p{i}', 'workId': f'w{i}', 'title': 'A research paper',
                   'booktitle': 'ICDE', 'scope': 'side', 'year': 2020 + i % 3} for i in range(5)]
        # A linked journal version must not erase the conference evidence or count twice.
        papers[0] = dict(papers[0], dblpKey='journals/vldb/extended', booktitle='', resolvedVersions=[papers[0]],
                         personVersionDblpKeys=['journals/vldb/extended', papers[0]['dblpKey']])
        papers.append(dict(papers[1]))
        person = {'id': 'dblp:a', 'dblpPid': 'a', 'name': 'A', 'db': {'events': [
            {'venue': 'ICDE', 'year': 2022, 'role': 'program_committee_member'}]}, 'ml': {'papers': papers}}
        records = {f'conf/icde/p{i}': {'pages': '1-12'} for i in range(5)}
        with tempfile.TemporaryDirectory() as directory, patch('scripts.focus_population.read_metadata', return_value=records):
            included, policy = apply_focus_population([person], Path(directory))
            self.assertEqual(len(included), 1)
            self.assertEqual(person['focus']['dbPapers'], 5)
            self.assertEqual(policy['candidateCount'], 1)
            person['db']['events'][0]['role'] = 'general_chair'
            included, policy = apply_focus_population([person], Path(directory))
            self.assertEqual(included, [])
            self.assertEqual(policy['candidateCount'], 0)

    def test_focus_requires_person_ownership_of_each_qualifying_version(self):
        for ownership in ['preprint_only', 'both', 'empty', 'legacy']:
            with self.subTest(ownership=ownership), tempfile.TemporaryDirectory() as directory:
                papers, records = [], {}
                for i, year in enumerate([2020, 2021]):
                    db_key, preprint_key = f'conf/icde/p{i}', f'journals/corr/p{i}'
                    db_version = {'dblpKey': db_key, 'title': 'Research paper', 'booktitle': 'ICDE',
                                  'year': year, 'scope': 'side'}
                    paper = {'dblpKey': preprint_key, 'workId': f'w{i}', 'title': 'Preprint',
                             'year': year, 'scope': 'side', 'resolvedVersions': [db_version]}
                    if ownership != 'legacy':
                        paper['personVersionDblpKeys'] = {'preprint_only': [preprint_key],
                                                         'both': [preprint_key, db_key], 'empty': []}[ownership]
                    papers.append(paper)
                    records[db_key] = {'pages': '1-12'}
                person = {'id': 'a', 'dblpPid': 'a', 'name': 'A', 'ml': {'papers': papers}, 'db': {'events': [
                    {'venue': 'ICDE', 'role': 'program_committee_member', 'year': year} for year in [2022, 2023]]}}
                with patch('scripts.focus_population.read_metadata', return_value=records):
                    included, _ = apply_focus_population([person], Path(directory))
                expected = ownership == 'both'
                self.assertEqual(len(included), int(expected))
                self.assertEqual(person['focus']['dbPapers'], 2 if expected else 0)
                self.assertEqual(person['focus']['label'], 'DB-focused' if expected else 'Limited evidence')
                with (Path(directory) / 'reports/focus_publication_audit.csv').open(encoding='utf-8-sig', newline='') as f:
                    audit = list(csv.DictReader(f))
                self.assertEqual({r['counted'] for r in audit}, {str(expected)})
                if not expected:
                    self.assertEqual({r['reason'] for r in audit}, {'Version not authored by this person'})

    def test_legacy_selected_record_counts_but_explicit_empty_ownership_does_not(self):
        papers = [{'dblpKey': f'conf/icde/p{i}', 'workId': f'w{i}', 'title': 'Research',
                   'booktitle': 'ICDE', 'scope': 'side', 'year': 2020 + i} for i in range(2)]
        papers.append({'dblpKey': 'conf/icml/ml', 'workId': 'ml', 'title': 'ML research',
                       'venue': 'ICML', 'scope': 'main', 'year': 2022, 'personVersionDblpKeys': []})
        person = {'id': 'a', 'dblpPid': 'a', 'name': 'A', 'ml': {'papers': papers}, 'db': {'events': [
            {'venue': 'ICDE', 'role': 'program_committee_member', 'year': year} for year in [2022, 2023]]}}
        with tempfile.TemporaryDirectory() as directory, patch('scripts.focus_population.read_metadata',
                return_value={p['dblpKey']: {'pages': '1-12'} for p in papers[:2]}):
            apply_focus_population([person], Path(directory))
            self.assertEqual((person['focus']['dbPapers'], person['focus']['mlPapers']), (2, 0))
            self.assertTrue(person['focus']['included'])

    def test_home_respects_a_generated_exclusion(self):
        person = {'dblpPid': 'a', 'focus': {'included': False}, 'db': {'events': [
            {'venue': 'ICDE', 'year': 2022, 'role': 'program_committee_member'}]}}
        self.assertEqual(home_population_summary([person])['total'], 0)

    def test_matrix_denominator_uses_the_same_filtered_cohort(self):
        p = {'id': 'dblp:a', 'dblpPid': 'a', 'db': {'events': [{'venue': 'ICDE', 'year': 2022}]},
             'ml': {'papers': [{'scope': 'main', 'venue': 'ICML', 'year': 2024}]}}
        payload = {'meta': {'populationFocusPolicyVersion': 1}, 'people': [p]}
        stats = build_stats(payload, [('ICDE', 'a', 2022), ('ICDE', 'excluded', 2022)])
        self.assertEqual(stats['dbVenueResolvedPeople']['ICDE'], 1)
        self.assertEqual(stats['matrix']['ICDE']['ICML']['percentOfResolvedDbPeople'], 100)

    def test_cohort_summary_affiliation_diagnostics_match_retained_review_rows(self):
        from scripts.build_site_data import primary_affiliation_review_rows
        def person(pid, confidence, conflict):
            return {'dblpPid': pid, 'name': pid, 'ml': {'papers': [], 'hasMain': False, 'hasSide': False},
                    'db': {'events': []}, 'provenance': {'primaryAffiliation': {
                        'confidence': confidence, 'hasConflict': conflict}}}
        people = [person('high', 'high', False), person('conflict', 'high', True),
                  person('low', 'low', True), person('excluded', 'low', True)]
        previous = {'publicationClassCounts': {}, 'primaryAffiliationConflicts': 3,
                    'primaryAffiliationLowConfidence': 2, 'primaryAffiliationReviewRows': 3}
        kept = people[:3]
        result = cohort_summary(kept, previous)
        self.assertEqual(result['primaryAffiliationConflicts'], 2)
        self.assertEqual(result['primaryAffiliationLowConfidence'], 1)
        self.assertEqual(result['primaryAffiliationReviewRows'], len(primary_affiliation_review_rows(kept)))
        self.assertEqual(result['primaryAffiliationReviewRows'], 2)
        self.assertEqual(previous['primaryAffiliationReviewRows'], 3)


if __name__ == '__main__':
    unittest.main()
