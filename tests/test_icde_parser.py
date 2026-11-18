from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import scrape_icde_reviewers as icde


class IcdeStructuralParserTests(unittest.TestCase):
    def parse_research(self, html: str):
        return icde._parse_role_sections(
            html.encode(),
            year=2099,
            source_url="https://example.test/research",
            fetched_at="now",
            patterns=icde.RESEARCH_ROLE_PATTERNS,
            allowed_roles={
                "program_committee_chair",
                "area_chair",
                "program_committee_member",
            },
        )

    def test_research_sections_and_malformed_joined_rows_are_conservative(self):
        html = """
        <h3>Chairs</h3>
        <ul><li>Gao Cong, Nanyang Technological University</li></ul>
        <h3>Associate Chairs</h3>
        <ul><li>Daniel Abadi, University of Maryland</li></ul>
        <h3>Program Committee Members</h3>
        <p>
          Alpha Person, University One<br>
          Gamma Person, TU Berlin Delta Person METU, Turkey<br>
          Eta Person<br>
          Theta Person, University Eight
        </p>
        <h2>Want to contribute?</h2>
        """
        rows = self.parse_research(html)
        people = {(r.role, r.name, r.affiliation) for r in rows}
        self.assertIn(("program_committee_member", "Gamma Person", "TU Berlin Delta Person METU, Turkey"), people)
        self.assertNotIn("Delta Person", {r.name for r in rows})
        self.assertIn(("program_committee_member", "Eta Person", ""), people)
        self.assertIn(("program_committee_member", "Theta Person", "University Eight"), people)

    def test_normal_affiliation_stays_one_row(self):
        self.assertEqual(
            icde.parse_person_line("Example Person, University of California Los Angeles, USA"),
            [("Example Person", "University of California Los Angeles, USA")],
        )

    def test_officer_headings_and_admin_boundary(self):
        html = """
        <h2>General Chairs</h2>
        <p>Murat Kantarcioglu, University of Texas at Dallas, USA</p>
        <h2>Research Program Committee Chairs</h2>
        <p>Gao Cong, Nanyang Technological University, Singapore</p>
        <h2>Demo Chairs</h2>
        <p>Yael Amsterdamer, Bar-Ilan University, Israel</p>
        <h2>Web Chairs</h2>
        <p>Web Person, University Example</p>
        """
        rows = icde.parse_icde_officers_page(
            html.encode(), year=2099, source_url="https://example.test/officers", fetched_at="now"
        )
        self.assertEqual(
            {(r.role, r.name) for r in rows},
            {
                ("general_chair", "Murat Kantarcioglu"),
                ("program_committee_chair", "Gao Cong"),
                ("demonstration_chair", "Yael Amsterdamer"),
            },
        )


    def test_award_committee_is_qualifying_scientific_service(self):
        html = """
        <h2>General Chair</h2>
        <p>General Person, Example University</p>
        <h2>Research Program Committee Chairs</h2>
        <p>Alice Example, Example University</p>
        <h2>Best Paper Award Committee (Research)</h2>
        <p>Senior One, University One<br>Senior Two, University Two</p>
        """
        rows = icde.parse_icde_officers_page(
            html.encode(), year=2099, source_url="https://example.test/officers", fetched_at="now"
        )
        self.assertEqual(
            {r.name for r in rows if r.role == "award_committee_member"},
            {"Senior One", "Senior Two"},
        )

    def test_industrial_and_demo_pc_pages(self):
        industrial = """
        <h2>Industry and Applications Chairs</h2>
        <ul><li>Chair Person, University One</li></ul>
        <h2>Program Committee Members</h2>
        <ul><li>Industry Reviewer, Company Two</li></ul>
        """
        demo = """
        <h2>Demo Chairs</h2>
        <ul><li>Demo Chair, University Three</li></ul>
        <h2>Program Committee Members</h2>
        <ul><li>Demo Reviewer, University Four</li></ul>
        """
        industrial_rows = icde.parse_icde_technical_pc_page(
            industrial.encode(), year=2099, source_url="https://example.test/industry",
            fetched_at="now", page_kind="industrial"
        )
        demo_rows = icde.parse_icde_technical_pc_page(
            demo.encode(), year=2099, source_url="https://example.test/demo",
            fetched_at="now", page_kind="demo"
        )
        self.assertIn(("industrial_pc_member", "Industry Reviewer"), {(r.role, r.name) for r in industrial_rows})
        self.assertIn(("demo_pc_member", "Demo Reviewer"), {(r.role, r.name) for r in demo_rows})

    def test_title_div_and_br_layout(self):
        html = """
        <div class="title2">General Chairs</div>
        <div>Malu Castellanos, Teradata, USA<br>Yannis Velegrakis, Utrecht University, Netherlands</div>
        <div class="title2">PC Chairs</div>
        <div>Gautam Das, University of Texas at Arlington, USA<br>
             Timos Sellis, Athena Research Center, Greece<br>
             BingSheng He, NUS, Singapore</div>
        <div class="title2">Industry and Applications Chairs</div>
        <div>Fatma Ozcan, Google, USA<br>Peter Boncz, CWI, Netherlands</div>
        <div class="title2">Publication and Proceedings Chair</div>
        <div>Admin Person, University Example</div>
        """
        rows = icde.parse_icde_officers_page(
            html.encode(), year=2099, source_url="https://example.test/officers", fetched_at="now"
        )
        counts = {}
        for row in rows:
            counts[row.role] = counts.get(row.role, 0) + 1
        self.assertEqual(counts["general_chair"], 2)
        self.assertEqual(counts["program_committee_chair"], 3)
        self.assertEqual(counts["industrial_track_chair"], 2)
        self.assertNotIn("publication_chair", counts)

    def test_cochair_variants_are_recognized(self):
        html = """
        <h3>General Co-Chairs</h3><p>Alice Example, University One</p>
        <h3>Research PC Co-Chairs</h3><p>Bob Example, Institute Two</p>
        """
        rows = icde.parse_icde_officers_page(
            html.encode(), year=2099, source_url="https://example.test/officers", fetched_at="now"
        )
        self.assertEqual({r.role for r in rows}, {"general_chair", "program_committee_chair"})

    def test_unusual_name_casing_with_affiliation(self):
        for line, expected in [
            ("KMA Solaiman, Purdue University", "KMA Solaiman"),
            ("YAO Lu, NUS", "YAO Lu"),
            ("ZHAOJING Luo, Beijing Institute of Technology", "ZHAOJING Luo"),
            ("Xueli liu, Tianjin University", "Xueli liu"),
            ("Bin Cui, Peking University", "Bin Cui"),
            ("Bin Wu, Alibaba Group", "Bin Wu"),
            ("Da Yan, Indiana University Bloomington", "Da Yan"),
        ]:
            rows = icde.parse_person_line(line)
            self.assertEqual(rows[0][0], expected)

    def test_normal_institution_names_are_not_people(self):
        for value in [
            "School of Data Science",
            "Pennsylvania State University",
            "Harbin Institute of Technology",
            "Data Analytics and Intelligence Lab",
            "Conference Overview",
            "Conflict of Interest",
            "Query Processing",
        ]:
            self.assertFalse(icde.plausible_person_name(value), value)

    def test_nested_wordpress_blocks_are_read_once(self):
        html = """
        <article>
          <div><h3>Associate Chairs</h3><ul>
            <li>Alpha Person, University One</li>
            <li>Beta Person, University Two</li>
            <li>Gamma Person, University Three</li>
          </ul></div>
          <div><h3>Program Committee Members</h3><p>
            One Reviewer, University A<br>
            Two Reviewer, University B<br>
            Three Reviewer, University C
          </p></div>
          <h2>Want to contribute?</h2>
          <p>Footer Person, University Footer</p>
        </article>
        """
        rows = self.parse_research(html)
        counts = {}
        for row in rows:
            counts[row.role] = counts.get(row.role, 0) + 1
        self.assertEqual(counts.get("area_chair"), 3)
        self.assertEqual(counts.get("program_committee_member"), 3)
        self.assertNotIn("Footer Person", {r.name for r in rows})

    def test_split_inline_name_and_affiliation(self):
        html = """
        <h3>Program Committee Members</h3>
        <p><strong>KMA Solaiman</strong>, Purdue University<br>
           <strong>YAO Lu</strong>, NUS<br>
           <strong>Rebecca Taft</strong><br>
           Full Person, Example University</p>
        <h2>Other Information</h2>
        """
        rows = self.parse_research(html)
        by_name = {r.name: r.affiliation for r in rows}
        self.assertEqual(by_name["KMA Solaiman"], "Purdue University")
        self.assertEqual(by_name["YAO Lu"], "NUS")
        self.assertEqual(by_name["Rebecca Taft"], "")
        self.assertEqual(by_name["Full Person"], "Example University")

    def test_sidebar_before_main_roles_is_ignored(self):
        html = """
        <div id="sidebar">
          <div class="title2">Topics of Interest</div>
          <ul><li>Cloud Data Management</li><li>Query Processing</li></ul>
          <a>Conference Overview</a>
        </div>
        <div id="main">
          <div class="title2">Research Area Chairs</div>
          Bin Cui, Peking University<br>
          Le Gruenwald, The University of Oklahoma<br>
          <div class="title2">Program Committee</div>
          Mingjie Tang, Ant Financial<br>
          Sibo Wang, The Chinese University of Hong Kong<br>
        </div>
        """
        rows = self.parse_research(html)
        self.assertEqual(
            {(r.role, r.name) for r in rows},
            {
                ("area_chair", "Bin Cui"),
                ("area_chair", "Le Gruenwald"),
                ("program_committee_member", "Mingjie Tang"),
                ("program_committee_member", "Sibo Wang"),
            },
        )

    def test_trailing_comments_are_not_parsed_as_people(self):
        html = """
        <div class="title2">Program Committee</div>
        Alice Example, Example University<br>
        <!-- HERE ENDS THE CONTENT.-->
        <!--end of mainbody-->
        """
        rows = self.parse_research(html)
        self.assertEqual({r.name for r in rows}, {"Alice Example"})

    def test_2022_wordpress_technical_pc_uses_article_lists_not_sidebar(self):
        html = """
        <div class="sidebar-content">
          <article><div class="entry-content">
            <h3>Industry and Applications Chairs</h3>
            <ul><li>Carol Example, University One</li></ul>
            <h3>Program Committe Members</h3>
            <ul>
              <li>Alice Example, Example University</li>
              <li>Bob Example, Example Institute</li>
            </ul>
            <h2>Want to contribute? Let's Start!</h2>
          </div></article>
          <div class="sidebar">
            <a>Student and Dnl Events</a>
            <a>Keynote Session</a>
            <a>PhD Symposium Program</a>
            <a>Workshop Schedules</a>
            <a>Best Papers Award</a>
          </div>
        </div>
        """
        rows = icde.parse_icde_technical_pc_page(
            html.encode(), year=2022, source_url="https://example.test/industry",
            fetched_at="now", page_kind="industrial"
        )
        self.assertEqual(
            {(r.role, r.name) for r in rows},
            {
                ("industrial_track_chair", "Carol Example"),
                ("industrial_pc_member", "Alice Example"),
                ("industrial_pc_member", "Bob Example"),
            },
        )

    def test_modal_content_label_is_not_parsed_as_person(self):
        html = """
        <div class="modal-layout-content">
          <h2>Program Committee Members</h2>
          <p>Alice Example, Example University</p>
          <div class="modal fade"><div class="modal-content">Modal content</div></div>
        </div>
        """
        rows = self.parse_research(html)
        self.assertEqual({r.name for r in rows}, {"Alice Example"})

    def test_split_officer_name_with_trailing_comma_keeps_person(self):
        html = """
        <h2>General Chair</h2><p>General Person, Example University</p>
        <h2>Research Program Committee Chairs</h2><p>Bob Example, Example University</p>
        <h2>Industry and Applications Chairs</h2>
        <p><strong>Themis Palpanas,</strong><br>Université Paris Cité, France<br>
           Nesime Tatbul, Intel Labs and MIT, USA<br>
           <strong>Kai Zeng,</strong><br>Huawei, China</p>
        <h2>Workshop Chairs</h2>
        """
        rows = icde.parse_icde_officers_page(
            html.encode(), year=2099, source_url="https://example.test/officers", fetched_at="now"
        )
        by_name = {r.name: r.affiliation for r in rows if r.role == "industrial_track_chair"}
        self.assertEqual(set(by_name), {"Themis Palpanas", "Nesime Tatbul", "Kai Zeng"})
        self.assertEqual(by_name["Themis Palpanas"], "Université Paris Cité, France")
        self.assertEqual(by_name["Kai Zeng"], "Huawei, China")
        self.assertNotIn("Université Paris Cité", by_name)

    def test_css_section_wrapper_does_not_turn_people_into_headings(self):
        html = """
        <section class="elementor-section elementor-top-section">
          <div class="elementor-container">
            <h3>Associate Chairs</h3>
            <div>Alice Example, University One</div>
            <div>Bob Example, University Two</div>
            <h3>Program Committee Members</h3>
            <div>Carol Example, University Three</div>
          </div>
        </section>
        """
        rows = self.parse_research(html)
        self.assertEqual(
            {(r.role, r.name) for r in rows},
            {
                ("area_chair", "Alice Example"),
                ("area_chair", "Bob Example"),
                ("program_committee_member", "Carol Example"),
            },
        )


    def test_outer_title_wrapper_does_not_mark_all_rows_as_headings(self):
        html = """
        <div class="conference-title-wrapper">
          <h3>Associate Chairs</h3>
          <ul>
            <li>Alice Example, University One</li>
            <li>Bob Example, University Two</li>
          </ul>
          <h3>Program Committee Members</h3>
          <p>Carol Example, University Three<br>Dave Example, University Four</p>
        </div>
        """
        rows = self.parse_research(html)
        self.assertEqual(
            {(r.role, r.name) for r in rows},
            {
                ("area_chair", "Alice Example"),
                ("area_chair", "Bob Example"),
                ("program_committee_member", "Carol Example"),
                ("program_committee_member", "Dave Example"),
            },
        )

    def test_person_card_heading_markup_does_not_drop_person(self):
        html = """
        <h3>General Chair</h3>
        <h4>General Person, Example University</h4>
        <h3>Research Program Committee Chairs</h3>
        <h4>Alice Lead, Example Institute</h4>
        <h3>Industry and Applications Chairs</h3>
        <h4>Themis Palpanas, Université Paris Cité, France</h4>
        <h4>Nesime Tatbul, Intel Labs and MIT, USA</h4>
        <h4>Kai Zeng, Huawei, China</h4>
        <h3>Web Chair</h3>
        <h4>Admin Person, University Example</h4>
        """
        rows = icde.parse_icde_officers_page(
            html.encode(), year=2099, source_url="https://example.test/officers", fetched_at="now"
        )
        self.assertEqual(
            {(r.role, r.name) for r in rows},
            {
                ("general_chair", "General Person"),
                ("program_committee_chair", "Alice Lead"),
                ("industrial_track_chair", "Themis Palpanas"),
                ("industrial_track_chair", "Nesime Tatbul"),
                ("industrial_track_chair", "Kai Zeng"),
            },
        )

    def test_2022_gutentor_officer_state_machine(self):
        html = """
        <div class="entry-content">
          <h3 class="wp-block-heading">General Chairs</h3>
          <p class="gutentor-single-item-title"><strong>Alice General</strong>, <em>University One</em></p>
          <p class="gutentor-single-item-title"><strong>Bob General</strong>, <em>University Two</em></p>
          <h3 class="wp-block-heading">Research Program Committee Chairs</h3>
          <p class="gutentor-single-item-title"><strong>Carol Lead</strong>, <em>University Three</em></p>
          <p class="gutentor-single-item-title"><strong>Dave Lead</strong>, <em>University Four</em></p>
          <h3 class="wp-block-heading">Demo Chairs</h3>
          <p class="gutentor-single-item-title"><strong>Eve Demo</strong>, <em>University Five</em></p>
          <h3 class="wp-block-heading">TKDE Poster Chairs</h3>
          <p class="gutentor-single-item-title"><strong>Ignored Poster</strong>, <em>University Six</em></p>
          <h3 class="wp-block-heading">Publicity Chairs</h3>
          <p class="gutentor-single-item-title"><strong>Ignored Admin</strong>, <em>University Seven</em></p>
        </div>
        """
        rows = icde.parse_icde_officers_page(
            html.encode(), year=2022, source_url="https://example.test/2022-officers", fetched_at="now"
        )
        self.assertEqual(
            {(r.role, r.name) for r in rows},
            {
                ("general_chair", "Alice General"),
                ("general_chair", "Bob General"),
                ("program_committee_chair", "Carol Lead"),
                ("program_committee_chair", "Dave Lead"),
                ("demonstration_chair", "Eve Demo"),
            },
        )

    def test_2022_gutentor_long_pc_container_uses_br_as_record_boundary(self):
        html = """
        <h3 class="wp-block-heading">Chairs</h3>
        <p class="gutentor-single-item-title"><strong>Alice Lead</strong>, <em>University One</em></p>
        <h3 class="wp-block-heading">Associate Chairs</h3>
        <p class="gutentor-single-item-title"><strong>Bob Senior</strong>, <em>University Two</em></p>
        <h3 class="wp-block-heading">Program Committee Members</h3>
        <p class="gutentor-single-item-title">
          <strong><strong>Carol</strong> <strong>Reviewer</strong>, </strong><em>Institute Three</em>
          <strong><br><strong>Da Yan</strong>,</strong> <em>University Four</em>
          <br><strong>Rebecca Taft</strong>
          <br><strong>Yongluan Zho</strong>u, <em>University Five</em>
        </p>
        <h2>Want to contribute?</h2>
        """
        rows = icde.parse_icde_page(
            html.encode(),
            year=2022,
            source_url="https://example.test/2022-research",
            fetched_at="now",
            validate=False,
        )
        self.assertEqual(
            {(r.role, r.name, r.affiliation) for r in rows},
            {
                ("program_committee_chair", "Alice Lead", "University One"),
                ("area_chair", "Bob Senior", "University Two"),
                ("program_committee_member", "Carol Reviewer", "Institute Three"),
                ("program_committee_member", "Da Yan", "University Four"),
                ("program_committee_member", "Rebecca Taft", ""),
                ("program_committee_member", "Yongluan Zhou", "University Five"),
            },
        )

    def test_auxiliary_reviewer_roster_owns_pc_role(self):
        def rec(role, name):
            return icde.ReviewerRecord(
                year=2099, role=role, name=name, affiliation="Example University",
                name_key=icde.normalize_name_key(name), raw_text=name,
                source_url="https://example.test", fetched_at="now",
            )

        primary = [
            rec("area_chair", "Area Person"),
            rec("program_committee_member", "Good Reviewer"),
            rec("program_committee_member", "Parser Artifact"),
        ]
        auxiliary = [
            rec("program_committee_member", "Good Reviewer"),
            rec("program_committee_member", "Second Reviewer"),
        ]
        merged = icde.merge_research_sources(primary, auxiliary)
        self.assertEqual(
            {(r.role, r.name) for r in merged},
            {
                ("area_chair", "Area Person"),
                ("program_committee_member", "Good Reviewer"),
                ("program_committee_member", "Second Reviewer"),
            },
        )


if __name__ == "__main__":
    unittest.main()
