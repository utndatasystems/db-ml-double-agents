#!/usr/bin/env python3
"""Scrape public IEEE ICDE research program-committee pages into SQLite.

This script targets the official conference pages and stores Area Chairs and
Program Committee Members. Program Committee Chairs can be included with
--include-chairs. The default source map currently covers 2024-2026; use
--source YEAR=URL for another official yearly page or if a site URL changes.

Known official sources:
    2026 https://icde2026.github.io/pc-chairs.html
    2025 https://ieee-icde.org/2025/program-committees/
    2024 https://icde2024.github.io/CFP_research.html

The output is deliberately not entity-resolved. `name_key` is a conservative
blocking key only.

Install:
    pip install requests beautifulsoup4

Examples:
    python scrape_icde_reviewers.py --years 2024 2025 2026
    python scrape_icde_reviewers.py --years 2026 --include-chairs
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
from typing import Iterable
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup, NavigableString, Tag


DEFAULT_UA = os.environ.get("ICDE_SCRAPER_UA", "icde-reviewer-research/1.2")
MIN_EXPECTED_REVIEWERS = 20

DEFAULT_SOURCES = {
    2026: "https://icde2026.github.io/pc-chairs.html",
    2025: "https://ieee-icde.org/2025/program-committees/",
    2024: "https://icde2024.github.io/CFP_research.html",
}

# The 2026 site is published from this official GitHub repository. If the
# GitHub Pages response is transformed/cached in an unexpected way, the raw
# repository file is an authoritative structural fallback.
FALLBACK_SOURCES = {
    2026: "https://raw.githubusercontent.com/icde2026/icde2026.github.io/main/pc-chairs.html",
}

ROLE_PATTERNS = [
    (re.compile(r"^(?:research\s+)?program\s+committee\s+chairs?\s*:?$", re.I), "program_committee_chair"),
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


def fetch_page(session: requests.Session, url: str, ignore_robots: bool = False) -> tuple[bytes, str]:
    if not ignore_robots and not robots_allows(session, url):
        raise PermissionError(f"robots.txt does not allow this user-agent to fetch {url}")
    response = session.get(url, timeout=30)
    response.raise_for_status()
    return response.content, datetime.now(timezone.utc).isoformat(timespec="seconds")


def open_db(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
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


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--years", nargs="+", type=int, default=[2026])
    parser.add_argument("--db", default="icde_reviewers.sqlite")
    parser.add_argument("--include-chairs", action="store_true")
    parser.add_argument("--delay", type=float, default=1.0)
    parser.add_argument("--source", action="append", default=[], metavar="YEAR=URL")
    parser.add_argument("--ignore-robots", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    overrides = parse_source_overrides(args.source)
    years = sorted(set(args.years))

    session = requests.Session()
    session.headers.update({"User-Agent": DEFAULT_UA, "Accept": "text/html,application/xhtml+xml"})
    conn = open_db(args.db)
    try:
        for i, year in enumerate(years):
            url = overrides.get(year) or DEFAULT_SOURCES.get(year)
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
                    actual_url = url
                    actual_html = html
                    actual_fetched_at = fetched_at
                except ValueError as primary_exc:
                    fallback_url = FALLBACK_SOURCES.get(year)
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
                    actual_url = fallback_url
                    actual_html = fallback_html
                    actual_fetched_at = fallback_fetched_at

                inserted = save_records(
                    conn, records, year=year, url=actual_url,
                    fetched_at=actual_fetched_at, html=actual_html
                )
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
