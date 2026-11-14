#!/usr/bin/env python3
"""Scrape public CIDR program-committee pages into SQLite.

The official CIDR yearly websites publish an Officers page such as:
    https://www.cidrdb.org/cidr2026/officers.html

CIDR's call for papers states that papers/demos are reviewed by the program
committee, so this script stores Program Committee members as reviewer records.
Program Committee Chairs and General Chairs are included by default because
they are qualifying scientific/conference leadership roles.

The output intentionally contains raw observations, not entity-resolved people.
The same person may occur in several years or with different affiliations.
`name_key` is only a conservative candidate-generation key for later ER.

Install:
    pip install requests beautifulsoup4

Examples:
    python scripts/scrape_cidr_reviewers.py --years 2022 2023 2024 2025 2026
    python scripts/scrape_cidr_reviewers.py --years 2026

Optional polite user-agent:
    export CIDR_SCRAPER_UA='my-research-project/1.0 (mailto:me@example.edu)'
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import sqlite3
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup, Tag

from scraper_common import clean_line, normalize_name_key

from service_roles import (
    assert_no_qualifying_person_loss,
    ensure_reviewer_service_schema,
    service_values,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = REPO_ROOT / "data" / "cidr_reviewers.sqlite"

DEFAULT_START_YEAR = 2022

def default_years() -> list[int]:
    """Years collected when --years is omitted: 2022 through last calendar year."""
    last_complete_year = datetime.now(timezone.utc).year - 1
    if last_complete_year < DEFAULT_START_YEAR:
        return []
    return list(range(DEFAULT_START_YEAR, last_complete_year + 1))

DEFAULT_UA = os.environ.get("CIDR_SCRAPER_UA", "cidr-reviewer-research/1.0")
OFFICERS_URL = "https://www.cidrdb.org/cidr{year}/officers.html"

ROLE_HEADINGS = {
    "general chair": "general_chair",
    "general chairs": "general_chair",
    "program committee": "program_committee_member",
    "program committee chair": "program_committee_chair",
    "program committee chairs": "program_committee_chair",
}


@dataclass(frozen=True)
class ReviewerRecord:
    year: int
    role: str
    name: str
    affiliation: str
    name_key: str
    raw_text: str
    source_url: str
    fetched_at: str




def plausible_person_name(value: str) -> bool:
    value = clean_line(value).strip("-–—,;:")
    if not value or len(value) > 140:
        return False
    if not any(ch.isalpha() for ch in value):
        return False
    words = value.split()
    return 1 <= len(words) <= 14


def split_trailing_parenthetical(raw: str) -> tuple[str, str] | None:
    """Split `Name (Affiliation)` while allowing parentheses in affiliation."""
    raw = clean_line(raw)
    if not raw.endswith(")"):
        return None

    depth = 0
    opening: int | None = None
    for i in range(len(raw) - 1, -1, -1):
        char = raw[i]
        if char == ")":
            depth += 1
        elif char == "(":
            depth -= 1
            if depth == 0:
                opening = i
                break
            if depth < 0:
                return None

    if opening is None:
        return None

    name = clean_line(raw[:opening]).rstrip(",").strip()
    affiliation = clean_line(raw[opening + 1 : -1])
    if plausible_person_name(name) and affiliation:
        return name, affiliation
    return None


def parse_person_line(line: str) -> tuple[str, str] | None:
    """Parse CIDR rows such as `Name (Affiliation)` or `Name, Affiliation`."""
    raw = clean_line(line)
    if not raw:
        return None

    paren = split_trailing_parenthetical(raw)
    if paren is not None:
        return paren

    if "," in raw:
        left, right = raw.split(",", 1)
        name = clean_line(left)
        affiliation = clean_line(right).strip()
        # Some historical pages wrap the affiliation itself in parentheses.
        if affiliation.startswith("(") and affiliation.endswith(")"):
            affiliation = affiliation[1:-1].strip()
        if plausible_person_name(name) and affiliation:
            return name, affiliation

    for sep in (" - ", " – ", " — "):
        if sep in raw:
            left, right = raw.split(sep, 1)
            name = clean_line(left)
            affiliation = clean_line(right)
            if plausible_person_name(name) and affiliation:
                return name, affiliation

    return None


def iter_committee_items(soup: BeautifulSoup):
    """Yield (role, text) pairs from heading/list structure in document order."""
    current_role: str | None = None
    saw_pc_heading = False

    for node in soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6", "li", "p"]):
        if not isinstance(node, Tag):
            continue
        text = clean_line(node.get_text(" ", strip=True))
        if not text:
            continue

        if node.name and node.name.startswith("h"):
            key = text.casefold().rstrip(":")
            current_role = ROLE_HEADINGS.get(key)
            if current_role is not None:
                saw_pc_heading = True
            continue

        if current_role is None:
            continue

        # Skip a parent paragraph if it contains list items; the li rows will be
        # visited separately and are less likely to duplicate text.
        if node.name == "p" and node.find("li") is not None:
            continue

        yield current_role, text

    if not saw_pc_heading:
        raise ValueError("No CIDR Program Committee heading found")


def parse_cidr_officers_page(
    html: bytes,
    *,
    year: int,
    source_url: str,
    fetched_at: str,
    include_chairs: bool = True,
) -> list[ReviewerRecord]:
    soup = BeautifulSoup(html, "html.parser")
    records: list[ReviewerRecord] = []
    seen: set[tuple[str, str, str]] = set()

    for role, raw in iter_committee_items(soup):
        # Chairs are qualifying DB-service roles from 2022 onward. Keep the
        # argument for CLI/API backwards compatibility, but always retain them.
        parsed = parse_person_line(raw)
        if parsed is None:
            continue
        name, affiliation = parsed
        key = (role, name, affiliation)
        if key in seen:
            continue
        seen.add(key)
        records.append(
            ReviewerRecord(
                year=year,
                role=role,
                name=name,
                affiliation=affiliation,
                name_key=normalize_name_key(name),
                raw_text=raw,
                source_url=source_url,
                fetched_at=fetched_at,
            )
        )

    if not any(r.role == "program_committee_member" for r in records):
        raise ValueError(f"Program Committee section found, but no member rows parsed from {source_url}")
    if not any(r.role == "program_committee_chair" for r in records):
        raise ValueError(f"CIDR Program Committee Chairs missing from {source_url}")
    if not any(r.role == "general_chair" for r in records):
        raise ValueError(f"CIDR General Chairs missing from {source_url}")
    return records


def robots_allows(session: requests.Session, target_url: str) -> bool:
    parsed = urlparse(target_url)
    robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
    try:
        response = session.get(robots_url, timeout=20)
    except requests.RequestException as exc:
        raise RuntimeError(f"Could not check {robots_url}: {exc}") from exc

    if response.status_code in (404, 410):
        return True
    if response.status_code in (401, 403):
        return False
    if not response.ok:
        raise RuntimeError(f"Could not check robots.txt at {robots_url}: HTTP {response.status_code}")

    rp = RobotFileParser()
    rp.set_url(robots_url)
    rp.parse(response.text.splitlines())
    return rp.can_fetch(DEFAULT_UA, target_url)


def fetch_page(session: requests.Session, url: str, ignore_robots: bool = False) -> tuple[bytes, str]:
    if not ignore_robots and not robots_allows(session, url):
        raise PermissionError(f"robots.txt does not allow this user-agent to fetch {url}")
    response = session.get(url, timeout=30)
    response.raise_for_status()
    return response.content, datetime.now(timezone.utc).isoformat(timespec="seconds")


def open_db(path: str) -> sqlite3.Connection:
    db_path = Path(path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS source_pages (
            id INTEGER PRIMARY KEY,
            year INTEGER NOT NULL,
            url TEXT NOT NULL,
            fetched_at TEXT NOT NULL,
            sha256 TEXT NOT NULL,
            UNIQUE(year, url, sha256)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS reviewer_records (
            id INTEGER PRIMARY KEY,
            year INTEGER NOT NULL,
            venue TEXT NOT NULL DEFAULT 'CIDR',
            track TEXT NOT NULL DEFAULT 'research_and_demo',
            role TEXT NOT NULL,
            name TEXT NOT NULL,
            affiliation TEXT NOT NULL,
            name_key TEXT NOT NULL,
            raw_text TEXT NOT NULL,
            source_url TEXT NOT NULL,
            fetched_at TEXT NOT NULL,
            UNIQUE(year, role, name, affiliation, source_url)
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_cidr_year ON reviewer_records(year)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_cidr_role ON reviewer_records(role)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_cidr_name_key ON reviewer_records(name_key)")
    ensure_reviewer_service_schema(conn, venue="CIDR")
    return conn


def save_records(
    conn: sqlite3.Connection,
    records: Iterable[ReviewerRecord],
    *,
    year: int,
    url: str,
    fetched_at: str,
    html: bytes,
    allow_qualifying_loss: bool = False,
) -> int:
    """Replace only the successfully parsed requested year."""
    rows = list(records)
    assert_no_qualifying_person_loss(
        conn, rows, venue="CIDR", year_column="year", year=year,
        allow_loss=allow_qualifying_loss,
    )
    with conn:
        conn.execute(
            "INSERT OR IGNORE INTO source_pages(year, url, fetched_at, sha256) VALUES (?, ?, ?, ?)",
            (year, url, fetched_at, hashlib.sha256(html).hexdigest()),
        )
        conn.execute("DELETE FROM reviewer_records WHERE year = ?", (year,))
        before = conn.total_changes
        prepared = []
        for r in rows:
            role_family, track, qualifies = service_values("CIDR", r)
            prepared.append((
                r.year, r.role, role_family, track, qualifies, r.name,
                r.affiliation, r.name_key, r.raw_text, r.source_url, r.fetched_at,
            ))
        conn.executemany(
            """
            INSERT INTO reviewer_records(
                year, role, role_family, track, qualifies_db_seed, name, affiliation,
                name_key, raw_text, source_url, fetched_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            prepared,
        )
        inserted = conn.total_changes - before
    return inserted

def print_summary(conn: sqlite3.Connection) -> None:
    print("\nDatabase summary:")
    for year, role, n in conn.execute(
        """
        SELECT year, role, COUNT(*)
        FROM reviewer_records
        GROUP BY year, role
        ORDER BY year, role
        """
    ):
        print(f"  {year}: {role}: {n}")
    total = conn.execute("SELECT COUNT(*) FROM reviewer_records").fetchone()[0]
    keys = conn.execute("SELECT COUNT(DISTINCT name_key) FROM reviewer_records").fetchone()[0]
    print(f"\nTotal raw records: {total}")
    print(f"Distinct conservative name keys: {keys}")
    print("Note: name keys are NOT entity-resolved people.")


def parse_source_overrides(values: list[str]) -> dict[int, str]:
    out: dict[int, str] = {}
    for value in values:
        try:
            year_text, url = value.split("=", 1)
            out[int(year_text)] = url
        except ValueError as exc:
            raise SystemExit(f"Invalid --source {value!r}; use YEAR=URL") from exc
    return out


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--years", nargs="+", type=int, default=None,
        help=(
            "Years to collect. Omit for 2022 through the last completed calendar year; "
            "explicit values override the default (for example: --years 2026)."
        ),
    )
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument(
        "--include-chairs", action="store_true",
        help="Deprecated compatibility flag; qualifying chairs are always included.",
    )
    parser.add_argument("--delay", type=float, default=1.0)
    parser.add_argument(
        "--source",
        action="append",
        default=[],
        metavar="YEAR=URL",
        help="Override the official Officers URL for a year; may be repeated.",
    )
    parser.add_argument(
        "--allow-qualifying-loss", action="store_true",
        help="Allow a rerun to remove previously qualifying people for a year.",
    )
    parser.add_argument("--ignore-robots", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    overrides = parse_source_overrides(args.source)
    years = sorted(set(args.years if args.years is not None else default_years()))

    session = requests.Session()
    session.headers.update({"User-Agent": DEFAULT_UA, "Accept": "text/html,application/xhtml+xml"})
    conn = open_db(args.db)
    failed = False
    try:
        for i, year in enumerate(years):
            url = overrides.get(year, OFFICERS_URL.format(year=year))
            print(f"Fetching {url}")
            try:
                html, fetched_at = fetch_page(session, url, ignore_robots=args.ignore_robots)
                records = parse_cidr_officers_page(
                    html,
                    year=year,
                    source_url=url,
                    fetched_at=fetched_at,
                    include_chairs=args.include_chairs,
                )
                inserted = save_records(
                    conn, records, year=year, url=url, fetched_at=fetched_at, html=html,
                    allow_qualifying_loss=args.allow_qualifying_loss,
                )
                print(f"  parsed {len(records)} records; replaced year with {inserted} rows")
            except Exception as exc:
                failed = True
                print(f"  ERROR for {year}: {exc}", file=sys.stderr)

            if i < len(years) - 1 and args.delay > 0:
                time.sleep(args.delay)

        print_summary(conn)
    finally:
        conn.close()
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
