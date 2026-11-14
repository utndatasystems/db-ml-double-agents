import json
import tempfile
import unittest
from pathlib import Path

from scripts.home_population import home_population_summary, home_publication_trend, home_first_time_publishers, write_home_population_summary


def person(pid, *events):
    return {"dblpPid": pid, "db": {"events": list(events)}}


def event(venue="SIGMOD", role="pc_member", year=2022, **extra):
    return {"venue": venue, "role": role, "year": year, **extra}


class HomePopulationTests(unittest.TestCase):
    def test_ml_year_label_covers_counted_publications_rather_than_service_years(self):
        member = person("a", event())
        member["ml"] = {"mainPaperCount": 2, "venues": ["ICML", "ICLR"], "papers": [
            {"scope": "main", "venue": "ICML", "year": 2001},
            {"scope": "main", "venue": "ICLR", "year": 2026},
            {"scope": "side", "venue": "ICML", "year": 1995},
            {"scope": "main", "venue": "OTHER", "year": 2030},
            {"scope": "main", "venue": "ICML", "year": None},
        ]}
        outsider = person("b", event(role="general_chair"))
        outsider["ml"] = {"mainPaperCount": 1, "venues": ["NeurIPS"], "papers": [
            {"scope": "main", "venue": "NeurIPS", "year": 2040},
        ]}
        summary = home_population_summary([member, outsider])
        self.assertEqual(summary["mlPublicationYears"], {"startYear": 2001, "endYear": 2026})
        self.assertEqual((summary["startYear"], summary["endYear"]), (2022, 2025))
        self.assertEqual(summary["withMainMlPaper"], 1)
        self.assertEqual(home_population_summary([])["mlPublicationYears"], {"startYear": None, "endYear": None})

    def test_first_timer_growth_is_annualized_not_an_average_of_yearly_changes(self):
        people = []
        for year, count in ((2016, 3), (2025, 53)):
            for i in range(count):
                p = person(f"{year}-{i}", event())
                p["ml"] = {"papers": [{"scope": "main", "venue": "ICML", "year": year}]}
                people.append(p)
        summary = home_first_time_publishers(people, {p["dblpPid"] for p in people}, 2025)
        self.assertAlmostEqual(summary["annualizedGrowth"], (53 / 3) ** (1 / 9) - 1)
        self.assertEqual(round(summary["annualizedGrowth"] * 100), 38)

    def test_growth_rate_is_unavailable_for_zero_baseline_or_single_year(self):
        p = person("a", event())
        p["ml"] = {"papers": [{"scope": "main", "venue": "ICML", "year": 2025}]}
        self.assertIsNone(home_first_time_publishers([p], {"a"}, 2025)["annualizedGrowth"])
        p["ml"]["papers"][0]["year"] = 2001
        self.assertIsNone(home_first_time_publishers([p], {"a"}, 2025)["annualizedGrowth"])

    def test_share_growth_uses_each_venues_first_paper_year(self):
        p = person("a", event())
        papers = [{"scope": "main", "venue": "NeurIPS", "year": 2010, "workId": "early"}]
        for venue, count in (("NeurIPS", 2), ("ICML", 3), ("ICLR", 4)):
            for year, n in ((2018, 1), (2020, count)):
                papers.extend({"scope": "main", "venue": venue, "year": year,
                               "workId": f"{venue}-{year}-{i}"} for i in range(n))
        p["ml"] = {"papers": papers}
        totals = {v: {"2010": 100, "2018": 100, "2020": 100} for v in ("NeurIPS", "ICML", "ICLR")}
        comparison = home_publication_trend([p], {"a"}, totals)["growthComparison"]
        self.assertEqual(comparison["endYear"], 2020)
        self.assertEqual({r["venue"]: r["startYear"] for r in comparison["ratios"]},
                         {"NeurIPS": 2010, "ICML": 2018, "ICLR": 2018})
        self.assertEqual({r["venue"]: r["ratio"] for r in comparison["ratios"]},
                         {"NeurIPS": 2, "ICML": 3, "ICLR": 4})
        # Missing a first-year total must not quietly move the baseline to 2018.
        totals["NeurIPS"].pop("2010")
        missing = home_publication_trend([p], {"a"}, totals)["growthComparison"]["ratios"][0]
        self.assertEqual(missing["startYear"], 2010)
        self.assertIsNone(missing["ratio"])

    def test_share_growth_handles_zero_endpoints_and_single_or_absent_paper_years(self):
        p = person("a", event())
        p["ml"] = {"papers": [
            {"scope": "main", "venue": "NeurIPS", "year": 2020, "workId": "latest"},
            {"scope": "main", "venue": "ICML", "year": 2018, "workId": "earlier"},
        ]}
        totals = {v: {"2018": 100, "2020": 100} for v in ("NeurIPS", "ICML", "ICLR")}
        rows = {r["venue"]: r for r in home_publication_trend([p], {"a"}, totals)["growthComparison"]["ratios"]}
        self.assertEqual(rows["NeurIPS"]["startYear"], 2020)
        self.assertIsNone(rows["NeurIPS"]["ratio"])
        self.assertEqual(rows["ICML"]["ratio"], 0)
        self.assertIsNone(rows["ICLR"]["startYear"])
        self.assertIsNone(rows["ICLR"]["ratio"])

    def test_excluded_focus_profiles_cannot_supply_an_earlier_growth_baseline(self):
        included = person("a", event())
        included["focus"] = {"included": True}
        included["ml"] = {"mainPaperCount": 2, "venues": ["NeurIPS"], "papers": [
            {"scope": "main", "venue": "NeurIPS", "year": 2018, "workId": "included-first"},
            {"scope": "main", "venue": "NeurIPS", "year": 2025, "workId": "included-last"},
        ]}
        excluded = person("b", event())
        excluded["focus"] = {"included": False}
        excluded["ml"] = {"mainPaperCount": 1, "venues": ["NeurIPS"], "papers": [
            {"scope": "main", "venue": "NeurIPS", "year": 2001, "workId": "excluded-first"},
        ]}
        totals = {v: {"2001": 100, "2018": 100, "2025": 100} for v in ("NeurIPS", "ICML", "ICLR")}
        summary = home_population_summary([included, excluded], totals)
        self.assertEqual(summary["total"], 1)
        self.assertEqual(summary["publicationTrend"]["startYear"], 2018)
        self.assertEqual(summary["publicationTrend"]["growthComparison"]["ratios"][0]["startYear"], 2018)
        # The chart also rejects an excluded profile if a caller supplies its PID.
        direct = home_publication_trend([included, excluded], {"a", "b"}, totals)
        self.assertEqual(direct["startYear"], 2018)
        self.assertEqual(sum(p["papers"] for p in direct["series"][0]["points"]), 2)

    def test_first_timers_use_full_history_and_deduplicate_people_and_venues(self):
        def author(pid, *years):
            p = person(pid, event())
            p["ml"] = {"papers": [{"scope": "main", "venue": "ICML", "year": y} for y in years]}
            return p
        people = [author("old", 2001, 2025), author("new", 2025, 2025),
                  author("new", 2025), author("future", 2026), author("outside", 2025)]
        people[1]["ml"]["papers"].append({"scope": "main", "venue": "ICLR", "year": 2025})
        summary = home_first_time_publishers(people, {"old", "new", "future"}, 2025)
        self.assertEqual(summary["startYear"], 2016)
        self.assertEqual(summary["latestCount"], 1)
        self.assertEqual(sum(p["firstTime"] for p in summary["points"]), 1)

    def test_side_papers_do_not_establish_first_main_conference_year(self):
        author = person("a", event())
        author["ml"] = {"papers": [
            {"scope": "side", "venue": "ICML", "year": 2010},
            {"scope": "main", "venue": "OTHER", "year": 2012},
            {"scope": "main", "venue": "ICML", "year": 2025},
        ]}
        self.assertEqual(home_first_time_publishers([author], {"a"}, 2025)["latestCount"], 1)
        self.assertEqual(home_first_time_publishers([author], {"a"}, None)["points"], [])

    def test_trend_deduplicates_papers_and_keeps_missing_totals_unavailable(self):
        paper = {"workId": "shared", "scope": "main", "venue": "NeurIPS", "year": 2018}
        a = person("a", event())
        a["ml"] = {"papers": [paper, dict(paper), dict(paper, workId="side", scope="side")]}
        b = person("b", event())
        b["ml"] = {"papers": [paper, dict(paper, workId="icml", venue="ICML", year=2019)]}
        outsider = person("c", event(role="general_chair"))
        outsider["ml"] = {"papers": [dict(paper, workId="outside")]}
        totals = {v: {"2018": 10, "2019": 20, "2020": 30} for v in ("NeurIPS", "ICML", "ICLR")}
        totals["ICML"].pop("2019")
        totals["ICLR"]["2021"] = 50
        trend = home_publication_trend([a, b, outsider], {"a", "b"}, totals)
        self.assertEqual((trend["startYear"], trend["endYear"]), (2018, 2020))
        series = {s["venue"]: s["points"] for s in trend["series"]}
        self.assertEqual(series["NeurIPS"][0], {"year": 2018, "papers": 1, "total": 10, "share": 10})
        self.assertEqual(series["ICML"][0]["share"], 0)
        self.assertEqual(series["ICML"][1]["papers"], 1)
        self.assertIsNone(series["ICML"][1]["share"])
        self.assertIsNone(series["ICML"][1]["total"])

    def test_trend_without_shared_totals_is_unavailable(self):
        trend = home_publication_trend([], set(), {"ICLR": {"2026": 100}})
        self.assertEqual(trend["series"], [])
        self.assertIsNone(trend["endYear"])

    def test_trend_includes_early_papers_before_later_venues_have_totals(self):
        author = person("a", event())
        author["ml"] = {"papers": [
            {"scope": "main", "venue": "ICML", "year": 2001, "workId": "early"},
            {"scope": "main", "venue": "ICLR", "year": 2018, "workId": "late"},
        ]}
        totals = {"ICML": {"2001": 80, "2018": 600},
                  "NeurIPS": {"2001": 200, "2018": 1000}, "ICLR": {"2018": 300}}
        trend = home_publication_trend([author], {"a"}, totals)
        self.assertEqual((trend["startYear"], trend["endYear"]), (2001, 2018))
        series = {s["venue"]: s["points"] for s in trend["series"]}
        self.assertEqual(series["ICML"][0]["share"], 1.25)
        self.assertIsNone(series["ICLR"][0]["share"])
        self.assertEqual(series["NeurIPS"][0]["share"], 0)

    def test_unique_identities_and_overlapping_venue_memberships(self):
        people = [
            person("a", event(), event(year=2023), event("VLDB", "associate_editor")),
            person("a", event()),  # Alias for the same identity.
            person("b", event("ICDE", "industrial_pc_member")),
            person("c", event("SIGMOD", "pods_pc_member")),
            person("d", event("EDBT", "program_committee_chair")),
            person("e", event("CIDR", "program_committee_member", 2025)),
        ]
        summary = home_population_summary(people)
        self.assertEqual(summary["total"], 5)
        counts = {row["venue"]: row["count"] for row in summary["venues"]}
        self.assertEqual(counts, {"SIGMOD": 2, "VLDB": 1, "ICDE": 1, "CIDR": 1, "EDBT": 1})

    def test_other_service_shadow_missing_identity_and_outside_years_excluded(self):
        excluded = [
            event(role="general_chair"), event(role="tutorial_chair"),
            event(role="award_committee_member"), event(role="shadow_pc_member"),
            event(track="shadow"), event(year=2021), event(year=2026),
            event(venue="OTHER"), event(role="unknown"),
        ]
        people = [person(str(i), item) for i, item in enumerate(excluded)]
        people.append(person("", event()))
        summary = home_population_summary(people)
        self.assertEqual(summary["total"], 0)
        self.assertTrue(all(row["count"] == 0 for row in summary["venues"]))

    def test_generated_file_updates_when_membership_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            write_home_population_summary(output, [person("a", event())])
            first = json.loads((output / "home_summary.json").read_text())
            write_home_population_summary(output, [person("a", event()), person("b", event())])
            second = json.loads((output / "home_summary.json").read_text())
            self.assertEqual((first["total"], second["total"]), (1, 2))

    def test_ml_authors_are_unique_and_restricted_to_the_pc_population(self):
        author = person("a", event())
        author["ml"] = {"mainPaperCount": 4, "venues": ["NeurIPS", "ICML"]}
        alias = person("a", event("VLDB", "review_board_member"))
        alias["ml"] = {"mainPaperCount": 4, "venues": ["NeurIPS", "ICML"]}
        side_only = person("b", event())
        side_only["ml"] = {"mainPaperCount": 0, "sidePaperCount": 20, "venues": ["ICLR"]}
        outsider = person("c", event(role="general_chair"))
        outsider["ml"] = {"mainPaperCount": 10, "venues": ["ICLR"]}
        summary = home_population_summary([author, alias, side_only, outsider])
        self.assertEqual(summary["total"], 2)
        self.assertEqual(summary["withMainMlPaper"], 1)
        self.assertEqual(
            {row["venue"]: row["count"] for row in summary["mlVenues"]},
            {"NeurIPS": 1, "ICML": 1, "ICLR": 0},
        )
