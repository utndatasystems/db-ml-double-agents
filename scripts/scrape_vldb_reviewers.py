#!/usr/bin/env python3
"""Scrape public PVLDB/VLDB review-board pages into SQLite.

The script targets the official Proceedings of the VLDB Endowment (PVLDB)
review-board pages, for example:

    https://www.vldb.org/pvldb/volumes/19/review-board

PVLDB volume 19 corresponds to the VLDB 2026 conference/review cycle
2025/2026; volume 18 to VLDB 2025; volume 17 to VLDB 2024, etc.  The mapping
used here is:

    pvldb_volume = conference_year - 2007

The script stores Editors in Chief, Associate Editors, Members of the Review
Board, General Chairs, and technical-track committees from the annual official
front-matter PDF. Tutorial committees are not included. PVLDB Editors in
Chief are treated as the research-program leadership layer because they serve
as the VLDB Program Chairs for the corresponding conference year.

The output intentionally contains *raw records*, not entity-resolved people.
The same person may therefore appear in multiple volumes or under slightly
different names/affiliations.  `name_key` is only a conservative blocking key
for later entity resolution.

Install:
    pip install requests beautifulsoup4 pypdf

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
import io
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
from bs4 import BeautifulSoup

from scraper_common import clean_line, normalize_name_key, extract_visible_lines

from service_roles import (
    assert_no_qualifying_person_loss,
    ensure_reviewer_service_schema,
    service_values,
)

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
OFFICERS_URL = "https://www.vldb.org/{year}/?officers="
FRONTMATTER_URL = "https://www.vldb.org/pvldb/vol{volume}/FrontMatterVol{volume}No12.pdf"

PAGE_HEADER_RE = re.compile(
    r"^PVLDB\s+Review\s+Board\s+for\s+Volume\s+(\d+)\s*\(([^)]*)\)\s*$",
    re.I,
)

ROLE_PATTERNS = [
    (re.compile(r"^Editors?(?:\s+in\s+|-in-?)Chief(?:\s+of\s+PVLDB)?$", re.I), "editor_in_chief"),
    (re.compile(r"^Associate\s+Editors?$", re.I), "associate_editor"),
    (
        re.compile(
            r"^(?:Members?\s+of\s+the\s+Review\s+Board|Review\s+Board)$", re.I
        ),
        "review_board_member",
    ),
]

OFFICER_ROLE_HEADINGS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"^General(?: Conference)? (?:Co[- ]?)?Chairs?$", re.I), "general_chair"),
    (
        re.compile(
            r"^(?:Industrial(?:\s+Track)?(?:\s+Program)?|"
            r"Industrial,\s+Applications?\s+and\s+Experience(?:\s+Program)?)\s+(?:Co[- ]?)?Chairs?$",
            re.I,
        ),
        "industrial_track_chair",
    ),
    (
        re.compile(r"^(?:Demonstration|Demo)(?:\s+Track)?\s+(?:Co[- ]?)?Chairs?$", re.I),
        "demonstration_chair",
    ),
    (re.compile(r"^Tutorials?(?:\s+Track)?\s+(?:Co[- ]?)?Chairs?$", re.I), "tutorial_chair"),
    (re.compile(r"^Workshop(?:\s+Track)?\s+(?:Co[- ]?)?Chairs?$", re.I), "workshop_chair"),
    (re.compile(r"^Panel (?:Co[- ]?)?Chairs?$", re.I), "panel_chair"),
    (re.compile(r"^PhD Workshop (?:Co[- ]?)?Chairs?$", re.I), "phd_symposium_chair"),
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



def parse_vldb_review_board_page(
    html: bytes,
    *,
    conference_year: int,
    expected_volume: int,
    source_url: str,
    fetched_at: str,
    include_eic: bool = True,
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
    if include_eic and not any(r.role == "editor_in_chief" for r in records):
        raise ValueError(f"No PVLDB Editor(s)-in-Chief parsed from {source_url}")

    return records


def _match_officer_role(heading: str) -> str | None:
    value = clean_line(heading).rstrip(":")
    for pattern, role in OFFICER_ROLE_HEADINGS:
        if pattern.match(value):
            return role
    return None


def _looks_like_officer_section_heading(heading: str) -> bool:
    """Return True for structural officer headings, but not person-name headings.

    Several 2022--2025 VLDB templates render each officer's name in an h4/h5
    element. Treating every HTML heading as a section boundary therefore drops
    all people under a recognized role. Only headings that themselves look like
    role/organization labels should terminate the current section.
    """
    value = clean_line(heading).rstrip(":").casefold()
    if not value:
        return False
    if _match_officer_role(heading) is not None:
        return True

    structural_tokens = (
        "chair",
        "committee",
        "organizing",
        "organisation",
        "organization",
        "proceedings",
        "publicity",
        "sponsorship",
        "registration",
        "finance",
        "local arrangements",
        "web team",
        "webmaster",
        "volunteer",
        "diversity",
        "inclusion",
        "social media",
        "advisory",
    )
    return any(token in value for token in structural_tokens)


def parse_vldb_officers_page(
    html: bytes,
    *,
    conference_year: int,
    pvldb_volume: int,
    source_url: str,
    fetched_at: str,
) -> list[ReviewerRecord]:
    """Parse qualifying general/technical chairs from a VLDB officer page.

    VLDB's 2022--2025 sites use more than one officer-card template. In some
    years a person's name is itself an h4/h5 heading and the affiliation is a
    separate text node. We therefore distinguish *role headings* from arbitrary
    HTML headings and support both combined ``Name, Affiliation`` rows and
    adjacent ``Name`` / ``Affiliation`` rows. Program Committee Chairs are
    intentionally not stored here because the same people are represented as
    PVLDB Editors in Chief on the review-board page.
    """
    soup = BeautifulSoup(html, "html.parser")
    heading_texts = {
        clean_line(h.get_text(" ", strip=True))
        for h in soup.find_all(re.compile(r"^h[1-6]$"))
        if clean_line(h.get_text(" ", strip=True))
    }
    lines = extract_visible_lines(html)

    records: list[ReviewerRecord] = []
    current_role: str | None = None
    matched_roles: set[str] = set()
    pending_name: str | None = None

    def append_record(name: str, affiliation: str, raw_text: str) -> None:
        assert current_role is not None
        records.append(
            ReviewerRecord(
                conference_year=conference_year,
                pvldb_volume=pvldb_volume,
                review_cycle="",
                role=current_role,
                name=name,
                affiliation=affiliation,
                name_key=normalize_name_key(name),
                raw_text=raw_text,
                source_url=source_url,
                fetched_at=fetched_at,
            )
        )

    for line in lines:
        if line in heading_texts:
            matched_role = _match_officer_role(line)
            if matched_role is not None:
                current_role = matched_role
                matched_roles.add(matched_role)
                pending_name = None
                continue

            # Person names are headings in some historical VLDB templates.
            # Only structural role/organization headings should end a section.
            if _looks_like_officer_section_heading(line):
                current_role = None
                pending_name = None
                continue

        if current_role is None:
            continue

        fragments = [
            clean_line(part)
            for part in re.split(r"\s+\|\s+", line)
            if clean_line(part)
        ]
        for fragment in fragments:
            # Fallback for officer cards rendered as two text nodes:
            #     Person Name
            #     Institution, Country
            # Once a standalone name is pending, the very next text fragment
            # belongs to that card. Check this before parse_person_line(), since
            # affiliations such as "CWI, Netherlands" can themselves look like
            # a comma-separated person row to the generic parser.
            if pending_name is not None:
                append_record(
                    pending_name,
                    fragment,
                    f"{pending_name} | {fragment}",
                )
                pending_name = None
                continue

            parsed = parse_person_line(fragment)
            if parsed is not None:
                name, affiliation = parsed
                append_record(name, affiliation, fragment)
                continue

            # The first line may itself be an h4/h5, hence the heading logic
            # above deliberately lets non-structural headings fall through.
            if plausible_person_name(fragment):
                pending_name = fragment.strip("-–—,;: ")

    if "general_chair" not in matched_roles:
        raise ValueError(f"No VLDB General Chair section found on {source_url}")
    if not any(r.role == "general_chair" for r in records):
        raise ValueError(
            f"VLDB General Chair section found, but no chairs parsed from {source_url}"
        )

    return records


def parse_vldb_frontmatter_pages(
    pages: Iterable[str], *, conference_year: int, expected_volume: int,
    source_url: str, fetched_at: str,
) -> list[ReviewerRecord]:
    """Read only explicitly labelled industry/demo committees in annual front matter.

    PDF text is read in column order. A new name starts a record even when the
    previous printed affiliation has an unmatched parenthesis (volume 18 does).
    Never infer tutorial committees or names from arbitrary front-matter pages.
    """
    pages = list(pages)
    if volume_for_year(conference_year) != expected_volume:
        raise ValueError("Conference year and PVLDB volume disagree")
    if not pages or not re.search(rf"Volume\s+{expected_volume}\s*,\s*No\.\s*12", pages[0], re.I):
        raise ValueError(f"Missing expected volume {expected_volume} front-matter header")
    caption = re.compile(r"^(INDUSTRIAL|NDUSTRIAL|DEMONSTRATION) TRACK CHAIRS AND REVIEWERS\s*[-–]\s*Vol\.\s*(\d+)$", re.I)
    heading = re.compile(r"^(Industrial|Demonstration) Track (PC Chairs|Program Chairs|Reviewers|Program Committee Members|Associate Editors)$", re.I)
    rows: list[ReviewerRecord] = []
    seen_tracks: set[str] = set()
    for page_number, text in enumerate(pages, 1):
        lines = [clean_line(line) for line in text.splitlines() if clean_line(line)]
        matches = [(i, caption.fullmatch(line)) for i, line in enumerate(lines) if caption.fullmatch(line)]
        if not matches:
            continue
        if len(matches) != 1 or int(matches[0][1].group(2)) != expected_volume:
            raise ValueError(f"Unexpected committee caption on PDF page {page_number}")
        start, match = matches[0]
        track = 'demo' if match.group(1).upper() == 'DEMONSTRATION' else 'industrial'
        if track in seen_tracks:
            raise ValueError(f"Repeated {track} committee page; review PDF layout")
        seen_tracks.add(track)
        role = None
        pending: list[str] = []

        def flush() -> None:
            if not pending:
                return
            raw = clean_line(' '.join(pending))
            name, affiliation = raw.split('(', 1)
            name, affiliation = clean_line(name), clean_line(affiliation)
            difference = raw.count('(') - raw.count(')')
            if difference == 0 and affiliation.endswith(')'):
                affiliation = affiliation[:-1].strip()
            elif difference != 1:
                raise ValueError(f"Malformed affiliation on PDF page {page_number}: {raw}")
            if not affiliation or not role:
                raise ValueError(f"Incomplete committee row on PDF page {page_number}: {raw}")
            rows.append(ReviewerRecord(conference_year, expected_volume, '', role,
                name, affiliation, normalize_name_key(name), f'[PDF page {page_number}] {raw}', source_url, fetched_at))
            pending.clear()

        for line in lines[start + 1:]:
            if re.fullmatch(r"PVLDB Vol\..*|[ivxlcdm]+", line, re.I):
                continue
            section = heading.fullmatch(line)
            if section:
                flush()
                section_track = 'demo' if section.group(1).lower() == 'demonstration' else 'industrial'
                if section_track != track:
                    raise ValueError(f"Mixed track headings on PDF page {page_number}")
                suffix = section.group(2).lower()
                if suffix in {'pc chairs', 'program chairs'}:
                    role = 'demonstration_chair' if track == 'demo' else 'industrial_track_chair'
                elif suffix == 'associate editors':
                    role = f'{track}_associate_editor'
                else:
                    role = f'{track}_pc_member'
                continue
            prefix = line.split('(', 1)[0].strip()
            is_person = '(' in line and len(prefix.split()) >= 2 and plausible_person_name(prefix) and not re.search(r"\b(university|institute|college|department|school|laboratory)\b", prefix, re.I)
            if is_person:
                flush()
                if role is None:
                    raise ValueError(f"Name before role on PDF page {page_number}: {line}")
                pending.append(line)
            elif pending:
                pending.append(line)
            else:
                raise ValueError(f"Unparsed committee line on PDF page {page_number}: {line}")
        flush()
        required = {f'{track}_pc_member', 'demonstration_chair' if track == 'demo' else 'industrial_track_chair'}
        if not required.issubset({r.role for r in rows}):
            raise ValueError(f"Missing {track} chairs or committee members in front matter")
    if seen_tracks != {'industrial', 'demo'}:
        raise ValueError("Front matter must contain both industry and demonstration committees")
    return rows


def parse_vldb_frontmatter_pdf(content: bytes, **kwargs) -> list[ReviewerRecord]:
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(content))
    return parse_vldb_frontmatter_pages((page.extract_text() or '' for page in reader.pages), **kwargs)


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
    ensure_reviewer_service_schema(conn, venue="VLDB")
    return conn


def save_records(
    conn: sqlite3.Connection,
    records: Iterable[ReviewerRecord],
    *,
    conference_year: int,
    pvldb_volume: int,
    source_pages: Iterable[tuple[str, str, bytes]],
    allow_qualifying_loss: bool = False,
) -> int:
    rows = list(records)
    pages = list(source_pages)
    assert_no_qualifying_person_loss(
        conn, rows, venue="VLDB", year_column="conference_year", year=conference_year,
        allow_loss=allow_qualifying_loss,
    )
    with conn:
        for url, fetched_at, html in pages:
            digest = hashlib.sha256(html).hexdigest()
            conn.execute(
                """
                INSERT OR IGNORE INTO source_pages(
                    conference_year, pvldb_volume, url, fetched_at, sha256
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (conference_year, pvldb_volume, url, fetched_at, digest),
            )
        # Replace a year's old rows only after all sources parsed successfully, so a
        # parser/source fix cannot leave stale partial rows behind.
        conn.execute(
            "DELETE FROM reviewer_records WHERE conference_year = ?",
            (conference_year,),
        )
        before = conn.total_changes
        prepared = []
        for r in rows:
            role_family, track, qualifies = service_values("VLDB", r)
            prepared.append((
                r.conference_year, r.pvldb_volume, r.review_cycle, r.role,
                role_family, track, qualifies, r.name, r.affiliation, r.name_key,
                r.raw_text, r.source_url, r.fetched_at,
            ))
        conn.executemany(
            """
            INSERT INTO reviewer_records(
                conference_year, pvldb_volume, review_cycle, role, role_family, track,
                qualifies_db_seed, name, affiliation, name_key, raw_text, source_url, fetched_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            prepared,
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
        help="Seconds to wait between HTTP requests (default: 1.0)",
    )
    parser.add_argument(
        "--include-eic",
        action="store_true",
        help=argparse.SUPPRESS,
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
            "Accept": "text/html,application/xhtml+xml,application/pdf",
        }
    )

    conn = open_db(args.db)
    failed = False
    try:
        for index, conference_year in enumerate(years):
            volume = volume_for_year(conference_year)
            review_url = REVIEW_BOARD_URL.format(volume=volume)
            officers_url = OFFICERS_URL.format(year=conference_year)

            try:
                print(
                    f"Fetching VLDB {conference_year} / PVLDB volume {volume}: {review_url}"
                )
                review_html, review_fetched_at = fetch_page(
                    session,
                    review_url,
                    ignore_robots=args.ignore_robots,
                )
                review_records = parse_vldb_review_board_page(
                    review_html,
                    conference_year=conference_year,
                    expected_volume=volume,
                    source_url=review_url,
                    fetched_at=review_fetched_at,
                    include_eic=True,
                )

                if args.delay > 0:
                    time.sleep(args.delay)

                print(f"Fetching VLDB {conference_year} officers: {officers_url}")
                officers_html, officers_fetched_at = fetch_page(
                    session,
                    officers_url,
                    ignore_robots=args.ignore_robots,
                )
                officer_records = parse_vldb_officers_page(
                    officers_html,
                    conference_year=conference_year,
                    pvldb_volume=volume,
                    source_url=officers_url,
                    fetched_at=officers_fetched_at,
                )

                if args.delay > 0:
                    time.sleep(args.delay)
                frontmatter_url = FRONTMATTER_URL.format(volume=volume)
                print(f"Fetching VLDB {conference_year} technical committees: {frontmatter_url}")
                frontmatter_pdf, frontmatter_fetched_at = fetch_page(
                    session, frontmatter_url, ignore_robots=args.ignore_robots,
                )
                technical_records = parse_vldb_frontmatter_pdf(
                    frontmatter_pdf, conference_year=conference_year, expected_volume=volume,
                    source_url=frontmatter_url, fetched_at=frontmatter_fetched_at,
                )
                # Keep the HTML source as the primary observation for exact
                # duplicate names/roles/affiliations; preserve distinct evidence.
                records = []
                seen = set()
                for record in review_records + officer_records + technical_records:
                    key = (record.role, record.name_key, clean_line(record.affiliation).casefold())
                    if key not in seen:
                        seen.add(key)
                        records.append(record)
                inserted = save_records(
                    conn,
                    records,
                    conference_year=conference_year,
                    pvldb_volume=volume,
                    source_pages=[
                        (review_url, review_fetched_at, review_html),
                        (officers_url, officers_fetched_at, officers_html),
                        (frontmatter_url, frontmatter_fetched_at, frontmatter_pdf),
                    ],
                    allow_qualifying_loss=args.allow_qualifying_loss,
                )
                print(
                    f"  parsed {len(review_records)} review-board records + "
                    f"{len(officer_records)} officer records + {len(technical_records)} technical records; "
                    f"replaced year with {inserted} rows"
                )
            except Exception as exc:
                failed = True
                print(
                    f"  ERROR for VLDB {conference_year}: {exc}",
                    file=sys.stderr,
                )

            if index < len(years) - 1 and args.delay > 0:
                time.sleep(args.delay)

        print_summary(conn)
    finally:
        conn.close()

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
