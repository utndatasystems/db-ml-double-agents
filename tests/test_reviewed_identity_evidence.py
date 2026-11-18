"""Guard the evidence-backed correction for the Pittsburgh service name."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from apply_profile_entity_resolution import load_reviews, proposal_for_group


class ReviewedIdentityEvidenceTests(unittest.TestCase):
    def setUp(self):
        path = Path(__file__).resolve().parents[1] / "data" / "reviewed_person_identities.csv"
        self.review = load_reviews(path).get("adam lee")
        self.group = dict(
            name_key="adam lee", representative_name="Adam Lee",
            status="resolved_unique_exact", resolved_dblp_pid="58/8815",
            reviewer_affiliations=["University of Pittsburgh"],
            candidates=[dict(pid="62/1373", canonical_name="Adam J. Lee",
                             verified_orcids=["0000-0002-2596-7256"])],
        )

    def test_pittsburgh_service_uses_reviewed_full_identity(self):
        self.assertIsNotNone(self.review, "The verified Pittsburgh identity review is missing")
        item, reason = proposal_for_group(self.group, [], None, self.review)
        self.assertEqual(reason, "proposed_reviewed")
        self.assertEqual((item["previous_pid"], item["candidate_pid"]), ("58/8815", "62/1373"))
        self.assertEqual(item["proposed_status"], "resolved_reviewed_profile")

    def test_review_does_not_cover_a_different_affiliation(self):
        self.assertIsNotNone(self.review)
        group = dict(self.group, reviewer_affiliations=["Another University"])
        item, reason = proposal_for_group(group, [], None, self.review)
        self.assertIsNone(item)
        self.assertEqual(reason, "reviewed_affiliations_changed")

    def test_corroborated_full_profiles_are_scoped_and_fail_closed(self):
        reviews = load_reviews(Path(__file__).resolve().parents[1] / "data/reviewed_person_identities.csv")
        for name, previous, target, affiliation in [
            ("brian cooper", "34/2717", "12/875", "Google"),
            ("yisu wang", "269/9625", "185/9964", "University of Washington"),
            ("djellel difallah", "409/8145", "09/10814", "NYU"),
        ]:
            with self.subTest(name=name):
                review = reviews[name]
                group = dict(name_key=name, representative_name=name, status="resolved_unique_exact",
                             resolved_dblp_pid=previous, reviewer_affiliations=[affiliation],
                             candidates=[dict(pid=target, canonical_name=name, verified_orcids=[])])
                item, reason = proposal_for_group(group, [], None, review)
                self.assertEqual(reason, "proposed_reviewed")
                self.assertEqual(item["candidate_pid"], target)
                for updates, expected in [
                    (dict(reviewer_affiliations=["Different organization"]), "reviewed_affiliations_changed"),
                    (dict(candidates=[]), "reviewed_candidate_missing"),
                    (dict(resolved_dblp_pid="another/person"), "reviewed_assignment_changed"),
                ]:
                    result, rejected = proposal_for_group(dict(group, **updates), [], None, review)
                    self.assertIsNone(result)
                    self.assertEqual(rejected, expected)

    def test_unresolved_identity_review_requires_exact_candidate_and_affiliation_scope(self):
        reviews = load_reviews(Path(__file__).resolve().parents[1] / "data/reviewed_person_identities.csv")
        review = reviews['angelos christos anadiotis']
        self.assertEqual(review['previous_pid'], '')
        group = dict(name_key='angelos christos anadiotis', representative_name='Angelos Christos Anadiotis',
                     status='candidate_unique_variant', resolved_dblp_pid=None,
                     reviewer_affiliations=['Oracle'], candidates=[dict(pid='85/8852',
                     canonical_name='Angelos-Christos G. Anadiotis', verified_orcids=['one', 'two'])])
        item, reason = proposal_for_group(group, [], None, review)
        self.assertEqual(reason, 'proposed_reviewed')
        self.assertEqual(item['candidate_pid'], '85/8852')
        self.assertIsNone(item['best_orcid'])
        for updates, expected in [
            (dict(reviewer_affiliations=['Unreviewed organization']), 'reviewed_affiliations_changed'),
            (dict(candidates=[]), 'reviewed_candidate_missing'),
            (dict(resolved_dblp_pid='another/person'), 'reviewed_assignment_changed'),
        ]:
            result, reason = proposal_for_group(dict(group, **updates), [], None, review)
            self.assertIsNone(result)
            self.assertEqual(reason, expected)

    def test_reviewed_identities_survive_new_unaffiliated_namesakes_without_relaxing_matching(self):
        reviews = load_reviews(Path(__file__).resolve().parents[1] / "data/reviewed_person_identities.csv")
        for name, target, competitor, affiliation in [
            ('dong deng', '71/8700-1', '448/4576', 'Rutgers University'),
            ('eugene wu', '41/3679', '447/2918', 'Columbia University'),
            ('lihong he', '35/850', '448/4499', 'Temple University'),
            ('qingqing ye', '194/0001-1', '446/7153', 'Hong Kong Polytechnic University'),
            ('yufei tao', 't/YufeiTao', '448/3919', 'Chinese University of Hong Kong'),
        ]:
            with self.subTest(name=name):
                group = dict(name_key=name, representative_name=name,
                             status='ambiguous_exact_name', resolved_dblp_pid=None,
                             reviewer_affiliations=[affiliation], candidates=[
                                 dict(pid=target, canonical_name=name, verified_orcids=[]),
                                 dict(pid=competitor, canonical_name=name, verified_orcids=[]),
                             ])
                # An unaffiliated namesake still prevents unreviewed matching.
                item, reason = proposal_for_group(group, [], None)
                self.assertIsNone(item)
                self.assertTrue(name in reviews, name + ' needs a persistent independently verified review')
                review = reviews[name]
                self.assertEqual(review['previous_pid'], '')
                item, reason = proposal_for_group(group, [], None, review)
                self.assertEqual(reason, 'proposed_reviewed')
                self.assertEqual(item['candidate_pid'], target)
                for updates, expected in [
                    (dict(reviewer_affiliations=['Unreviewed organization']), 'reviewed_affiliations_changed'),
                    (dict(candidates=[group['candidates'][1]]), 'reviewed_candidate_missing'),
                    (dict(resolved_dblp_pid=competitor), 'reviewed_assignment_changed'),
                ]:
                    item, reason = proposal_for_group(dict(group, **updates), [], None, review)
                    self.assertIsNone(item)
                    self.assertEqual(reason, expected)


if __name__ == "__main__":
    unittest.main()
