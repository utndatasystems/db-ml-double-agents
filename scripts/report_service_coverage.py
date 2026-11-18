#!/usr/bin/env python3
"""Report 2022+ DB-service coverage by normalized role family.

Run after the five venue scrapers. The report exits non-zero if a venue/year is
missing a role family that the study expects to collect, so a whole senior layer
cannot disappear silently after a website/parser change.
"""

from __future__ import annotations

import argparse
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

VENUES = {
    "SIGMOD": (REPO_ROOT / "data" / "sigmod_reviewers.sqlite", "year"),
    "VLDB": (REPO_ROOT / "data" / "vldb_reviewers.sqlite", "conference_year"),
    "ICDE": (REPO_ROOT / "data" / "icde_reviewers.sqlite", "year"),
    "EDBT": (REPO_ROOT / "data" / "edbt_reviewers.sqlite", "year"),
    "CIDR": (REPO_ROOT / "data" / "cidr_reviewers.sqlite", "year"),
}

ROLE_FAMILIES = (
    "general_chair",
    "research_program_chair",
    "senior_reviewer",
    "research_reviewer",
    "technical_track_chair",
)

REQUIRED = {
    "SIGMOD": {"general_chair", "research_program_chair", "senior_reviewer", "research_reviewer"},
    "VLDB": {"general_chair", "research_program_chair", "senior_reviewer", "research_reviewer"},
    "ICDE": {"general_chair", "research_program_chair", "senior_reviewer", "research_reviewer"},
    "EDBT": {"general_chair", "research_program_chair", "senior_reviewer", "research_reviewer"},
    "CIDR": {"general_chair", "research_program_chair", "research_reviewer"},
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--years", nargs="+", type=int, default=None)
    return parser.parse_args()


def default_years() -> list[int]:
    last_complete = datetime.now(timezone.utc).year - 1
    return list(range(2022, last_complete + 1))


def main() -> int:
    args = parse_args()
    years = sorted(set(args.years if args.years is not None else default_years()))
    failures: list[str] = []

    header = ["venue", "year", *ROLE_FAMILIES, "qualifying_people"]
    print("\t".join(header))

    for venue, (path, year_column) in VENUES.items():
        if not path.exists():
            failures.append(f"{venue}: missing database {path}")
            continue
        conn = sqlite3.connect(path)
        try:
            columns = {str(r[1]) for r in conn.execute("PRAGMA table_info(reviewer_records)")}
            needed = {"role_family", "qualifies_db_seed", "name_key", year_column}
            missing_columns = sorted(needed - columns)
            if missing_columns:
                failures.append(f"{venue}: missing schema columns {', '.join(missing_columns)}")
                continue

            for year in years:
                counts = {family: 0 for family in ROLE_FAMILIES}
                for family, count in conn.execute(
                    f"""
                    SELECT role_family, COUNT(DISTINCT name_key)
                    FROM reviewer_records
                    WHERE {year_column}=? AND qualifies_db_seed=1
                    GROUP BY role_family
                    """,
                    (year,),
                ):
                    if family in counts:
                        counts[str(family)] = int(count)

                people = int(conn.execute(
                    f"SELECT COUNT(DISTINCT name_key) FROM reviewer_records "
                    f"WHERE {year_column}=? AND qualifies_db_seed=1",
                    (year,),
                ).fetchone()[0])

                row = [venue, str(year), *(str(counts[f]) for f in ROLE_FAMILIES), str(people)]
                print("\t".join(row))

                missing_required = sorted(f for f in REQUIRED[venue] if counts.get(f, 0) == 0)
                if missing_required:
                    failures.append(
                        f"{venue} {year}: missing required role families: {', '.join(missing_required)}"
                    )
        finally:
            conn.close()

    if failures:
        print("\nCoverage failures:")
        for failure in failures:
            print(f"  - {failure}")
        return 1

    print("\nCoverage checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
