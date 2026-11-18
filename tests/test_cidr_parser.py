from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import scrape_cidr_reviewers as cidr


class CidrParserTests(unittest.TestCase):
    def test_heading_list_structure(self):
        html = b"""
        <h2>General Chairs</h2>
        <ul><li>Alice Chair (Example University)</li></ul>
        <h2>Program Committee Chairs</h2>
        <ul><li>Bob Chair, Example Institute</li></ul>
        <h2>Program Committee</h2>
        <ul>
          <li>Carol Reviewer (Database Lab)</li>
          <li>Dan Reviewer, Another University</li>
        </ul>
        """
        rows = cidr.parse_cidr_officers_page(
            html, year=2025, source_url="https://example.test/cidr", fetched_at="now"
        )
        self.assertEqual(
            {(row.role, row.name) for row in rows},
            {
                ("general_chair", "Alice Chair"),
                ("program_committee_chair", "Bob Chair"),
                ("program_committee_member", "Carol Reviewer"),
                ("program_committee_member", "Dan Reviewer"),
            },
        )


if __name__ == "__main__":
    unittest.main()
