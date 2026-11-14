from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import scrape_vldb_reviewers as vldb


class VldbParserTests(unittest.TestCase):
    def test_review_board_sections(self):
        html = b"""
        <h1>PVLDB Review Board for Volume 18 (2024-2025)</h1>
        <h2>Editors-in-Chief</h2>
        <p>Alice Editor (Example University)</p>
        <h2>Associate Editors</h2>
        <p>Bob Editor, Example Institute</p>
        <h2>Members of the Review Board</h2>
        <p>Carol Reviewer (Database Lab)</p>
        <p>Dan Reviewer, Another University</p>
        <h2>Contact</h2>
        """
        rows = vldb.parse_vldb_review_board_page(
            html, conference_year=2025, expected_volume=18,
            source_url="https://example.test/vldb", fetched_at="now"
        )
        self.assertEqual(
            {(row.role, row.name) for row in rows},
            {
                ("editor_in_chief", "Alice Editor"),
                ("associate_editor", "Bob Editor"),
                ("review_board_member", "Carol Reviewer"),
                ("review_board_member", "Dan Reviewer"),
            },
        )


if __name__ == "__main__":
    unittest.main()
