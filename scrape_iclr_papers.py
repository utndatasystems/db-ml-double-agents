#!/usr/bin/env python3
"""Collect accepted ICLR paper titles and authors from official proceedings.

Official ICLR proceedings currently cover 2024 onward.

Example:
  python scrape_iclr_papers.py --years 2024 2025 2026 --db data/iclr_papers.sqlite
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

USER_AGENT = (
    "AcademicMetadataCollector/1.0 "
    "(public conference proceedings metadata; contact: local research script)"
)
TRACK_LABELS = {"Conference"}


def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def person_key(name: str) -> str:
    s = unicodedata.normalize("NFKD", name)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = s.casefold()
    return re.sub(r"[^a-z0-9]+", "", s)


def index_url(year: int) -> str:
    if year < 2024:
        raise ValueError(
            "The official proceedings.iclr.cc archive starts at ICLR 2024. "
            "Use OpenReview for older ICLR editions."
        )
    return f"https://proceedings.iclr.cc/paper_files/paper/{year}"


def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"})
    adapter = requests.adapters.HTTPAdapter(max_retries=3)
    s.mount("https://", adapter)
    return s


def fetch(session: requests.Session, url: str, timeout: int = 60) -> str:
    r = session.get(url, timeout=timeout)
    r.raise_for_status()
    return r.text


def nearest_record_container(a: Tag) -> Tag:
    for name in ("li", "article", "p", "div"):
        parent = a.find_parent(name)
        if parent is not None and len(clean_text(parent.get_text(" ", strip=True))) < 5000:
            return parent
    return a.parent if isinstance(a.parent, Tag) else a


def extract_authors_and_track(a: Tag, container: Tag, title: str) -> tuple[str, str | None]:
    full_text = clean_text(container.get_text(" ", strip=True))
    track = "Conference" if "Conference" in full_text else None

    for selector in (".authors", ".author", "i", "em"):
        for tag in container.select(selector):
            if a in tag.parents:
                continue
            t = clean_text(tag.get_text(" ", strip=True))
            if t and t != title and t != "Conference" and not t.lower().startswith("bibtex"):
                return t, track

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
        if t == "Conference":
            track = t
            break
        if t.lower() in {"bibtex", "paper", "supplemental"}:
            continue
        pieces.append(t)
    if pieces:
        return ", ".join(pieces), track

    parts = [clean_text(x) for x in container.stripped_strings]
    try:
        i = parts.index(title)
    except ValueError:
        i = -1
    out: list[str] = []
    for t in parts[i + 1 :]:
        if t == "Conference":
            track = t
            break
        if t.lower() in {"bibtex", "paper", "supplemental"}:
            continue
        out.append(t)
    return ", ".join(out), track


def split_authors(raw: str) -> list[str]:
    raw = re.sub(r"\s+Conference\s*$", "", clean_text(raw))
    return [clean_text(x) for x in raw.split(",") if clean_text(x) and clean_text(x) != "Conference"]


def uid_from_url(url: str) -> str:
    stem = Path(urlparse(url).path).stem
    stem = re.sub(r"-Abstract(?:-Conference)?$", "", stem, flags=re.I)
    return stem or hashlib.sha1(url.encode()).hexdigest()


def parse_index(html: str, year: int, source_index_url: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    records: list[dict] = []
    seen: set[str] = set()

    for a in soup.find_all("a", href=True):
        href = a.get("href", "")
        h = href.lower()
        if "abstract" not in h or f"/{year}/" not in h:
            continue
        title = clean_text(a.get_text(" ", strip=True))
        if not title:
            continue
        source_url = urljoin(source_index_url, href)
        uid = uid_from_url(source_url)
        if uid in seen:
            continue
        container = nearest_record_container(a)
        raw_authors, track = extract_authors_and_track(a, container, title)
        authors = split_authors(raw_authors)
        if not authors:
            continue
        records.append({
            "paper_uid": f"iclr-{year}-{uid}",
            "external_id": uid,
            "title": title,
            "track": track or "Conference",
            "source_url": source_url,
            "source_index_url": source_index_url,
            "raw_author_text": raw_authors,
            "authors": authors,
        })
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
        """
    )


def replace_year(con: sqlite3.Connection, year: int, records: list[dict], fetched_at: str) -> None:
    with con:
        con.execute("DELETE FROM papers WHERE venue=? AND year=?", ("ICLR", year))
        for rec in records:
            con.execute(
                """INSERT INTO papers
                (paper_uid, venue, year, external_id, title, track, source_url, source_index_url, raw_author_text, fetched_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (rec["paper_uid"], "ICLR", year, rec["external_id"], rec["title"], rec["track"],
                 rec["source_url"], rec["source_index_url"], rec["raw_author_text"], fetched_at),
            )
            con.executemany(
                "INSERT INTO paper_authors (paper_uid, author_position, author_name, author_key) VALUES (?, ?, ?, ?)",
                [(rec["paper_uid"], pos, name, person_key(name)) for pos, name in enumerate(rec["authors"], 1)],
            )


def print_summary(con: sqlite3.Connection) -> None:
    print("\nDatabase summary:")
    for year, papers, authors in con.execute(
        """SELECT p.year, COUNT(DISTINCT p.paper_uid), COUNT(a.author_name)
           FROM papers p JOIN paper_authors a USING (paper_uid)
           WHERE p.venue='ICLR' GROUP BY p.year ORDER BY p.year"""
    ):
        print(f"  {year}: {papers} papers; {authors} paper-author rows")
    n = con.execute(
        "SELECT COUNT(DISTINCT author_key) FROM paper_authors a JOIN papers p USING (paper_uid) WHERE p.venue='ICLR'"
    ).fetchone()[0]
    print(f"Distinct conservative author keys: {n}")
    print("Note: author keys are NOT entity-resolved people.")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", nargs="+", type=int, default=[2024, 2025, 2026])
    ap.add_argument("--db", default="iclr_papers.sqlite")
    ap.add_argument("--delay", type=float, default=1.0)
    ap.add_argument("--min-papers", type=int, default=100)
    args = ap.parse_args()

    db_path = Path(args.db)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db_path)
    init_db(con)
    session = make_session()

    for n, year in enumerate(args.years):
        try:
            url = index_url(year)
            print(f"Fetching ICLR {year}: {url}")
            records = parse_index(fetch(session, url), year, url)
            if len(records) < args.min_papers:
                raise RuntimeError(
                    f"Only {len(records)} papers parsed from {url}; expected at least {args.min_papers}. "
                    "Database rows for this year were NOT replaced."
                )
            replace_year(con, year, records, datetime.now(timezone.utc).isoformat())
            print(f"  parsed {len(records)} papers and {sum(len(r['authors']) for r in records)} paper-author rows")
        except Exception as exc:
            print(f"  ERROR for {year}: {exc}")
        if n + 1 < len(args.years):
            time.sleep(max(0.0, args.delay))

    print_summary(con)
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
