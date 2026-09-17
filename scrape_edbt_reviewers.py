#!/usr/bin/env python3
"""Scrape public EDBT research program-committee records into SQLite.

EDBT is slightly different from CIDR/ICDE: for many editions the most stable
public source for the *research* PC is the official OpenProceedings front matter
PDF. Those PDFs label roles such as:

    Research Program Committee Chair
    Senior Program Committee Members
    Program Committee Members

This script first checks the official OpenProceedings year index for a
"Front matter" link. If no such link is exposed, it tries the historical
canonical URL pattern:

    https://openproceedings.org/{year}/conf/edbt/frontmatter.pdf

You can always override the PDF URL with --frontmatter YEAR=URL.

By default the script stores Senior PC members and ordinary PC members. Use
--include-chairs to include the Research Program Committee Chair(s).

Important: the output contains raw reviewer observations, not entity-resolved
people. `name_key` is only a conservative blocking key for later ER.

Install:
    pip install requests beautifulsoup4 pypdf

Examples:
    python scrape_edbt_reviewers.py --years 2019 2020 2021
    python scrape_edbt_reviewers.py --years 2021 --include-chairs
    python scrape_edbt_reviewers.py --years 2025 \
        --frontmatter 2025=https://official.example/frontmatter.pdf

Optional polite user-agent:
    export EDBT_SCRAPER_UA='my-research-project/1.0 (mailto:me@example.edu)'
"""

from __future__ import annotations

import argparse
import hashlib
import io
import os
import re
import sqlite3
import sys
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup
from pypdf import PdfReader


DEFAULT_UA = os.environ.get("EDBT_SCRAPER_UA", "edbt-reviewer-research/1.0")
INDEX_URL = "https://openproceedings.org/html/pages/{year}_edbt.html"
CANONICAL_FRONTMATTER_URL = "https://openproceedings.org/{year}/conf/edbt/frontmatter.pdf"

ROLE_PATTERNS = [
    (re.compile(r"^(?:Research\s+)?Program\s+Committee\s+Chairs?$", re.I), "program_committee_chair"),
    (re.compile(r"^Senior\s+Program\s+Committee(?:\s+Members?)?$", re.I), "senior_program_committee_member"),
    (re.compile(r"^(?:Research\s+)?Program\s+Committee\s+Members?$", re.I), "program_committee_member"),
]

STOP_PATTERNS = [
    re.compile(r"^(?:Industrial(?:\s*&\s*Applications?)?|Industry(?:\s*&\s*Applications?)?)\s+Track\s+Program\s+Committee", re.I),
    re.compile(r"^Demonstration\s+Track\s+Program\s+Committee", re.I),
    re.compile(r"^Demo(?:nstration)?\s+Track\s+Program\s+Committee", re.I),
    re.compile(r"^Tutorial\s+Track\s+Program\s+Committee", re.I),
    re.compile(r"^Short\s+Paper\s+Program\s+Committee", re.I),
    re.compile(r"^Conference\s+Organization", re.I),
]


@dataclass(frozen=True)
class ReviewerRecord:
    year: int
    role: str
    name: str
    affiliation: str
    name_key: str
    raw_text: str
    source_page: int
    source_url: str
    fetched_at: str


def clean_line(text: str) -> str:
    text = text.replace("\xa0", " ").replace("\u200b", "")
    text = text.replace("\u00ad", "")  # soft hyphen
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def normalize_name_key(name: str) -> str:
    value = unicodedata.normalize("NFKD", name)
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    value = value.casefold()
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def role_for_line(line: str) -> str | None:
    value = clean_line(line).rstrip(":")
    for pattern, role in ROLE_PATTERNS:
        if pattern.fullmatch(value):
            return role
    return None


def is_stop_heading(line: str) -> bool:
    value = clean_line(line).rstrip(":")
    return any(pattern.match(value) for pattern in STOP_PATTERNS)


def plausible_name(value: str) -> bool:
    value = clean_line(value).strip("-–—,;:\"'“”")
    if not value or len(value) > 150 or not any(ch.isalpha() for ch in value):
        return False
    words = value.split()
    if not 1 <= len(words) <= 16:
        return False
    blocked = (
        "program committee",
        "conference organization",
        "industrial track",
        "demonstration track",
        "research track",
    )
    low = value.casefold()
    return not any(token in low for token in blocked)


def parse_person_line(line: str) -> tuple[str, str] | None:
    """Parse front-matter rows, normally `Name, Affiliation, Country`."""
    raw = clean_line(line)
    raw = re.sub(r"^[•▪●○\-*]+\s*", "", raw)
    if "," not in raw:
        return None
    name, affiliation = raw.split(",", 1)
    name = clean_line(name).strip(" \t\"'“”")
    affiliation = clean_line(affiliation).strip(" \t\"'“”")
    if plausible_name(name) and affiliation:
        return name, affiliation
    return None


def extract_pdf_pages(pdf_bytes: bytes) -> list[list[str]]:
    reader = PdfReader(io.BytesIO(pdf_bytes))
    pages: list[list[str]] = []
    for page in reader.pages:
        try:
            text = page.extract_text(extraction_mode="layout") or ""
        except TypeError:
            # Compatibility with older pypdf releases.
            text = page.extract_text() or ""
        lines = [clean_line(line) for line in text.splitlines()]
        pages.append([line for line in lines if line])
    return pages


def parse_edbt_frontmatter(
    pdf_bytes: bytes,
    *,
    year: int,
    source_url: str,
    fetched_at: str,
    include_chairs: bool = False,
) -> list[ReviewerRecord]:
    pages = extract_pdf_pages(pdf_bytes)
    records: list[ReviewerRecord] = []
    current_role: str | None = None
    entered_research_pc = False
    seen: set[tuple[str, str, str]] = set()

    for page_number, lines in enumerate(pages, start=1):
        for line in lines:
            role = role_for_line(line)
            if role:
                # Avoid the table-of-contents occurrence of a generic heading:
                # we only enter once a specific research-PC role heading appears.
                current_role = role
                entered_research_pc = True
                continue

            if entered_research_pc and is_stop_heading(line):
                # Front matter usually puts other tracks immediately after the
                # research PC. We deliberately stop so they are not mixed in.
                return records

            if not entered_research_pc or current_role is None:
                continue

            parsed = parse_person_line(line)
            if parsed is None:
                continue
            name, affiliation = parsed

            if current_role == "program_committee_chair" and not include_chairs:
                continue

            key = (current_role, name, affiliation)
            if key in seen:
                continue
            seen.add(key)
            records.append(
                ReviewerRecord(
                    year=year,
                    role=current_role,
                    name=name,
                    affiliation=affiliation,
                    name_key=normalize_name_key(name),
                    raw_text=line,
                    source_page=page_number,
                    source_url=source_url,
                    fetched_at=fetched_at,
                )
            )

    if not entered_research_pc:
        raise ValueError(
            "Could not find Research/Senior/Program Committee headings in the EDBT front matter"
        )
    if not any(r.role in {"senior_program_committee_member", "program_committee_member"} for r in records):
        raise ValueError("Research PC headings found, but no reviewer records were parsed")
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


def fetch_bytes(
    session: requests.Session,
    url: str,
    *,
    ignore_robots: bool = False,
    accept: str = "*/*",
) -> tuple[bytes, str, str]:
    if not ignore_robots and not robots_allows(session, url):
        raise PermissionError(f"robots.txt does not allow this user-agent to fetch {url}")
    response = session.get(url, timeout=45, headers={"Accept": accept})
    response.raise_for_status()
    fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return response.content, fetched_at, response.headers.get("Content-Type", "")


def discover_frontmatter_url(index_html: bytes, index_url: str) -> str | None:
    soup = BeautifulSoup(index_html, "html.parser")
    candidates: list[str] = []
    for a in soup.find_all("a", href=True):
        text = clean_line(a.get_text(" ", strip=True)).casefold()
        href = str(a.get("href"))
        href_low = href.casefold()
        if "front matter" in text or "frontmatter" in href_low:
            candidates.append(urljoin(index_url, href))
    # Prefer PDFs if several links are present.
    candidates.sort(key=lambda u: (not u.casefold().endswith(".pdf"), u))
    return candidates[0] if candidates else None


def resolve_frontmatter_url(
    session: requests.Session,
    *,
    year: int,
    override: str | None,
    ignore_robots: bool,
) -> tuple[str, bytes | None, str | None, str | None]:
    """Return (pdf_url, index_html, index_fetched_at, index_url).

    The caller records the index as provenance when it could be retrieved.
    """
    if override:
        return override, None, None, None

    index_url = INDEX_URL.format(year=year)
    index_html: bytes | None = None
    index_fetched_at: str | None = None
    try:
        index_html, index_fetched_at, _ = fetch_bytes(
            session,
            index_url,
            ignore_robots=ignore_robots,
            accept="text/html,application/xhtml+xml",
        )
        discovered = discover_frontmatter_url(index_html, index_url)
        if discovered:
            return discovered, index_html, index_fetched_at, index_url
    except Exception as exc:
        print(f"  warning: could not inspect OpenProceedings index: {exc}", file=sys.stderr)

    return (
        CANONICAL_FRONTMATTER_URL.format(year=year),
        index_html,
        index_fetched_at,
        index_url if index_html is not None else None,
    )


def open_db(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS source_documents (
            id INTEGER PRIMARY KEY,
            year INTEGER NOT NULL,
            url TEXT NOT NULL,
            content_type TEXT NOT NULL DEFAULT '',
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
            venue TEXT NOT NULL DEFAULT 'EDBT',
            track TEXT NOT NULL DEFAULT 'research',
            role TEXT NOT NULL,
            name TEXT NOT NULL,
            affiliation TEXT NOT NULL,
            name_key TEXT NOT NULL,
            raw_text TEXT NOT NULL,
            source_page INTEGER,
            source_url TEXT NOT NULL,
            fetched_at TEXT NOT NULL,
            UNIQUE(year, role, name, affiliation, source_url)
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_edbt_year ON reviewer_records(year)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_edbt_role ON reviewer_records(role)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_edbt_name_key ON reviewer_records(name_key)")
    return conn


def save_source(
    conn: sqlite3.Connection,
    *,
    year: int,
    url: str,
    content: bytes,
    fetched_at: str,
    content_type: str,
) -> None:
    conn.execute(
        """
        INSERT OR IGNORE INTO source_documents(year, url, content_type, fetched_at, sha256)
        VALUES (?, ?, ?, ?, ?)
        """,
        (year, url, content_type, fetched_at, hashlib.sha256(content).hexdigest()),
    )


def save_records(conn: sqlite3.Connection, records: Iterable[ReviewerRecord]) -> int:
    before = conn.total_changes
    conn.executemany(
        """
        INSERT OR IGNORE INTO reviewer_records(
            year, role, name, affiliation, name_key, raw_text,
            source_page, source_url, fetched_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                r.year, r.role, r.name, r.affiliation, r.name_key, r.raw_text,
                r.source_page, r.source_url, r.fetched_at,
            )
            for r in records
        ],
    )
    conn.commit()
    return conn.total_changes - before


def print_summary(conn: sqlite3.Connection) -> None:
    print("\nDatabase summary:")
    for year, role, n in conn.execute(
        "SELECT year, role, COUNT(*) FROM reviewer_records GROUP BY year, role ORDER BY year, role"
    ):
        print(f"  {year}: {role}: {n}")
    total = conn.execute("SELECT COUNT(*) FROM reviewer_records").fetchone()[0]
    keys = conn.execute("SELECT COUNT(DISTINCT name_key) FROM reviewer_records").fetchone()[0]
    print(f"\nTotal raw records: {total}")
    print(f"Distinct conservative name keys: {keys}")
    print("Note: name keys are NOT entity-resolved people.")


def parse_overrides(values: list[str]) -> dict[int, str]:
    result: dict[int, str] = {}
    for value in values:
        try:
            year_text, url = value.split("=", 1)
            result[int(year_text)] = url
        except ValueError as exc:
            raise SystemExit(f"Invalid --frontmatter {value!r}; use YEAR=URL") from exc
    return result


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--years", nargs="+", type=int, default=[2021])
    parser.add_argument("--db", default="edbt_reviewers.sqlite")
    parser.add_argument("--include-chairs", action="store_true")
    parser.add_argument("--delay", type=float, default=1.0)
    parser.add_argument(
        "--frontmatter",
        action="append",
        default=[],
        metavar="YEAR=URL",
        help="Override the official front-matter PDF URL for a year; may be repeated.",
    )
    parser.add_argument("--ignore-robots", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    overrides = parse_overrides(args.frontmatter)
    years = sorted(set(args.years))

    session = requests.Session()
    session.headers.update({"User-Agent": DEFAULT_UA})
    conn = open_db(args.db)
    try:
        for i, year in enumerate(years):
            try:
                pdf_url, index_html, index_fetched_at, index_url = resolve_frontmatter_url(
                    session,
                    year=year,
                    override=overrides.get(year),
                    ignore_robots=args.ignore_robots,
                )
                if index_html is not None and index_fetched_at and index_url:
                    save_source(
                        conn,
                        year=year,
                        url=index_url,
                        content=index_html,
                        fetched_at=index_fetched_at,
                        content_type="text/html",
                    )

                print(f"Fetching {pdf_url}")
                pdf_bytes, fetched_at, content_type = fetch_bytes(
                    session,
                    pdf_url,
                    ignore_robots=args.ignore_robots,
                    accept="application/pdf",
                )
                if not pdf_bytes.startswith(b"%PDF"):
                    raise ValueError(
                        f"Expected a PDF at {pdf_url}, got Content-Type={content_type!r}. "
                        "If this edition uses another official URL, pass --frontmatter YEAR=URL."
                    )

                records = parse_edbt_frontmatter(
                    pdf_bytes,
                    year=year,
                    source_url=pdf_url,
                    fetched_at=fetched_at,
                    include_chairs=args.include_chairs,
                )
                save_source(
                    conn,
                    year=year,
                    url=pdf_url,
                    content=pdf_bytes,
                    fetched_at=fetched_at,
                    content_type=content_type or "application/pdf",
                )
                inserted = save_records(conn, records)
                print(f"  parsed {len(records)} records; inserted {inserted} new rows")
            except Exception as exc:
                print(f"  ERROR for {year}: {exc}", file=sys.stderr)

            if i < len(years) - 1 and args.delay > 0:
                time.sleep(args.delay)

        print_summary(conn)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
