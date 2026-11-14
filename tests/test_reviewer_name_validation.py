from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import enrich_reviewers_with_dblp as enrich


class ReviewerNameValidationTests(unittest.TestCase):
    def test_embedded_parenthetical_nicknames_are_valid_names(self):
        for name in ("Tsz Nam (Edison) Chan", "Xin (Alwin) Tang"):
            with self.subTest(name=name):
                self.assertEqual(enrich.validate_person_name(name), (True, ""))

    def test_affiliation_like_or_unbalanced_parentheses_remain_invalid(self):
        for name in (
            "Nofar Carmeli (École Normale Supérieure (ENS)",
            "Ada Lovelace (Analytical Engine Lab)",
        ):
            with self.subTest(name=name):
                self.assertEqual(
                    enrich.validate_person_name(name),
                    (False, "contains_parenthetical_affiliation_or_concatenation"),
                )


if __name__ == "__main__":
    unittest.main()
