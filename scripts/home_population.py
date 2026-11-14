"""Count distinct DBLP identities with qualifying program-committee service."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

START_YEAR = 2022
END_YEAR = 2025
VENUES = (
    ("SIGMOD", "SIGMOD/PODS"),
    ("VLDB", "VLDB"),
    ("ICDE", "ICDE"),
    ("CIDR", "CIDR"),
    ("EDBT", "EDBT"),
)
ML_VENUES = ("NeurIPS", "ICML", "ICLR")
# Include research, industry and demo PCs and their program leadership.
# General chairs, awards, tutorial/workshop service and Shadow PCs alone
# cannot qualify someone for this population.
PC_ROLES = frozenset({
    "pc_member", "program_committee_member", "review_board_member",
    "senior_program_committee_member", "associate_editor", "area_chair",
    "associate_chair", "pods_pc_member", "industrial_pc_member", "demo_pc_member",
    "demo_associate_editor", "industrial_associate_editor",
    "program_committee_chair", "program_chair", "program_co_chair",
    "research_program_chair", "research_program_co_chair", "program_vice_chair",
    "pc_track_chair", "editor_in_chief", "pods_program_chair",
    "industrial_track_chair", "demonstration_chair", "demo_chair",
})


def home_publication_trend(
    people: list[dict[str, Any]], population: set[str], totals: dict[str, Any]
) -> dict[str, Any]:
    # End at a shared year; extend backwards to the cohort's first paper
    # with a usable denominator. Venues without early totals have gaps.
    available = {
        venue: {int(year): count for year, count in totals.get(venue, {}).items()
                if str(year).isdigit() and isinstance(count, (int, float)) and count > 0}
        for venue in ML_VENUES
    }
    common = set.intersection(*(set(years) for years in available.values()))
    if not common:
        return {"startYear": None, "endYear": None, "series": [], "growthComparison": None}
    end = max(common)
    papers: dict[tuple[str, int], set[str]] = {}
    for person in people:
        if person.get("focus") and not person["focus"].get("included"):
            continue
        if str(person.get("dblpPid") or "").strip() not in population:
            continue
        for paper in (person.get("ml") or {}).get("papers", []):
            venue, year = paper.get("venue"), paper.get("year")
            if paper.get("scope") != "main" or venue not in available:
                continue
            if not isinstance(year, int) or not 1900 <= year <= end:
                continue
            key = paper.get("workId") or paper.get("canonicalDblpKey") or paper.get("dblpKey")
            if not key and paper.get("title"):
                key = json.dumps([venue, year, paper["title"], paper.get("url", "")])
            if key:
                papers.setdefault((venue, year), set()).add(key)
    observed_years = [year for venue, year in papers if year in available[venue]]
    if not observed_years:
        return {"startYear": None, "endYear": None, "series": [], "growthComparison": None}
    start = min(observed_years)
    series = []
    for venue in ML_VENUES:
        points = []
        for year in range(start, end + 1):
            count = len(papers.get((venue, year), set()))
            total = available[venue].get(year)
            points.append({"year": year, "papers": count, "total": total,
                           "share": 100 * count / total if total else None})
        series.append({"venue": venue, "points": points})
    # Each venue starts at its own first recorded qualifying paper year.
    # Missing totals at that first year make growth unavailable, not a later baseline.
    comparison = {"endYear": end, "ratios": []}
    for item in series:
        venue = item["venue"]
        first_year = min((year for v, year in papers if v == venue), default=None)
        first_total = available[venue].get(first_year)
        first_share = (100 * len(papers[(venue, first_year)]) / first_total
                       if first_total else None)
        final_share = item["points"][-1]["share"]
        ratio = (final_share / first_share
                 if first_year is not None and first_year < end and first_share
                 and final_share is not None else None)
        comparison["ratios"].append({"venue": venue, "startYear": first_year, "ratio": ratio})
    return {"startYear": start, "endYear": end, "series": series, "growthComparison": comparison}


def home_first_time_publishers(
    people: list[dict[str, Any]], population: set[str], end_year: int | None
) -> dict[str, Any]:
    years_by_pid: dict[str, set[int]] = {}
    for person in people:
        if person.get("focus") and not person["focus"].get("included"):
            continue
        pid = str(person.get("dblpPid") or "").strip()
        if pid not in population:
            continue
        for paper in (person.get("ml") or {}).get("papers", []):
            year = paper.get("year")
            if (paper.get("scope") == "main" and paper.get("venue") in ML_VENUES
                    and isinstance(year, int) and year >= 1900):
                years_by_pid.setdefault(pid, set()).add(year)
    # First appearances use the entire recorded history, not just the chart
    # window. An author appearing at two venues in their first year counts once.
    first_years = [min(years) for years in years_by_pid.values()]
    observed = [year for year in first_years if end_year is not None and year <= end_year]
    if not observed:
        return {"startYear": None, "endYear": None, "latestCount": None,
                "annualizedGrowth": None, "points": []}
    start = max(min(observed), end_year - 9)
    points = [{"year": year, "firstTime": first_years.count(year)}
              for year in range(start, end_year + 1)]
    first, last = points[0]["firstTime"], points[-1]["firstTime"]
    growth = (last / first) ** (1 / (end_year - start)) - 1 if first > 0 and end_year > start else None
    return {"startYear": start, "endYear": end_year,
            "latestCount": last, "annualizedGrowth": growth, "points": points}


def home_population_summary(
    people: list[dict[str, Any]], main_venue_year_totals: dict[str, Any] | None = None
) -> dict[str, Any]:
    rosters: dict[str, set[str]] = {venue: set() for venue, _ in VENUES}
    main_ml_authors: set[str] = set()
    ml_rosters: dict[str, set[str]] = {venue: set() for venue in ML_VENUES}
    for person in people:
        if person.get("focus") and not person["focus"].get("included"):
            continue
        pid = str(person.get("dblpPid") or "").strip()
        if not pid:
            continue
        if (person.get("ml") or {}).get("mainPaperCount", 0) > 0:
            main_ml_authors.add(pid)
            for venue in (person.get("ml") or {}).get("venues", []):
                if venue in ml_rosters:
                    ml_rosters[venue].add(pid)
        for event in (person.get("db") or {}).get("events", []):
            venue = event.get("venue")
            role = event.get("role") or ""
            track = str(event.get("track") or "").casefold()
            year = event.get("year")
            if venue not in rosters or role not in PC_ROLES or "shadow" in track:
                continue
            if not isinstance(year, int) or not START_YEAR <= year <= END_YEAR:
                continue
            rosters[venue].add(pid)
    population = set().union(*rosters.values())
    ml_years = {
        paper["year"]
        for person in people if str(person.get("dblpPid") or "").strip() in population
        for paper in (person.get("ml") or {}).get("papers", [])
        if paper.get("scope") == "main" and paper.get("venue") in ML_VENUES
        and isinstance(paper.get("year"), int) and paper["year"] >= 1900
    }
    trend = home_publication_trend(people, population, main_venue_year_totals or {})
    return {
        "startYear": START_YEAR,
        "endYear": END_YEAR,
        "total": len(population),
        "withMainMlPaper": len(population & main_ml_authors),
        "mlPublicationYears": {
            "startYear": min(ml_years, default=None),
            "endYear": max(ml_years, default=None),
        },
        "publicationTrend": trend,
        "firstTimePublishers": home_first_time_publishers(people, population, trend["endYear"]),
        "mlVenues": [
            {"venue": venue, "label": venue, "count": len(population & ml_rosters[venue])}
            for venue in ML_VENUES
        ],
        "venues": [
            {"venue": venue, "label": label, "count": len(rosters[venue])}
            for venue, label in VENUES
        ],
    }


def write_home_population_summary(
    output_dir: Path, people: list[dict[str, Any]],
    main_venue_year_totals: dict[str, Any] | None = None,
) -> None:
    target = output_dir / "home_summary.json"
    temporary = target.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(home_population_summary(people, main_venue_year_totals), indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(target)
