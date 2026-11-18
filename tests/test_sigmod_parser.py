from __future__ import annotations

import sys
import unittest
from dataclasses import replace
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import scrape_sigmod_reviewers as sigmod


class SigmodSidePcParserTests(unittest.TestCase):
    def test_same_name_different_affiliations_survive_research_source_merge(self):
        # Official SIGMOD 2024/2025 lists Chao Zhang at both institutions;
        # 2025 also lists Jia Yu at Washington State and Wherobots.
        html = b'''<h1>Organization: SIGMOD Program Committee</h1>
            <p>Program Chairs</p><p>Example Chair, Example University</p>
            <p>PC members</p>
            <p>Chao Zhang, University of Waterloo</p>
            <p>Chao Zhang, Tsinghua University</p>
            <p>Jia Yu, Washington State University</p>
            <p>Jia Yu, Wherobots Inc.</p>'''
        rows = sigmod.parse_sigmod_pc_page(html, 2025, 'https://example.test/pc', 'now')
        # An exact duplicate from a second source is still one service row.
        duplicate = replace(rows[1], source_url='https://example.test/other',
                            affiliation='UNIVERSITY OF WATERLOO')
        for groups in ((rows, [duplicate]), ([duplicate], rows)):
            merged = sigmod.merge_sigmod_records(*groups)
            self.assertEqual(len(merged), 5)
            self.assertEqual({(r.name, sigmod.normalize_name_key(r.affiliation))
                              for r in merged if r.role == 'pc_member'}, {
                ('Chao Zhang', 'university of waterloo'),
                ('Chao Zhang', 'tsinghua university'),
                ('Jia Yu', 'washington state university'),
                ('Jia Yu', 'wherobots inc'),
            })

    def test_side_pc_preserves_differing_affiliations_before_merge(self):
        html = b'''<h1>Organization: SIGMOD Industrial Program Committee</h1>
            <p>Program Committee Members</p>
            <p>Example Person, First University</p>
            <p>Example Person, Second University</p>
            <p>Example Person, FIRST UNIVERSITY</p><p>Credits</p>'''
        rows = sigmod.parse_sigmod_track_pc_page(
            html, year=2025, source_url='https://example.test/industrial',
            fetched_at='now', page_kind='industrial')
        self.assertEqual(len(rows), 2)
        self.assertEqual({r.affiliation for r in rows},
                         {'First University', 'Second University'})
        self.assertEqual(len(sigmod.merge_sigmod_records(rows)), 2)

    def test_officer_cards_supply_pods_program_chairs_missing_from_pc_roles(self):
        # Official 2022/2024 officer pages use h3 headings and linked names in
        # table cells. Their PODS PC pages omit the chair or list him as a member.
        for year, name, affiliation in [
                (2022, "Pablo Barcel\u00f3", "Universidad Cat\u00f3lica de Chile, Chile"),
                (2024, "Wim Martens", "University of Bayreuth, Germany")]:
            html = f'''<h3>SIGMOD General Chairs</h3><table><tr><td>
                <a>Zachary Ives</a><br>University of Pennsylvania, USA</td></tr></table>
                <h3>PODS General Chair</h3><table><tr><td>
                <a>Leonid Libkin</a><br>University of Edinburgh, UK</td></tr></table>
                <h3>PODS Program Chair</h3><table><tr><td>
                <a>{name}</a><br>{affiliation}</td></tr></table>
                <h3>PODS Publicity Chair</h3><table><tr><td>
                <a>Another Person</a><br>Other University</td></tr></table>'''.encode("utf-8")
            rows = sigmod.parse_sigmod_officers_page(
                html, year, f"https://{year}.sigmod.org/org_conference_officers.shtml", "now")
            self.assertEqual([(r.name, r.role) for r in rows],
                             [("Zachary Ives", "general_chair"), (name, "pods_program_chair")])
            chair = rows[1]
            self.assertEqual(chair.affiliation, affiliation)
            member = replace(chair, role="pods_pc_member", affiliation=affiliation.rsplit(",", 1)[0],
                             source_url=f"https://{year}.sigmod.org/org_pods_pc.shtml")
            research = replace(chair, role="pc_member")
            for groups in [(rows, [member, research]), ([member, research], rows)]:
                merged = sigmod.merge_sigmod_records(*groups)
                self.assertEqual({r.role for r in merged if r.name == name}, {"pods_program_chair", "pc_member"})
                self.assertEqual(next(r.source_url for r in merged if r.role == "pods_program_chair"), chair.source_url)
            # Chair evidence is scoped to one year, not every same-name roster.
            merged = sigmod.merge_sigmod_records(rows, [replace(member, year=year + 1)])
            self.assertTrue(any(r.role == "pods_pc_member" and r.year == year + 1 for r in merged))
            for other_affiliation in ("Other University", ""):
                merged = sigmod.merge_sigmod_records(rows, [replace(member, affiliation=other_affiliation)])
                self.assertTrue(any(r.role == "pods_pc_member" for r in merged))
            for chair_org, member_org in [("University of California, Berkeley", "University of California, San Diego"),
                                           ("CNRS, Lab A", "CNRS, Lab B")]:
                merged = sigmod.merge_sigmod_records([replace(chair, affiliation=chair_org)],
                                                     [replace(member, affiliation=member_org)])
                self.assertEqual({r.role for r in merged}, {"pods_program_chair", "pods_pc_member"})

    def test_industrial_pc_includes_tim_kraska(self):
        html = b"""
        <h1>Organization: SIGMOD Industrial Track Program Committee</h1>
        <p>Chairs</p>
        <p>Chair Person, Example Labs</p>
        <p>Program Committee Members</p>
        <p>Tim Kraska, MIT</p>
        <p>Fatma Ozcan, Google</p>
        <p>Credits</p>
        """
        rows = sigmod.parse_sigmod_track_pc_page(
            html, year=2025, source_url="https://example.test/industrial",
            fetched_at="now", page_kind="industrial",
        )
        by_name = {row.name: row.role for row in rows}
        self.assertEqual(by_name["Chair Person"], "industrial_track_chair")
        self.assertEqual(by_name["Tim Kraska"], "industrial_pc_member")
        self.assertEqual(by_name["Fatma Ozcan"], "industrial_pc_member")

    def test_pods_nested_parenthesized_affiliation(self):
        html = """
        <h1>Organization: PODS Program Committee</h1>
        <p>Program Committee Members</p>
        <p>Nofar Carmeli (École Normale Supérieure (ENS), Paris)</p>
        <p>Credits</p>
        """.encode("utf-8")
        rows = sigmod.parse_sigmod_track_pc_page(
            html, year=2023, source_url="https://example.test/pods",
            fetched_at="now", page_kind="pods",
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].name, "Nofar Carmeli")
        self.assertEqual(rows[0].affiliation, "École Normale Supérieure (ENS), Paris")

    def test_pods_parenthesized_people(self):
        html = b"""
        <h1>Organization: PODS Program Committee</h1>
        <p>Program Committee Chair</p>
        <p>Benny Kimelfeld (Technion)</p>
        <p>Program Committee Members</p>
        <p>Dan Suciu (University of Washington)</p>
        <p>Credits</p>
        """
        rows = sigmod.parse_sigmod_track_pc_page(
            html, year=2025, source_url="https://example.test/pods",
            fetched_at="now", page_kind="pods",
        )
        self.assertEqual(
            {(row.role, row.name) for row in rows},
            {
                ("pods_program_chair", "Benny Kimelfeld"),
                ("pods_pc_member", "Dan Suciu"),
            },
        )

    def test_pods_inline_pc_chair_is_not_part_of_the_affiliation(self):
        html = b"""
        <h1>Organization: PODS Program Committee</h1>
        <p>Program Committee Members</p>
        <p>Hung Ngo (Relational AI, Inc., PC Chair)</p>
        <p>Dan Suciu (University of Washington)</p>
        <p>Credits</p>
        """
        rows = sigmod.parse_sigmod_track_pc_page(
            html, year=2023, source_url="https://2023.sigmod.org/org_pods_pc.shtml",
            fetched_at="now", page_kind="pods",
        )
        self.assertEqual([(r.name, r.role, r.affiliation) for r in rows], [
            ("Hung Ngo", "pods_program_chair", "Relational AI, Inc."),
            ("Dan Suciu", "pods_pc_member", "University of Washington"),
        ])
        self.assertEqual(rows[0].raw_text, "Hung Ngo (Relational AI, Inc., PC Chair)")

    def test_inline_pods_rule_does_not_infer_unstated_roles(self):
        html = b"""
        <h1>Organization: PODS Program Committee</h1>
        <p>Program Committee Members</p>
        <p>Example Person (Chair of Database Systems, Example University)</p>
        <p>Other Person (PC Chair Research Institute)</p>
        <p>Credits</p>
        """
        rows = sigmod.parse_sigmod_track_pc_page(
            html, year=2023, source_url="https://example.test/pods",
            fetched_at="now", page_kind="pods",
        )
        self.assertTrue(all(r.role == "pods_pc_member" for r in rows))
        self.assertEqual(rows[0].affiliation, "Chair of Database Systems, Example University")
        self.assertEqual(rows[1].affiliation, "PC Chair Research Institute")
        industrial = html.replace(b"PODS", b"SIGMOD Industrial Track").replace(
            b"Chair of Database Systems, Example University", b"Example University, PC Chair")
        rows = sigmod.parse_sigmod_track_pc_page(
            industrial, year=2023, source_url="https://example.test/industry",
            fetched_at="now", page_kind="industrial",
        )
        self.assertEqual(rows[0].role, "industrial_pc_member")
        self.assertEqual(rows[0].affiliation, "Example University, PC Chair")


if __name__ == "__main__":
    unittest.main()
