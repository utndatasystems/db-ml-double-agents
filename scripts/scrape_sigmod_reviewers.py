#!/usr/bin/env python3
"""Scrape public SIGMOD research-track reviewing committee pages into SQLite.

The script collects Associate Editors and PC Members from official yearly
SIGMOD conference websites such as:
    https://2026.sigmod.org/org_sigmod_pc.shtml

It intentionally stores *raw records* and does not perform entity resolution.
The same person can therefore appear multiple times across years/rounds or
under slightly different spellings/affiliations. Use the `name_key` only for
candidate generation, not as a final person identifier.

Usage:
    pip install requests beautifulsoup4
    python scripts/scrape_sigmod_reviewers.py --years 2022 2023 2024 2025 2026

Optional polite user-agent:
    export SIGMOD_SCRAPER_UA='my-research-project/1.0 (mailto:me@example.edu)'
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import sqlite3
import sys
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = REPO_ROOT / "data" / "sigmod_reviewers.sqlite"

DEFAULT_START_YEAR = 2022

def default_years() -> list[int]:
    """Years collected when --years is omitted: 2022 through last calendar year."""
    last_complete_year = datetime.now(timezone.utc).year - 1
    if last_complete_year < DEFAULT_START_YEAR:
        return []
    return list(range(DEFAULT_START_YEAR, last_complete_year + 1))

DEFAULT_UA = os.environ.get(
    "SIGMOD_SCRAPER_UA",
    "sigmod-reviewer-research/1.0",
)

PC_URL = "https://{year}.sigmod.org/org_sigmod_pc.shtml"

# These are the reviewing roles exposed on the official SIGMOD PC pages.
ROLE_PATTERNS = [
    (re.compile(r"^Associate Editors(?:\s*\(([^)]*)\))?$", re.I), "associate_editor"),
    (re.compile(r"^PC\s+members(?:\s*\(([^)]*)\))?$", re.I), "pc_member"),
]

# Common footer/navigation markers. They are only considered after we have
# entered a reviewer section.
STOP_PATTERNS = [
    re.compile(r"^Follow us\b", re.I),
    re.compile(r"^Sponsors?\b", re.I),
    re.compile(r"^Hosted by\b", re.I),
    re.compile(r"^Copyright\b", re.I),
    re.compile(r"^Contact\b", re.I),
]

SKIP_PREFIXES = (
    "note:",
    "notes:",
    "program committee co-chairs",
    "program co-chairs",
    "program co-chairs",
    "general chair",
    "general chairs",
)


@dataclass(frozen=True)
class ReviewerRecord:
    year: int
    role: str
    review_round: str
    name: str
    affiliation: str
    rapid_response_reviewer: int
    name_key: str
    raw_text: str
    source_url: str
    fetched_at: str


def clean_line(text: str) -> str:
    text = text.replace("\xa0", " ")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def normalize_name_key(name: str) -> str:
    """Conservative key for blocking/candidate generation, not ER."""
    value = unicodedata.normalize("NFKD", name)
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    value = value.casefold()
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def parse_round(header_value: str | None) -> str:
    if not header_value:
        return ""
    value = clean_line(header_value)
    # Normalize "Round 1 & 2" etc. but preserve the source semantics.
    return value


def looks_like_person_record(line: str) -> bool:
    if not line or "," not in line:
        return False
    lower = line.casefold()
    if lower.startswith(SKIP_PREFIXES):
        return False
    if ":" in line.split(",", 1)[0]:
        return False
    return True


def parse_person_line(line: str) -> tuple[str, str, int] | None:
    """Parse a visible line like 'Name, Affiliation'.

    We preserve the original line separately because historical pages contain
    typos and occasional malformed separators.
    """
    raw = clean_line(line)
    if not looks_like_person_record(raw):
        return None

    # A few recent SIGMOD pages contain the literal artifact ',!,' inside names.
    repaired = raw.replace(",!,", " ")

    name_part, affiliation = repaired.split(",", 1)
    name = clean_line(name_part)
    affiliation = clean_line(affiliation)

    rapid_response = 0
    if name.startswith("*"):
        rapid_response = 1
        name = name.lstrip("*").strip()

    # Basic sanity guards: keep unusual names, reject obvious non-record prose.
    if not name or not affiliation or len(name) > 120:
        return None
    if len(name.split()) > 12:
        return None

    return name, affiliation, rapid_response


def extract_visible_lines(html: bytes) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")

    # Make explicit line breaks survive get_text().
    for br in soup.find_all("br"):
        br.replace_with("\n")

    lines = [clean_line(x) for x in soup.get_text("\n").splitlines()]
    return [x for x in lines if x]


def parse_sigmod_pc_page(html: bytes, year: int, source_url: str, fetched_at: str) -> list[ReviewerRecord]:
    lines = extract_visible_lines(html)

    # Ignore navigation before the actual committee heading.
    start = None
    for i, line in enumerate(lines):
        low = line.casefold()
        if "sigmod program committee" in low and "organization" in low:
            start = i + 1
            break
    if start is None:
        # Fallback for templates that omit the "Organization:" prefix.
        for i, line in enumerate(lines):
            if line.casefold() == "sigmod program committee":
                start = i + 1
                break
    if start is None:
        raise ValueError(f"Could not locate SIGMOD Program Committee heading on {source_url}")

    records: list[ReviewerRecord] = []
    current_role: str | None = None
    current_round = ""
    saw_reviewer_section = False

    for line in lines[start:]:
        matched_header = False
        for pattern, role in ROLE_PATTERNS:
            m = pattern.match(line)
            if m:
                current_role = role
                current_round = parse_round(m.group(1))
                saw_reviewer_section = True
                matched_header = True
                break
        if matched_header:
            continue

        if current_role is None:
            continue

        if any(p.search(line) for p in STOP_PATTERNS):
            break

        parsed = parse_person_line(line)
        if parsed is None:
            # Do not end the section on notes/formatting text; just ignore it.
            continue

        name, affiliation, rapid_response = parsed
        records.append(
            ReviewerRecord(
                year=year,
                role=current_role,
                review_round=current_round,
                name=name,
                affiliation=affiliation,
                rapid_response_reviewer=rapid_response,
                name_key=normalize_name_key(name),
                raw_text=line,
                source_url=source_url,
                fetched_at=fetched_at,
            )
        )

    if not saw_reviewer_section:
        raise ValueError(f"No Associate Editor / PC Member sections found on {source_url}")
    if not records:
        raise ValueError(f"Reviewer sections were found, but no records parsed from {source_url}")

    return records


def robots_allows(session: requests.Session, target_url: str) -> bool:
    """Respect robots.txt when it is available.

    robots.txt is not a legal determination; it is a crawling preference. 404/410
    means no robots file was published at that location. On 401/403 we decline
    to crawl. Other retrieval failures are reported and treated conservatively.
    """
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
        raise RuntimeError(
            f"Could not check robots.txt at {robots_url}: HTTP {response.status_code}"
        )

    rp = RobotFileParser()
    rp.set_url(robots_url)
    rp.parse(response.text.splitlines())
    return rp.can_fetch(DEFAULT_UA, target_url)


def fetch_page(session: requests.Session, url: str, ignore_robots: bool = False) -> tuple[bytes, str]:
    if not ignore_robots and not robots_allows(session, url):
        raise PermissionError(f"robots.txt does not allow this user-agent to fetch {url}")

    response = session.get(url, timeout=30)
    response.raise_for_status()
    fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return response.content, fetched_at


def open_db(path: str) -> sqlite3.Connection:
    db_path = Path(path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
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
            venue TEXT NOT NULL DEFAULT 'SIGMOD',
            role TEXT NOT NULL,
            review_round TEXT NOT NULL DEFAULT '',
            name TEXT NOT NULL,
            affiliation TEXT NOT NULL,
            rapid_response_reviewer INTEGER NOT NULL DEFAULT 0 CHECK (rapid_response_reviewer IN (0,1)),
            name_key TEXT NOT NULL,
            raw_text TEXT NOT NULL,
            source_url TEXT NOT NULL,
            fetched_at TEXT NOT NULL,
            UNIQUE(year, role, review_round, name, affiliation, source_url)
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_reviewers_year ON reviewer_records(year)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_reviewers_name_key ON reviewer_records(name_key)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_reviewers_role ON reviewer_records(role)")
    return conn


def save_records(
    conn: sqlite3.Connection,
    records: Iterable[ReviewerRecord],
    *,
    year: int,
    url: str,
    fetched_at: str,
    html: bytes,
) -> int:
    """Replace only the successfully parsed requested year."""
    rows = list(records)
    digest = hashlib.sha256(html).hexdigest()
    with conn:
        conn.execute(
            "INSERT OR IGNORE INTO source_pages(year, url, fetched_at, sha256) VALUES (?, ?, ?, ?)",
            (year, url, fetched_at, digest),
        )
        conn.execute("DELETE FROM reviewer_records WHERE year = ?", (year,))
        before = conn.total_changes
        conn.executemany(
            """
            INSERT INTO reviewer_records(
                year, role, review_round, name, affiliation,
                rapid_response_reviewer, name_key, raw_text,
                source_url, fetched_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    r.year, r.role, r.review_round, r.name, r.affiliation,
                    r.rapid_response_reviewer, r.name_key, r.raw_text,
                    r.source_url, r.fetched_at,
                )
                for r in rows
            ],
        )
        inserted = conn.total_changes - before
    return inserted

def print_summary(conn: sqlite3.Connection) -> None:
    print("\nDatabase summary:")
    rows = conn.execute(
        """
        SELECT year, role, review_round, COUNT(*) AS n
        FROM reviewer_records
        GROUP BY year, role, review_round
        ORDER BY year, role, review_round
        """
    ).fetchall()
    for year, role, review_round, n in rows:
        round_label = f" [{review_round}]" if review_round else ""
        print(f"  {year}: {role}{round_label}: {n}")

    n_rows = conn.execute("SELECT COUNT(*) FROM reviewer_records").fetchone()[0]
    n_keys = conn.execute("SELECT COUNT(DISTINCT name_key) FROM reviewer_records").fetchone()[0]
    print(f"\nTotal raw reviewer records: {n_rows}")
    print(f"Distinct conservative name keys: {n_keys}")
    print("Note: distinct name keys are NOT entity-resolved people.")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--years",
        nargs="+",
        type=int,
        default=None,
        help=(
            "Years to collect. Omit for 2022 through the last completed calendar year; "
            "explicit values override the default (for example: --years 2026)."
        ),
    )
    parser.add_argument(
        "--db",
        default=str(DEFAULT_DB),
        help=f"SQLite output file (default: {DEFAULT_DB})",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=1.0,
        help="Seconds to wait between yearly page requests (default: 1.0)",
    )
    parser.add_argument(
        "--ignore-robots",
        action="store_true",
        help="Skip robots.txt check. Use only after reviewing the site's policy yourself.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    years = sorted(set(args.years if args.years is not None else default_years()))

    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": DEFAULT_UA,
            "Accept": "text/html,application/xhtml+xml",
        }
    )

    conn = open_db(args.db)
    try:
        for index, year in enumerate(years):
            url = PC_URL.format(year=year)
            print(f"Fetching {url}")
            try:
                html, fetched_at = fetch_page(session, url, ignore_robots=args.ignore_robots)
                records = parse_sigmod_pc_page(html, year, url, fetched_at)
                inserted = save_records(
                    conn, records, year=year, url=url, fetched_at=fetched_at, html=html
                )
                print(f"  parsed {len(records)} records; replaced year with {inserted} rows")
            except Exception as exc:
                print(f"  ERROR for {year}: {exc}", file=sys.stderr)

            if index < len(years) - 1 and args.delay > 0:
                time.sleep(args.delay)

        print_summary(conn)
    finally:
        conn.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
