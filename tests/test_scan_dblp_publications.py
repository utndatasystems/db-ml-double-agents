import sys
import gzip
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from scan_dblp_publications import main_venue_for_record, scan, TargetPerson, load_reviewed_authors


class MainVenueClassificationTests(unittest.TestCase):
    def test_iclr_poster_is_main_conference_track(self):
        self.assertEqual(
            main_venue_for_record(
                key="conf/iclr/LiYS018",
                booktitle="ICLR (Poster)",
                streams=[],
            ),
            "ICLR",
        )

    def test_iclr_workshop_is_not_main(self):
        self.assertIsNone(
            main_venue_for_record(
                key="conf/iclr/ThomasGDRR18",
                booktitle="ICLR (Workshop)",
                streams=[],
            )
        )

    def test_poster_alias_does_not_apply_outside_iclr_namespace(self):
        self.assertIsNone(
            main_venue_for_record(
                key="conf/example/Paper18",
                booktitle="ICLR (Poster)",
                streams=[],
            )
        )


class ReviewedAuthorReplacementTests(unittest.TestCase):
    def test_verified_split_bibliographies_are_record_scoped(self):
        reviews = load_reviewed_authors(REPO_ROOT / "data/reviewed_publication_authors.csv", {"12/875", "09/10814", "185/9964"})
        self.assertEqual(len(reviews["12/875"]), 4)
        self.assertEqual(len(reviews["09/10814"]), 5)
        self.assertNotIn("185/9964", reviews, "Do not transfer the Yisu Wang namesake's papers")
        self.assertTrue(all(source == "34/2717" and signature == "Brian Cooper"
                            for _, source, signature in reviews["12/875"]))
        self.assertTrue(all(source == "409/8145" and signature == "Djellel Difallah"
                            for _, source, signature in reviews["09/10814"]))

    def scan_authors(self, authors, extra_xml=""):
        source = TargetPerson("source", "Short Name", ("Short Name", "Other Author"), ())
        target = TargetPerson("target", "Full Name", ("Full Name",), (),
                              (("conf/test/p", "source", "Short Name"),))
        third = TargetPerson("third", "Third Person", ("Third Person",), ())
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            dtd = directory / "fixture.dtd"
            dtd.write_text("")
            xml = directory / "fixture.xml.gz"
            body = '<dblp><inproceedings key="conf/test/p">' + authors + \
                   '<title>Research</title><year>2025</year><booktitle>Test</booktitle></inproceedings>' + extra_xml + '</dblp>'
            with gzip.open(xml, "wb") as handle:
                handle.write(body.encode())
            return scan(xml, dtd, [source, target, third])[0]

    def test_review_replaces_source_assignment_when_both_people_are_targets(self):
        rows = self.scan_authors('<author pid="source">Short Name</author>')
        self.assertEqual([(r["dblp_pid"], r["author_position"], r["match_basis"]) for r in rows],
                         [("target", 1, "reviewed_publication_author")])

    def test_review_keeps_other_source_author_positions(self):
        rows = self.scan_authors('<author pid="source">Short Name</author><author pid="source">Other Author</author>')
        self.assertEqual({(r["dblp_pid"], r["author_position"]) for r in rows},
                         {("target", 1), ("source", 2)})

    def test_rejected_duplicate_signature_review_keeps_automatic_assignment(self):
        rows = self.scan_authors('<author pid="source">Short Name</author><author>Short Name</author>')
        self.assertEqual([r["dblp_pid"] for r in rows], ["source"])

    def test_review_replaces_globally_owned_exact_signature_without_author_pid(self):
        rows = self.scan_authors('<author>Short Name</author>',
                                 '<www key="homepages/source"><author>Short Name</author></www>')
        self.assertEqual([r["dblp_pid"] for r in rows], ["target"])

    def test_contradictory_orcid_does_not_remove_source_assignment(self):
        rows = self.scan_authors('<author pid="source" orcid="0000-0001-0000-0001">Short Name</author>')
        self.assertEqual([r["dblp_pid"] for r in rows], ["source"])

    def test_contradictory_explicit_pid_does_not_remove_its_assignment(self):
        rows = self.scan_authors('<author pid="third">Short Name</author>')
        self.assertEqual([r["dblp_pid"] for r in rows], ["third"])


if __name__ == "__main__":
    unittest.main()
