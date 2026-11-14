from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from service_roles import classify_service_role


class ServiceRolePolicyTests(unittest.TestCase):
    def test_flagship_technical_pc_roles_seed_db_population(self):
        for role, track in [
            ("industrial_pc_member", "industrial"),
            ("demo_pc_member", "demo"),
            ("pods_pc_member", "pods"),
            ("award_committee_member", "awards"),
        ]:
            with self.subTest(role=role):
                metadata = classify_service_role("SIGMOD", role)
                self.assertEqual(metadata.track, track)
                self.assertEqual(metadata.qualifies_db_seed, 1)

    def test_tutorial_and_workshop_pc_membership_do_not_seed(self):
        for role in ("tutorial_pc_member", "workshop_pc_member"):
            with self.subTest(role=role):
                metadata = classify_service_role("SIGMOD", role)
                self.assertEqual(metadata.qualifies_db_seed, 0)

    def test_pods_chair_is_research_leadership(self):
        metadata = classify_service_role("SIGMOD", "pods_program_chair")
        self.assertEqual(metadata.role_family, "research_program_chair")
        self.assertEqual(metadata.track, "pods")
        self.assertEqual(metadata.qualifies_db_seed, 1)


if __name__ == "__main__":
    unittest.main()
