import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import scraper_repairs


class ScraperRepairTests(unittest.TestCase):
    def test_only_current_exact_source_name_corrections_remain(self):
        self.assertEqual(
            set(scraper_repairs.KNOWN_NAME_REPAIRS),
            {
                "Papotti Paolo",
                "Tien Tuan Anh Ding",
                "H. V. Jadadish",
                "Amine Mhedhbi to Polytechnique Montreal",
            },
        )

    def test_repairs_do_not_change_affiliation(self):
        name, affiliation, repair = scraper_repairs.repair_scraped_reviewer(
            "Papotti Paolo", "EURECOM"
        )
        self.assertEqual(name, "Paolo Papotti")
        self.assertEqual(affiliation, "EURECOM")
        self.assertIsNotNone(repair)

    def test_icde_source_typos_and_annotations_are_exact_repairs(self):
        cases = {
            "H. V. Jadadish": "H. V. Jagadish",
            "Amine Mhedhbi to Polytechnique Montreal": "Amine Mhedhbi",
        }
        for raw_name, expected_name in cases.items():
            name, affiliation, repair = scraper_repairs.repair_scraped_reviewer(
                raw_name, "Example Affiliation"
            )
            self.assertEqual(name, expected_name)
            self.assertEqual(affiliation, "Example Affiliation")
            self.assertIsNotNone(repair)

    def test_unknown_names_pass_through_unchanged(self):
        name, affiliation, repair = scraper_repairs.repair_scraped_reviewer(
            "Example Person", "Example University"
        )
        self.assertEqual((name, affiliation), ("Example Person", "Example University"))
        self.assertIsNone(repair)


if __name__ == "__main__":
    unittest.main()
