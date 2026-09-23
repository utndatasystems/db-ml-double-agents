#!/usr/bin/env python3
"""Build DB × main-ML crossover matrix statistics for the static website.

Only ``scope=main`` papers from ``site_data.json`` contribute to the matrix.
Side papers are bibliography context and never count as ML crossover evidence.

For a selected publication year Y, a cell requires observed DB service at the
DB venue in any scraped service year and a main ML paper at the selected ML
venue in Y. The denominator remains the all-years resolved DB-service roster for
that DB venue. This makes publication-year exploration meaningful outside the
2022–2025 service scrape window.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ENTITY_DB = REPO_ROOT / "data" / "entity_resolution.sqlite"
DEFAULT_SITE_DATA = REPO_ROOT / "website" / "data" / "site_data.json"
DEFAULT_OUTPUT_DIR = REPO_ROOT / "website" / "data"


def require_tables(conn: sqlite3.Connection, names: set[str]) -> None:
    have = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    missing = names - have
    if missing:
        raise RuntimeError(
            "entity_resolution.sqlite is missing table(s): " + ", ".join(sorted(missing))
        )


def load_resolved_db_service(db_path: Path) -> list[tuple[str, str, int]]:
    conn = sqlite3.connect(db_path)
    try:
        require_tables(conn, {"name_resolution", "reviewer_observations"})
        rows = conn.execute(
            """
            SELECT DISTINCT o.venue, r.resolved_dblp_pid, o.year
            FROM name_resolution r
            JOIN reviewer_observations o ON o.name_key = r.name_key
            WHERE r.resolved_dblp_pid IS NOT NULL
              AND o.venue IS NOT NULL
              AND TRIM(o.venue) <> ''
              AND o.year IS NOT NULL
            """
        ).fetchall()
        return [
            (str(venue).strip(), str(pid).strip(), int(year))
            for venue, pid, year in rows
            if str(venue).strip() and str(pid).strip()
        ]
    finally:
        conn.close()


def load_site_payload(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload.get("people"), list):
        raise RuntimeError("site_data.json does not contain a people array")
    return payload


def person_id(person: dict[str, Any]) -> str:
    return str(person.get("id") or person.get("dblpPid") or "").replace("dblp:", "", 1).strip()


def as_year(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def main_papers(person: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        paper
        for paper in (person.get("ml") or {}).get("papers", [])
        if str(paper.get("scope") or "main").strip().casefold() == "main"
        and str(paper.get("venue") or "").strip()
    ]


def matrix_from_cells(
    *,
    db_venues: list[str],
    ml_venues: list[str],
    denominators: dict[str, int],
    cell_people: dict[tuple[str, str], set[str]],
) -> dict[str, dict[str, dict[str, float | int | None]]]:
    matrix: dict[str, dict[str, dict[str, float | int | None]]] = {}
    for db_venue in db_venues:
        denominator = int(denominators.get(db_venue, 0))
        row: dict[str, dict[str, float | int | None]] = {}
        for ml_venue in ml_venues:
            count = len(cell_people.get((db_venue, ml_venue), set()))
            pct = round(100.0 * count / denominator, 1) if denominator else None
            row[ml_venue] = {
                "people": count,
                "resolvedDbPeople": denominator,
                "percentOfResolvedDbPeople": pct,
            }
        matrix[db_venue] = row
    return matrix


def build_stats(
    payload: dict[str, Any], db_service: list[tuple[str, str, int]]
) -> dict[str, Any]:
    people: list[dict[str, Any]] = payload["people"]
    db_venues = sorted({venue for venue, _pid, _year in db_service})
    ml_venues = sorted(
        {
            str(paper.get("venue")).strip()
            for person in people
            for paper in main_papers(person)
            if str(paper.get("venue") or "").strip()
        }
    )

    all_denom_sets: dict[str, set[str]] = defaultdict(set)
    for venue, pid, _year in db_service:
        all_denom_sets[venue].add(pid)
    all_denominators = {venue: len(all_denom_sets[venue]) for venue in db_venues}

    all_cells: dict[tuple[str, str], set[str]] = defaultdict(set)
    for person in people:
        pid = person_id(person)
        if not pid:
            continue
        person_db = {
            str(event.get("venue")).strip()
            for event in (person.get("db") or {}).get("events", [])
            if str(event.get("venue") or "").strip()
        }
        person_ml = {str(paper.get("venue")).strip() for paper in main_papers(person)}
        for db_venue in person_db:
            for ml_venue in person_ml:
                all_cells[(db_venue, ml_venue)].add(pid)

    years = sorted(
        {
            year
            for person in people
            for paper in main_papers(person)
            for year in [as_year(paper.get("year"))]
            if year is not None
        }
    )

    by_year: dict[str, Any] = {}
    for year in years:
        cells: dict[tuple[str, str], set[str]] = defaultdict(set)
        for person in people:
            pid = person_id(person)
            if not pid:
                continue
            person_db = {
                str(event.get("venue")).strip()
                for event in (person.get("db") or {}).get("events", [])
                if str(event.get("venue") or "").strip()
            }
            person_ml = {
                str(paper.get("venue")).strip()
                for paper in main_papers(person)
                if as_year(paper.get("year")) == year
            }
            for db_venue in person_db:
                for ml_venue in person_ml:
                    cells[(db_venue, ml_venue)].add(pid)

        by_year[str(year)] = {
            "dbVenueResolvedPeople": all_denominators,
            "matrix": matrix_from_cells(
                db_venues=db_venues,
                ml_venues=ml_venues,
                denominators=all_denominators,
                cell_people=cells,
            ),
        }

    return {
        "meta": {
            "generatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "schemaVersion": 4,
            "publicationScope": "main only",
            "allTimeDenominator": "distinct resolved DBLP PIDs with service at the DB venue",
            "allTimeNumerator": "distinct people with DB service at the DB venue and an exact main NeurIPS/ICML/ICLR paper",
            "selectedYearMeaning": "selected publication year restricts main ML papers only; DB service remains the all-years observed roster",
        },
        "years": years,
        "dbVenues": db_venues,
        "mlVenues": ml_venues,
        "dbVenueResolvedPeople": all_denominators,
        "matrix": matrix_from_cells(
            db_venues=db_venues,
            ml_venues=ml_venues,
            denominators=all_denominators,
            cell_people=all_cells,
        ),
        "byYear": by_year,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--entity-db", type=Path, default=DEFAULT_ENTITY_DB)
    parser.add_argument("--site-data", type=Path, default=DEFAULT_SITE_DATA)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args()

    entity_db = args.entity_db.resolve()
    site_data = args.site_data.resolve()
    output_dir = args.output_dir.resolve()
    if not entity_db.exists():
        raise SystemExit(f"Missing required input: {entity_db}")
    if not site_data.exists():
        raise SystemExit(f"Missing required input: {site_data}")

    stats = build_stats(load_site_payload(site_data), load_resolved_db_service(entity_db))
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "matrix_stats.json"
    js_path = output_dir / "matrix_stats.js"
    text = json.dumps(stats, ensure_ascii=False, indent=2)
    json_path.write_text(text, encoding="utf-8")
    js_path.write_text(f"window.CROSSOVER_MATRIX_STATS = {text};\n", encoding="utf-8")

    print("Resolved DB people by venue (all observed years):")
    for venue in stats["dbVenues"]:
        print(f"  {venue}: {stats['dbVenueResolvedPeople'].get(venue, 0)}")
    print("\nYear-aware matrices built for:")
    print("  " + ", ".join(str(year) for year in stats["years"]))
    print("  Selected publication year = exact main ML papers in that year; DB service remains all observed years.")
    print(f"\nWrote: {json_path}")
    print(f"Wrote: {js_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
