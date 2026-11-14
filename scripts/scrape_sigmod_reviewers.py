#!/usr/bin/env python3
"""Scrape public SIGMOD research service and leadership pages into SQLite.

The script combines the official SIGMOD research-PC page with the conference
officer page for each year. It stores Program Co-Chairs, Associate Editors,
PC Members, General Chairs, and selected technical-track chairs.

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
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup, Tag

from scraper_common import clean_line, normalize_name_key, extract_visible_lines

from service_roles import (
    assert_no_qualifying_person_loss,
    ensure_reviewer_service_schema,
    service_values,
)


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
OFFICERS_URL = "https://{year}.sigmod.org/org_conference_officers.shtml"

# The SIGMOD/PODS site template changed the filenames for the side-track PCs
# after 2023, so try both official forms. These are conference-level technical
# committees and are collected as DB-community service, not publication data.
EXTRA_PC_URLS = {
    "industrial": (
        "https://{year}.sigmod.org/org_sigmod_industry_pc.shtml",
        "https://{year}.sigmod.org/org_industrial_pc.shtml",
    ),
    "demo": (
        "https://{year}.sigmod.org/org_sigmod_demo_pc.shtml",
        "https://{year}.sigmod.org/org_demo_pc.shtml",
    ),
    "pods": (
        "https://{year}.sigmod.org/org_pods_pc.shtml",
    ),
}

# Research-program roles exposed on the official SIGMOD PC pages.
ROLE_PATTERNS = [
    (
        re.compile(r"^Program(?: Committee)?\s+co-?chairs?$", re.I),
        "program_co_chair",
    ),
    (
        re.compile(r"^Program(?: Committee)?\s+chairs?$", re.I),
        "program_chair",
    ),
    (
        re.compile(r"^Associate Editors(?:\s*\((?P<round>[^)]*)\))?$", re.I),
        "associate_editor",
    ),
    (
        re.compile(r"^PC\s+members(?:\s*\((?P<round>[^)]*)\))?$", re.I),
        "pc_member",
    ),
]

# Senior/technical SIGMOD roles on the conference-officer page. Program
# chairs are intentionally read from the research-PC page to avoid duplicate
# service rows for the same role.
OFFICER_ROLE_PATTERNS = [
    (re.compile(r"^SIGMOD General Chairs?:?$", re.I), "general_chair"),
    (re.compile(r"^PODS Program(?:\s+Committee)?\s+(?:Co-?)?Chairs?:?$", re.I), "pods_program_chair"),
    (
        re.compile(r"^(?:SIGMOD\s+)?Industrial(?:\s+Program)?\s+Chairs?:?$", re.I),
        "industrial_track_chair",
    ),
    (
        re.compile(r"^(?:SIGMOD\s+)?Demonstration\s+Chairs?:?$", re.I),
        "demonstration_chair",
    ),
    (
        re.compile(r"^(?:SIGMOD\s+)?Tutorials?\s+Chairs?:?$", re.I),
        "tutorial_chair",
    ),
    (
        re.compile(r"^(?:SIGMOD\s+)?Panels?\s+Chairs?:?$", re.I),
        "panel_chair",
    ),
    (
        re.compile(r"^(?:SIGMOD\s+)?Workshop\s+(?:Co-?)?chairs?:?$", re.I),
        "workshop_chair",
    ),
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


def _split_trailing_parenthesized_affiliation(raw: str) -> tuple[str, str] | None:
    """Split ``Name (Affiliation)`` while allowing nested parentheses.

    PODS affiliation strings can themselves contain parenthesized acronyms,
    for example ``Name (Institution (ABC), City)``. A flat regular
    expression cannot parse that safely, so scan backwards from the final
    parenthesis and find its balanced opening delimiter. Comma-separated
    ``Name, Affiliation (Qualifier)`` rows are deliberately left to the normal
    comma parser below.
    """
    if not raw.endswith(")"):
        return None

    depth = 0
    for index in range(len(raw) - 1, -1, -1):
        char = raw[index]
        if char == ")":
            depth += 1
        elif char == "(":
            depth -= 1
            if depth < 0:
                return None
            if depth == 0:
                name = clean_line(raw[:index]).lstrip("*").strip()
                affiliation = clean_line(raw[index + 1 : -1])
                if "," in name:
                    return None
                if name and affiliation:
                    return name, affiliation
                return None

    return None


def parse_person_line(line: str) -> tuple[str, str, int] | None:
    """Parse a visible line like 'Name, Affiliation'.

    We preserve the original line separately because historical pages contain
    typos and occasional malformed separators.
    """
    raw = clean_line(line)

    # PODS pages (and a few older SIGMOD side-track pages) use
    # ``Name (Affiliation)`` instead of a comma separator.
    parenthesized = _split_trailing_parenthesized_affiliation(raw)
    if parenthesized is not None:
        name, affiliation = parenthesized
        if name and affiliation and len(name) <= 120 and len(name.split()) <= 12:
            return name, affiliation, int(raw.startswith("*"))

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
                current_round = parse_round(m.groupdict().get("round"))
                if role in {"associate_editor", "pc_member"}:
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
    if not any(r.role in {"program_co_chair", "program_chair"} for r in records):
        raise ValueError(f"No SIGMOD research Program Chair(s) parsed from {source_url}")

    return records



def pods_role_and_affiliation(role: str, affiliation: str) -> tuple[str, str]:
    """Read an explicit terminal PODS PC-chair annotation, never infer a title."""
    if role in {"pods_pc_member", "pods_program_chair"}:
        inline_chair = re.fullmatch(r"(.+),\s*PC\s+Chair", affiliation, re.I)
        if inline_chair:
            return "pods_program_chair", clean_line(inline_chair[1])
    return role, affiliation


def parse_sigmod_track_pc_page(
    html: bytes,
    *,
    year: int,
    source_url: str,
    fetched_at: str,
    page_kind: str,
) -> list[ReviewerRecord]:
    """Parse a conference-level Industrial, Demo, or PODS PC page."""
    config = {
        "industrial": ("industrial_pc_member", "industrial_track_chair", ("industrial", "industry")),
        "demo": ("demo_pc_member", "demonstration_chair", ("demo", "demonstration")),
        "pods": ("pods_pc_member", "pods_program_chair", ("pods",)),
    }
    if page_kind not in config:
        raise ValueError(f"Unknown SIGMOD side-PC kind: {page_kind}")
    member_role, chair_role, title_tokens = config[page_kind]
    lines = extract_visible_lines(html)

    start = None
    for i, line in enumerate(lines):
        low = line.casefold()
        if "program committee" in low and any(token in low for token in title_tokens):
            start = i + 1
    if start is None:
        raise ValueError(f"Could not locate {page_kind} Program Committee heading on {source_url}")

    records: list[ReviewerRecord] = []
    seen: set[tuple[str, str, str]] = set()
    current_role = member_role
    for line in lines[start:]:
        low = line.casefold().strip(":")
        if low in {"chair", "chairs", "program committee chair", "program committee chairs", "track chair", "track chairs"}:
            current_role = chair_role
            continue
        if low in {"pc members", "pc member", "program committee members", "program committee member"}:
            current_role = member_role
            continue
        if any(p.search(line) for p in STOP_PATTERNS) or low in {"image", "credits"}:
            if records:
                break
            continue

        parsed = parse_person_line(line)
        if parsed is None:
            continue
        name, affiliation, rapid_response = parsed
        role = current_role
        # Some PODS rosters put the chair annotation inside the affiliation
        # instead of a separate heading, e.g. "Relational AI, Inc., PC Chair".
        # Match only that explicit terminal annotation; leave ordinary chair
        # titles and institution names alone and do not change the next row.
        if page_kind == "pods":
            role, affiliation = pods_role_and_affiliation(role, affiliation)
        key = (role, normalize_name_key(name), normalize_name_key(affiliation))
        if key in seen:
            continue
        seen.add(key)
        records.append(
            ReviewerRecord(
                year=year, role=role, review_round="", name=name,
                affiliation=affiliation, rapid_response_reviewer=rapid_response,
                name_key=normalize_name_key(name), raw_text=line,
                source_url=source_url, fetched_at=fetched_at,
            )
        )

    if not any(r.role == member_role for r in records):
        raise ValueError(f"No {page_kind} PC members parsed from {source_url}")
    return records

def _match_officer_role(heading: str) -> str | None:
    value = clean_line(heading)
    for pattern, role in OFFICER_ROLE_PATTERNS:
        if pattern.match(value):
            return role
    return None


def _plausible_linked_person_name(value: str) -> bool:
    value = clean_line(value).strip("|,;:")
    if not value or len(value) > 120 or "," in value:
        return False
    if not any(ch.isalpha() for ch in value):
        return False
    if not (1 <= len(value.split()) <= 12):
        return False
    lowered = value.casefold()
    blocked = (
        "university", "institute", "research center", "homepage",
        "organization", "sigmod", "pods", "image", "credits", "conference",
    )
    return not any(token in lowered for token in blocked)


def parse_sigmod_officers_page(
    html: bytes,
    year: int,
    source_url: str,
    fetched_at: str,
) -> list[ReviewerRecord]:
    """Parse qualifying senior/technical SIGMOD chairs from officer cards.

    The 2022--2025 SIGMOD sites use heading + table/card sections. Names are
    linked inside an officer cell and the affiliation is plain text in the same
    cell. Walking one heading section at a time avoids accidentally collecting
    administrative officers. PODS program chairs are read here because some
    years list them only on this page, or as ordinary members on the PODS PC page.
    """
    soup = BeautifulSoup(html, "html.parser")
    records: list[ReviewerRecord] = []
    matched_sections: set[str] = set()

    for heading in soup.find_all(re.compile(r"^h[1-6]$")):
        role = _match_officer_role(heading.get_text(" ", strip=True))
        if role is None:
            continue
        matched_sections.add(role)

        seen_cells: set[int] = set()
        for node in heading.next_elements:
            if (
                isinstance(node, Tag)
                and node is not heading
                and re.fullmatch(r"h[1-6]", node.name or "")
            ):
                break
            if not isinstance(node, Tag) or node.name != "a":
                continue

            name = clean_line(node.get_text(" ", strip=True))
            if not _plausible_linked_person_name(name):
                continue

            cell = (
                node.find_parent("td")
                or node.find_parent("li")
                or node.find_parent("p")
            )
            if cell is None:
                continue
            marker = id(cell)
            if marker in seen_cells:
                continue
            seen_cells.add(marker)

            pieces = [clean_line(x) for x in cell.stripped_strings if clean_line(x)]
            affiliation_parts: list[str] = []
            removed_name = False
            for piece in pieces:
                if not removed_name and piece == name:
                    removed_name = True
                    continue
                if piece.casefold() == "image" or piece == "|":
                    continue
                affiliation_parts.append(piece.strip(" |"))
            affiliation = clean_line(" ".join(x for x in affiliation_parts if x))
            raw_text = clean_line(" | ".join(pieces))

            records.append(
                ReviewerRecord(
                    year=year,
                    role=role,
                    review_round="",
                    name=name,
                    affiliation=affiliation,
                    rapid_response_reviewer=0,
                    name_key=normalize_name_key(name),
                    raw_text=raw_text,
                    source_url=source_url,
                    fetched_at=fetched_at,
                )
            )

    if "general_chair" not in matched_sections:
        raise ValueError(f"No SIGMOD General Chair section found on {source_url}")
    if not any(r.role == "general_chair" for r in records):
        raise ValueError(
            f"SIGMOD General Chair section found, but no chairs parsed from {source_url}"
        )

    return records



def merge_sigmod_records(*groups: Iterable[ReviewerRecord]) -> list[ReviewerRecord]:
    """De-duplicate repeated roles, retaining explicit PODS chair evidence."""
    records = [record for group in groups for record in group]
    def pods_identity(record):
        # Officer cards often append a country to the institution in the PC
        # list. Remove only explicit country suffixes, preserving comma-bearing
        # campuses and labs such as University of California, San Diego.
        parts = record.affiliation.rsplit(",", 1)
        country_suffixes = {"chile", "germany", "usa", "united states", "uk", "united kingdom"}
        affiliation = parts[0] if len(parts) == 2 and normalize_name_key(parts[1]) in country_suffixes else record.affiliation
        organization = normalize_name_key(affiliation)
        return (record.year, record.name_key, organization) if organization else None

    pods_chairs = {pods_identity(r) for r in records if r.role == "pods_program_chair"}
    pods_chairs.discard(None)
    out: list[ReviewerRecord] = []
    seen: set[tuple[int, str, str, str, str]] = set()
    for record in records:
        if record.role == "pods_pc_member" and pods_identity(record) in pods_chairs:
            continue
        # A shared display name does not identify a person. Keep differing
        # affiliations as source evidence for the downstream identity review.
        key = (record.year, record.role, record.review_round, record.name_key,
               normalize_name_key(record.affiliation))
        if key in seen:
            continue
        seen.add(key)
        out.append(record)
    return out

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
    ensure_reviewer_service_schema(conn, venue="SIGMOD")
    return conn


def save_records(
    conn: sqlite3.Connection,
    records: Iterable[ReviewerRecord],
    *,
    year: int,
    source_pages: Iterable[tuple[str, str, bytes]],
    allow_qualifying_loss: bool = False,
) -> int:
    """Replace a year only after every required source parsed successfully."""
    rows = list(records)
    pages = list(source_pages)
    assert_no_qualifying_person_loss(
        conn, rows, venue="SIGMOD", year_column="year", year=year,
        allow_loss=allow_qualifying_loss,
    )
    with conn:
        for url, fetched_at, html in pages:
            digest = hashlib.sha256(html).hexdigest()
            conn.execute(
                "INSERT OR IGNORE INTO source_pages(year, url, fetched_at, sha256) VALUES (?, ?, ?, ?)",
                (year, url, fetched_at, digest),
            )
        conn.execute("DELETE FROM reviewer_records WHERE year = ?", (year,))
        before = conn.total_changes
        prepared = []
        for r in rows:
            role_family, track, qualifies = service_values("SIGMOD", r)
            prepared.append((
                r.year, r.role, role_family, track, qualifies, r.review_round,
                r.name, r.affiliation, r.rapid_response_reviewer, r.name_key,
                r.raw_text, r.source_url, r.fetched_at,
            ))
        conn.executemany(
            """
            INSERT INTO reviewer_records(
                year, role, role_family, track, qualifies_db_seed, review_round,
                name, affiliation, rapid_response_reviewer, name_key, raw_text,
                source_url, fetched_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            prepared,
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
        help="Seconds to wait between HTTP requests (default: 1.0)",
    )
    parser.add_argument(
        "--allow-qualifying-loss",
        action="store_true",
        help="Allow a rerun to remove previously qualifying people for a year.",
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
    failed = False
    try:
        for index, year in enumerate(years):
            pc_url = PC_URL.format(year=year)
            officers_url = OFFICERS_URL.format(year=year)
            try:
                source_pages: list[tuple[str, str, bytes]] = []

                print(f"Fetching {pc_url}")
                pc_html, pc_fetched_at = fetch_page(
                    session, pc_url, ignore_robots=args.ignore_robots
                )
                pc_records = parse_sigmod_pc_page(
                    pc_html, year, pc_url, pc_fetched_at
                )
                source_pages.append((pc_url, pc_fetched_at, pc_html))

                if args.delay > 0:
                    time.sleep(args.delay)

                print(f"Fetching {officers_url}")
                officers_html, officers_fetched_at = fetch_page(
                    session, officers_url, ignore_robots=args.ignore_robots
                )
                officer_records = parse_sigmod_officers_page(
                    officers_html, year, officers_url, officers_fetched_at
                )
                source_pages.append((officers_url, officers_fetched_at, officers_html))

                extra_records: list[ReviewerRecord] = []
                for kind, url_templates in EXTRA_PC_URLS.items():
                    errors: list[str] = []
                    parsed_extra = None
                    for template in url_templates:
                        extra_url = template.format(year=year)
                        try:
                            if args.delay > 0:
                                time.sleep(args.delay)
                            print(f"Fetching SIGMOD {year} {kind} PC: {extra_url}")
                            extra_html, extra_fetched_at = fetch_page(
                                session, extra_url, ignore_robots=args.ignore_robots
                            )
                            rows = parse_sigmod_track_pc_page(
                                extra_html, year=year, source_url=extra_url,
                                fetched_at=extra_fetched_at, page_kind=kind,
                            )
                            parsed_extra = (rows, extra_url, extra_fetched_at, extra_html)
                            break
                        except Exception as exc:
                            errors.append(f"{extra_url}: {exc}")
                    if parsed_extra is None:
                        raise RuntimeError(
                            f"No {kind} PC source parsed for SIGMOD {year}: "
                            + " | ".join(errors)
                        )
                    rows, extra_url, extra_fetched_at, extra_html = parsed_extra
                    extra_records.extend(rows)
                    source_pages.append((extra_url, extra_fetched_at, extra_html))

                records = merge_sigmod_records(pc_records, officer_records, extra_records)
                inserted = save_records(
                    conn, records, year=year, source_pages=source_pages,
                    allow_qualifying_loss=args.allow_qualifying_loss,
                )
                print(
                    f"  parsed {len(pc_records)} research-PC records + "
                    f"{len(officer_records)} officer records + "
                    f"{len(extra_records)} technical/PODS PC records; "
                    f"replaced year with {inserted} rows"
                )
            except Exception as exc:
                failed = True
                print(f"  ERROR for {year}: {exc}", file=sys.stderr)

            if index < len(years) - 1 and args.delay > 0:
                time.sleep(args.delay)

        print_summary(conn)
    finally:
        conn.close()

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
