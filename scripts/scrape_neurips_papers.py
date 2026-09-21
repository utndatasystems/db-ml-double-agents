#!/usr/bin/env python3
"""Collect accepted NeurIPS paper titles and authors from the official proceedings.

The scraper intentionally reads the yearly proceedings index only; it does not
open every paper page. Results are written to SQLite using two tables:
  papers         one row per accepted paper
  paper_authors  one row per author position

Examples:
  python scripts/scrape_neurips_papers.py --years 2022 2023 2024 2025 --db data/neurips_papers.sqlite
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sqlite3
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup, NavigableString, Tag

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = REPO_ROOT / "data" / "neurips_papers.sqlite"

USER_AGENT = (
    "AcademicMetadataCollector/1.0 "
    "(public conference proceedings metadata; contact: local research script)"
)

TRACK_LABELS = {
    "Main Conference Track",
    "Datasets and Benchmarks Track",
    "Creative AI",
    "Conference",
}


def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def person_key(name: str) -> str:
    s = unicodedata.normalize("NFKD", name)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = s.casefold()
    return re.sub(r"[^a-z0-9]+", "", s)


def index_url(year: int) -> str:
    # 2025 split the proceedings into separate books; use the main conference.
    if year == 2025:
        return "https://proceedings.neurips.cc/paper_files/paper/2025/vol38-main-conference"
    if 1987 <= year <= 2024:
        return f"https://proceedings.neurips.cc/paper/{year}"
    raise ValueError(
        f"No stable official NeurIPS proceedings index is configured for {year}. "
        "Add the official proceedings URL to index_url() when it becomes available."
    )


def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"})
    adapter = requests.adapters.HTTPAdapter(max_retries=3)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s


def fetch(session: requests.Session, url: str, timeout: int = 60) -> str:
    r = session.get(url, timeout=timeout)
    r.raise_for_status()
    return r.text


def looks_like_paper_link(href: str, year: int) -> bool:
    h = href.lower()
    return "abstract" in h and f"/{year}/" in h and ("/paper/" in h or "/paper_files/paper/" in h)


def nearest_record_container(a: Tag) -> Tag:
    # NeurIPS proceedings normally use one list item per paper.
    for name in ("li", "article", "p", "div"):
        parent = a.find_parent(name)
        if parent is not None:
            text = clean_text(parent.get_text(" ", strip=True))
            if len(text) < 5000:
                return parent
    return a.parent if isinstance(a.parent, Tag) else a


def extract_authors_and_track(a: Tag, container: Tag, title: str) -> tuple[str, str | None]:
    track = None
    full_text = clean_text(container.get_text(" ", strip=True))
    for label in TRACK_LABELS:
        if label in full_text:
            track = label
            break

    # Strong selectors first, for current and historical proceedings variants.
    for selector in (".authors", ".author", "i", "em"):
        for tag in container.select(selector):
            if a in tag.parents:
                continue
            t = clean_text(tag.get_text(" ", strip=True))
            if t and t != title and t not in TRACK_LABELS and not t.lower().startswith("bibtex"):
                if len(t) >= 3:
                    return t, track

    # Common structure: <a>Title</a> Authors ... [track label].
    pieces: list[str] = []
    for sib in a.next_siblings:
        if isinstance(sib, NavigableString):
            t = clean_text(str(sib))
        elif isinstance(sib, Tag):
            t = clean_text(sib.get_text(" ", strip=True))
        else:
            continue
        if not t:
            continue
        if t in TRACK_LABELS:
            track = t
            break
        if t.lower() in {"bibtex", "paper", "supplemental"}:
            continue
        pieces.append(t)

    if pieces:
        # Usually a single comma-separated string. If markup split authors into
        # multiple sibling nodes, preserve separators conservatively.
        return ", ".join(pieces), track

    # Last fallback: inspect strings inside the record container after title.
    parts = [clean_text(x) for x in container.stripped_strings]
    try:
        i = parts.index(title)
    except ValueError:
        i = -1
    candidates: list[str] = []
    for t in parts[i + 1 :]:
        if t in TRACK_LABELS:
            track = t
            break
        if t.lower() in {"bibtex", "paper", "supplemental"}:
            continue
        candidates.append(t)
    return ", ".join(candidates), track


def split_authors(raw: str) -> list[str]:
    raw = clean_text(raw)
    raw = re.sub(r"\s+(Main Conference Track|Datasets and Benchmarks Track|Creative AI|Conference)\s*$", "", raw)
    names = [clean_text(x) for x in raw.split(",")]
    return [x for x in names if x and x not in TRACK_LABELS]


def paper_uid_from_url(url: str) -> str:
    stem = Path(urlparse(url).path).stem
    stem = re.sub(r"-Abstract(?:-Conference)?$", "", stem, flags=re.I)
    if stem:
        return stem
    return hashlib.sha1(url.encode("utf-8")).hexdigest()


def parse_index(html: str, year: int, source_index_url: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    records: list[dict] = []
    seen: set[str] = set()

    for a in soup.find_all("a", href=True):
        href = a.get("href", "")
        if not looks_like_paper_link(href, year):
            continue

        title = clean_text(a.get_text(" ", strip=True))
        if not title:
            continue
        source_url = urljoin(source_index_url, href)
        uid = paper_uid_from_url(source_url)
        if uid in seen:
            continue

        container = nearest_record_container(a)
        raw_authors, track = extract_authors_and_track(a, container, title)
        authors = split_authors(raw_authors)
        if not authors:
            continue

        records.append(
            {
                "paper_uid": f"neurips-{year}-{uid}",
                "external_id": uid,
                "title": title,
                "track": track,
                "source_url": source_url,
                "source_index_url": source_index_url,
                "raw_author_text": raw_authors,
                "authors": authors,
            }
        )
        seen.add(uid)

    return records


def init_db(con: sqlite3.Connection) -> None:
    con.execute("PRAGMA foreign_keys = ON")
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS papers (
            paper_uid TEXT PRIMARY KEY,
            venue TEXT NOT NULL,
            year INTEGER NOT NULL,
            external_id TEXT,
            title TEXT NOT NULL,
            track TEXT,
            source_url TEXT NOT NULL,
            source_index_url TEXT NOT NULL,
            raw_author_text TEXT,
            fetched_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_papers_venue_year ON papers(venue, year);

        CREATE TABLE IF NOT EXISTS paper_authors (
            paper_uid TEXT NOT NULL,
            author_position INTEGER NOT NULL,
            author_name TEXT NOT NULL,
            author_key TEXT NOT NULL,
            PRIMARY KEY (paper_uid, author_position),
            FOREIGN KEY (paper_uid) REFERENCES papers(paper_uid) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_paper_authors_key ON paper_authors(author_key);
        CREATE INDEX IF NOT EXISTS idx_paper_authors_name ON paper_authors(author_name);
        """
    )


def replace_year(con: sqlite3.Connection, year: int, records: list[dict], fetched_at: str) -> None:
    with con:
        con.execute("DELETE FROM papers WHERE venue = ? AND year = ?", ("NeurIPS", year))
        for rec in records:
            con.execute(
                """INSERT INTO papers
                   (paper_uid, venue, year, external_id, title, track, source_url,
                    source_index_url, raw_author_text, fetched_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    rec["paper_uid"], "NeurIPS", year, rec["external_id"], rec["title"],
                    rec["track"], rec["source_url"], rec["source_index_url"],
                    rec["raw_author_text"], fetched_at,
                ),
            )
            con.executemany(
                "INSERT INTO paper_authors (paper_uid, author_position, author_name, author_key) VALUES (?, ?, ?, ?)",
                [
                    (rec["paper_uid"], pos, name, person_key(name))
                    for pos, name in enumerate(rec["authors"], start=1)
                ],
            )


def print_summary(con: sqlite3.Connection) -> None:
    print("\nDatabase summary:")
    for year, papers, authors in con.execute(
        """SELECT p.year, COUNT(DISTINCT p.paper_uid), COUNT(a.author_name)
           FROM papers p JOIN paper_authors a USING (paper_uid)
           WHERE p.venue = 'NeurIPS'
           GROUP BY p.year ORDER BY p.year"""
    ):
        print(f"  {year}: {papers} papers; {authors} paper-author rows")
    total_people = con.execute(
        "SELECT COUNT(DISTINCT author_key) FROM paper_authors a JOIN papers p USING (paper_uid) WHERE p.venue='NeurIPS'"
    ).fetchone()[0]
    print(f"Distinct conservative author keys: {total_people}")
    print("Note: author keys are NOT entity-resolved people.")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", nargs="+", type=int, default=[2022, 2023, 2024, 2025])
    ap.add_argument("--db", default=str(DEFAULT_DB))
    ap.add_argument("--delay", type=float, default=1.0, help="Delay between yearly index requests")
    ap.add_argument("--min-papers", type=int, default=100, help="Fail a year if fewer papers are parsed")
    args = ap.parse_args()

    db_path = Path(args.db)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db_path)
    init_db(con)
    session = make_session()

    for n, year in enumerate(args.years):
        try:
            url = index_url(year)
            print(f"Fetching NeurIPS {year}: {url}")
            html = fetch(session, url)
            records = parse_index(html, year, url)
            if len(records) < args.min_papers:
                raise RuntimeError(
                    f"Only {len(records)} papers parsed from {url}; expected at least {args.min_papers}. "
                    "The proceedings layout may have changed. Database rows for this year were NOT replaced."
                )
            fetched_at = datetime.now(timezone.utc).isoformat()
            replace_year(con, year, records, fetched_at)
            author_rows = sum(len(r["authors"]) for r in records)
            print(f"  parsed {len(records)} papers and {author_rows} paper-author rows")
        except Exception as exc:
            print(f"  ERROR for {year}: {exc}")
        if n + 1 < len(args.years):
            time.sleep(max(0.0, args.delay))

    print_summary(con)
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
