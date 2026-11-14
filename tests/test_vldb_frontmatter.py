import sys
import tempfile
import sqlite3
import contextlib
import io
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import scrape_vldb_reviewers as vldb
from service_roles import service_values
from home_population import PC_ROLES


class VldbFrontMatterTests(unittest.TestCase):
    def parse(self, industrial='', demo='', volume=18):
        return vldb.parse_vldb_frontmatter_pages([
            'Proceedings of the VLDB Endowment\nVolume 18, No. 12 - August 2025',
            'INDUSTRIAL TRACK CHAIRS AND REVIEWERS - Vol. 18\n'
            'Industrial Track Program Chairs\nAlice Chair (Example University)\n'
            'Industrial Track Program Committee Members\n' + industrial,
            'DEMONSTRATION TRACK CHAIRS AND REVIEWERS - Vol. 18\n'
            'Demonstration Track Program Chairs\nBob Chair (Other University)\n'
            'Demonstration Track Associate Editors\nCarol Editor (Research Lab)\n'
            'Demonstration Track Program Committee Members\n' + demo,
            'TUTORIAL TRACK CHAIRS AND REVIEWERS - Vol. 18\nTutorial Track Reviewers\nExcluded Person (Institute)'
        ], conference_year=2025, expected_volume=volume, source_url='https://example.test/front.pdf', fetched_at='now')

    def test_wrapped_affiliations_and_broken_outer_parentheses_preserve_next_people(self):
        rows = self.parse(
            'Gustavo Example (ETH (University), Switzerland)\n'
            'Lyublena Example (ex-Meta (now: Antova.Rocks, USA)\n'
            'AnHai Example (University of Wisconsin (Madison, USA)\n'
            'Next Person (Oracle, USA)',
            'Wrapped Person (University of California,\nBerkeley)\nLast Person (CWI)')
        by_name = {r.name: r for r in rows}
        self.assertEqual(by_name['Gustavo Example'].affiliation, 'ETH (University), Switzerland')
        self.assertEqual(by_name['Lyublena Example'].affiliation, 'ex-Meta (now: Antova.Rocks, USA)')
        self.assertEqual(by_name['AnHai Example'].affiliation, 'University of Wisconsin (Madison, USA)')
        self.assertIn('Next Person', by_name)
        self.assertEqual(by_name['Wrapped Person'].affiliation, 'University of California, Berkeley')
        self.assertNotIn('Excluded Person', by_name)
        self.assertEqual(by_name['Carol Editor'].role, 'demo_associate_editor')
        self.assertIn('PDF page 3', by_name['Wrapped Person'].raw_text)

    def test_wrong_volume_or_missing_track_fails_closed(self):
        with self.assertRaises(ValueError):
            self.parse('Industry Person (Lab)', 'Demo Person (Lab)', volume=17)
        with self.assertRaises(ValueError):
            self.parse('Industry Person (Lab)', '')

    def test_explicit_2025_heading_typo_does_not_lose_industry_roster(self):
        pages = ['Volume 18, No. 12 - August 2025',
                 'NDUSTRIAL TRACK CHAIRS AND REVIEWERS - Vol. 18\nIndustrial Track PC Chairs\nAlice Person (Lab)\nIndustrial Track Reviewers\nOther Person (Lab)',
                 'DEMONSTRATION TRACK CHAIRS AND REVIEWERS - Vol. 18\nDemonstration Track PC Chairs\nBob Person (Lab)\nDemonstration Track Reviewers\nThird Person (Lab)']
        rows = vldb.parse_vldb_frontmatter_pages(pages, conference_year=2025, expected_volume=18, source_url='https://example.test', fetched_at='now')
        self.assertEqual(len(rows), 4)

    def test_technical_associate_editors_qualify_without_becoming_research_track(self):
        rows = self.parse('Industry Person (Lab)', 'Demo Person (Lab)')
        ae = next(r for r in rows if r.role == 'demo_associate_editor')
        self.assertEqual(service_values('VLDB', ae), ('senior_reviewer', 'demo', 1))
        self.assertIn(ae.role, PC_ROLES)

    def test_pdf_failure_preserves_the_previously_complete_year(self):
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / 'vldb.sqlite'
            previous = vldb.ReviewerRecord(2025, 18, '2024/2025', 'review_board_member',
                'Existing Person', 'Example University', 'existing person', 'original evidence',
                'https://example.test/old', 'old-date')
            conn = vldb.open_db(str(database))
            vldb.save_records(conn, [previous], conference_year=2025, pvldb_volume=18, source_pages=[])
            conn.close()
            with patch.object(vldb, 'fetch_page', side_effect=[(b'board', 'new'), (b'officers', 'new'), RuntimeError('PDF unavailable')]), \
                 patch.object(vldb, 'parse_vldb_review_board_page', return_value=[previous]), \
                 patch.object(vldb, 'parse_vldb_officers_page', return_value=[previous]), \
                 contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(vldb.main(['--years', '2025', '--db', str(database), '--delay', '0']), 1)
            with contextlib.closing(sqlite3.connect(database)) as conn:
                self.assertEqual(conn.execute('SELECT name,raw_text,fetched_at FROM reviewer_records').fetchall(),
                                 [('Existing Person', 'original evidence', 'old-date')])
                self.assertEqual(conn.execute('SELECT COUNT(*) FROM source_pages').fetchone()[0], 0)


if __name__ == '__main__':
    unittest.main()
