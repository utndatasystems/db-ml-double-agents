from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import scrape_edbt_reviewers as edbt


class EdbtParentheticalAffiliationTests(unittest.TestCase):
    def test_comma_row_can_end_affiliation_with_parentheses(self):
        parsed = edbt.parse_person_line(
            "Zeyi Wen, Hong Kong University of Science and Technology (Guangzhou)"
        )
        self.assertEqual(
            parsed,
            (
                "Zeyi Wen",
                "Hong Kong University of Science and Technology (Guangzhou)",
                None,
            ),
        )

    def test_historical_parenthesized_affiliation_still_works(self):
        parsed = edbt.parse_person_line("Jane Doe (Example University, USA)")
        self.assertEqual(parsed, ("Jane Doe", "Example University, USA", None))



if __name__ == "__main__":
    unittest.main()
