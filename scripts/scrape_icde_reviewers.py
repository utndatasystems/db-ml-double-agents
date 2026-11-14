#!/usr/bin/env python3
"""Scrape public IEEE ICDE research-service pages into SQLite.

The scraper intentionally separates *page parsing* from *study inclusion*.
It collects research-review and qualifying scientific leadership roles from
ICDE's official research-PC and organizing/officer pages; ``service_roles.py``
then maps those raw roles to normalized role families used by the study.

The parser is structural for normal ICDE pages: it recognizes role section
labels and walks the rows beneath each section across lists, tables, ``<br>``
blocks, bold names, and split name/affiliation cells.  The archived 2022
research-PC page has one narrow layout adapter because its entire ordinary-PC
roster is dumped into a single Gutentor paragraph; that adapter uses the
page's own headings, ``<br>`` boundaries, ``<strong>`` names, and ``<em>``
affiliations rather than historical person manifests or expected counts.

When --years is omitted, the scraper collects 2022 through the last completed
calendar year. Use --source/--officer-source to point a year at a replacement
official page if a conference site moves.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import sqlite3
import sys
import time
import warnings
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator, Sequence
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup, Comment, NavigableString, Tag
from urllib3.exceptions import InsecureRequestWarning

from scraper_common import clean_line, normalize_name_key

from service_roles import (
    assert_no_qualifying_person_loss,
    ensure_reviewer_service_schema,
    service_values,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = REPO_ROOT / "data" / "icde_reviewers.sqlite"
DEFAULT_START_YEAR = 2022
DEFAULT_UA = os.environ.get("ICDE_SCRAPER_UA", "icde-reviewer-research/1.3")

# These are source locations, not data fixtures. Conference websites move and
# therefore need an explicit source map for historical reproducibility.
DEFAULT_SOURCES = {
    2022: "https://icde2022.ieeecomputer.my/research-program-committee/",
    2023: "https://icde2023.ics.uci.edu/research-program-committee/",
    2024: "https://icde2024.github.io/CFP_research.html",
    2025: "https://ieee-icde.org/2025/program-committees/",
    2026: "https://icde2026.github.io/pc-chairs.html",
}
ICDE_OFFICER_SOURCES = {
    2022: "https://icde2022.ieeecomputer.my/organizing-committee/",
    2023: "https://icde2023.ics.uci.edu/organizing-committee/",
    2024: "https://icde2024.github.io/conference_officers.html",
    2025: "https://ieee-icde.org/2025/committees/",
}
# Separate conference-level technical-track PC pages. These are additive
# service sources: a missing historical page should not invalidate the main
# research-PC scrape for that year.
ICDE_TECHNICAL_PC_SOURCES = {
    2022: {
        "industrial": "https://icde2022.ieeecomputer.my/industry-track-committee/",
        "demo": "https://icde2022.ieeecomputer.my/demonstration-track-committee/",
    },
    2024: {
        "industrial": "https://icde2024.github.io/CFP_industry.html",
        "demo": "https://icde2024.github.io/CFP_demos.html",
    },
    2025: {
        "industrial": "https://ieee-icde.org/2025/call-for-industry-and-application/",
        "demo": "https://ieee-icde.org/2025/call-for-demonstrations/",
    },
}
# Some years publish the ordinary reviewer roster separately. This is an
# additional official source to merge if the main PC page is structurally
# incomplete; it is not a hard-coded person list.
AUXILIARY_REVIEWER_SOURCES = {
    2022: "https://icde2022.ieeecomputer.my/reviewers/",
}
FALLBACK_SOURCES = {
    2026: "https://raw.githubusercontent.com/icde2026/icde2026.github.io/main/pc-chairs.html",
}

# The archived 2023 host currently serves an incomplete certificate chain.
INSECURE_TLS_HOSTS = {"icde2023.ics.uci.edu"}

MIN_EXPECTED_RESEARCH_REVIEWERS = 20
MAX_PLAUSIBLE_CHAIRS_PER_ROLE = 20
MAX_PLAUSIBLE_OFFICER_ROWS = 100


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


# ---------------------------------------------------------------------------
# Role labels
# ---------------------------------------------------------------------------

RESEARCH_ROLE_PATTERNS: Sequence[tuple[re.Pattern[str], str]] = (
    (re.compile(r"^(?:research\s+)?program\s+committee\s+(?:co-?)?chairs?\s*:?$", re.I), "program_committee_chair"),
    (re.compile(r"^(?:research\s+)?pc\s+(?:co-?)?chairs?\s*:?$", re.I), "program_committee_chair"),
    (re.compile(r"^(?:co-?)?chairs?\s*:?$", re.I), "program_committee_chair"),
    (re.compile(r"^associate\s+chairs?\s*:?$", re.I), "area_chair"),
    (re.compile(r"^(?:research\s+)?area\s+chairs?\s*:?$", re.I), "area_chair"),
    (re.compile(r"^(?:research\s+)?(?:pc|program\s+committee)\s+members?\s*:?$", re.I), "program_committee_member"),
    (re.compile(r"^program\s+committee\s*:?$", re.I), "program_committee_member"),
)

OFFICER_ROLE_PATTERNS: Sequence[tuple[re.Pattern[str], str]] = (
    (re.compile(r"^general\s+(?:co-?)?chairs?\s*:?$", re.I), "general_chair"),
    (re.compile(r"^(?:research\s+)?(?:program\s+committee|pc)\s+(?:co-?)?chairs?\s*:?$", re.I), "program_committee_chair"),
    (re.compile(r"^industry\s+(?:and|&)\s+applications?(?:\s+track)?\s+(?:co-?)?chairs?\s*:?$", re.I), "industrial_track_chair"),
    (re.compile(r"^(?:demo|demonstration)s?(?:\s+track)?\s+(?:co-?)?chairs?\s*:?$", re.I), "demonstration_chair"),
    (re.compile(r"^tutorial(?:\s+track)?\s+(?:co-?)?chairs?\s*:?$", re.I), "tutorial_chair"),
    (re.compile(r"^workshop(?:\s+track)?\s+(?:co-?)?chairs?\s*:?$", re.I), "workshop_chair"),
    (re.compile(r"^panel(?:\s+track)?\s+(?:co-?)?chairs?\s*:?$", re.I), "panel_chair"),
    (re.compile(r"^phd\s+(?:symposium|workshop)(?:\s+track)?\s+(?:co-?)?chairs?\s*:?$", re.I), "phd_symposium_chair"),
    (re.compile(r"^special\s+session\s+data\s+engineering\s+future\s+technologies\s+(?:co-?)?chairs?\s*:?$", re.I), "special_session_chair"),
    (re.compile(r"^lightning\s+talks?(?:\s+track)?\s+(?:co-?)?chairs?\s*:?$", re.I), "lightning_talks_chair"),
    (re.compile(r"^review\s+quality\s+(?:co-?)?chairs?\s*:?$", re.I), "review_quality_chair"),
    (re.compile(r"^(?:best\s+paper\s+)?awards?\s+committee(?:\s*\([^)]*\))?\s*:?$", re.I), "award_committee_member"),
)

# Unrecognized chair/committee labels end the current role section. This is
# what prevents administrative sections (web, finance, registration, etc.)
# from bleeding into the preceding scientific role without enumerating every
# possible administrative title.
GENERIC_ROLE_BOUNDARY_RE = re.compile(
    r"\b(?:chairs?|committee|organizing\s+committee|conference\s+officers?)\b",
    re.I,
)

HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6", "th"}


def default_years() -> list[int]:
    last_complete_year = datetime.now(timezone.utc).year - 1
    if last_complete_year < DEFAULT_START_YEAR:
        return []
    return list(range(DEFAULT_START_YEAR, last_complete_year + 1))




def _role_for_text(text: str, patterns: Sequence[tuple[re.Pattern[str], str]]) -> str | None:
    value = clean_line(text)
    for pattern, role in patterns:
        if pattern.fullmatch(value):
            return role
    return None


def research_role_for_text(text: str) -> str | None:
    return _role_for_text(text, RESEARCH_ROLE_PATTERNS)


def officer_role_for_text(text: str) -> str | None:
    return _role_for_text(text, OFFICER_ROLE_PATTERNS)


def _looks_headingish_tag(node: Tag) -> bool:
    if node.name in HEADING_TAGS:
        return True
    tokens: list[str] = []
    for attr in ("class", "id"):
        value = node.get(attr, [])
        if isinstance(value, str):
            tokens.append(value)
        else:
            tokens.extend(str(x) for x in value)
    label = " ".join(tokens).casefold()
    return any(word in label for word in ("title", "heading", "header"))


BLOCK_BOUNDARY_TAGS = {"p", "li", "div", "td", "tr", "section", "article", "main"}


def _text_node_is_headingish(parent: Tag) -> bool:
    """Return whether a text node belongs to its *local* heading container.

    Conference CMSes often put the entire page inside wrappers whose class
    contains words such as ``title`` or ``header``.  Treating every descendant
    of such a wrapper as a heading makes real committee rows disappear.  Walk
    only through inline wrappers; the first block container owns the token.
    """
    ancestor: Tag | None = parent
    while isinstance(ancestor, Tag):
        if _looks_headingish_tag(ancestor):
            return True
        if ancestor.name in BLOCK_BOUNDARY_TAGS:
            return False
        ancestor = ancestor.parent if isinstance(ancestor.parent, Tag) else None
    return False


NON_CONTENT_TAGS = {"script", "style", "noscript", "form", "nav", "aside", "footer"}


def _iter_text_tokens(soup: BeautifulSoup) -> Iterator[tuple[str, bool]]:
    """Yield visible semantic text tokens once, in DOM order.

    This intentionally uses BeautifulSoup's native text-node order instead of
    trying to emulate browser layout. ICDE pages across years use very
    different markup, but their semantic content is consistently ordered:
    role label, then people. ``<br>`` separated rows naturally become separate
    text nodes; inline ``<strong>`` name markup is handled by the pending-name
    state in ``_parse_role_sections``.

    Semantic browser-chrome tags and HTML comments are excluded before
    role-state parsing. Avoid generic class/id filtering here: archived
    conference themes reuse labels such as ``sidebar`` and ``modal`` inside
    containers that can also hold real committee content. Source-specific
    scoping belongs in the source-specific parser instead.
    """
    for tag in soup(list(NON_CONTENT_TAGS)):
        tag.decompose()

    for node in soup.find_all(string=True):
        if isinstance(node, Comment) or not isinstance(node, NavigableString):
            continue
        parent = node.parent
        if parent is None or parent.name in NON_CONTENT_TAGS:
            continue

        headingish = _text_node_is_headingish(parent)
        for part in str(node).replace("\xa0", " ").splitlines():
            text = clean_line(part)
            if text:
                yield text, headingish


def _strip_bullet(value: str) -> str:
    return re.sub(r"^[•▪●○\-*]+\s*", "", clean_line(value)).strip()


# These words are generic page/organization vocabulary that should never be
# interpreted as a person's bare name. They are intentionally conference- and
# year-agnostic.
NAME_BLOCK_WORDS = {
    "university", "institute", "research", "center", "centre", "school", "college",
    "laboratory", "laboratories", "technology", "technologies", "group", "department",
    "science", "sciences", "engineering", "analytics", "intelligence", "data",
    "conference", "committee", "program", "programme", "track", "chair", "chairs",
    "overview", "information", "dates", "guidelines", "submission", "submissions",
    "review", "reviewing", "process", "conflict", "interest", "topics", "organization",
    "organisation", "calendar", "disclaimer", "copyright", "round", "security", "privacy",
    "database", "query", "processing", "management", "call", "papers", "paper",
    "local", "arrangements", "publicity", "publication", "publications", "sponsorship",
    "registration", "finance", "financial", "diversity", "inclusion", "volunteer",
    "evangelist", "webmaster", "content",
    # Common non-English organization words seen on international conference
    # pages. They prevent a split affiliation from becoming a person's name.
    "universite", "université", "universita", "università", "universidad",
    "universidade", "universitat", "universiteit",
}


def plausible_person_name(value: str, *, allow_unusual_case: bool = False) -> bool:
    """Conservative, generic person-name check.

    A comma-delimited ``Name, Affiliation`` row provides strong structure, so
    capitalization is deliberately permissive there. Bare text is stricter to
    avoid treating page headings or affiliations as people.
    """
    value = _strip_bullet(value).strip(" ,;:\"'“”‘’")
    if not value or len(value) > 140 or "," in value or ":" in value:
        return False
    if re.search(r"(?:https?://|www\.|@|\d{3,})", value, re.I):
        return False

    words = value.split()
    if not 2 <= len(words) <= 8:
        return False

    lowered = [w.strip(".,;:()[]{}\"'“”‘’").casefold() for w in words]
    if any(w in NAME_BLOCK_WORDS for w in lowered):
        return False
    if GENERIC_ROLE_BOUNDARY_RE.search(value):
        return False

    lexical = []
    for raw in words:
        token = raw.strip(".,;:()[]{}\"'“”‘’")
        low = token.casefold()
        if not token:
            continue
        if not any(ch.isalpha() for ch in token):
            return False
        lexical.append(token)
    if len(lexical) < 2:
        return False

    if allow_unusual_case:
        return True

    # Bare names should look name-like. Initials, all-caps initials, accented
    # names, hyphenated names and ordinary title case are all accepted.
    good = 0
    for token in lexical:
        if re.fullmatch(r"[A-Z](?:\.)?", token):
            good += 1
        elif re.fullmatch(r"[A-Z]{2,4}", token):
            good += 1
        elif token[0].isupper():
            good += 1
    return good >= max(1, len(lexical) - 1)


def parse_person_line(line: str, *, allow_name_only: bool = True) -> list[tuple[str, str]]:
    """Parse one person row without guessing across malformed row boundaries.

    If a CMS accidentally concatenates two people into one text node, this
    function keeps the first well-formed ``Name, Affiliation`` row rather than
    heuristically inventing a split. Losing an occasional malformed row is
    preferable to manufacturing dozens of false people from institution names.
    """
    raw = _strip_bullet(line).strip()
    if not raw or raw.casefold() in {"image", "photo"}:
        return []

    # Normalize common smart-quote artifacts around CSV-like page text.
    raw = raw.replace(",”", ",").replace(",“", ",").replace("”", "").replace("“", "")

    if "," in raw:
        name, affiliation = raw.split(",", 1)
        name = clean_line(name).strip(" ,;:\"'“”‘’")
        affiliation = clean_line(affiliation).strip(" ,;:-–—\"'“”‘’")
        if plausible_person_name(name, allow_unusual_case=True) and affiliation:
            return [(name, affiliation)]
        # Some WordPress person cards put ``Name,`` in a bold/span node and
        # the affiliation in the following text node. Preserve the trailing
        # comma as a structural cue instead of dropping the person.
        if allow_name_only and not affiliation and raw.rstrip().endswith(","):
            if plausible_person_name(name, allow_unusual_case=True):
                return [(name, "")]
        return []

    if allow_name_only and plausible_person_name(raw):
        return [(raw, "")]
    return []


def _starts_generic_boundary(text: str) -> bool:
    value = clean_line(text).rstrip(":")
    return bool(value and len(value) <= 160 and GENERIC_ROLE_BOUNDARY_RE.search(value))


def _parse_role_sections(
    html: bytes,
    *,
    year: int,
    source_url: str,
    fetched_at: str,
    patterns: Sequence[tuple[re.Pattern[str], str]],
    allowed_roles: set[str],
) -> list[ReviewerRecord]:
    """Parse role-labelled sections from heterogeneous conference HTML.

    The state machine is intentionally small: exact role labels open a section;
    another heading/role label closes it; text inside the section is parsed as
    either ``Name, Affiliation`` or as a split name/affiliation pair.
    """
    soup = BeautifulSoup(html, "html.parser")

    records: list[ReviewerRecord] = []
    seen: set[tuple[str, str, str]] = set()
    current_role: str | None = None
    pending_name: str | None = None
    pending_raw = ""

    def add(role: str, name: str, affiliation: str, raw: str) -> None:
        if role not in allowed_roles:
            return
        name = clean_line(name).strip(" ,;:\"'“”‘’")
        affiliation = clean_line(affiliation).strip(" ,;:-–—\"'“”‘’")
        if not plausible_person_name(name, allow_unusual_case=bool(affiliation)):
            return
        key = (role, normalize_name_key(name), clean_line(affiliation).casefold())
        if key in seen:
            return
        seen.add(key)
        records.append(
            ReviewerRecord(
                year=year,
                role=role,
                name=name,
                affiliation=affiliation,
                name_key=key[1],
                raw_text=clean_line(raw),
                source_url=source_url,
                fetched_at=fetched_at,
            )
        )

    def flush_pending() -> None:
        nonlocal pending_name, pending_raw
        if pending_name is not None and current_role is not None:
            add(current_role, pending_name, "", pending_raw or pending_name)
        pending_name = None
        pending_raw = ""

    for text, headingish in _iter_text_tokens(soup):
        role = _role_for_text(text, patterns)
        if role is not None:
            flush_pending()
            current_role = role
            continue

        # Explicit role-like labels always end the active section, including
        # administrative roles we deliberately do not collect.
        if _starts_generic_boundary(text):
            flush_pending()
            current_role = None
            continue

        if current_role is None:
            continue

        value = _strip_bullet(text)
        if not value or value.casefold() in {"image", "photo"}:
            continue

        # Parse a structurally valid person row before considering heading
        # metadata. Some conference templates render person cards in h4/h5 or
        # title-styled elements, so markup alone must not discard a good row.
        full_rows = parse_person_line(value, allow_name_only=False)
        if full_rows:
            flush_pending()
            for name, affiliation in full_rows:
                add(current_role, name, affiliation, value)
            continue

        # HTML often renders <strong>Name</strong>, Affiliation as two text
        # nodes. A pending bare name consumes the next non-person token as its
        # affiliation; a second person row flushes the first as affiliationless.
        if pending_name is not None:
            if value.startswith(","):
                add(current_role, pending_name, value.lstrip(" ,;:-–—"), f"{pending_name}{value}")
                pending_name = None
                pending_raw = ""
                continue
            if not plausible_person_name(value):
                # A true heading closes the section rather than becoming an
                # affiliation for the preceding bare name.
                if headingish:
                    flush_pending()
                    current_role = None
                    continue
                add(current_role, pending_name, value, f"{pending_name}, {value}")
                pending_name = None
                pending_raw = ""
                continue
            flush_pending()

        name_only = parse_person_line(value, allow_name_only=True)
        if name_only:
            pending_name = name_only[0][0]
            pending_raw = value
            continue

        if headingish:
            flush_pending()
            current_role = None

    flush_pending()
    return records

def _parse_icde_2022_gutentor_research_page(
    html: bytes,
    *,
    year: int,
    source_url: str,
    fetched_at: str,
    include_chairs: bool = True,
) -> list[ReviewerRecord]:
    """Parse the archived ICDE 2022 Gutentor research-PC layout.

    That page uses normal Gutentor person cards for the chair sections, but the
    ordinary PC roster is dumped into one ``p.gutentor-single-item-title`` with
    ``<br>`` separators. Treat each Gutentor paragraph as a record container
    and each ``<br>``-delimited fragment inside it as one row. The current role
    is the latest recognized heading encountered in DOM order.

    This is deliberately layout-specific rather than person-specific: there
    are no historical name manifests, expected counts, or named repairs here.
    """
    soup = BeautifulSoup(html, "html.parser")
    allowed_roles = {"area_chair", "program_committee_member"}
    if include_chairs:
        allowed_roles.add("program_committee_chair")

    records: list[ReviewerRecord] = []
    seen: set[tuple[str, str, str]] = set()
    current_role: str | None = None

    def add(name: str, affiliation: str, raw: str) -> None:
        if current_role not in allowed_roles:
            return
        cleaned_name = clean_line(name).strip(' ,;:"\'“”‘’')
        cleaned_affiliation = clean_line(affiliation).strip(' ,;:-–—"\'“”‘’')
        # Malformed inline markup can split a one-character suffix from a
        # surname (for example ``Zho</strong>u``). Rejoin only that structural
        # artifact; this does not depend on any person's identity.
        cleaned_name = re.sub(
            r"\b([A-Za-zÀ-ÖØ-öø-ÿ]{2,})\s+([a-z])\b",
            r"\1\2",
            cleaned_name,
        )
        if not plausible_person_name(
            cleaned_name,
            allow_unusual_case=bool(cleaned_affiliation),
        ):
            return
        key = (current_role, normalize_name_key(cleaned_name), cleaned_affiliation.casefold())
        if key in seen:
            return
        seen.add(key)
        records.append(
            ReviewerRecord(
                year=year,
                role=current_role,
                name=cleaned_name,
                affiliation=cleaned_affiliation,
                name_key=key[1],
                raw_text=clean_line(raw),
                source_url=source_url,
                fetched_at=fetched_at,
            )
        )

    # Select headings and Gutentor person containers together so the latest
    # recognized heading naturally owns the following person rows.
    selector = (
        ".wp-block-heading, h1, h2, h3, h4, h5, h6, "
        "p.gutentor-single-item-title"
    )
    for element in soup.select(selector):
        classes = element.get("class", [])
        is_person_container = (
            element.name == "p" and "gutentor-single-item-title" in classes
        )

        if not is_person_container:
            heading = clean_line(element.get_text(" ", strip=True))
            role = research_role_for_text(heading)
            if role is not None:
                current_role = role
            elif _starts_generic_boundary(heading):
                current_role = None
            continue

        if current_role not in allowed_roles:
            continue

        # ICDE 2022's long PC roster is a single paragraph. Splitting its raw
        # inner HTML on <br> preserves row boundaries even when <strong> tags
        # are oddly nested across those breaks.
        chunks = re.split(r"<br\s*/?>", element.decode_contents(), flags=re.I)
        for chunk_html in chunks:
            fragment = BeautifulSoup(chunk_html, "html.parser")
            raw_text = clean_line(fragment.get_text(" ", strip=True))
            if not raw_text:
                continue

            em_nodes = fragment.find_all("em")
            affiliation = clean_line(
                " ".join(node.get_text(" ", strip=True) for node in em_nodes)
            )
            for node in em_nodes:
                node.decompose()
            name = clean_line(fragment.get_text(" ", strip=True)).strip(" ,;:-–—")
            if name:
                add(name, affiliation, raw_text)

    return records


def merge_icde_records(*groups: Iterable[ReviewerRecord]) -> list[ReviewerRecord]:
    """Collapse repeated observations while preserving distinct affiliations.

    A blank label can be enriched by a nonempty label for the same year,
    role and name. Different nonempty labels remain separate evidence.
    """
    rows = [record for group in groups for record in group]
    with_affiliation = {(r.year, r.role, r.name_key) for r in rows if clean_line(r.affiliation)}
    out: list[ReviewerRecord] = []
    seen: set[tuple[int, str, str, str]] = set()
    for record in rows:
        subject = (record.year, record.role, record.name_key)
        affiliation = clean_line(record.affiliation).casefold()
        if not affiliation and subject in with_affiliation:
            continue
        key = (*subject, affiliation)
        if key not in seen:
            seen.add(key)
            out.append(record)
    return out


def merge_research_sources(
    primary: Sequence[ReviewerRecord],
    auxiliary_reviewers: Sequence[ReviewerRecord] | None = None,
) -> list[ReviewerRecord]:
    """Merge official research sources with explicit role ownership.

    A dedicated reviewer-only page is authoritative for ordinary PC members;
    the main committee page remains authoritative for program/area chairs.
    This prevents parser artifacts from being unioned into the PC population
    while still allowing multiple official pages to complement each other.
    """
    if auxiliary_reviewers is None:
        return merge_icde_records(primary)
    non_pc = [r for r in primary if r.role != "program_committee_member"]
    return merge_icde_records(non_pc, auxiliary_reviewers)


def validate_research_records(records: Sequence[ReviewerRecord], source_url: str) -> None:
    counts: dict[str, int] = {}
    for record in records:
        counts[record.role] = counts.get(record.role, 0) + 1
    if counts.get("area_chair", 0) < 1:
        raise ValueError(f"No ICDE Area/Associate Chair section was parsed from {source_url}")
    if counts.get("program_committee_member", 0) < MIN_EXPECTED_RESEARCH_REVIEWERS:
        raise ValueError(
            f"Only {counts.get('program_committee_member', 0)} ICDE PC members were parsed "
            f"from {source_url}; expected at least {MIN_EXPECTED_RESEARCH_REVIEWERS}. "
            "The page layout may have changed."
        )


def validate_officer_records(records: Sequence[ReviewerRecord], source_url: str) -> None:
    counts: dict[str, int] = {}
    for record in records:
        counts[record.role] = counts.get(record.role, 0) + 1
    missing = [role for role in ("general_chair", "program_committee_chair") if counts.get(role, 0) == 0]
    if missing:
        raise ValueError(
            f"ICDE officer page {source_url} is missing required role(s): {', '.join(missing)}"
        )
    oversized = {
        role: count for role, count in counts.items()
        if count > MAX_PLAUSIBLE_CHAIRS_PER_ROLE
    }
    if oversized or len(records) > MAX_PLAUSIBLE_OFFICER_ROWS:
        detail = ", ".join(f"{role}={count}" for role, count in sorted(oversized.items()))
        raise ValueError(
            f"ICDE officer parse looks implausibly large ({detail or len(records)}); "
            "possible section bleed after a site-layout change."
        )


def parse_icde_page(
    html: bytes,
    *,
    year: int,
    source_url: str,
    fetched_at: str,
    include_chairs: bool = True,
    validate: bool = True,
) -> list[ReviewerRecord]:
    allowed = {"area_chair", "program_committee_member"}
    if include_chairs:
        allowed.add("program_committee_chair")
    if year == 2022:
        records = _parse_icde_2022_gutentor_research_page(
            html,
            year=year,
            source_url=source_url,
            fetched_at=fetched_at,
            include_chairs=include_chairs,
        )
    else:
        records = _parse_role_sections(
            html,
            year=year,
            source_url=source_url,
            fetched_at=fetched_at,
            patterns=RESEARCH_ROLE_PATTERNS,
            allowed_roles=allowed,
        )
    if validate:
        validate_research_records(records, source_url)
    return records


def parse_icde_reviewer_list_page(
    html: bytes,
    *,
    year: int,
    source_url: str,
    fetched_at: str,
    minimum_expected: int = MIN_EXPECTED_RESEARCH_REVIEWERS,
) -> list[ReviewerRecord]:
    """Parse a reviewer-only page that has no Area-Chair section.

    The structural parser is reused with a temporary ``Reviewers`` role label.
    """
    patterns = (
        (re.compile(r"^reviewers?\s*:?$", re.I), "program_committee_member"),
        (re.compile(r"^(?:research\s+)?(?:pc|program\s+committee)\s+members?\s*:?$", re.I), "program_committee_member"),
    )
    records = _parse_role_sections(
        html,
        year=year,
        source_url=source_url,
        fetched_at=fetched_at,
        patterns=patterns,
        allowed_roles={"program_committee_member"},
    )
    if len(records) < minimum_expected:
        raise ValueError(
            f"Only {len(records)} reviewer rows parsed from {source_url}; "
            f"expected at least {minimum_expected}."
        )
    return records


def _parse_icde_2022_gutentor_officers_page(
    html: bytes,
    *,
    year: int,
    source_url: str,
    fetched_at: str,
) -> list[ReviewerRecord]:
    """Parse the archived ICDE 2022 Gutentor organizing-committee page.

    That page is highly regular inside ``.entry-content``: a WordPress heading
    names the role, followed by a Gutentor list whose
    ``p.gutentor-single-item-title`` elements each represent exactly one
    person.  Keep this source-specific parser isolated to 2022; later ICDE
    editions continue to use the generic structural parser.

    Recognized scientific/technical headings open a role section.  Any other
    heading closes it, so administrative sections such as Publicity, Web,
    Publication, Finance, Registration, Sponsorship and Local Arrangement are
    ignored without needing a person or count manifest.
    """
    soup = BeautifulSoup(html, "html.parser")
    root = soup.select_one(".entry-content") or soup

    records: list[ReviewerRecord] = []
    seen: set[tuple[str, str, str]] = set()
    current_role: str | None = None

    for element in root.find_all(["h1", "h2", "h3", "h4", "h5", "h6", "p"]):
        if element.name in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            current_role = officer_role_for_text(element.get_text(" ", strip=True))
            continue

        if "gutentor-single-item-title" not in (element.get("class") or []):
            continue
        if current_role is None:
            continue

        strong = element.find("strong")
        if strong is None:
            continue
        name = clean_line(strong.get_text(" ", strip=True)).strip(" ,;:")
        if not plausible_person_name(name, allow_unusual_case=True):
            continue

        em = element.find("em")
        affiliation = clean_line(em.get_text(" ", strip=True)) if em is not None else ""
        affiliation = affiliation.strip(" ,;:-–—")

        key = (current_role, normalize_name_key(name), clean_line(affiliation).casefold())
        if key in seen:
            continue
        seen.add(key)
        records.append(
            ReviewerRecord(
                year=year,
                role=current_role,
                name=name,
                affiliation=affiliation,
                name_key=key[1],
                raw_text=clean_line(element.get_text(" ", strip=True)),
                source_url=source_url,
                fetched_at=fetched_at,
            )
        )

    return records


def parse_icde_officers_page(
    html: bytes,
    *,
    year: int,
    source_url: str,
    fetched_at: str,
) -> list[ReviewerRecord]:
    if year == 2022:
        records = _parse_icde_2022_gutentor_officers_page(
            html,
            year=year,
            source_url=source_url,
            fetched_at=fetched_at,
        )
    else:
        allowed = {role for _, role in OFFICER_ROLE_PATTERNS}
        records = _parse_role_sections(
            html,
            year=year,
            source_url=source_url,
            fetched_at=fetched_at,
            patterns=OFFICER_ROLE_PATTERNS,
            allowed_roles=allowed,
        )
    validate_officer_records(records, source_url)
    return records


TECHNICAL_PC_ROLE_PATTERNS = {
    "industrial": (
        (re.compile(r"^industry\s+(?:and|&)\s+applications?(?:\s+track)?\s+(?:co-?)?chairs?\s*:?$", re.I), "industrial_track_chair"),
        (re.compile(r"^(?:area\s+chairs?|reviewing\s+board|reviewers?|(?:program\s+committe(?:e)?|pc)(?:\s+members?)?)\s*:?$", re.I), "industrial_pc_member"),
    ),
    "demo": (
        (re.compile(r"^(?:demo|demonstration)s?(?:\s+track)?\s+(?:co-?)?chairs?\s*:?$", re.I), "demonstration_chair"),
        (re.compile(r"^(?:(?:program\s+committee|pc)(?:\s+members?)?|reviewers?)\s*:?$", re.I), "demo_pc_member"),
    ),
}


def _parse_icde_2022_wordpress_technical_pc_page(
    html: bytes,
    *,
    year: int,
    source_url: str,
    fetched_at: str,
    page_kind: str,
) -> list[ReviewerRecord]:
    """Parse the archived ICDE 2022 WordPress technical-PC list pages.

    The 2022 industry/demo pages are ordinary article lists: a role heading is
    followed by ``<li>`` person rows. Their site-wide sidebar appears after the
    article and previously inherited the last open PC role. Scope to the
    article's ``.entry-content`` when available and let any subsequent heading
    close the active role. This is intentionally isolated to 2022.
    """
    patterns = TECHNICAL_PC_ROLE_PATTERNS[page_kind]
    allowed_roles = {role for _, role in patterns}
    soup = BeautifulSoup(html, "html.parser")
    root = soup.select_one(".entry-content") or soup

    records: list[ReviewerRecord] = []
    seen: set[tuple[str, str, str]] = set()
    current_role: str | None = None

    for element in root.find_all(["h1", "h2", "h3", "h4", "h5", "h6", "li"]):
        if element.name != "li":
            current_role = _role_for_text(element.get_text(" ", strip=True), patterns)
            continue
        if current_role is None or current_role not in allowed_roles:
            continue

        raw = clean_line(element.get_text(" ", strip=True))
        rows = parse_person_line(raw, allow_name_only=False)
        for name, affiliation in rows:
            key = (current_role, normalize_name_key(name), clean_line(affiliation).casefold())
            if key in seen:
                continue
            seen.add(key)
            records.append(
                ReviewerRecord(
                    year=year,
                    role=current_role,
                    name=name,
                    affiliation=affiliation,
                    name_key=key[1],
                    raw_text=raw,
                    source_url=source_url,
                    fetched_at=fetched_at,
                )
            )

    return records


def parse_icde_technical_pc_page(
    html: bytes,
    *,
    year: int,
    source_url: str,
    fetched_at: str,
    page_kind: str,
) -> list[ReviewerRecord]:
    patterns = TECHNICAL_PC_ROLE_PATTERNS.get(page_kind)
    if patterns is None:
        raise ValueError(f"Unsupported ICDE technical-PC page kind: {page_kind}")
    allowed = {role for _, role in patterns}
    if year == 2022:
        records = _parse_icde_2022_wordpress_technical_pc_page(
            html,
            year=year,
            source_url=source_url,
            fetched_at=fetched_at,
            page_kind=page_kind,
        )
    else:
        records = _parse_role_sections(
            html,
            year=year,
            source_url=source_url,
            fetched_at=fetched_at,
            patterns=patterns,
            allowed_roles=allowed,
        )
    member_role = "industrial_pc_member" if page_kind == "industrial" else "demo_pc_member"
    if not any(record.role == member_role for record in records):
        raise ValueError(f"No {page_kind} PC members parsed from {source_url}")
    return records


# ---------------------------------------------------------------------------
# Networking and storage
# ---------------------------------------------------------------------------

def _verify_tls(url: str) -> bool:
    return urlparse(url).hostname not in INSECURE_TLS_HOSTS


def _session_get(session: requests.Session, url: str, **kwargs):
    verify = _verify_tls(url)
    if not verify:
        print(
            f"  warning: disabling TLS certificate verification for archived official host "
            f"{urlparse(url).hostname}",
            file=sys.stderr,
        )
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", InsecureRequestWarning)
            return session.get(url, verify=False, **kwargs)
    return session.get(url, verify=True, **kwargs)


def robots_allows(session: requests.Session, target_url: str) -> bool:
    parsed = urlparse(target_url)
    robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
    try:
        response = _session_get(session, robots_url, timeout=20)
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
    response = _session_get(session, url, timeout=30)
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
            venue TEXT NOT NULL DEFAULT 'ICDE',
            track TEXT NOT NULL DEFAULT 'research',
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
    conn.execute("CREATE INDEX IF NOT EXISTS idx_icde_year ON reviewer_records(year)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_icde_role ON reviewer_records(role)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_icde_name_key ON reviewer_records(name_key)")
    ensure_reviewer_service_schema(conn, venue="ICDE")
    return conn


def save_records(
    conn: sqlite3.Connection,
    records: Iterable[ReviewerRecord],
    *,
    year: int,
    url: str,
    fetched_at: str,
    html: bytes,
    extra_sources: Iterable[tuple[str, str, bytes]] = (),
    allow_qualifying_loss: bool = False,
) -> int:
    rows = list(records)
    assert_no_qualifying_person_loss(
        conn,
        rows,
        venue="ICDE",
        year_column="year",
        year=year,
        allow_loss=allow_qualifying_loss,
    )
    sources = [(url, fetched_at, html), *list(extra_sources)]
    for source_url, source_fetched_at, source_html in sources:
        conn.execute(
            "INSERT OR IGNORE INTO source_pages(year, url, fetched_at, sha256) VALUES (?, ?, ?, ?)",
            (year, source_url, source_fetched_at, hashlib.sha256(source_html).hexdigest()),
        )

    with conn:
        conn.execute("DELETE FROM reviewer_records WHERE year = ?", (year,))
        before = conn.total_changes
        prepared = []
        for record in rows:
            role_family, track, qualifies = service_values("ICDE", record)
            prepared.append(
                (
                    record.year,
                    record.role,
                    role_family,
                    track,
                    qualifies,
                    record.name,
                    record.affiliation,
                    record.name_key,
                    record.raw_text,
                    record.source_url,
                    record.fetched_at,
                )
            )
        conn.executemany(
            """
            INSERT OR IGNORE INTO reviewer_records(
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
        "SELECT year, role, COUNT(*) FROM reviewer_records GROUP BY year, role ORDER BY year, role"
    ):
        print(f"  {year}: {role}: {n}")
    total = conn.execute("SELECT COUNT(*) FROM reviewer_records").fetchone()[0]
    keys = conn.execute("SELECT COUNT(DISTINCT name_key) FROM reviewer_records").fetchone()[0]
    print(f"\nTotal raw records: {total}")
    print(f"Distinct conservative name keys: {keys}")
    print("Note: name keys are NOT entity-resolved people.")


def parse_source_overrides(values: list[str]) -> dict[int, str]:
    result: dict[int, str] = {}
    for value in values:
        try:
            year, url = value.split("=", 1)
            result[int(year)] = url
        except ValueError as exc:
            raise SystemExit(f"Invalid --source {value!r}; expected YEAR=URL") from exc
    return result


def source_for_year(year: int) -> str | None:
    if year in DEFAULT_SOURCES:
        return DEFAULT_SOURCES[year]
    if year >= 2026:
        return f"https://icde{year}.github.io/pc-chairs.html"
    return None


def fallback_source_for_year(year: int) -> str | None:
    if year in FALLBACK_SOURCES:
        return FALLBACK_SOURCES[year]
    if year >= 2026:
        return f"https://raw.githubusercontent.com/icde{year}/icde{year}.github.io/main/pc-chairs.html"
    return None


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--years",
        nargs="+",
        type=int,
        default=None,
        help="Years to collect. Omit for 2022 through the last completed calendar year.",
    )
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument(
        "--include-chairs",
        action="store_true",
        help="Deprecated compatibility flag; qualifying research chairs are always included.",
    )
    parser.add_argument("--delay", type=float, default=1.0)
    parser.add_argument("--source", action="append", default=[], metavar="YEAR=URL")
    parser.add_argument(
        "--officer-source",
        action="append",
        default=[],
        metavar="YEAR=URL",
        help="Override the official organizing/officer page for a year; may be repeated.",
    )
    parser.add_argument(
        "--allow-qualifying-loss",
        action="store_true",
        help="Allow a rerun to remove previously qualifying people for a year.",
    )
    parser.add_argument("--ignore-robots", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    overrides = parse_source_overrides(args.source)
    officer_overrides = parse_source_overrides(args.officer_source)
    years = sorted(set(args.years if args.years is not None else default_years()))

    session = requests.Session()
    session.headers.update({"User-Agent": DEFAULT_UA, "Accept": "text/html,application/xhtml+xml"})
    conn = open_db(args.db)
    failed = False

    def collect_research(
        year: int,
        url: str,
    ) -> tuple[list[ReviewerRecord], str, bytes, str, list[tuple[str, str, bytes]]]:
        html, fetched_at = fetch_page(session, url, ignore_robots=args.ignore_robots)
        records = parse_icde_page(
            html,
            year=year,
            source_url=url,
            fetched_at=fetched_at,
            include_chairs=True,
            validate=False,
        )
        extra_sources: list[tuple[str, str, bytes]] = []

        # Prefer the main research-committee page. An auxiliary reviewer-only
        # page is a recovery source, not a second population to union blindly.
        # Fetch it only when the main page produced an implausibly small PC
        # section, and use it only if it improves that section.
        primary_pc_count = sum(r.role == "program_committee_member" for r in records)
        auxiliary_url = AUXILIARY_REVIEWER_SOURCES.get(year)
        if (
            primary_pc_count < MIN_EXPECTED_RESEARCH_REVIEWERS
            and auxiliary_url
            and year not in overrides
        ):
            print(f"Fetching ICDE {year} auxiliary reviewers: {auxiliary_url}")
            auxiliary_html, auxiliary_fetched_at = fetch_page(
                session, auxiliary_url, ignore_robots=args.ignore_robots
            )
            auxiliary_records = parse_icde_reviewer_list_page(
                auxiliary_html,
                year=year,
                source_url=auxiliary_url,
                fetched_at=auxiliary_fetched_at,
                minimum_expected=1,
            )
            if len(auxiliary_records) > primary_pc_count:
                records = merge_research_sources(records, auxiliary_records)
            extra_sources.append((auxiliary_url, auxiliary_fetched_at, auxiliary_html))

        validate_research_records(records, url)
        return records, url, html, fetched_at, extra_sources

    try:
        for index, year in enumerate(years):
            url = overrides.get(year) or source_for_year(year)
            if not url:
                failed = True
                print(
                    f"  ERROR for {year}: no built-in official source. "
                    f"Pass --source {year}=https://official.example/...",
                    file=sys.stderr,
                )
                continue

            print(f"Fetching {url}")
            try:
                try:
                    records, actual_url, actual_html, actual_fetched_at, extra_sources = (
                        collect_research(year, url)
                    )
                except ValueError as primary_exc:
                    fallback_url = fallback_source_for_year(year)
                    if not fallback_url or fallback_url == url:
                        raise
                    print(f"  primary parser/source failed: {primary_exc}")
                    print(f"  trying official source-repository fallback: {fallback_url}")
                    records, actual_url, actual_html, actual_fetched_at, extra_sources = (
                        collect_research(year, fallback_url)
                    )

                research_count = len(records)

                officer_url = officer_overrides.get(year) or ICDE_OFFICER_SOURCES.get(year)
                officer_records: list[ReviewerRecord] = []
                if officer_url:
                    print(f"Fetching ICDE {year} officers: {officer_url}")
                    officer_html, officer_fetched_at = fetch_page(
                        session, officer_url, ignore_robots=args.ignore_robots
                    )
                    officer_records = parse_icde_officers_page(
                        officer_html,
                        year=year,
                        source_url=officer_url,
                        fetched_at=officer_fetched_at,
                    )
                    extra_sources.append((officer_url, officer_fetched_at, officer_html))

                technical_records: list[ReviewerRecord] = []
                for page_kind, technical_url in ICDE_TECHNICAL_PC_SOURCES.get(year, {}).items():
                    try:
                        print(f"Fetching ICDE {year} {page_kind} PC: {technical_url}")
                        technical_html, technical_fetched_at = fetch_page(
                            session, technical_url, ignore_robots=args.ignore_robots
                        )
                        parsed = parse_icde_technical_pc_page(
                            technical_html,
                            year=year,
                            source_url=technical_url,
                            fetched_at=technical_fetched_at,
                            page_kind=page_kind,
                        )
                        technical_records.extend(parsed)
                        extra_sources.append((technical_url, technical_fetched_at, technical_html))
                    except Exception as exc:
                        raise RuntimeError(f"ICDE {year} {page_kind} PC source failed: {exc}") from exc

                records = merge_icde_records(records, officer_records, technical_records)
                inserted = save_records(
                    conn,
                    records,
                    year=year,
                    url=actual_url,
                    fetched_at=actual_fetched_at,
                    html=actual_html,
                    extra_sources=extra_sources,
                    allow_qualifying_loss=args.allow_qualifying_loss,
                )
                print(
                    f"  parsed {research_count} research-PC records + "
                    f"{len(officer_records)} officer records + "
                    f"{len(technical_records)} technical-PC records; merged to {len(records)} unique "
                    f"role/person rows; replaced year with {inserted} rows"
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
