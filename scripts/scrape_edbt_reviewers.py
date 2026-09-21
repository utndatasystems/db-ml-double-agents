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

The parser is layout-aware: it splits visual PDF columns before whitespace
normalization and replaces a year's old rows only after a successful parse.

Install:
    pip install requests beautifulsoup4 pypdf

Examples:
    python scripts/scrape_edbt_reviewers.py --years 2019 2020 2021
    python scripts/scrape_edbt_reviewers.py --years 2021 --include-chairs
    python scripts/scrape_edbt_reviewers.py --years 2025 \
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
from pathlib import Path
from typing import Iterable
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup
from pypdf import PdfReader

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = REPO_ROOT / "data" / "edbt_reviewers.sqlite"

DEFAULT_UA = os.environ.get("EDBT_SCRAPER_UA", "edbt-reviewer-research/1.0")
INDEX_URL = "https://openproceedings.org/html/pages/{year}_edbt.html"
CANONICAL_FRONTMATTER_URL = (
    "https://openproceedings.org/{year}/conf/edbt/frontmatter.pdf"
)

# The front-matter layout changes between years.  In particular:
#   * 2019 uses one-column "Name, Affiliation" rows.
#   * 2020 uses two columns and "Name (Affiliation)" rows.
#   * 2021 uses two columns and "Name, Affiliation" rows.
#
# We enter the research committee only from a specific "Research Program
# Committee..." heading.  A generic "Program Committee Members" heading is not
# allowed to start parsing because that text also appears in tables of contents.
RESEARCH_PC_START_RE = re.compile(
    r"^Research\s+Program\s+Committee(?:\s+Chairs?)?$",
    re.I,
)
SENIOR_PC_RE = re.compile(
    r"^Senior\s+Program\s+Committee(?:\s+Members?)?$",
    re.I,
)
ORDINARY_PC_RE = re.compile(
    r"^(?:Research\s+)?Program\s+Committee\s+Members?$",
    re.I,
)

# These are searched rather than matched at position zero.  Some PDFs glue a
# roman page number to the following heading, e.g.
# "iiIndustrial/Applications Program Committee".
STOP_PATTERNS = [
    re.compile(
        r"Industr(?:ial|y)"
        r"(?:\s*/\s*Applications?|\s*&\s*Applications?|\s+and\s+Applications?)?"
        r"\s+(?:Track\s+)?Program\s+Committee\b",
        re.I,
    ),
    re.compile(
        r"Demo(?:nstration)?\s+(?:Track\s+)?Program\s+Committee\b", re.I
    ),
    re.compile(r"Tutorial\s+(?:Track\s+)?Program\s+Committee\b", re.I),
    re.compile(r"Short\s+Paper\s+(?:Track\s+)?Program\s+Committee\b", re.I),
    re.compile(r"Additional\s+Reviewers\b", re.I),
    re.compile(r"Conference\s+Organization\b", re.I),
    re.compile(r"Test-of-Time\s+Award\b", re.I),
]

# These counts come directly from the official OpenProceedings front matter and
# are useful as regression guards against silent PDF-layout parsing failures.
# Chairs are intentionally not included here.
KNOWN_RESEARCH_PC_COUNTS = {
    2019: {
        "program_committee_member": 83,
    },
    2020: {
        "senior_program_committee_member": 26,
        "program_committee_member": 54,
    },
    2021: {
        "senior_program_committee_member": 13,
        "program_committee_member": 118,
    },
}

# Known text-extraction anomalies in the official front matter.  Keep these
# corrections narrow and explicit rather than guessing where an omitted comma
# should have been.  The raw source text is still preserved in `raw_text`.
KNOWN_PERSON_LINE_FIXES = {
    # EDBT 2019, Research Program Committee: the PDF text layer omits the
    # comma between the reviewer's name and affiliation.
    "Mohammad Sadoghi UC Davis": ("Mohammad Sadoghi", "UC Davis"),
}


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


def is_research_pc_start(line: str) -> bool:
    value = clean_line(line).rstrip(":")
    return bool(RESEARCH_PC_START_RE.fullmatch(value))


def role_for_line(line: str, *, entered_research_pc: bool) -> str | None:
    """Return a role heading, but never enter from a generic TOC heading."""
    value = clean_line(line).rstrip(":")

    # A specific research-PC heading is allowed to start the section.
    if RESEARCH_PC_START_RE.fullmatch(value):
        if re.search(r"\bChairs?\b", value, re.I):
            return "program_committee_chair"
        # 2019 uses "Research Program Committee" with no separate member
        # heading.  2020 uses the same heading before a single chair row and
        # then switches to Senior/Program Committee Members.
        return "program_committee_member"

    if not entered_research_pc:
        return None

    if SENIOR_PC_RE.fullmatch(value):
        return "senior_program_committee_member"

    if ORDINARY_PC_RE.fullmatch(value):
        return "program_committee_member"

    return None


def is_stop_heading(line: str) -> bool:
    value = clean_line(line).rstrip(":")
    return any(pattern.search(value) for pattern in STOP_PATTERNS)


def plausible_name(value: str) -> bool:
    value = clean_line(value).strip("-–—,;:\"'“”")
    if not value or len(value) > 100 or not any(ch.isalpha() for ch in value):
        return False

    words = value.split()
    # Research-PC lists in these front matters use full names.  Requiring at
    # least two tokens rejects prose fragments such as "Historically" and
    # "methodology" that leaked from the old parser.
    if not 2 <= len(words) <= 8:
        return False

    blocked = (
        "program committee",
        "conference organization",
        "industrial track",
        "demonstration track",
        "research track",
        "test-of-time",
        "after careful consideration",
    )
    low = value.casefold()
    return not any(token in low for token in blocked)


def parse_person_line(line: str) -> tuple[str, str, str | None] | None:
    """Parse one research-PC entry.

    Supported layouts:
        Name, Affiliation, Country
        Name (Affiliation, Country)
        Name (Affiliation, Country) – Chair

    The third return value is an optional per-row role override.  This is
    needed for EDBT 2020, whose page says simply "Research Program Committee"
    and marks the chair on the person row itself.
    """
    raw = clean_line(line)
    raw = re.sub(r"^[•▪●○\-*]+\s*", "", raw)

    role_override: str | None = None

    # A very small set of explicitly verified PDF text-layer anomalies.
    # Do this before the normal comma/parenthesis parsers.
    if raw in KNOWN_PERSON_LINE_FIXES:
        name, affiliation = KNOWN_PERSON_LINE_FIXES[raw]
        return name, affiliation, role_override

    chair_match = re.search(r"\s+[–—-]\s*Chair\s*$", raw, re.I)
    if chair_match:
        role_override = "program_committee_chair"
        raw = raw[: chair_match.start()].rstrip()

    # 2020: Name (Affiliation).  Use the final parenthesized group so a name
    # containing a nickname in parentheses is not split at the first "(".
    parenthesized = re.fullmatch(r"(.+)\s+\(([^()]*)\)", raw)
    if parenthesized:
        name = clean_line(parenthesized.group(1)).strip(" \t\"'“”")
        affiliation = clean_line(parenthesized.group(2)).strip(" \t\"'“”")
        if plausible_name(name) and affiliation:
            return name, affiliation, role_override
        return None

    # 2019/2021: Name, Affiliation, Country.
    if "," in raw:
        name, affiliation = raw.split(",", 1)
        name = clean_line(name).strip(" \t\"'“”")
        affiliation = clean_line(affiliation).strip(" \t\"'“”")
        if plausible_name(name) and affiliation:
            return name, affiliation, role_override

    return None


def split_layout_line(line: str) -> list[str]:
    """Split a pypdf layout line into visual columns before whitespace cleanup.

    `extract_text(extraction_mode="layout")` represents a two-column PDF with a
    large run of spaces between the left and right entries.  The old scraper
    called `clean_line()` first, collapsing that gap and accidentally turning
    two reviewers into one string.
    """
    raw = (
        line.replace("\xa0", " ")
        .replace("\u200b", "")
        .replace("\u00ad", "")
        .strip()
    )
    if not raw:
        return []

    # Three or more spaces is a layout gap, not ordinary word spacing.
    parts = re.split(r"\s{3,}", raw)
    return [clean_line(part) for part in parts if clean_line(part)]


def extract_pdf_pages(pdf_bytes: bytes) -> list[list[str]]:
    reader = PdfReader(io.BytesIO(pdf_bytes))
    pages: list[list[str]] = []

    for page in reader.pages:
        try:
            text = page.extract_text(extraction_mode="layout") or ""
            lines: list[str] = []
            for raw_line in text.splitlines():
                lines.extend(split_layout_line(raw_line))
        except TypeError:
            # Compatibility with older pypdf releases.  Plain extraction
            # normally emits the two columns as separate text lines.
            text = page.extract_text() or ""
            lines = [
                clean_line(line)
                for line in text.splitlines()
                if clean_line(line)
            ]

        pages.append(lines)

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
    finished_research_pc = False
    seen: set[tuple[str, str, str]] = set()
    unparsed_candidates: list[tuple[int, str]] = []

    for page_number, lines in enumerate(pages, start=1):
        for line in lines:
            # Do not let a generic "Program Committee Members" line in a table
            # of contents start parsing.  We enter only after a specific
            # Research Program Committee heading.
            if is_research_pc_start(line):
                entered_research_pc = True

            role = role_for_line(
                line,
                entered_research_pc=entered_research_pc,
            )
            if role:
                current_role = role
                continue

            if entered_research_pc and is_stop_heading(line):
                finished_research_pc = True
                break

            if not entered_research_pc or current_role is None:
                continue

            parsed = parse_person_line(line)
            if parsed is None:
                # Keep a few suspicious list-like lines for diagnostics.  This
                # catches PDF extraction regressions without treating prose as
                # a person.
                if (
                    len(line) <= 180
                    and ("," in line or "(" in line or ")" in line)
                    and not is_stop_heading(line)
                ):
                    unparsed_candidates.append((page_number, line))
                continue

            name, affiliation, role_override = parsed
            record_role = role_override or current_role

            if record_role == "program_committee_chair" and not include_chairs:
                continue

            key = (record_role, name, affiliation)
            if key in seen:
                continue
            seen.add(key)

            records.append(
                ReviewerRecord(
                    year=year,
                    role=record_role,
                    name=name,
                    affiliation=affiliation,
                    name_key=normalize_name_key(name),
                    raw_text=line,
                    source_page=page_number,
                    source_url=source_url,
                    fetched_at=fetched_at,
                )
            )

        if finished_research_pc:
            break

    if not entered_research_pc:
        raise ValueError(
            "Could not find a Research Program Committee heading in the EDBT front matter"
        )

    if not records:
        raise ValueError(
            "Research PC heading found, but no reviewer records were parsed"
        )

    # Regression guard for the three editions used by this project.  If pypdf
    # changes its layout extraction behavior, fail loudly instead of writing a
    # silently corrupted reviewer database.
    expected = KNOWN_RESEARCH_PC_COUNTS.get(year, {})
    if expected:
        actual: dict[str, int] = {}
        for record in records:
            actual[record.role] = actual.get(record.role, 0) + 1

        mismatches = [
            f"{role}: expected {expected_count}, parsed {actual.get(role, 0)}"
            for role, expected_count in expected.items()
            if actual.get(role, 0) != expected_count
        ]
        if mismatches:
            examples = "\n".join(
                f"    page {page}: {text}"
                for page, text in unparsed_candidates[:12]
            )
            detail = "\n".join(mismatches)
            if examples:
                detail += "\n  Unparsed list-like examples:\n" + examples
            raise ValueError(
                "EDBT research-PC count sanity check failed:\n  " + detail
            )

    if unparsed_candidates:
        print(
            f"  note: {len(unparsed_candidates)} list-like lines inside the "
            "research-PC section were not parsed; known-year count checks passed."
        )

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
        raise RuntimeError(
            f"Could not check robots.txt at {robots_url}: HTTP {response.status_code}"
        )
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
        raise PermissionError(
            f"robots.txt does not allow this user-agent to fetch {url}"
        )
    response = session.get(url, timeout=45, headers={"Accept": accept})
    response.raise_for_status()
    fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return (
        response.content,
        fetched_at,
        response.headers.get("Content-Type", ""),
    )


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
        print(
            f"  warning: could not inspect OpenProceedings index: {exc}",
            file=sys.stderr,
        )

    return (
        CANONICAL_FRONTMATTER_URL.format(year=year),
        index_html,
        index_fetched_at,
        index_url if index_html is not None else None,
    )


def open_db(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS source_documents (
            id INTEGER PRIMARY KEY,
            year INTEGER NOT NULL,
            url TEXT NOT NULL,
            content_type TEXT NOT NULL DEFAULT '',
            fetched_at TEXT NOT NULL,
            sha256 TEXT NOT NULL,
            UNIQUE(year, url, sha256)
        )
        """)
    conn.execute("""
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
        """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_edbt_year ON reviewer_records(year)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_edbt_role ON reviewer_records(role)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_edbt_name_key ON reviewer_records(name_key)"
    )
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
        (
            year,
            url,
            content_type,
            fetched_at,
            hashlib.sha256(content).hexdigest(),
        ),
    )


def replace_year_records(
    conn: sqlite3.Connection,
    *,
    year: int,
    records: Iterable[ReviewerRecord],
) -> tuple[int, int]:
    """Replace one year's rows only after that year's PDF parsed successfully.

    This is essential when fixing scraper bugs: a rerun must remove the old
    malformed rows instead of merely INSERT OR IGNORE-ing the corrected rows
    beside them.
    """
    rows = list(records)
    if any(r.year != year for r in rows):
        raise ValueError(
            "replace_year_records received records from multiple years"
        )

    previous = conn.execute(
        "SELECT COUNT(*) FROM reviewer_records WHERE year=?",
        (year,),
    ).fetchone()[0]

    with conn:
        conn.execute("DELETE FROM reviewer_records WHERE year=?", (year,))
        conn.executemany(
            """
            INSERT INTO reviewer_records(
                year, role, name, affiliation, name_key, raw_text,
                source_page, source_url, fetched_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    r.year,
                    r.role,
                    r.name,
                    r.affiliation,
                    r.name_key,
                    r.raw_text,
                    r.source_page,
                    r.source_url,
                    r.fetched_at,
                )
                for r in rows
            ],
        )

    return previous, len(rows)


def print_summary(conn: sqlite3.Connection) -> None:
    print("\nDatabase summary:")
    for year, role, n in conn.execute(
        "SELECT year, role, COUNT(*) FROM reviewer_records GROUP BY year, role ORDER BY year, role"
    ):
        print(f"  {year}: {role}: {n}")
    total = conn.execute("SELECT COUNT(*) FROM reviewer_records").fetchone()[0]
    keys = conn.execute(
        "SELECT COUNT(DISTINCT name_key) FROM reviewer_records"
    ).fetchone()[0]
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
            raise SystemExit(
                f"Invalid --frontmatter {value!r}; use YEAR=URL"
            ) from exc
    return result


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--years", nargs="+", type=int, default=[2021])
    parser.add_argument("--db", default=str(DEFAULT_DB))
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
                pdf_url, index_html, index_fetched_at, index_url = (
                    resolve_frontmatter_url(
                        session,
                        year=year,
                        override=overrides.get(year),
                        ignore_robots=args.ignore_robots,
                    )
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
                previous, inserted = replace_year_records(
                    conn,
                    year=year,
                    records=records,
                )
                print(
                    f"  parsed {len(records)} records; replaced {previous} old rows "
                    f"with {inserted} corrected rows"
                )
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
