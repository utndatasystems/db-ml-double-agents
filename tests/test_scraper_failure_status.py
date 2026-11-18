"""A failed required source must not masquerade as a successful refresh."""
import contextlib
import io
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import scrape_cidr_reviewers as cidr
import scrape_edbt_reviewers as edbt
import scrape_icde_reviewers as icde
import scrape_sigmod_reviewers as sigmod
import scrape_vldb_reviewers as vldb


class ScraperFailureStatusTests(unittest.TestCase):
    def test_edbt_absent_rosters_are_explicit_and_known_rosters_remain_required(self):
        for year, kind in [(2022, 'industrial'), (2022, 'demo'), (2023, 'industrial')]:
            self.assertEqual(edbt.edbt_track_source_candidates(year, kind), [])
        for year, kind in [(2023, 'demo'), (2024, 'industrial'), (2024, 'demo'), (2025, 'industrial'), (2025, 'demo')]:
            self.assertTrue(edbt.edbt_track_source_candidates(year, kind))

    def test_required_source_failure_returns_nonzero_and_does_not_replace_records(self):
        for module, fetch_name, save_name in [
            (cidr, 'fetch_page', 'save_records'),
            (edbt, 'fetch_and_parse_edbt_source', 'replace_year_records'),
            (icde, 'fetch_page', 'save_records'),
            (sigmod, 'fetch_page', 'save_records'),
            (vldb, 'fetch_page', 'save_records'),
        ]:
            with self.subTest(module=module.__name__), tempfile.TemporaryDirectory() as tmp, contextlib.ExitStack() as stack:
                stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
                stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
                stack.enter_context(patch.object(module, fetch_name, side_effect=RuntimeError('required source unavailable')))
                if module is edbt:
                    stack.enter_context(patch.object(edbt, 'committee_source_candidates', return_value=['https://example.test/committee']))
                save = stack.enter_context(patch.object(module, save_name))
                result = module.main(['--years', '2025', '--db', str(Path(tmp) / 'source.sqlite'), '--delay', '0'])
                self.assertNotEqual(result, 0)
                save.assert_not_called()

    def test_edbt_failed_year_source_metadata_cannot_leak_into_a_later_success(self):
        def research(session, *, year, url, **kwargs):
            row = edbt.ReviewerRecord(year=year, role='program_committee_member', name='Example Person',
                affiliation='Example University', name_key='example person', raw_text='original row',
                source_page=None, source_url=url, fetched_at='new-date')
            return [row], b'committee bytes', 'new-date', 'text/html'
        with tempfile.TemporaryDirectory() as tmp:
            database=Path(tmp)/'edbt.sqlite'
            with patch.object(edbt, 'committee_source_candidates', return_value=['https://example.test/committee']), \
                 patch.object(edbt, 'fetch_and_parse_edbt_source', side_effect=research), \
                 patch.object(edbt, 'EDBT_OFFICER_URLS', {2024:['https://example.test/officers']}), \
                 patch.object(edbt, 'EDBT_TRACK_PC_PAGES', {}), \
                 patch.object(edbt, 'fetch_bytes', side_effect=RuntimeError('officers unavailable')), \
                 contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertNotEqual(edbt.main(['--years','2024','2025','--db',str(database),'--delay','0']),0)
            with contextlib.closing(sqlite3.connect(database)) as conn:
                self.assertEqual(conn.execute('SELECT DISTINCT year FROM reviewer_records').fetchall(),[(2025,)])
                self.assertEqual(conn.execute('SELECT DISTINCT year FROM source_documents').fetchall(),[(2025,)])


if __name__ == '__main__':
    unittest.main()
