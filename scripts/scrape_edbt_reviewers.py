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
--include-chairs to include research PC chairs.

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
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup, NavigableString, Tag
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
    2023: "https://www.cs.uoi.gr/~edbticdt2023/",
    2024: "https://dastlab.github.io/edbticdt2024/",
    2025: "https://edbticdt2025.upc.edu/",
    2026: "https://edbticdt2026.github.io/",
}

EDBT_COMMITTEE_URLS = {
    2022: [
        "https://conferences.inf.ed.ac.uk/edbticdt2022/contents/EDBT_Committee.html",
    ],
    2023: [
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

MIN_NEWER_RESEARCH_PC = 20

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
    # Official EDBT 2022 committee page: 6 Senior PC + 35 PC members.
    2022: {
        "senior_program_committee_member": 6,
        "program_committee_member": 35,
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
    source_page: int | None
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
    seen: set[tuple[str, str]] = set()
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


def html_visible_lines(html: bytes) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    for br in soup.find_all("br"):
        br.replace_with("\n")
    return [
        clean_line(line)
        for line in soup.get_text("\n").splitlines()
        if clean_line(line)
    ]



EDBT_ORG_MARKERS = {
    "university", "universita", "università", "universite", "université", "universitat",
    "institute", "institut", "research", "center", "centre", "college", "school",
    "laboratory", "lab", "labs", "technology", "technical", "technological", "polytechnique", "engineering", "computing", "systems", "management", "corporation", "corp", "inc", "microsoft", "google",
    "amazon", "oracle", "alibaba", "ibm", "huawei", "meta", "snowflake", "eurecom",
    "inria", "epfl", "eth", "kaist", "qcri", "ntu", "nus", "unsw", "sap", "cwi",
    "dfki", "athena", "neo4j", "know-center", "hbku", "forth",
}
EDBT_COUNTRY_PHRASES = {
    "usa", "uk", "canada", "china", "germany", "france", "italy", "greece", "australia",
    "austria", "belgium", "denmark", "finland", "japan", "korea", "south korea",
    "netherlands", "norway", "singapore", "spain", "sweden", "switzerland", "turkey",
    "poland", "portugal", "qatar", "israel", "india", "ireland", "united states",
    "united kingdom", "hong kong", "hong kong sar",
}
EDBT_NAME_CONNECTORS = {"de", "del", "da", "dos", "van", "von", "der", "den", "la", "le", "bin", "al"}
EDBT_LOCATION_SUFFIXES = {"southern california", "california davis", "san diego", "los angeles", "new brunswick", "twin cities", "reutlingen", "mainz"}
EDBT_CORPORATE_NAME_TOKENS = {
    "ltd", "limited", "llc", "gmbh", "inc", "corp", "corporation",
    "company", "technologies", "technology", "research", "center", "centre",
}
EDBT_COUNTRY_WORDS = {
    word
    for phrase in EDBT_COUNTRY_PHRASES
    for word in phrase.split()
    if len(word) > 2
}


def _edbt_nameish(value: str) -> bool:
    value = clean_line(value).strip(" *")
    tokens = value.split()
    if not 2 <= len(tokens) <= 6:
        return False

    # A candidate produced by the collapsed-list splitter must look like a
    # complete personal name, not the tail of an affiliation.  This rejects
    # failures such as ``(UK) Ltd`` while preserving legitimate nickname forms
    # such as ``Tsz Nam (Edison) Chan``.
    if tokens[0].startswith("(") or "&" in value:
        return False

    normalized_words = [
        re.sub(r"[^a-zà-öø-ÿ]+", "", token.casefold())
        for token in tokens
    ]
    low_words = {word for word in normalized_words if word}
    if (
        low_words & EDBT_ORG_MARKERS
        or low_words & EDBT_CORPORATE_NAME_TOKENS
        or value.casefold() in EDBT_COUNTRY_PHRASES
        or value.casefold() in EDBT_LOCATION_SUFFIXES
    ):
        return False

    first_word = normalized_words[0] if normalized_words else ""
    if first_word in EDBT_COUNTRY_WORDS:
        return False
    if tokens[0].strip(".,").casefold() in EDBT_NAME_CONNECTORS:
        return False

    for token in tokens:
        core = token.strip("()[]{}.,;:'\"“”‘’")
        if not core:
            return False
        if core.casefold() in EDBT_NAME_CONNECTORS:
            continue
        if re.fullmatch(r"[A-Z]\.", core):
            continue
        if core[0].isupper():
            continue
        return False
    return plausible_name(value)


def _edbt_affiliation_score(value: str, *, prior_affiliation: str = "") -> int:
    value = clean_line(value).strip(" ,;:-")
    if not value:
        return 0
    low = value.casefold().strip(" .")
    last_word = re.sub(r"[^a-z]+", "", value.split()[-1].casefold()) if value.split() else ""
    if last_word in {"of", "and", "de", "di", "del", "at", "in"}:
        return 0
    if low in EDBT_COUNTRY_PHRASES:
        return 12
    words = re.findall(r"[A-Za-zÀ-ÖØ-öø-ÿ0-9.+&/-]+", value)
    lows = {w.casefold().strip(".") for w in words}
    if len(words) == 1 and (words[0].isupper() or low in EDBT_ORG_MARKERS):
        return 11
    score = 0
    if lows & EDBT_ORG_MARKERS:
        score += 7
    if any(len(w) >= 2 and w.isupper() for w in words):
        score += 5
    if any(ch.isdigit() for ch in value) or any(ch in value for ch in "/&.-"):
        score += 3
    if prior_affiliation and _edbt_affiliation_score(prior_affiliation) >= 5 and len(words) <= 3:
        score += 4
    return score


def _edbt_name_suffix(value: str, *, prior_affiliation: str = "") -> tuple[str, str] | None:
    value = clean_line(value).strip()
    tokens = value.split()
    best: tuple[int, str, str] | None = None
    for n in range(2, min(6, len(tokens)) + 1):
        name = " ".join(tokens[-n:]).strip(" *")
        prefix = " ".join(tokens[:-n]).strip()
        if not prefix or not _edbt_nameish(name):
            continue
        aff_score = _edbt_affiliation_score(prefix, prior_affiliation=prior_affiliation)
        if aff_score < 4:
            continue
        # Once the prefix is clearly an affiliation, prefer the longest valid
        # personal-name suffix.  The old 2-token preference truncated names
        # such as ``Björn Þ Jónsson`` and ``Tsz Nam (Edison) Chan``.
        score = min(aff_score, 8) * 100 + n
        if best is None or score > best[0]:
            best = (score, prefix, name)
    if best is None:
        return None
    return best[1], best[2]


def parse_edbt_collapsed_people(text: str) -> list[tuple[str, str]]:
    """Split a flattened official EDBT committee section into person rows."""
    text = clean_line(text)
    text = re.sub(r"\s+NOTE:\s+PC members annotated.*$", "", text, flags=re.I)
    chunks = [clean_line(x) for x in text.split(",")]
    if len(chunks) < 2:
        return []
    current_name = chunks[0].strip(" *")
    current_aff: list[str] = []
    rows: list[tuple[str, str]] = []
    for chunk in chunks[1:]:
        prior = clean_line(", ".join(current_aff))
        split = _edbt_name_suffix(chunk, prior_affiliation=prior)
        if split is None:
            current_aff.append(chunk)
            continue
        aff_piece, next_name = split
        if aff_piece:
            current_aff.append(aff_piece)
        affiliation = clean_line(", ".join(x for x in current_aff if x)).strip(" ,")
        if _edbt_nameish(current_name):
            rows.append((current_name.strip(" *"), affiliation))
        current_name = next_name.strip(" *")
        current_aff = []
    affiliation = clean_line(", ".join(x for x in current_aff if x)).strip(" ,")
    if _edbt_nameish(current_name):
        rows.append((current_name.strip(" *"), affiliation))
    return rows


def iter_edbt_heading_blocks(soup: BeautifulSoup):
    """Yield (role, raw text) for content between EDBT committee headings."""
    headings = soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6"])
    for h in headings:
        role = html_committee_role(clean_line(h.get_text(" ", strip=True)))
        if role is None:
            continue
        parts: list[str] = []
        for node in h.next_siblings:
            if isinstance(node, Tag) and node.name in {"h1", "h2", "h3", "h4", "h5", "h6"}:
                break
            if isinstance(node, NavigableString):
                t = clean_line(str(node))
            elif isinstance(node, Tag):
                t = clean_line(" ".join(node.stripped_strings))
            else:
                t = ""
            if t:
                parts.append(t)
        text = clean_line(" ".join(parts))
        if text:
            yield role, text

def parse_edbt_committee_html(
    html: bytes,
    *,
    year: int,
    source_url: str,
    fetched_at: str,
    include_chairs: bool = False,
) -> list[ReviewerRecord]:
    """Parse research-PC rows from an official yearly EDBT committee page.

    Yearly sites use a mixture of lists, paragraphs, and HTML tables.  Parse
    semantic blocks first (including table cells) and then use visible lines as
    a fallback.  Sanity guards prevent a collapsed page from silently replacing
    a complete local year.
    """
    soup = BeautifulSoup(html, "html.parser")
    for br in soup.find_all("br"):
        br.replace_with("\n")

    records: list[ReviewerRecord] = []
    seen: set[tuple[str, str, str]] = set()
    current_role: str | None = None
    entered = False
    stopped = False

    def add_row(role: str, name: str, affiliation: str, raw: str) -> None:
        if role == "program_committee_chair" and not include_chairs:
            return
        name = clean_line(name).strip(" \t\"'“”")
        affiliation = clean_line(affiliation).strip(" \t\"'“”")
        if not plausible_name(name):
            return
        key = (role, normalize_name_key(name))
        if key in seen:
            return
        seen.add(key)
        records.append(
            ReviewerRecord(
                year=year,
                role=role,
                name=name,
                affiliation=affiliation,
                name_key=normalize_name_key(name),
                raw_text=clean_line(raw),
                source_page=None,
                source_url=source_url,
                fetched_at=fetched_at,
            )
        )

    # Preferred pass for the 2022+ yearly sites. Their raw HTML often
    # concatenates the visually separate people into one section-level string.
    for block_role, block_text in iter_edbt_heading_blocks(soup):
        entered = True
        for name, affiliation in parse_edbt_collapsed_people(block_text):
            add_row(block_role, name, affiliation, block_text)

    # First pass: semantic HTML blocks.  Table rows are handled specially so
    # layouts with separate Name / Affiliation / Country cells remain parseable.
    for node in soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6", "tr", "li", "p"]):
        text = clean_line(node.get_text(" ", strip=True))
        if not text:
            continue

        role = html_committee_role(text)
        if role is not None:
            current_role = role
            entered = True
            continue
        if is_html_research_umbrella(text):
            entered = True
            current_role = None
            continue
        if entered and is_html_stop_heading(text):
            if records:
                stopped = True
                break
            current_role = None
            continue
        if not entered or current_role is None:
            continue

        if node.name == "tr":
            cells = [
                clean_line(cell.get_text(" ", strip=True))
                for cell in node.find_all(["td", "th"], recursive=False)
                if clean_line(cell.get_text(" ", strip=True))
            ]
            if len(cells) >= 2 and plausible_name(cells[0]):
                add_row(current_role, cells[0], ", ".join(cells[1:]), text)
                continue

        parsed = parse_person_line(text)
        if parsed is not None:
            name, affiliation, role_override = parsed
            add_row(role_override or current_role, name, affiliation, text)

    # Second pass: visible lines. Useful for older layouts that use plain text
    # and <br> separators rather than semantic list/table rows.
    if not stopped:
        current_role = None
        entered_lines = False
        for line in [clean_line(x) for x in soup.get_text("\n").splitlines() if clean_line(x)]:
            role = html_committee_role(line)
            if role is not None:
                current_role = role
                entered_lines = True
                continue
            if is_html_research_umbrella(line):
                entered_lines = True
                current_role = None
                continue
            if entered_lines and is_html_stop_heading(line):
                if records:
                    break
                current_role = None
                continue
            if not entered_lines or current_role is None:
                continue
            parsed = parse_person_line(line)
            if parsed is None:
                continue
            name, affiliation, role_override = parsed
            add_row(role_override or current_role, name, affiliation, line)
        entered = entered or entered_lines

    if not entered:
        raise ValueError("No EDBT research program-committee heading found")
    if not records:
        raise ValueError("EDBT research committee heading found, but no reviewer rows parsed")

    actual: dict[str, int] = {}
    for record in records:
        actual[record.role] = actual.get(record.role, 0) + 1

    expected = KNOWN_RESEARCH_PC_COUNTS.get(year)
    if expected:
        mismatches = [
            f"{role}: expected {count}, parsed {actual.get(role, 0)}"
            for role, count in expected.items()
            if actual.get(role, 0) != count
        ]
        if mismatches:
            raise ValueError("EDBT committee count sanity check failed: " + "; ".join(mismatches))
    elif sum(actual.get(role, 0) for role in (
        "senior_program_committee_member", "program_committee_member"
    )) < MIN_NEWER_RESEARCH_PC:
        raise ValueError(
            f"Only {len(records)} EDBT research-PC rows parsed from {source_url}; "
            f"expected at least {MIN_NEWER_RESEARCH_PC}."
        )
    return records


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
    parser.add_argument(
        "--years", nargs="+", type=int, default=None,
        help="Years to collect. Omit for 2022 through the last completed calendar year; explicit values override the default.",
    )
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--include-chairs", action="store_true")
    parser.add_argument("--delay", type=float, default=1.0)
    parser.add_argument(
        "--source",
        action="append",
        default=[],
        metavar="YEAR=URL",
        help="Override the official committee page/PDF for a year; may be repeated.",
    )
    parser.add_argument(
        "--frontmatter",
        action="append",
        default=[],
        metavar="YEAR=URL",
        help=argparse.SUPPRESS,  # backwards-compatible alias for older PDF runs
    )
    parser.add_argument("--ignore-robots", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    overrides = parse_overrides(args.frontmatter)
    overrides.update(parse_overrides(args.source))
    years = sorted(set(args.years if args.years is not None else default_years()))

    session = requests.Session()
    session.headers.update({"User-Agent": DEFAULT_UA})
    conn = open_db(args.db)
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
                            include_chairs=args.include_chairs,
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
                previous, inserted = replace_year_records(
                    conn,
                    year=year,
                    records=records,
                )
                print(
                    f"  parsed {len(records)} records; replaced {previous} old rows "
                    f"with {inserted} rows"
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
