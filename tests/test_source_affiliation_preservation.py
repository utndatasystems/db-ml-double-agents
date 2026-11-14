"""Distinct source affiliations must survive parsing and merging."""
import sys
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import scrape_icde_reviewers as icde
import scrape_edbt_reviewers as edbt


class SourceAffiliationPreservationTests(unittest.TestCase):
    def assert_affiliations(self, rows):
        people = [r for r in rows if r.name == "Example Person"]
        self.assertEqual(len(people), 2)
        self.assertEqual({r.affiliation for r in people}, {"University One", "University Two"})

    def test_icde_research_parser_keeps_different_affiliations(self):
        html = b'''<h2>Program Committee Members</h2><ul>
        <li>Example Person, University One</li><li>Example Person, University Two</li>
        <li>Example Person, University One</li></ul>'''
        self.assert_affiliations(icde._parse_role_sections(
            html, year=2025, source_url="https://example.test", fetched_at="now",
            patterns=icde.RESEARCH_ROLE_PATTERNS, allowed_roles={"program_committee_member"}))

    def test_icde_2022_parser_keeps_different_affiliations(self):
        html = b'''<h2>Program Committee Members</h2>
        <p class="gutentor-single-item-title"><strong>Example Person</strong>,
        <em>University One</em><br><strong>Example Person</strong>,
        <em>University Two</em><br><strong>Example Person</strong>,
        <em>University One</em></p>'''
        self.assert_affiliations(icde.parse_icde_page(
            html, year=2022, source_url="https://example.test", fetched_at="now", validate=False))

    def test_edbt_committee_parser_keeps_different_affiliations(self):
        html = b'''<h2>EDBT Program Committee Members</h2><ul>
        <li>Example Person, University One</li><li>Example Person, University Two</li>
        <li>Example Person, University One</li></ul>'''
        with patch.object(edbt, "MIN_RESEARCH_PC_ROWS", 1):
            self.assert_affiliations(edbt.parse_edbt_committee_html(
                html, year=2025, source_url="https://example.test", fetched_at="now"))

    def test_edbt_track_parser_keeps_different_affiliations(self):
        html = b'''<h2>Program Committee</h2><ul>
        <li>Example Person, University One</li><li>Example Person, University Two</li>
        <li>Example Person, University One</li></ul>'''
        self.assert_affiliations(edbt.parse_edbt_track_pc_html(
            html, year=2025, source_url="https://example.test", fetched_at="now", page_kind="industrial"))

    def test_merges_keep_affiliations_years_and_roles(self):
        for module, merge in [(icde, icde.merge_icde_records), (edbt, edbt.merge_edbt_records)]:
            with self.subTest(module=module.__name__):
                args = dict(year=2025, role="program_committee_member", name="Example Person",
                            name_key="example person", affiliation="University One", raw_text="",
                            source_url="https://example.test", fetched_at="now")
                if module is edbt:
                    args["source_page"] = None
                a = module.ReviewerRecord(**args)
                b = replace(a, affiliation="University Two")
                self.assert_affiliations(merge([a, b], [a, replace(b, affiliation=" university two ")]))
                self.assertEqual(len(merge([a, replace(a, year=2024), replace(a, role="area_chair")])), 3)

    def test_icde_blank_affiliation_is_enriched_in_either_input_order(self):
        full = icde.ReviewerRecord(year=2025, role="program_committee_member", name="Example Person",
            name_key="example person", affiliation="University One", raw_text="",
            source_url="https://example.test", fetched_at="now")
        blank = replace(full, affiliation="")
        for rows in ([blank, full], [full, blank]):
            self.assertEqual(icde.merge_icde_records(rows), [full])


if __name__ == "__main__":
    unittest.main()
