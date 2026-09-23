#!/usr/bin/env python3
"""Scrape public PVLDB/VLDB review-board pages into SQLite.

The script targets the official Proceedings of the VLDB Endowment (PVLDB)
review-board pages, for example:

    https://www.vldb.org/pvldb/volumes/19/review-board

PVLDB volume 19 corresponds to the VLDB 2026 conference/review cycle
2025/2026; volume 18 to VLDB 2025; volume 17 to VLDB 2024, etc.  The mapping
used here is:

    pvldb_volume = conference_year - 2007

By default the script stores the two roles most useful for reviewer analysis:
Associate Editors and Members of the Review Board.  Editors in Chief can be
included with --include-eic.

The output intentionally contains *raw records*, not entity-resolved people.
The same person may therefore appear in multiple volumes or under slightly
different names/affiliations.  `name_key` is only a conservative blocking key
for later entity resolution.

Install:
    pip install requests beautifulsoup4

Examples:
    python scripts/scrape_vldb_reviewers.py --years 2022 2023 2024 2025 2026
    python scripts/scrape_vldb_reviewers.py --years 2026 --db vldb_reviewers.sqlite
    python scripts/scrape_vldb_reviewers.py --years 2025 2026 --include-eic

Optional polite user-agent:
    export VLDB_SCRAPER_UA='my-research-project/1.0 (mailto:me@example.edu)'
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
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = REPO_ROOT / "data" / "vldb_reviewers.sqlite"

DEFAULT_START_YEAR = 2022

def default_years() -> list[int]:
    """Years collected when --years is omitted: 2022 through last calendar year."""
    last_complete_year = datetime.now(timezone.utc).year - 1
    if last_complete_year < DEFAULT_START_YEAR:
        return []
    return list(range(DEFAULT_START_YEAR, last_complete_year + 1))

DEFAULT_UA = os.environ.get(
    "VLDB_SCRAPER_UA",
    "vldb-reviewer-research/1.0",
)

REVIEW_BOARD_URL = "https://www.vldb.org/pvldb/volumes/{volume}/review-board"

PAGE_HEADER_RE = re.compile(
    r"^PVLDB\s+Review\s+Board\s+for\s+Volume\s+(\d+)\s*\(([^)]*)\)\s*$",
    re.I,
)

ROLE_PATTERNS = [
    (re.compile(r"^Editors?\s+in\s+Chief$", re.I), "editor_in_chief"),
    (re.compile(r"^Associate\s+Editors?$", re.I), "associate_editor"),
    (
        re.compile(
            r"^(?:Members?\s+of\s+the\s+Review\s+Board|Review\s+Board)$", re.I
        ),
        "review_board_member",
    ),
]

# Headings that may follow the review-board listing on older templates.
STOP_PATTERNS = [
    re.compile(r"^Publication\s+Editors?$", re.I),
    re.compile(r"^PVLDB\s+Managing\s+Editor$", re.I),
    re.compile(r"^PVLDB\s+Advisory", re.I),
    re.compile(r"^Contact$", re.I),
    re.compile(r"^Organization$", re.I),
    re.compile(r"^Publication\s+Policies$", re.I),
    re.compile(r"^Rights\s*&\s*Responsibilities$", re.I),
]


@dataclass(frozen=True)
class ReviewerRecord:
    conference_year: int
    pvldb_volume: int
    review_cycle: str
    role: str
    name: str
    affiliation: str
    name_key: str
    raw_text: str
    source_url: str
    fetched_at: str


def clean_line(text: str) -> str:
    text = text.replace("\xa0", " ")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def normalize_name_key(name: str) -> str:
    """Conservative key for candidate generation, not final identity."""
    value = unicodedata.normalize("NFKD", name)
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    value = value.casefold()
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def volume_for_year(conference_year: int) -> int:
    volume = conference_year - 2007
    if volume <= 0:
        raise ValueError(
            f"Conference year {conference_year} does not map to a valid PVLDB volume"
        )
    return volume


def plausible_person_name(value: str) -> bool:
    value = clean_line(value).strip("-–—,;:")
    if not value or len(value) > 140:
        return False
    if "(" in value or ")" in value:
        return False
    if not any(ch.isalpha() for ch in value):
        return False
    words = value.split()
    if not (1 <= len(words) <= 14):
        return False
    lowered = value.casefold()
    blocked = (
        "review board",
        "associate editor",
        "editors in chief",
        "proceedings of the vldb",
        "volume ",
    )
    return not any(token in lowered for token in blocked)


def split_trailing_parenthetical(raw: str) -> tuple[str, str] | None:
    """Split `Name (Affiliation)` while allowing nested parentheses in affiliation."""
    if not raw.endswith(")"):
        return None

    depth = 0
    opening = None
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
    """Parse recent PVLDB person rows.

    Common forms are:
        Name (Affiliation)
        Name(Affiliation)
        Name, Affiliation

    The comma form is handled first only when the text before the comma looks
    like a person name.  This avoids breaking affiliations containing commas.
    """
    raw = clean_line(line)
    if not raw:
        return None

    # Occasional page rows use a comma rather than an outer affiliation pair,
    # e.g. "Alvin Cheung, University of California (Berkeley)".
    if "," in raw:
        left, right = raw.split(",", 1)
        left = clean_line(left)
        right = clean_line(right)
        if plausible_person_name(left) and right:
            return left, right

    parsed = split_trailing_parenthetical(raw)
    if parsed is not None:
        return parsed

    # Older VLDB/PVLDB pages sometimes use "Name - Affiliation".
    for sep in (" - ", " – ", " — "):
        if sep in raw:
            left, right = raw.split(sep, 1)
            left = clean_line(left)
            right = clean_line(right)
            if plausible_person_name(left) and right:
                return left, right

    return None


def extract_visible_lines(html: bytes) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")

    for br in soup.find_all("br"):
        br.replace_with("\n")

    lines = [clean_line(x) for x in soup.get_text("\n").splitlines()]
    return [x for x in lines if x]


def parse_vldb_review_board_page(
    html: bytes,
    *,
    conference_year: int,
    expected_volume: int,
    source_url: str,
    fetched_at: str,
    include_eic: bool = False,
) -> list[ReviewerRecord]:
    lines = extract_visible_lines(html)

    start = None
    actual_volume = None
    review_cycle = ""
    for i, line in enumerate(lines):
        match = PAGE_HEADER_RE.match(line)
        if match:
            start = i + 1
            actual_volume = int(match.group(1))
            review_cycle = clean_line(match.group(2))
            break

    if start is None:
        raise ValueError(
            f"Could not locate PVLDB Review Board heading on {source_url}"
        )
    if actual_volume != expected_volume:
        raise ValueError(
            f"Expected PVLDB volume {expected_volume}, but page says volume {actual_volume}: {source_url}"
        )

    records: list[ReviewerRecord] = []
    current_role: str | None = None
    saw_review_role = False

    for line in lines[start:]:
        matched_role = False
        for pattern, role in ROLE_PATTERNS:
            if pattern.match(line):
                current_role = role
                if role in {"associate_editor", "review_board_member"}:
                    saw_review_role = True
                matched_role = True
                break
        if matched_role:
            continue

        if current_role is None:
            continue

        if any(pattern.match(line) for pattern in STOP_PATTERNS):
            break

        # Some historical pages render two people in one textual row separated
        # by a vertical bar.  Treat each side as its own source fragment.
        fragments = [
            clean_line(part)
            for part in re.split(r"\s+\|\s+", line)
            if clean_line(part)
        ]
        for fragment in fragments:
            parsed = parse_person_line(fragment)
            if parsed is None:
                continue

            name, affiliation = parsed
            if current_role == "editor_in_chief" and not include_eic:
                continue

            records.append(
                ReviewerRecord(
                    conference_year=conference_year,
                    pvldb_volume=expected_volume,
                    review_cycle=review_cycle,
                    role=current_role,
                    name=name,
                    affiliation=affiliation,
                    name_key=normalize_name_key(name),
                    raw_text=fragment,
                    source_url=source_url,
                    fetched_at=fetched_at,
                )
            )

    if not saw_review_role:
        raise ValueError(
            f"No Associate Editor / Review Board sections found on {source_url}"
        )
    if not records:
        raise ValueError(
            f"Review-board sections were found, but no records parsed from {source_url}"
        )

    return records


def robots_allows(session: requests.Session, target_url: str) -> bool:
    """Respect robots.txt when available.

    robots.txt is a crawling preference, not a legal determination.  A missing
    robots.txt (404/410) is treated as no published robots restriction.  We
    decline on 401/403 and fail conservatively on other unexpected responses.
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

    parser = RobotFileParser()
    parser.set_url(robots_url)
    parser.parse(response.text.splitlines())
    return parser.can_fetch(DEFAULT_UA, target_url)


def fetch_page(
    session: requests.Session,
    url: str,
    *,
    ignore_robots: bool = False,
) -> tuple[bytes, str]:
    if not ignore_robots and not robots_allows(session, url):
        raise PermissionError(
            f"robots.txt does not allow this user-agent to fetch {url}"
        )

    response = session.get(url, timeout=30)
    response.raise_for_status()
    fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return response.content, fetched_at


def open_db(path: str) -> sqlite3.Connection:
    db_path = Path(path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS source_pages (
            id INTEGER PRIMARY KEY,
            conference_year INTEGER NOT NULL,
            pvldb_volume INTEGER NOT NULL,
            url TEXT NOT NULL,
            fetched_at TEXT NOT NULL,
            sha256 TEXT NOT NULL,
            UNIQUE(conference_year, pvldb_volume, url, sha256)
        )
        """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS reviewer_records (
            id INTEGER PRIMARY KEY,
            conference_year INTEGER NOT NULL,
            pvldb_volume INTEGER NOT NULL,
            review_cycle TEXT NOT NULL DEFAULT '',
            venue TEXT NOT NULL DEFAULT 'VLDB',
            publication TEXT NOT NULL DEFAULT 'PVLDB',
            role TEXT NOT NULL,
            name TEXT NOT NULL,
            affiliation TEXT NOT NULL,
            name_key TEXT NOT NULL,
            raw_text TEXT NOT NULL,
            source_url TEXT NOT NULL,
            fetched_at TEXT NOT NULL,
            UNIQUE(conference_year, pvldb_volume, role, name, affiliation, source_url)
        )
        """)

    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_vldb_reviewers_year ON reviewer_records(conference_year)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_vldb_reviewers_volume ON reviewer_records(pvldb_volume)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_vldb_reviewers_name_key ON reviewer_records(name_key)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_vldb_reviewers_role ON reviewer_records(role)"
    )
    return conn


def save_records(
    conn: sqlite3.Connection,
    records: Iterable[ReviewerRecord],
    *,
    conference_year: int,
    pvldb_volume: int,
    url: str,
    fetched_at: str,
    html: bytes,
) -> int:
    digest = hashlib.sha256(html).hexdigest()
    conn.execute(
        """
        INSERT OR IGNORE INTO source_pages(
            conference_year, pvldb_volume, url, fetched_at, sha256
        ) VALUES (?, ?, ?, ?, ?)
        """,
        (conference_year, pvldb_volume, url, fetched_at, digest),
    )

    rows = list(records)
    with conn:
        # One official review-board source is used per conference year. Replace
        # a year's old rows only after the new page parsed successfully, so a
        # parser/source fix cannot leave stale partial rows behind.
        conn.execute(
            "DELETE FROM reviewer_records WHERE conference_year = ?",
            (conference_year,),
        )
        before = conn.total_changes
        conn.executemany(
            """
            INSERT INTO reviewer_records(
                conference_year, pvldb_volume, review_cycle, role,
                name, affiliation, name_key, raw_text, source_url, fetched_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    r.conference_year,
                    r.pvldb_volume,
                    r.review_cycle,
                    r.role,
                    r.name,
                    r.affiliation,
                    r.name_key,
                    r.raw_text,
                    r.source_url,
                    r.fetched_at,
                )
                for r in rows
            ],
        )
        inserted = conn.total_changes - before
    return inserted


def print_summary(conn: sqlite3.Connection) -> None:
    print("\nDatabase summary:")
    rows = conn.execute("""
        SELECT conference_year, pvldb_volume, review_cycle, role, COUNT(*) AS n
        FROM reviewer_records
        GROUP BY conference_year, pvldb_volume, review_cycle, role
        ORDER BY conference_year, role
        """).fetchall()

    for year, volume, cycle, role, count in rows:
        cycle_label = f" [{cycle}]" if cycle else ""
        print(
            f"  VLDB {year} / PVLDB vol. {volume}{cycle_label}: {role}: {count}"
        )

    total = conn.execute("SELECT COUNT(*) FROM reviewer_records").fetchone()[0]
    distinct_keys = conn.execute(
        "SELECT COUNT(DISTINCT name_key) FROM reviewer_records"
    ).fetchone()[0]
    print(f"\nTotal raw reviewer records: {total}")
    print(f"Distinct conservative name keys: {distinct_keys}")
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
        help="Seconds to wait between review-board requests (default: 1.0)",
    )
    parser.add_argument(
        "--include-eic",
        action="store_true",
        help="Also store Editors in Chief. By default only AEs and Review Board members are stored.",
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
        for index, conference_year in enumerate(years):
            volume = volume_for_year(conference_year)
            url = REVIEW_BOARD_URL.format(volume=volume)
            print(
                f"Fetching VLDB {conference_year} / PVLDB volume {volume}: {url}"
            )

            try:
                html, fetched_at = fetch_page(
                    session,
                    url,
                    ignore_robots=args.ignore_robots,
                )
                records = parse_vldb_review_board_page(
                    html,
                    conference_year=conference_year,
                    expected_volume=volume,
                    source_url=url,
                    fetched_at=fetched_at,
                    include_eic=args.include_eic,
                )
                inserted = save_records(
                    conn,
                    records,
                    conference_year=conference_year,
                    pvldb_volume=volume,
                    url=url,
                    fetched_at=fetched_at,
                    html=html,
                )
                print(
                    f"  parsed {len(records)} records; replaced year with {inserted} rows"
                )
            except Exception as exc:
                print(
                    f"  ERROR for VLDB {conference_year}: {exc}",
                    file=sys.stderr,
                )

            if index < len(years) - 1 and args.delay > 0:
                time.sleep(args.delay)

        print_summary(conn)
    finally:
        conn.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
