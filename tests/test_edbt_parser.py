from __future__ import annotations

import sys
import contextlib
import io
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import scrape_edbt_reviewers as edbt


class EdbtSourceRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.db = Path(self.directory.name) / 'edbt.sqlite'
        self.requests = []
        self.committee = ('<h3>EDBT Program Committee Members</h3><ul>' + ''.join(
            f'<li>Researcher {i}, University {i}</li>' for i in range(20)
        ) + '</ul>').encode()
        self.officers = b'''<h3>General Chairs</h3><p>General Person, University</p>
            <h3>EDBT Program Chairs</h3><p>Program Person, University</p>'''
        self.demo = b'''<h3>Program Committee</h3><ul>
            <li>Demo Person, University</li></ul>'''

    def fetch(self, session, url, **kwargs):
        self.requests.append(url)
        if url.startswith('https:'):
            raise edbt.requests.HTTPError('403 Forbidden')
        if 'EDBT_Committee.html' in url:
            content = self.committee
        elif 'conference_officers.html' in url:
            content = self.officers
        elif 'call_for_demos.html' in url:
            content = self.demo
        else:
            content = b'<h2>Conference homepage</h2>'
        return content, '2026-11-17T00:00:00+00:00', 'text/html'

    def run_scraper(self, year, fetch=None):
        with patch.object(edbt, 'fetch_bytes', side_effect=fetch or self.fetch), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            return edbt.main(['--years', str(year), '--delay', '0', '--db', str(self.db)])

    def test_2022_https_failure_uses_official_http_sources_with_provenance(self):
        self.assertEqual(self.run_scraper(2022), 0)
        with sqlite3.connect(self.db) as conn:
            rows = conn.execute('SELECT role, source_url FROM reviewer_records').fetchall()
        self.assertEqual(len(rows), 22)
        self.assertEqual(sum(role == 'program_committee_member' for role, _ in rows), 20)
        self.assertTrue(all(url.startswith('http://conferences.inf.ed.ac.uk/') for _, url in rows))
        self.assertIn(edbt.EDBT_COMMITTEE_URLS[2022][0], self.requests)

    def test_2023_original_host_supplies_committee_officers_and_demo(self):
        self.assertEqual(self.run_scraper(2023), 0)
        with sqlite3.connect(self.db) as conn:
            rows = conn.execute('SELECT role, source_url FROM reviewer_records').fetchall()
        self.assertEqual(len(rows), 23)
        self.assertIn('demo_pc_member', {role for role, _ in rows})
        self.assertTrue(all(url.startswith('http://edbticdt2023.cs.uoi.gr/') for _, url in rows))
        self.assertFalse(any('www.cs.uoi.gr' in url for url in self.requests))

    def test_missing_required_demo_source_fails_and_preserves_existing_year(self):
        self.assertEqual(self.run_scraper(2023), 0)
        with sqlite3.connect(self.db) as conn:
            before = conn.execute('SELECT * FROM reviewer_records ORDER BY id').fetchall()

        def unavailable_demo(session, url, **kwargs):
            if 'call_for_demos.html' in url:
                raise edbt.requests.Timeout('Source unavailable')
            return self.fetch(session, url, **kwargs)

        self.assertEqual(self.run_scraper(2023, unavailable_demo), 1)
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(conn.execute('SELECT * FROM reviewer_records ORDER BY id').fetchall(), before)


class EdbtStructuralParserTests(unittest.TestCase):
    def test_br_boundaries_preserve_rows_without_guessing(self):
        people = "<br>".join(
            f"Researcher {i}, University {i}, Country" for i in range(1, 21)
        )
        html = f"""
        <h3>EDBT 2025 Senior Program Committee Members</h3>
        <p>Senior Person, Example University, Country</p>
        <h3>EDBT 2025 Program Committee Members</h3>
        <p>{people}</p>
        <h3>EDBT 2025 Industrial Program Committee Members</h3>
        <p>Industry Person, Database Company</p>
        <h3>EDBT 2025 Test of Time Award Committee</h3>
        <p>Award Person, Example University</p>
        """.encode()
        rows = edbt.parse_edbt_committee_html(
            html, year=2025, source_url="https://example.test/committee",
            fetched_at="now", include_chairs=True,
        )
        by_name = {row.name: row.role for row in rows}
        self.assertEqual(by_name["Industry Person"], "industrial_pc_member")
        self.assertEqual(by_name["Award Person"], "award_committee_member")
        self.assertEqual(
            sum(row.role == "program_committee_member" for row in rows), 20
        )

    def test_industrial_call_page(self):
        html = b"""
        <h2>EDBT 2025 Industrial and Application Track Call for Papers</h2>
        <h3>Track Chairs</h3>
        <ul><li>Shimin Chen, Chinese Academy of Sciences, China</li></ul>
        <h3>Program Committee</h3>
        <ul>
          <li>Graham Cormode, University of Warwick</li>
          <li>Yuanyuan Tian, Microsoft</li>
        </ul>
        <h3>Important Dates</h3>
        """
        rows = edbt.parse_edbt_track_pc_html(
            html, year=2025, source_url="https://example.test/iap",
            fetched_at="now", page_kind="industrial",
        )
        self.assertEqual(
            {(row.role, row.name) for row in rows},
            {
                ("industrial_track_chair", "Shimin Chen"),
                ("industrial_pc_member", "Graham Cormode"),
                ("industrial_pc_member", "Yuanyuan Tian"),
            },
        )

    def test_recent_committee_span_rows_preserve_inline_name_and_affiliation(self):
        ordinary = "\n".join(
            f'<span class="brmedium"><b>Researcher {i}</b>, University {i}, Country</span>'
            for i in range(1, 21)
        )
        html = f"""
        <h2>EDBT Program Committee</h2>
        <h3>EDBT 2024 Senior Program Committee Members</h3>
        <p>
          <span class="brmedium"><b>Ashraf Aboulnaga</b>, Qatar Computing Research Institute, Qatar</span>
          <span class="brmedium"><b>Avigdor Gal</b>, Technion, Israel</span>
        </p>
        <h3>EDBT 2024 Program Committee Members</h3>
        <p>{ordinary}</p>
        """.encode()
        rows = edbt.parse_edbt_committee_html(
            html, year=2024, source_url="https://example.test/committee",
            fetched_at="now", include_chairs=True,
        )
        by_name = {row.name: row.affiliation for row in rows}
        self.assertEqual(
            by_name["Ashraf Aboulnaga"],
            "Qatar Computing Research Institute, Qatar",
        )
        self.assertEqual(by_name["Researcher 20"], "University 20, Country")

    def test_officer_heading_wrapped_in_b_and_people_split_by_brmedium(self):
        html = b"""
        <h2>Conference Officers</h2>
        <br><b><h3>General Chairs</h3></b>
        <p>
          <b><a>Giuseppe Polese</a></b>, University of Salerno, Italy
          <span class="brmedium"></span>
          <b><a>Loredana Caruccio</a></b>, University of Salerno, Italy
        </p>
        <br><b><h3>EDBT Program Chairs</h3></b>
        <p>
          <b><a>Letizia Tanca</a></b>, Polytechnic University of Milan, Italy
          <span class="brmedium"></span>
          <b><a>Qiong Luo</a></b>, HKUST &amp; HKUST (Guangzhou), China
        </p>
        <br><b><h3>Tutorial Chairs</h3></b>
        <p><b>C Mohan</b>, Tsinghua University, China</p>
        <br><b><h3>Workshop Chairs</h3></b>
        <p><b>H V Jagadish</b>, University of Michigan, USA</p>
        """
        rows = edbt.parse_edbt_officers_html(
            html, year=2024, source_url="https://example.test/officers",
            fetched_at="now",
        )
        self.assertEqual(
            {(row.role, row.name) for row in rows},
            {
                ("general_chair", "Giuseppe Polese"),
                ("general_chair", "Loredana Caruccio"),
                ("program_committee_chair", "Letizia Tanca"),
                ("program_committee_chair", "Qiong Luo"),
                ("tutorial_chair", "C Mohan"),
                ("workshop_chair", "H V Jagadish"),
            },
        )

    def test_industrial_span_rows_without_comma_after_bold_name(self):
        html = b"""
        <h2>EDBT 2024 Industrial and Application Track Call for Papers</h2>
        <b><h3>Track Chairs</h3></b>
        <ul>
          <li>Flip Korn, Google Research</li>
          <li>Divesh Srivastava, AT&amp;T Chief Data Office</li>
        </ul>
        <h3>Program Committee</h3>
        <span class="brmedium"><b>Adriane Chapman</b> University of Southampton, UK</span>
        <span class="brmedium"><b>Alexander Boehm</b> Google, Germany</span>
        <b><h3>Important Dates</h3></b>
        """
        rows = edbt.parse_edbt_track_pc_html(
            html, year=2024, source_url="https://example.test/iap",
            fetched_at="now", page_kind="industrial",
        )
        self.assertEqual(
            {(row.role, row.name) for row in rows},
            {
                ("industrial_track_chair", "Flip Korn"),
                ("industrial_track_chair", "Divesh Srivastava"),
                ("industrial_pc_member", "Adriane Chapman"),
                ("industrial_pc_member", "Alexander Boehm"),
            },
        )


if __name__ == "__main__":
    unittest.main()
