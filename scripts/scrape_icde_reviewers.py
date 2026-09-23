#!/usr/bin/env python3
"""Scrape public IEEE ICDE research program-committee pages into SQLite.

This script targets the official conference pages and stores Area Chairs and
Program Committee Members. Program Committee Chairs can be included with
--include-chairs. When --years is omitted, the scraper collects 2022 through the last completed calendar year. The source map currently covers 2022-2026; use
--source YEAR=URL for another official yearly page or if a site URL changes.

Known official sources:
    2022 https://icde2022.ieeecomputer.my/research-program-committee/
    2023 https://icde2023.ics.uci.edu/research-program-committee/
    2026 https://icde2026.github.io/pc-chairs.html
    2025 https://ieee-icde.org/2025/program-committees/
    2024 https://icde2024.github.io/CFP_research.html

The output is deliberately not entity-resolved. `name_key` is a conservative
blocking key only.

Install:
    pip install requests beautifulsoup4

Examples:
    python scripts/scrape_icde_reviewers.py --years 2022 2023 2024 2025 2026
    python scripts/scrape_icde_reviewers.py --years 2026 --include-chairs
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
import warnings
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import requests
from urllib3.exceptions import InsecureRequestWarning
from bs4 import BeautifulSoup, NavigableString, Tag


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = REPO_ROOT / "data" / "icde_reviewers.sqlite"

DEFAULT_START_YEAR = 2022

def default_years() -> list[int]:
    """Years collected when --years is omitted: 2022 through last calendar year."""
    last_complete_year = datetime.now(timezone.utc).year - 1
    if last_complete_year < DEFAULT_START_YEAR:
        return []
    return list(range(DEFAULT_START_YEAR, last_complete_year + 1))

DEFAULT_UA = os.environ.get("ICDE_SCRAPER_UA", "icde-reviewer-research/1.2")
MIN_EXPECTED_REVIEWERS = 20

DEFAULT_SOURCES = {
    2022: "https://icde2022.ieeecomputer.my/research-program-committee/",
    2023: "https://icde2023.ics.uci.edu/research-program-committee/",
    2026: "https://icde2026.github.io/pc-chairs.html",
    2025: "https://ieee-icde.org/2025/program-committees/",
    2024: "https://icde2024.github.io/CFP_research.html",
}

# ICDE 2022 publishes the ordinary research reviewers on a separate official
# page.  The main committee page collapses most PC entries into one HTML block,
# so using this page avoids silently parsing only the first reviewer.
ICDE_2022_REVIEWERS_URL = "https://icde2022.ieeecomputer.my/reviewers/"

# The archived 2022 reviewer page contains two HTML/text-layer joins where two
# consecutive reviewers are rendered as one line, plus two names whose
# affiliation is omitted. Keep these narrow, source-specific corrections
# explicit rather than guessing splits in arbitrary names.
ICDE_2022_REVIEWER_LINE_FIXES = {
    "Foto Afrati, NTUA, Greece": [
        ("Foto Afrati", "NTUA, Greece"),
    ],
    "Zoi Kaoudi, TU Berlin Pinar Karagoz METU, Turkey": [
        ("Zoi Kaoudi", "TU Berlin"),
        ("Pinar Karagoz", "METU, Turkey"),
    ],
    "Tarique Siddiqui, Microsoft Research Alkis Simitsis Athena Research Center": [
        ("Tarique Siddiqui", "Microsoft Research"),
        ("Alkis Simitsis", "Athena Research Center"),
    ],
    "Kostas Stefanidis": [("Kostas Stefanidis", "")],
    "Rebecca Taft": [("Rebecca Taft", "")],
}

# The archived 2023 host currently serves an incomplete certificate chain.
# Disable certificate verification only for this exact historical official host;
# all other sources retain normal TLS verification.
INSECURE_TLS_HOSTS = {"icde2023.ics.uci.edu"}

# Sanity floors for editions whose historical layouts are especially fragile.
MIN_EXPECTED_BY_YEAR_ROLE = {
    # Official ICDE 2022 pages list 22 Associate Chairs and 201 research reviewers.
    2022: {"area_chair": 22, "program_committee_member": 201},
}

# The 2026 site is published from this official GitHub repository. If the
# GitHub Pages response is transformed/cached in an unexpected way, the raw
# repository file is an authoritative structural fallback.
FALLBACK_SOURCES = {
    2026: "https://raw.githubusercontent.com/icde2026/icde2026.github.io/main/pc-chairs.html",
}

ROLE_PATTERNS = [
    # 2022 uses the short headings "Chairs" and "Associate Chairs".
    # Later editions use Research Program Committee / Area Chairs variants.
    (re.compile(r"^(?:research\s+)?program\s+committee\s+chairs?\s*:?$", re.I), "program_committee_chair"),
    (re.compile(r"^chairs?\s*:?$", re.I), "program_committee_chair"),
    (re.compile(r"^associate\s+chairs?\s*:?$", re.I), "area_chair"),
    (re.compile(r"^(?:research\s+)?area\s+chairs?\s*:?$", re.I), "area_chair"),
    (re.compile(r"^(?:pc\s+members?|program\s+committee\s+members?)\s*:?$", re.I), "program_committee_member"),
    (re.compile(r"^program\s+committee\s*:?$", re.I), "program_committee_member"),
]


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


def clean_line(text: str) -> str:
    text = text.replace("\xa0", " ").replace("\u200b", "")
    text = re.sub(r"\s+", " ", text)
    return text.strip(" \t\r\n")


def normalize_name_key(name: str) -> str:
    value = unicodedata.normalize("NFKD", name)
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    value = value.casefold()
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def role_for_text(text: str) -> str | None:
    value = clean_line(text)
    for pattern, role in ROLE_PATTERNS:
        if pattern.fullmatch(value):
            return role
    return None


def plausible_person_name(value: str) -> bool:
    value = clean_line(value).strip("-–—,;:\"'“”")
    if not value or len(value) > 150 or not any(c.isalpha() for c in value):
        return False
    words = value.split()
    if not 1 <= len(words) <= 16:
        return False
    lowered = value.casefold()
    blocked = (
        "program committee", "area chairs", "call for papers", "important dates",
        "submission", "conference", "research track", "review process",
    )
    return not any(token in lowered for token in blocked)


def strip_quote_artifacts(value: str) -> str:
    return clean_line(value).strip(" \t,;:\"'“”‘’")


def parse_person_line(line: str) -> tuple[str, str] | None:
    raw = clean_line(line)
    raw = re.sub(r"^[•▪●○\-*]+\s*", "", raw)
    if "," not in raw:
        return None

    left, right = raw.split(",", 1)
    name = strip_quote_artifacts(left)
    affiliation = strip_quote_artifacts(right)
    if plausible_person_name(name) and affiliation:
        return name, affiliation
    return None


def text_after_tag(tag: Tag) -> str:
    """Return visible text following `tag` inside its parent row."""
    parts: list[str] = []
    for sibling in tag.next_siblings:
        if isinstance(sibling, NavigableString):
            parts.append(str(sibling))
        elif isinstance(sibling, Tag):
            parts.append(sibling.get_text(" ", strip=True))
    value = clean_line(" ".join(parts))
    return strip_quote_artifacts(value.lstrip(" ,;:-–—"))


def extract_strong_name_rows(
    soup: BeautifulSoup,
    *,
    year: int,
    source_url: str,
    fetched_at: str,
    include_chairs: bool,
) -> list[ReviewerRecord]:
    """Parse ICDE layouts that encode rows as <div><strong>Name</strong>, Affiliation.</div>.

    ICDE 2026 uses this structure and contains a few stray closing </strong>
    tags. Reading the name from <strong> and the affiliation from its following
    siblings is substantially more robust than flattening the entire page.
    """
    root = soup.select_one("section.officers") or soup.select_one("main")
    if root is None:
        return []

    records: list[ReviewerRecord] = []
    current_role: str | None = None
    seen: set[tuple[str, str, str]] = set()

    # On the 2026 page, headings and reviewer divs are direct children of the
    # officers section. recursive=False avoids accidentally parsing menus/footer.
    elements = root.find_all(["h1", "h2", "h3", "h4", "h5", "h6", "div", "p", "li"], recursive=False)
    if not elements:
        elements = root.find_all(["h1", "h2", "h3", "h4", "h5", "h6", "div", "p", "li"])

    for node in elements:
        text = clean_line(node.get_text(" ", strip=True))
        heading_role = role_for_text(text)
        if heading_role:
            current_role = heading_role
            continue

        if node.name and node.name.startswith("h"):
            if current_role is not None:
                current_role = None
            continue

        if current_role is None:
            continue
        if current_role == "program_committee_chair" and not include_chairs:
            continue

        strong = node.find("strong")
        if strong is None:
            continue
        name = strip_quote_artifacts(strong.get_text(" ", strip=True))
        if not plausible_person_name(name):
            continue

        affiliation = text_after_tag(strong)
        if not affiliation:
            # Fallback for parsers that normalize malformed markup differently.
            full = clean_line(node.get_text(" ", strip=True))
            if full.startswith(name):
                affiliation = strip_quote_artifacts(full[len(name):].lstrip(" ,;:-–—"))
        if not affiliation:
            continue

        raw = f"{name}, {affiliation}"
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
                raw_text=raw,
                source_url=source_url,
                fetched_at=fetched_at,
            )
        )
    return records


def extract_blocks(soup: BeautifulSoup):
    """Yield semantic text blocks in document order without obvious duplicates."""
    for node in soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6", "li", "p", "div"]):
        if not isinstance(node, Tag):
            continue
        # Divs are useful on some WordPress templates, but only when they are
        # leaves with no more specific block elements inside them.
        if node.name == "div" and node.find(["div", "p", "li", "h1", "h2", "h3", "h4", "h5", "h6"]):
            continue
        if node.name == "p" and node.find("li") is not None:
            continue
        text = clean_line(node.get_text(" ", strip=True))
        if text:
            yield node, text


def parse_icde_page(
    html: bytes,
    *,
    year: int,
    source_url: str,
    fetched_at: str,
    include_chairs: bool = False,
) -> list[ReviewerRecord]:
    soup = BeautifulSoup(html, "html.parser")

    # First try the explicit <strong>Name</strong>, affiliation row structure.
    # This is the canonical layout used by the official ICDE 2026 page.
    strong_rows = extract_strong_name_rows(
        soup,
        year=year,
        source_url=source_url,
        fetched_at=fetched_at,
        include_chairs=include_chairs,
    )
    strong_target_count = sum(
        r.role in {"area_chair", "program_committee_member"} for r in strong_rows
    )
    if strong_target_count >= MIN_EXPECTED_REVIEWERS:
        return strong_rows

    records: list[ReviewerRecord] = list(strong_rows)
    current_role: str | None = None
    saw_target_role = strong_target_count > 0
    seen: set[tuple[str, str, str]] = {
        (r.role, r.name, r.affiliation) for r in records
    }

    for node, text in extract_blocks(soup):
        heading_role = role_for_text(text)
        if heading_role:
            current_role = heading_role
            if heading_role in {"area_chair", "program_committee_member"}:
                saw_target_role = True
            continue

        # A new unrelated heading ends the current committee section. This is
        # important for the 2024 CFP, where committee lists live inside a much
        # longer call-for-papers page.
        if node.name and node.name.startswith("h") and current_role is not None:
            current_role = None
            continue

        if current_role is None:
            continue

        # Some templates put several rows in one text block separated by hard
        # line breaks. BeautifulSoup may preserve those in get_text("\n").
        pieces = [clean_line(x) for x in node.get_text("\n", strip=True).splitlines()]
        pieces = [x for x in pieces if x]
        if not pieces:
            pieces = [text]

        for piece in pieces:
            if role_for_text(piece):
                continue
            parsed = parse_person_line(piece)
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
                    raw_text=piece,
                    source_url=source_url,
                    fetched_at=fetched_at,
                )
            )

    # Some ICDE templates (notably 2026) render most names as bare text plus
    # <br> elements inside a container. The semantic-block pass can therefore
    # find a heading and only one row. If the result is suspiciously small, run
    # a second conservative visible-line pass and merge/de-duplicate records.
    target_count = sum(r.role in {"area_chair", "program_committee_member"} for r in records)
    if target_count < MIN_EXPECTED_REVIEWERS:
        for br in soup.find_all("br"):
            br.replace_with("\n")
        lines = [clean_line(x) for x in soup.get_text("\n").splitlines() if clean_line(x)]
        current_role = None
        for line in lines:
            r = role_for_text(line)
            if r:
                current_role = r
                if r in {"area_chair", "program_committee_member"}:
                    saw_target_role = True
                continue
            if current_role is None:
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
                    source_url=source_url,
                    fetched_at=fetched_at,
                )
            )

    if not saw_target_role:
        raise ValueError(f"No ICDE Area Chair / PC Member section found on {source_url}")

    target_count = sum(r.role in {"area_chair", "program_committee_member"} for r in records)
    if target_count < MIN_EXPECTED_REVIEWERS:
        raise ValueError(
            f"Only {target_count} ICDE research reviewer records were parsed from {source_url}; "
            f"expected at least {MIN_EXPECTED_REVIEWERS}. The page layout may have changed."
        )
    return records



ORG_MARKERS = {
    "university", "universita", "università", "universite", "université", "universitat",
    "institute", "institut", "research", "center", "centre", "college", "school",
    "laboratory", "laboratories", "lab", "labs", "technology", "technical", "technological", "polytechnique", "engineering", "computing", "systems", "management", "corporation", "corp", "inc",
    "microsoft", "google", "amazon", "oracle", "alibaba", "ibm", "huawei", "meta",
    "bytedance", "snowflake", "eurecom", "inria", "epfl", "eth", "kaist",
    "qcri", "ntua", "ntu", "unsw", "sap", "ebay", "cwi", "dfki", "forth",
    "athena", "neo4j", "instacart", "purdue", "harvard", "stanford", "mit",
}
COUNTRY_PHRASES = {
    "usa", "uk", "canada", "china", "germany", "france", "italy", "greece",
    "australia", "austria", "belgium", "denmark", "finland", "japan", "korea",
    "south korea", "netherlands", "norway", "singapore", "spain", "sweden",
    "switzerland", "turkey", "poland", "portugal", "qatar", "israel", "india",
    "ireland", "united states", "united kingdom", "hong kong", "hong kong sar",
    "catalonia",
}
NAME_CONNECTORS = {"de", "del", "da", "dos", "van", "von", "der", "den", "la", "le", "bin", "al"}
AFFILIATION_LOCATION_SUFFIXES = {"southern california", "california davis", "san diego", "los angeles", "new brunswick", "twin cities", "reutlingen", "mainz"}


def _nameish(value: str) -> bool:
    value = clean_line(value).strip(" *")
    tokens = value.split()
    if not 2 <= len(tokens) <= 6:
        return False
    low_words = {re.sub(r"[^a-zà-öø-ÿ]+", "", t.casefold()) for t in tokens}
    if low_words & ORG_MARKERS:
        return False
    if value.casefold() in COUNTRY_PHRASES or value.casefold() in AFFILIATION_LOCATION_SUFFIXES:
        return False
    if tokens[0].strip(".,").casefold() in NAME_CONNECTORS:
        return False
    for token in tokens:
        core = token.strip("()[]{}.,;:'\"“”‘’")
        if not core:
            return False
        low = core.casefold()
        if low in NAME_CONNECTORS:
            continue
        if re.fullmatch(r"[A-Z]\.", core):
            continue
        if core[0].isupper():
            continue
        return False
    return plausible_person_name(value)


def _affiliation_score(value: str, *, prior_affiliation: str = "") -> int:
    value = clean_line(value).strip(" ,;:-")
    if not value:
        return 0
    low = value.casefold().strip(" .")
    last_word = re.sub(r"[^a-z]+", "", value.split()[-1].casefold()) if value.split() else ""
    if last_word in {"of", "and", "de", "di", "del", "at", "in"}:
        return 0
    if low in COUNTRY_PHRASES:
        return 12
    words = re.findall(r"[A-Za-zÀ-ÖØ-öø-ÿ0-9.+&/-]+", value)
    low_words = {w.casefold().strip(".") for w in words}
    if len(words) == 1 and (words[0].isupper() or low in ORG_MARKERS):
        return 11
    score = 0
    if low_words & ORG_MARKERS:
        score += 7
    if any(len(w) >= 2 and w.isupper() for w in words):
        score += 5
    if any(ch.isdigit() for ch in value) or any(ch in value for ch in "/&.-"):
        score += 3
    # If an earlier comma already established an institution, a short trailing
    # location such as "Riverside" can legitimately finish the affiliation.
    if prior_affiliation and _affiliation_score(prior_affiliation) >= 5 and len(words) <= 3:
        score += 4
    return score


def _candidate_name_suffix(value: str, *, prior_affiliation: str = "") -> tuple[str, str] | None:
    value = clean_line(value).strip()
    if not value:
        return None
    tokens = value.split()
    best: tuple[int, str, str] | None = None
    for n in range(2, min(6, len(tokens)) + 1):
        name = " ".join(tokens[-n:]).strip(" *")
        prefix = " ".join(tokens[:-n]).strip()
        if not prefix or not _nameish(name):
            continue
        aff_score = _affiliation_score(prefix, prior_affiliation=prior_affiliation)
        if aff_score < 4:
            continue
        # Strong affiliation boundary wins. Then prefer complete 2-3 token names.
        score = aff_score * 10 + (8 if n == 2 else 7 if n == 3 else 4 if n == 4 else 1)
        if best is None or score > best[0]:
            best = (score, prefix, name)
    if best is None:
        return None
    return best[1], best[2]


def _parse_generic_collapsed_block(text: str) -> list[tuple[str, str]]:
    chunks = [clean_line(x) for x in text.split(",")]
    if len(chunks) < 2:
        return []
    current_name = chunks[0].strip(" *")
    current_aff: list[str] = []
    rows: list[tuple[str, str]] = []
    for chunk in chunks[1:]:
        prior = clean_line(", ".join(current_aff))
        split = _candidate_name_suffix(chunk, prior_affiliation=prior)
        if split is None:
            current_aff.append(chunk)
            continue
        aff_piece, next_name = split
        if aff_piece:
            current_aff.append(aff_piece)
        affiliation = clean_line(", ".join(x for x in current_aff if x)).strip(" ,")
        if _nameish(current_name):
            rows.append((current_name.strip(" *"), affiliation))
        current_name = next_name.strip(" *")
        current_aff = []
    affiliation = clean_line(", ".join(x for x in current_aff if x)).strip(" ,")
    if _nameish(current_name):
        rows.append((current_name.strip(" *"), affiliation))
    return rows


def parse_collapsed_person_block(text: str) -> list[tuple[str, str]]:
    """Recover ``Name, Affiliation`` rows from a flattened ICDE 2022 block."""
    text = clean_line(text)
    if not text:
        return []
    replacements = {
        "Zoi Kaoudi, TU Berlin Pinar Karagoz METU, Turkey":
            "Zoi Kaoudi, TU Berlin ||| Pinar Karagoz, METU, Turkey",
        "Tarique Siddiqui, Microsoft Research Alkis Simitsis Athena Research Center":
            "Tarique Siddiqui, Microsoft Research ||| Alkis Simitsis, Athena Research Center",
        "Kostas Stefanidis Kurt Stockinger,":
            "Kostas Stefanidis, ||| Kurt Stockinger,",
        "Rebecca Taft Jian Tan,":
            "Rebecca Taft, ||| Jian Tan,",
    }
    for old, new in replacements.items():
        text = text.replace(old, new)
    text = re.sub(r"(?<![A-Za-z.])\b[A-Z]\b(?=\s+[A-ZÀ-ÖØ-Þ])", " ", text)
    rows: list[tuple[str, str]] = []
    for segment in (clean_line(x) for x in text.split("|||")):
        if not segment:
            continue
        parsed = _parse_generic_collapsed_block(segment)
        if parsed:
            rows.extend(parsed)
            continue
        # Explicit affiliation-less historical exceptions.
        bare = segment.strip(" ,")
        if bare in {"Kostas Stefanidis", "Rebecca Taft"}:
            rows.append((bare, ""))
    return rows


def _text_between_reviewer_headings(soup: BeautifulSoup) -> str:
    """Return the official ICDE reviewer-list body, preserving source order."""
    headings = soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6"])
    start = None
    for h in headings:
        if clean_line(h.get_text(" ", strip=True)).casefold() == "reviewers":
            start = h
    if start is None:
        return ""
    parts: list[str] = []
    for node in start.next_siblings:
        if isinstance(node, Tag) and node.name in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            heading = clean_line(node.get_text(" ", strip=True)).casefold()
            if heading and heading != "reviewers":
                break
        if isinstance(node, NavigableString):
            t = clean_line(str(node))
        elif isinstance(node, Tag):
            t = clean_line(" ".join(node.stripped_strings))
        else:
            t = ""
        if t:
            parts.append(t)
    return clean_line(" ".join(parts))

def parse_icde_reviewer_list_page(
    html: bytes,
    *,
    year: int,
    source_url: str,
    fetched_at: str,
) -> list[ReviewerRecord]:
    """Parse the dedicated ICDE research-reviewer page.

    ICDE 2022's archived WordPress page visually renders one reviewer per line,
    but the raw HTML collapses many of those rows into larger text blocks.  We
    therefore try ordinary visible lines first and then a source-specific
    collapsed-block parser before accepting the result.
    """
    soup = BeautifulSoup(html, "html.parser")
    for br in soup.find_all("br"):
        br.replace_with("\n")

    records: list[ReviewerRecord] = []
    # This dedicated page lists each reviewer once.  Deduplicate by normalized
    # person name so a flattened fallback cannot add a second, corrupted
    # affiliation for the same reviewer.
    seen_names: set[str] = set()

    def add(name: str, affiliation: str, raw: str) -> None:
        name = clean_line(name).strip(" *")
        affiliation = clean_line(affiliation).strip(" ,")
        # The dedicated 2022 reviewer page occasionally exposes an affiliation
        # fragment as its own ``Name, Affiliation`` line (for example
        # ``NTUA, Greece`` from ``Foto Afrati, NTUA, Greece``).  Research
        # reviewer names on this source are full names, so require at least two
        # tokens here rather than accepting organization acronyms as people.
        if len(name.split()) < 2 or not plausible_person_name(name):
            return
        key = normalize_name_key(name)
        if key in seen_names:
            return
        seen_names.add(key)
        records.append(
            ReviewerRecord(
                year=year,
                role="program_committee_member",
                name=name,
                affiliation=affiliation,
                name_key=normalize_name_key(name),
                raw_text=clean_line(raw),
                source_url=source_url,
                fetched_at=fetched_at,
            )
        )

    page_text = clean_line(soup.get_text(" ", strip=True))

    # Apply only narrowly verified ICDE 2022 source corrections before the
    # generic parser.  This preserves the two joined reviewer pairs and the two
    # affiliation-less names on the archived page, while preventing fragments
    # such as ``NTUA, Greece`` from becoming people.
    if year == 2022:
        for source_fragment, fixed_rows in ICDE_2022_REVIEWER_LINE_FIXES.items():
            if source_fragment in page_text:
                for fixed_name, fixed_affiliation in fixed_rows:
                    add(fixed_name, fixed_affiliation, source_fragment)

    lines = [clean_line(x) for x in soup.get_text("\n").splitlines() if clean_line(x)]
    active = False
    for line in lines:
        low = line.casefold()
        if low == "reviewers":
            active = True
            continue
        if active and (low.startswith("want to contribute") or low == "organization"):
            break
        if not active or re.fullmatch(r"[A-Z]", line):
            continue
        fixed = ICDE_2022_REVIEWER_LINE_FIXES.get(line) if year == 2022 else None
        if fixed is not None:
            for name, affiliation in fixed:
                add(name, affiliation, line)
            continue
        parsed = parse_person_line(line)
        if parsed is not None and len(line) < 220:
            add(parsed[0], parsed[1], line)
        elif line.count(",") >= 2:
            for name, affiliation in parse_collapsed_person_block(line):
                add(name, affiliation, line)

    # On the archived 2022 page, requests/BeautifulSoup can still expose one
    # large content node rather than the visual rows. Parse the whole reviewer
    # section as a final fallback and merge/de-duplicate it with the line pass.
    block = _text_between_reviewer_headings(soup)
    if block:
        # Drop the explanatory sentence before the A-section if present.
        block = re.sub(
            r"^The reviewers.? names are arranged according to the last name alphabetically\.?\s*",
            "",
            block,
            flags=re.I,
        )
        for name, affiliation in parse_collapsed_person_block(block):
            add(name, affiliation, block)

    expected = 201 if year == 2022 else 190
    if len(records) < expected:
        raise ValueError(
            f"Only {len(records)} reviewer rows parsed from {source_url}; "
            f"expected at least {expected} research reviewers."
        )
    return records


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


def validate_year_counts(records: list[ReviewerRecord], year: int) -> None:
    expected = MIN_EXPECTED_BY_YEAR_ROLE.get(year)
    if not expected:
        return
    counts: dict[str, int] = {}
    for record in records:
        counts[record.role] = counts.get(record.role, 0) + 1
    failures = [
        f"{role}: parsed {counts.get(role, 0)}, expected at least {minimum}"
        for role, minimum in expected.items()
        if counts.get(role, 0) < minimum
    ]
    if failures:
        raise ValueError(
            f"ICDE {year} committee sanity check failed: " + "; ".join(failures)
        )


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
    return conn


def save_records(
    conn: sqlite3.Connection,
    records: Iterable[ReviewerRecord],
    *, year: int, url: str, fetched_at: str, html: bytes,
) -> int:
    conn.execute(
        "INSERT OR IGNORE INTO source_pages(year, url, fetched_at, sha256) VALUES (?, ?, ?, ?)",
        (year, url, fetched_at, hashlib.sha256(html).hexdigest()),
    )
    # Replace this year's rows from this source only after parsing succeeded.
    # This keeps reruns reproducible and removes stale partial results from an
    # older parser version (for example, the former ICDE 2026 one-row scrape).
    with conn:
        # ICDE uses one research-committee source per year. Once a new parse
        # has passed sanity checks, replace all rows for that year. This also
        # removes stale partial rows written by older parser versions even when
        # the source URL changes.
        conn.execute(
            "DELETE FROM reviewer_records WHERE year = ?",
            (year,),
        )
        before = conn.total_changes
        conn.executemany(
            """
            INSERT OR IGNORE INTO reviewer_records(
                year, role, name, affiliation, name_key, raw_text, source_url, fetched_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [(r.year, r.role, r.name, r.affiliation, r.name_key, r.raw_text, r.source_url, r.fetched_at) for r in records],
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
            y, u = value.split("=", 1)
            result[int(y)] = u
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
        return (
            f"https://raw.githubusercontent.com/icde{year}/"
            f"icde{year}.github.io/main/pc-chairs.html"
        )
    return None


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--years", nargs="+", type=int, default=None,
        help="Years to collect. Omit for 2022 through the last completed calendar year; explicit values override the default.",
    )
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--include-chairs", action="store_true")
    parser.add_argument("--delay", type=float, default=1.0)
    parser.add_argument("--source", action="append", default=[], metavar="YEAR=URL")
    parser.add_argument("--ignore-robots", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    overrides = parse_source_overrides(args.source)
    years = sorted(set(args.years if args.years is not None else default_years()))

    session = requests.Session()
    session.headers.update({"User-Agent": DEFAULT_UA, "Accept": "text/html,application/xhtml+xml"})
    conn = open_db(args.db)
    try:
        for i, year in enumerate(years):
            url = overrides.get(year) or source_for_year(year)
            if not url:
                print(
                    f"  ERROR for {year}: no built-in official source. "
                    f"Pass --source {year}=https://official.example/...",
                    file=sys.stderr,
                )
                continue
            print(f"Fetching {url}")
            try:
                html, fetched_at = fetch_page(session, url, ignore_robots=args.ignore_robots)
                try:
                    records = parse_icde_page(
                        html,
                        year=year,
                        source_url=url,
                        fetched_at=fetched_at,
                        include_chairs=args.include_chairs,
                    )

                    # ICDE 2022 has a dedicated official reviewer page.  Keep
                    # chairs/associate chairs from the committee page and take
                    # ordinary PC members from the dedicated reviewer list.
                    if year == 2022 and year not in overrides:
                        reviewer_html, reviewer_fetched_at = fetch_page(
                            session,
                            ICDE_2022_REVIEWERS_URL,
                            ignore_robots=args.ignore_robots,
                        )
                        pc_records = parse_icde_reviewer_list_page(
                            reviewer_html,
                            year=year,
                            source_url=ICDE_2022_REVIEWERS_URL,
                            fetched_at=reviewer_fetched_at,
                        )
                        records = [
                            r for r in records
                            if r.role != "program_committee_member"
                        ] + pc_records

                    validate_year_counts(records, year)
                    actual_url = url
                    actual_html = html
                    actual_fetched_at = fetched_at
                except ValueError as primary_exc:
                    fallback_url = fallback_source_for_year(year)
                    if not fallback_url or fallback_url == url:
                        raise
                    print(f"  primary parser/source failed: {primary_exc}")
                    print(f"  trying official source-repository fallback: {fallback_url}")
                    fallback_html, fallback_fetched_at = fetch_page(
                        session, fallback_url, ignore_robots=args.ignore_robots
                    )
                    records = parse_icde_page(
                        fallback_html,
                        year=year,
                        source_url=fallback_url,
                        fetched_at=fallback_fetched_at,
                        include_chairs=args.include_chairs,
                    )
                    validate_year_counts(records, year)
                    actual_url = fallback_url
                    actual_html = fallback_html
                    actual_fetched_at = fallback_fetched_at

                inserted = save_records(
                    conn, records, year=year, url=actual_url,
                    fetched_at=actual_fetched_at, html=actual_html
                )
                print(f"  parsed {len(records)} records; replaced year with {inserted} rows")
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
