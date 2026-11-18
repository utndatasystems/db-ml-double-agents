#!/usr/bin/env python3
"""Scrape public EDBT research program-committee records into SQLite.

Source strategy
---------------
EDBT 2022 onward uses the official yearly EDBT/ICDT conference committee
pages. Older editions (2019-2021) use the official OpenProceedings front-matter
PDFs, whose column layouts require a PDF-aware parser.

When --years is omitted, the scraper collects 2022 through the last completed
calendar year. Passing --years explicitly overrides that range, e.g.
``--years 2026`` updates only 2026. Existing rows for other years are left
untouched.

By default Senior PC members and ordinary research PC members are stored. Use
For 2022 onward, research PC chairs and qualifying conference/technical-track
chairs are included by default from the official officer pages.

Examples:
    python scripts/scrape_edbt_reviewers.py
    python scripts/scrape_edbt_reviewers.py --years 2026
    python scripts/scrape_edbt_reviewers.py --years 2020 2021
    python scripts/scrape_edbt_reviewers.py --years 2025 --source 2025=https://official.example/committee

Install:
    pip install requests beautifulsoup4 pypdf
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
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup, NavigableString, Tag

from scraper_common import clean_line, normalize_name_key

from service_roles import (
    assert_no_qualifying_person_loss,
    ensure_reviewer_service_schema,
    service_values,
)
from pypdf import PdfReader

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = REPO_ROOT / "data" / "edbt_reviewers.sqlite"

DEFAULT_START_YEAR = 2022

def default_years() -> list[int]:
    """Years collected when --years is omitted: 2022 through last calendar year."""
    last_complete_year = datetime.now(timezone.utc).year - 1
    if last_complete_year < DEFAULT_START_YEAR:
        return []
    return list(range(DEFAULT_START_YEAR, last_complete_year + 1))

DEFAULT_UA = os.environ.get("EDBT_SCRAPER_UA", "edbt-reviewer-research/1.0")
INDEX_URL = "https://openproceedings.org/html/pages/{year}_edbt.html"
CANONICAL_FRONTMATTER_URL = (
    "https://openproceedings.org/{year}/conf/edbt/frontmatter.pdf"
)

# Official yearly conference sites.  The 2022 site exposes the committee as a
# direct HTML page; later editions use a ``contents=...`` page selector.  A few
# alternative URL forms are tried because the yearly site templates changed.
EDBT_HOME_URLS = {
    2022: "https://conferences.inf.ed.ac.uk/edbticdt2022/",
    # The conference's original virtual host serves HTTP; its HTTPS endpoint
    # and the old www.cs.uoi.gr/~edbticdt2023 alias time out on some networks.
    2023: "http://edbticdt2023.cs.uoi.gr/",
    2024: "https://dastlab.github.io/edbticdt2024/",
    2025: "https://edbticdt2025.upc.edu/",
    2026: "https://edbticdt2026.github.io/",
}

EDBT_COMMITTEE_URLS = {
    2022: [
        "https://conferences.inf.ed.ac.uk/edbticdt2022/contents/EDBT_Committee.html",
        # The official HTTP entry point remains reachable when direct HTTPS
        # requests return 403. Both paths still go through robots checks.
        "http://conferences.inf.ed.ac.uk/edbticdt2022/contents/EDBT_Committee.html",
    ],
    2023: [
        "http://edbticdt2023.cs.uoi.gr/contents/EDBT_Committee.html",
        "http://edbticdt2023.cs.uoi.gr/?contents=EDBT_Committee.html",
        "https://www.cs.uoi.gr/~edbticdt2023/?contents=EDBT_Committee.html",
        "https://www.cs.uoi.gr/~edbticdt2023/contents/EDBT_Committee.html",
    ],
    2024: [
        "https://dastlab.github.io/edbticdt2024/?contents=EDBT_Committee.html",
        "https://dastlab.github.io/edbticdt2024/contents/EDBT_Committee.html",
    ],
    2025: [
        "https://edbticdt2025.upc.edu/?contents=EDBT_Committee.html",
        "https://edbticdt2025.upc.edu/contents/EDBT_Committee.html",
    ],
    2026: [
        "https://edbticdt2026.github.io/?contents=EDBT_Committee.html",
        "https://edbticdt2026.github.io/contents/EDBT_Committee.html",
    ],
}

# Conference-officer pages supply General Chairs, EDBT Program Chairs, and
# technical-track chairs that are not part of the research-PC page.
# Conference-level technical PCs that are strong evidence of participation in
# the DB community.  Their pages are separate from the main research committee
# on several recent EDBT sites.
EDBT_TRACK_PC_PAGES = {
    "industrial": "call_for_IAP.html",
    "demo": "call_for_demos.html",
}

# Reviewed official call pages contain no member roster in these cases (the
# 2023 industry page lists chairs only, already collected from officers).
# Absence is different from failure to retrieve a roster known to be published.
EDBT_TRACK_WITHOUT_MEMBER_ROSTER = frozenset({
    (2022, "industrial"), (2022, "demo"), (2023, "industrial"),
})

EDBT_OFFICER_URLS = {
    2022: [
        "https://conferences.inf.ed.ac.uk/edbticdt2022/contents/conference_officers.html",
        "http://conferences.inf.ed.ac.uk/edbticdt2022/contents/conference_officers.html",
        "https://conferences.inf.ed.ac.uk/edbticdt2022/?contents=conference_officers.html",
    ],
    2023: [
        "http://edbticdt2023.cs.uoi.gr/contents/conference_officers.html",
        "http://edbticdt2023.cs.uoi.gr/?contents=conference_officers.html",
        "https://www.cs.uoi.gr/~edbticdt2023/contents/conference_officers.html",
        "https://www.cs.uoi.gr/~edbticdt2023/?contents=conference_officers.html",
    ],
    2024: [
        "https://dastlab.github.io/edbticdt2024/contents/conference_officers.html",
        "https://dastlab.github.io/edbticdt2024/?contents=conference_officers.html",
    ],
    2025: [
        "https://edbticdt2025.upc.edu/contents/conference_officers.html",
        "https://edbticdt2025.upc.edu/?contents=conference_officers.html",
    ],
}

MIN_RESEARCH_PC_ROWS = 20

# The front-matter layout changes between years.  In particular:
#   * 2019 uses one-column "Name, Affiliation" rows.
#   * 2020 uses two columns and "Name (Affiliation)" rows.
#   * 2021 uses two columns and "Name, Affiliation" rows.
#
# Enter the research committee only from a heading that is explicitly
# research-specific or EDBT-specific. A bare "Program Committee Members"
# heading is intentionally not allowed to start parsing because similar text
# may appear in front-matter tables of contents. Later editions sometimes
# prefix committee headings with "EDBT" and/or the conference year.
_EDBT_PREFIX = r"(?:EDBT(?:\s+\d{4})?\s+)?"
RESEARCH_PC_START_RE = re.compile(
    rf"^(?:(?:{_EDBT_PREFIX}Research\s+Program\s+Committee)|(?:EDBT(?:\s+\d{{4}})?\s+Program\s+Committee))(?:\s+Chairs?)?$",
    re.I,
)
SENIOR_PC_RE = re.compile(
    rf"^{_EDBT_PREFIX}Senior\s+Program\s+Committee(?:\s+Members?)?$",
    re.I,
)
ORDINARY_PC_RE = re.compile(
    rf"^{_EDBT_PREFIX}(?:Research\s+)?Program\s+Committee\s+Members?$",
    re.I,
)
# An explicitly EDBT-labelled member heading is also safe as an entry point in
# editions that omit a separate umbrella "EDBT Program Committee" heading.
EDBT_MEMBER_START_RE = re.compile(
    r"^EDBT(?:\s+\d{4})?\s+(?:Senior\s+)?Program\s+Committee\s+Members?$",
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

# Research-program pages should contain more than a handful of people. Keep
# this deliberately loose: exact historical counts and named people do not
# belong in parser correctness logic. The shared no-loss guard provides the
# stronger replacement safety check when updating an existing database.


@dataclass(frozen=True)
class ReviewerRecord:
    year: int
    role: str
    name: str
    affiliation: str
    name_key: str
    raw_text: str
    source_page: int | None
    source_url: str
    fetched_at: str




def is_research_pc_start(line: str) -> bool:
    value = clean_line(line).rstrip(":")
    return bool(
        RESEARCH_PC_START_RE.fullmatch(value)
        or EDBT_MEMBER_START_RE.fullmatch(value)
    )


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

    chair_match = re.search(r"\s+[–—-]\s*Chair\s*$", raw, re.I)
    if chair_match:
        role_override = "program_committee_chair"
        raw = raw[: chair_match.start()].rstrip()

    # 2020: Name (Affiliation).  Use the final parenthesized group so a name
    # containing a nickname in parentheses is not split at the first "(".
    parenthesized = re.fullmatch(r"(.+)\s+\(([^()]*)\)", raw)
    # Only use the historical ``Name (Affiliation)`` form when the
    # text before the final parenthesis is itself just the name.  Modern
    # comma-style rows may legitimately end their affiliation with a
    # qualifier such as ``HKUST (Guangzhou)``; treating that suffix as
    # the whole affiliation would swallow the real reviewer name.
    if parenthesized and "," not in parenthesized.group(1):
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

    research_rows = sum(
        record.role in {"senior_program_committee_member", "program_committee_member"}
        for record in records
    )
    if research_rows < MIN_RESEARCH_PC_ROWS:
        examples = "\n".join(
            f"    page {page}: {text}"
            for page, text in unparsed_candidates[:12]
        )
        detail = (
            f"Only {research_rows} EDBT research-PC rows were parsed from {source_url}; "
            f"expected at least {MIN_RESEARCH_PC_ROWS}."
        )
        if examples:
            detail += "\n  Unparsed list-like examples:\n" + examples
        raise ValueError(detail)

    if unparsed_candidates:
        print(
            f"  note: {len(unparsed_candidates)} list-like lines inside the "
            "research-PC section were not parsed."
        )


    return records


def html_committee_role(line: str) -> str | None:
    """Map EDBT committee headings on the yearly HTML sites to stored roles."""
    value = clean_line(line).rstrip(":")
    # Remove an optional leading conference label/year for simpler matching.
    value = re.sub(r"^EDBT(?:\s+\d{4})?\s+", "", value, flags=re.I)
    if re.fullmatch(r"Senior\s+Program\s+Committee(?:\s+Members?)?", value, re.I):
        return "senior_program_committee_member"
    if re.fullmatch(r"(?:Research\s+)?Program\s+Committee\s+Members?", value, re.I):
        return "program_committee_member"
    if re.fullmatch(r"(?:Research\s+)?Program\s+Committee\s+Chairs?", value, re.I):
        return "program_committee_chair"
    if re.fullmatch(r"(?:Industrial|Industry)(?:\s+(?:and|&)\s+Applications?)?(?:\s+Track)?\s+Program\s+Committee(?:\s+Members?)?", value, re.I):
        return "industrial_pc_member"
    if re.fullmatch(r"Demo(?:nstration)?(?:\s+Track)?\s+Program\s+Committee(?:\s+Members?)?", value, re.I):
        return "demo_pc_member"
    if re.fullmatch(r"Tutorials?(?:\s+Track)?\s+Program\s+Committee(?:\s+Members?)?", value, re.I):
        return "tutorial_pc_member"
    if re.fullmatch(r"(?:Test\s+of\s+Time|Awards?)\s+(?:Award\s+)?Committee", value, re.I):
        return "award_committee_member"
    return None


def is_html_research_umbrella(line: str) -> bool:
    value = clean_line(line).rstrip(":")
    return bool(
        re.fullmatch(r"EDBT(?:\s+\d{4})?\s+(?:Research\s+)?Program\s+Committee", value, re.I)
        or re.fullmatch(r"EDBT\s+Program\s+Committee", value, re.I)
    )


def is_html_stop_heading(line: str) -> bool:
    value = clean_line(line).rstrip(":")
    if re.search(r"\bICDT\b", value, re.I):
        return True
    return is_stop_heading(value) or bool(
        re.search(
            r"\b(?:Industrial|Industry|Demo|Demonstration|Tutorial|Test\s+of\s+Time|PhD)\b",
            value,
            re.I,
        )
    )


EDBT_OFFICER_ROLE_PATTERNS = [
    (re.compile(r"^General\s+(?:Co-)?Chairs?$", re.I), "general_chair"),
    (re.compile(r"^EDBT\s+(?:(?:Research\s+)?Program(?:\s+Committee)?|PC)\s+(?:Co-)?Chairs?$", re.I), "program_committee_chair"),
    (re.compile(r"^EDBT\s+Industr(?:ial|y)(?:\s*(?:&|and|/)\s*Applications?)?(?:\s+Track)?\s+(?:Co-)?Chairs?$", re.I), "industrial_track_chair"),
    (re.compile(r"^EDBT\s+Demo(?:nstration)?s?(?:\s+Track)?\s+(?:Co-)?Chairs?$", re.I), "demonstration_chair"),
    (re.compile(r"^(?:EDBT\s+)?Tutorials?(?:\s+Track)?\s+(?:Co-)?Chairs?$", re.I), "tutorial_chair"),
    (re.compile(r"^(?:EDBT\s+)?Workshops?(?:\s+Track)?\s+(?:Co-)?Chairs?$", re.I), "workshop_chair"),
    (re.compile(r"^EDBT\s+PhD\s+(?:Symposium|Workshop)(?:\s+Track)?\s+(?:Co-)?Chairs?$", re.I), "phd_symposium_chair"),
    (re.compile(r"^EDBT\s+Panels?(?:\s+Track)?\s+(?:Co-)?Chairs?$", re.I), "panel_chair"),
]


def edbt_officer_role_for_text(text: str) -> str | None:
    value = clean_line(text).rstrip(":")
    for pattern, role in EDBT_OFFICER_ROLE_PATTERNS:
        if pattern.fullmatch(value):
            return role
    return None


def edbt_officer_boundary_text(text: str) -> bool:
    """Return True for short chair/officer labels that end a role section.

    Historical EDBT/ICDT sites often render labels such as ``ICDT Program
    Chair`` or ``Publicity Chair`` as bold paragraphs rather than semantic
    headings.  They must still terminate the preceding EDBT role so names from
    unrelated/admin sections cannot leak into it.
    """
    value = clean_line(text).rstrip(":")
    if not value or len(value) > 140:
        return False
    low = value.casefold()
    return "chair" in low or low in {"conference officers", "conference organization"}


def _inline_person_line(name_tag: Tag) -> str | None:
    """Build one ``Name, affiliation`` row around a bold/strong name tag.

    EDBT's recent sites use inline markup such as ``<b>Name</b>, Affiliation``
    and often separate adjacent people with an empty ``span.brmedium``.  Reading
    the whole paragraph loses that record boundary; reading with
    ``get_text("\n")`` splits the name from its affiliation.  Walk only the
    inline siblings belonging to this person instead.
    """
    if name_tag.find(["h1", "h2", "h3", "h4", "h5", "h6"]) is not None:
        return None
    name = clean_line(name_tag.get_text(" ", strip=True))
    if not plausible_name(name):
        return None

    parts: list[str] = []
    sibling = name_tag.next_sibling
    while sibling is not None:
        if isinstance(sibling, Tag):
            if sibling.name in {"h1", "h2", "h3", "h4", "h5", "h6", "br"}:
                break
            classes = set(sibling.get("class", []))
            if sibling.name == "span" and "brmedium" in classes:
                break
            if sibling.name in {"b", "strong"} or sibling.find(["b", "strong"]):
                break
            text = clean_line(sibling.get_text(" ", strip=True))
        else:
            text = clean_line(str(sibling))
        if text:
            parts.append(text)
        sibling = sibling.next_sibling

    affiliation = clean_line(" ".join(parts)).lstrip(" ,;:-")
    if not affiliation:
        return None
    return f"{name}, {affiliation}"


def _row_text_from_tag(tag: Tag) -> str:
    """Preserve one structural EDBT row without splitting inline name markup."""
    name_tag = tag.find(["b", "strong"])
    if name_tag is not None:
        name = clean_line(name_tag.get_text(" ", strip=True))
        if plausible_name(name):
            full = clean_line(tag.get_text(" ", strip=True))
            if full.startswith(name):
                affiliation = clean_line(full[len(name):]).lstrip(" ,;:-")
                if affiliation:
                    return f"{name}, {affiliation}"
    return clean_line(tag.get_text(" ", strip=True))


def _lines_between_heading_and_next(heading: Tag) -> list[str]:
    """Return person-sized rows until the next HTML heading in document order.

    The heading itself may be nested in a formatting tag (``<b><h3>...``), so
    iterating only ``heading.next_siblings`` is incorrect.  Recent EDBT pages
    primarily use list/table rows, ``span.brmedium`` person rows, or bold names
    separated by empty ``span.brmedium`` markers.  A simple paragraph/<br>
    fallback remains for older pages.
    """
    heading_names = {"h1", "h2", "h3", "h4", "h5", "h6"}
    rows: list[str] = []
    seen_tags: set[int] = set()

    for node in heading.next_elements:
        if isinstance(node, Tag) and node.name in heading_names:
            break
        if not isinstance(node, Tag):
            continue

        # Explicit browser-visible row containers are the most reliable source.
        classes = set(node.get("class", []))
        if node.name in {"li", "tr"} or (
            node.name == "span" and "brmedium" in classes and clean_line(node.get_text(" ", strip=True))
        ):
            marker = id(node)
            if marker not in seen_tags:
                seen_tags.add(marker)
                text = _row_text_from_tag(node)
                if text:
                    rows.append(text)
            continue

        # Officer pages often put several people in one <p>, separated only by
        # an empty span.brmedium.  Reconstruct each bold-name row directly.
        if node.name in {"b", "strong"}:
            if node.find(list(heading_names)) is not None:
                continue
            parent = node.parent if isinstance(node.parent, Tag) else None
            if parent is not None:
                parent_classes = set(parent.get("class", []))
                if parent.name in {"li", "tr"} or (
                    parent.name == "span" and "brmedium" in parent_classes
                ):
                    continue
            text = _inline_person_line(node)
            if text:
                rows.append(text)
            continue

        # Plain paragraphs are still used on some years.  Only flatten them
        # when they do not already contain one of the structural row forms.
        if node.name == "p":
            if node.find(["li", "tr", "b", "strong"]) is not None:
                continue
            if node.find("span", class_="brmedium") is not None:
                continue
            fragment = BeautifulSoup(str(node), "html.parser")
            for br in fragment.find_all("br"):
                br.replace_with("\n")
            for raw in fragment.get_text(" ").splitlines():
                text = clean_line(raw)
                if text:
                    rows.append(text)

    # Preserve order while suppressing duplicate rows caused by nested markup.
    out: list[str] = []
    seen_rows: set[str] = set()
    for row in rows:
        if row not in seen_rows:
            seen_rows.add(row)
            out.append(row)
    return out


def iter_edbt_officer_heading_rows(soup: BeautifulSoup):
    for heading in soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6"]):
        role = edbt_officer_role_for_text(clean_line(heading.get_text(" ", strip=True)))
        if role is not None:
            yield role, _lines_between_heading_and_next(heading)


def parse_edbt_officers_html(
    html: bytes,
    *,
    year: int,
    source_url: str,
    fetched_at: str,
) -> list[ReviewerRecord]:
    """Parse EDBT leadership using the page's actual section boundaries."""
    soup = BeautifulSoup(html, "html.parser")
    records: list[ReviewerRecord] = []
    seen: set[tuple[str, str, str]] = set()

    def add(role: str, name: str, affiliation: str, raw: str) -> None:
        name = clean_line(name).strip(" \t\"'“”")
        affiliation = clean_line(affiliation).strip(" \t\"'“”")
        if not plausible_name(name):
            return
        key = (role, normalize_name_key(name), clean_line(affiliation).casefold())
        if key in seen:
            return
        seen.add(key)
        records.append(
            ReviewerRecord(
                year=year, role=role, name=name, affiliation=affiliation,
                name_key=normalize_name_key(name), raw_text=clean_line(raw),
                source_page=None, source_url=source_url, fetched_at=fetched_at,
            )
        )

    matched_roles: set[str] = set()
    for role, lines in iter_edbt_officer_heading_rows(soup):
        matched_roles.add(role)
        for line in lines:
            parsed = parse_person_line(line)
            if parsed is None:
                continue
            name, affiliation, _ = parsed
            add(role, name, affiliation, line)

    required = {"general_chair", "program_committee_chair"}
    missing = sorted(required - matched_roles)
    if missing:
        raise ValueError(
            f"EDBT officer page {source_url} is missing required role(s): {', '.join(missing)}"
        )
    if not any(r.role == "general_chair" for r in records):
        raise ValueError(f"EDBT General Chair section found, but no chairs parsed from {source_url}")
    if not any(r.role == "program_committee_chair" for r in records):
        raise ValueError(f"EDBT Program Chair section found, but no chairs parsed from {source_url}")
    return records

def merge_edbt_records(*groups: Iterable[ReviewerRecord]) -> list[ReviewerRecord]:
    out: list[ReviewerRecord] = []
    seen: set[tuple[int, str, str, str]] = set()
    for group in groups:
        for record in group:
            key = (record.year, record.role, record.name_key, clean_line(record.affiliation).casefold())
            if key in seen:
                continue
            seen.add(key)
            out.append(record)
    return out


def iter_edbt_heading_rows(soup: BeautifulSoup):
    """Yield (role, visible rows) for EDBT committee sections."""
    for heading in soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6"]):
        role = html_committee_role(clean_line(heading.get_text(" ", strip=True)))
        if role is not None:
            yield role, _lines_between_heading_and_next(heading)


def parse_edbt_committee_html(
    html: bytes,
    *,
    year: int,
    source_url: str,
    fetched_at: str,
    include_chairs: bool = False,
) -> list[ReviewerRecord]:
    """Parse official EDBT committee sections from structural HTML rows.

    Follow headings and browser-visible structural row boundaries.  Modern
    EDBT pages intentionally have no collapsed-text guessing fallback: if the
    structure changes enough that rows cannot be parsed, fail loudly instead.
    """
    soup = BeautifulSoup(html, "html.parser")
    records: list[ReviewerRecord] = []
    seen: set[tuple[str, str, str]] = set()
    entered = False

    def add_row(role: str, name: str, affiliation: str, raw: str) -> None:
        if role == "program_committee_chair" and not include_chairs:
            return
        name = clean_line(name).strip(" \t\"'“”")
        affiliation = clean_line(affiliation).strip(" \t\"'“”")
        if not plausible_name(name):
            return
        key = (role, normalize_name_key(name), clean_line(affiliation).casefold())
        if key in seen:
            return
        seen.add(key)
        records.append(
            ReviewerRecord(
                year=year, role=role, name=name, affiliation=affiliation,
                name_key=normalize_name_key(name), raw_text=clean_line(raw),
                source_page=None, source_url=source_url, fetched_at=fetched_at,
            )
        )

    for role, lines in iter_edbt_heading_rows(soup):
        entered = True
        for line in lines:
            parsed = parse_person_line(line)
            if parsed is None:
                continue
            name, affiliation, role_override = parsed
            add_row(role_override or role, name, affiliation, line)

    if not entered:
        raise ValueError("No EDBT program-committee heading found")
    if not records:
        raise ValueError("EDBT committee heading found, but no reviewer rows parsed")

    actual: dict[str, int] = {}
    for record in records:
        actual[record.role] = actual.get(record.role, 0) + 1

    research_rows = sum(actual.get(role, 0) for role in (
        "senior_program_committee_member", "program_committee_member"
    ))
    if research_rows < MIN_RESEARCH_PC_ROWS:
        raise ValueError(
            f"Only {research_rows} EDBT research-PC rows parsed from {source_url}; "
            f"expected at least {MIN_RESEARCH_PC_ROWS}."
        )
    return records


def parse_edbt_track_pc_html(
    html: bytes,
    *,
    year: int,
    source_url: str,
    fetched_at: str,
    page_kind: str,
) -> list[ReviewerRecord]:
    """Parse a conference-level EDBT Industrial/Application or Demo PC page."""
    config = {
        "industrial": ("industrial_pc_member", "industrial_track_chair"),
        "demo": ("demo_pc_member", "demonstration_chair"),
    }
    if page_kind not in config:
        raise ValueError(f"Unknown EDBT side-PC kind: {page_kind}")
    member_role, chair_role = config[page_kind]

    soup = BeautifulSoup(html, "html.parser")
    records: list[ReviewerRecord] = []
    seen: set[tuple[str, str, str]] = set()

    def add(role: str, line: str) -> None:
        parsed = parse_person_line(line)
        if parsed is None:
            return
        name, affiliation, _ = parsed
        key = (role, normalize_name_key(name), clean_line(affiliation).casefold())
        if key in seen:
            return
        seen.add(key)
        records.append(
            ReviewerRecord(
                year=year, role=role, name=name, affiliation=affiliation,
                name_key=normalize_name_key(name), raw_text=line, source_page=None,
                source_url=source_url, fetched_at=fetched_at,
            )
        )

    for heading in soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6"]):
        title = clean_line(heading.get_text(" ", strip=True)).rstrip(":")
        if re.fullmatch(r"(?:Track|Program)\s+(?:Co-?)?Chairs?", title, re.I):
            role = chair_role
        elif re.fullmatch(r"Program\s+Committee(?:\s+Members?)?", title, re.I):
            role = member_role
        else:
            continue
        for line in _lines_between_heading_and_next(heading):
            add(role, line)

    if not any(r.role == member_role for r in records):
        raise ValueError(f"No EDBT {page_kind} PC members parsed from {source_url}")
    return records


def edbt_track_source_candidates(year: int, page_kind: str) -> list[str]:
    if (year, page_kind) in EDBT_TRACK_WITHOUT_MEMBER_ROSTER:
        return []
    home = EDBT_HOME_URLS.get(year)
    filename = EDBT_TRACK_PC_PAGES.get(page_kind)
    if not home or not filename:
        return []
    return [
        urljoin(home, f"contents/{filename}"),
        f"{home}?contents={filename}",
    ]

def discover_committee_links(html: bytes, base_url: str) -> list[str]:
    """Find EDBT committee links advertised by a yearly conference homepage."""
    soup = BeautifulSoup(html, "html.parser")
    found: list[tuple[int, str]] = []
    for a in soup.find_all("a", href=True):
        text = clean_line(a.get_text(" ", strip=True))
        href = str(a.get("href"))
        combined = f"{text} {href}".casefold()
        if "committee" not in combined:
            continue
        # Prefer links explicitly mentioning EDBT over generic committee pages,
        # and avoid clearly ICDT-only links.
        if "icdt" in combined and "edbt" not in combined:
            continue
        score = 0
        if "edbt" in combined:
            score -= 10
        if "program" in combined:
            score -= 3
        found.append((score, urljoin(base_url, href)))
    return [url for _, url in sorted(set(found), key=lambda x: x)]


def committee_source_candidates(
    session: requests.Session,
    *,
    year: int,
    override: str | None,
    ignore_robots: bool,
) -> list[str]:
    if override:
        return [override]

    candidates = list(EDBT_COMMITTEE_URLS.get(year, []))
    home = EDBT_HOME_URLS.get(year)
    if home:
        try:
            home_html, _, _ = fetch_bytes(
                session,
                home,
                ignore_robots=ignore_robots,
                accept="text/html,application/xhtml+xml",
            )
            candidates = discover_committee_links(home_html, home) + candidates
        except Exception as exc:
            print(f"  warning: could not inspect EDBT {year} homepage: {exc}", file=sys.stderr)

    # De-duplicate while keeping discovery/known-source priority.
    out: list[str] = []
    seen: set[str] = set()
    for url in candidates:
        if url not in seen:
            seen.add(url)
            out.append(url)
    return out


def fetch_and_parse_edbt_source(
    session: requests.Session,
    *,
    year: int,
    url: str,
    ignore_robots: bool,
    include_chairs: bool,
) -> tuple[list[ReviewerRecord], bytes, str, str]:
    content, fetched_at, content_type = fetch_bytes(
        session,
        url,
        ignore_robots=ignore_robots,
        accept="text/html,application/xhtml+xml,application/pdf,*/*",
    )
    if content.startswith(b"%PDF") or "pdf" in content_type.casefold():
        records = parse_edbt_frontmatter(
            content,
            year=year,
            source_url=url,
            fetched_at=fetched_at,
            include_chairs=include_chairs,
        )
    else:
        records = parse_edbt_committee_html(
            content,
            year=year,
            source_url=url,
            fetched_at=fetched_at,
            include_chairs=include_chairs,
        )
    return records, content, fetched_at, content_type


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
    db_path = Path(path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
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
    ensure_reviewer_service_schema(conn, venue="EDBT")
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
    allow_qualifying_loss: bool = False,
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

    assert_no_qualifying_person_loss(
        conn, rows, venue="EDBT", year_column="year", year=year,
        allow_loss=allow_qualifying_loss,
    )

    previous = conn.execute(
        "SELECT COUNT(*) FROM reviewer_records WHERE year=?",
        (year,),
    ).fetchone()[0]

    with conn:
        conn.execute("DELETE FROM reviewer_records WHERE year=?", (year,))
        prepared = []
        for r in rows:
            role_family, track, qualifies = service_values("EDBT", r)
            prepared.append((
                r.year, r.role, role_family, track, qualifies, r.name, r.affiliation,
                r.name_key, r.raw_text, r.source_page, r.source_url, r.fetched_at,
            ))
        conn.executemany(
            """
            INSERT INTO reviewer_records(
                year, role, role_family, track, qualifies_db_seed, name, affiliation,
                name_key, raw_text, source_page, source_url, fetched_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            prepared,
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
    parser.add_argument(
        "--years", nargs="+", type=int, default=None,
        help="Years to collect. Omit for 2022 through the last completed calendar year; explicit values override the default.",
    )
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument(
        "--include-chairs", action="store_true",
        help="Deprecated compatibility flag; qualifying EDBT chairs are always included.",
    )
    parser.add_argument("--delay", type=float, default=1.0)
    parser.add_argument(
        "--source",
        action="append",
        default=[],
        metavar="YEAR=URL",
        help="Override the official committee page/PDF for a year; may be repeated.",
    )
    parser.add_argument(
        "--officer-source",
        action="append",
        default=[],
        metavar="YEAR=URL",
        help="Override the official conference-officer page for a year; may be repeated.",
    )
    parser.add_argument(
        "--frontmatter",
        action="append",
        default=[],
        metavar="YEAR=URL",
        help=argparse.SUPPRESS,  # backwards-compatible alias for older PDF runs
    )
    parser.add_argument(
        "--allow-qualifying-loss", action="store_true",
        help="Allow a rerun to remove previously qualifying people for a year.",
    )
    parser.add_argument("--ignore-robots", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    overrides = parse_overrides(args.frontmatter)
    overrides.update(parse_overrides(args.source))
    officer_overrides = parse_overrides(args.officer_source)
    years = sorted(set(args.years if args.years is not None else default_years()))

    session = requests.Session()
    session.headers.update({"User-Agent": DEFAULT_UA})
    conn = open_db(args.db)
    failed = False
    try:
        for i, year in enumerate(years):
            try:
                # 2019-2021 retain the verified OpenProceedings PDF parser.
                if year <= 2021 and year not in overrides:
                    pdf_url, index_html, index_fetched_at, index_url = resolve_frontmatter_url(
                        session,
                        year=year,
                        override=None,
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
                    candidates = [pdf_url]
                else:
                    candidates = committee_source_candidates(
                        session,
                        year=year,
                        override=overrides.get(year),
                        ignore_robots=args.ignore_robots,
                    )

                if not candidates:
                    raise RuntimeError(
                        f"No built-in official EDBT committee source is configured for {year}. "
                        f"Pass --source {year}=URL."
                    )

                errors: list[str] = []
                parsed = None
                for source_url in candidates:
                    print(f"Fetching {source_url}")
                    try:
                        records, content, fetched_at, content_type = fetch_and_parse_edbt_source(
                            session,
                            year=year,
                            url=source_url,
                            ignore_robots=args.ignore_robots,
                            include_chairs=True,
                        )
                        parsed = (source_url, records, content, fetched_at, content_type)
                        break
                    except Exception as exc:
                        errors.append(f"{source_url}: {exc}")
                        print(f"  source failed: {exc}", file=sys.stderr)

                if parsed is None:
                    raise RuntimeError(
                        "No EDBT source candidate parsed successfully:\n  " + "\n  ".join(errors)
                    )

                source_url, records, content, fetched_at, content_type = parsed
                save_source(
                    conn,
                    year=year,
                    url=source_url,
                    content=content,
                    fetched_at=fetched_at,
                    content_type=content_type or (
                        "application/pdf" if content.startswith(b"%PDF") else "text/html"
                    ),
                )
                committee_count = len(records)
                officer_records: list[ReviewerRecord] = []
                if officer_overrides.get(year):
                    officer_candidates = [officer_overrides[year]]
                else:
                    officer_candidates = list(EDBT_OFFICER_URLS.get(year, []))

                if officer_candidates:
                    officer_errors: list[str] = []
                    officer_parsed = None
                    for officer_url in officer_candidates:
                        print(f"Fetching EDBT {year} officers: {officer_url}")
                        try:
                            officer_content, officer_fetched_at, officer_content_type = fetch_bytes(
                                session, officer_url, ignore_robots=args.ignore_robots,
                                accept="text/html,application/xhtml+xml",
                            )
                            parsed_officers = parse_edbt_officers_html(
                                officer_content, year=year, source_url=officer_url,
                                fetched_at=officer_fetched_at,
                            )
                            officer_parsed = (
                                officer_url, parsed_officers, officer_content,
                                officer_fetched_at, officer_content_type,
                            )
                            break
                        except Exception as exc:
                            officer_errors.append(f"{officer_url}: {exc}")
                            print(f"  officer source failed: {exc}", file=sys.stderr)

                    if officer_parsed is None:
                        raise RuntimeError(
                            "No EDBT officer source candidate parsed successfully:\n  "
                            + "\n  ".join(officer_errors)
                        )

                    (
                        officer_url, officer_records, officer_content,
                        officer_fetched_at, officer_content_type,
                    ) = officer_parsed
                    save_source(
                        conn, year=year, url=officer_url, content=officer_content,
                        fetched_at=officer_fetched_at, content_type=officer_content_type or "text/html",
                    )

                track_records: list[ReviewerRecord] = []
                for track_kind in EDBT_TRACK_PC_PAGES:
                    track_errors: list[str] = []
                    track_parsed = None
                    track_urls = edbt_track_source_candidates(year, track_kind)
                    if not track_urls:
                        continue  # No official source configured for this year.
                    for track_url in track_urls:
                        try:
                            if args.delay > 0:
                                time.sleep(args.delay)
                            print(f"Fetching EDBT {year} {track_kind} PC: {track_url}")
                            track_content, track_fetched_at, track_content_type = fetch_bytes(
                                session, track_url, ignore_robots=args.ignore_robots,
                                accept="text/html,application/xhtml+xml",
                            )
                            rows = parse_edbt_track_pc_html(
                                track_content, year=year, source_url=track_url,
                                fetched_at=track_fetched_at, page_kind=track_kind,
                            )
                            track_parsed = (
                                rows, track_url, track_content, track_fetched_at, track_content_type
                            )
                            break
                        except Exception as exc:
                            track_errors.append(f"{track_url}: {exc}")
                    if track_parsed is None:
                        raise RuntimeError(
                            f"No EDBT {year} {track_kind} PC source parsed: "
                            + " | ".join(track_errors)
                        )
                    rows, track_url, track_content, track_fetched_at, track_content_type = track_parsed
                    track_records.extend(rows)
                    save_source(
                        conn, year=year, url=track_url, content=track_content,
                        fetched_at=track_fetched_at, content_type=track_content_type or "text/html",
                    )

                records = merge_edbt_records(records, officer_records, track_records)
                previous, inserted = replace_year_records(
                    conn,
                    year=year,
                    records=records,
                    allow_qualifying_loss=args.allow_qualifying_loss,
                )
                print(
                    f"  parsed {committee_count} committee records + "
                    f"{len(officer_records)} officer records + "
                    f"{len(track_records)} technical-PC records; replaced {previous} old rows "
                    f"with {inserted} rows"
                )
            except Exception as exc:
                conn.rollback()  # Do not commit partial source metadata with a later year.
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
