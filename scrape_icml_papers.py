#!/usr/bin/env python3
"""Collect accepted ICML paper titles and authors from official PMLR volumes.

Example:
  python scrape_icml_papers.py --years 2022 2023 2024 2025 --db data/icml_papers.sqlite
"""

from __future__ import annotations

import argparse
import re
import sqlite3
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup, Tag

USER_AGENT = (
    "AcademicMetadataCollector/1.0 "
    "(public conference proceedings metadata; contact: local research script)"
)

# Official PMLR volume numbers for ICML.
ICML_VOLUMES = {
    2015: 37,
    2016: 48,
    2017: 70,
    2018: 80,
    2019: 97,
    2020: 119,
    2021: 139,
    2022: 162,
    2023: 202,
    2024: 235,
    2025: 267,
}


def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def person_key(name: str) -> str:
    s = unicodedata.normalize("NFKD", name)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = s.casefold()
    return re.sub(r"[^a-z0-9]+", "", s)


def index_url(year: int) -> tuple[int, str]:
    if year not in ICML_VOLUMES:
        raise ValueError(
            f"No ICML PMLR volume is configured for {year}. "
            "Update ICML_VOLUMES after the official PMLR volume is published."
        )
    volume = ICML_VOLUMES[year]
    return volume, f"https://proceedings.mlr.press/v{volume}/"


def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"})
    s.mount("https://", requests.adapters.HTTPAdapter(max_retries=3))
    return s


def fetch(session: requests.Session, url: str, timeout: int = 60) -> str:
    r = session.get(url, timeout=timeout)
    r.raise_for_status()
    return r.text


def find_abs_link(block: Tag) -> Tag | None:
    for a in block.find_all("a", href=True):
        if clean_text(a.get_text(" ", strip=True)).strip("[]").casefold() == "abs":
            return a
        href = a.get("href", "")
        if re.search(r"/v\d+/[^/]+\.html$", href):
            return a
    return None


def parse_paper_block(block: Tag, year: int, volume: int, source_index_url: str) -> dict | None:
    title_el = block.select_one(".title, p.title, h3.title, h4.title")
    authors_el = block.select_one(".authors, span.authors")
    details_el = block.select_one(".details, p.details")
    abs_link = find_abs_link(block)

    title = clean_text(title_el.get_text(" ", strip=True)) if title_el else ""
    if not title:
        # Fallback: title is often the first substantial text before details.
        strings = [clean_text(x) for x in block.stripped_strings]
        for s in strings:
            if s and s.lower() not in {"abs", "download pdf", "openreview", "software"} and "Proceedings of" not in s:
                title = s
                break

    if authors_el:
        raw_authors = clean_text(authors_el.get_text(" ", strip=True))
    elif details_el:
        raw_details = clean_text(details_el.get_text(" ", strip=True))
        raw_authors = raw_details.split(";", 1)[0].strip()
    else:
        raw_authors = ""
        # Search text containing the standard '; Proceedings of ...' marker.
        for s in [clean_text(x) for x in block.stripped_strings]:
            if ";" in s and "Proceedings of" in s:
                raw_authors = s.split(";", 1)[0].strip()
                break

    if not title or not raw_authors or abs_link is None:
        return None

    authors = [clean_text(x) for x in re.split(r",\s*", raw_authors) if clean_text(x)]
    if not authors:
        return None

    source_url = urljoin(source_index_url, abs_link.get("href", ""))
    external_id = Path(urlparse(source_url).path).stem
    return {
        "paper_uid": f"icml-{year}-{external_id}",
        "external_id": external_id,
        "title": title,
        "track": "Conference",
        "pmlr_volume": volume,
        "source_url": source_url,
        "source_index_url": source_index_url,
        "raw_author_text": raw_authors,
        "authors": authors,
    }


def parse_index(html: str, year: int, volume: int, source_index_url: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    records: list[dict] = []
    seen: set[str] = set()

    blocks = soup.select("div.paper, article.paper, li.paper")

    # Current PMLR pages use paper blocks. Fallback: climb from each [abs] link.
    if not blocks:
        candidate_blocks: list[Tag] = []
        seen_nodes: set[int] = set()
        for a in soup.find_all("a", href=True):
            if clean_text(a.get_text(" ", strip=True)).strip("[]").casefold() != "abs":
                continue
            parent = a.parent if isinstance(a.parent, Tag) else None
            chosen = None
            for _ in range(8):
                if not isinstance(parent, Tag):
                    break
                txt = clean_text(parent.get_text(" ", strip=True))
                if parent.select_one(".title, p.title") is not None or ("Proceedings of" in txt and ";" in txt):
                    chosen = parent
                    break
                parent = parent.parent if isinstance(parent.parent, Tag) else None
            if isinstance(chosen, Tag) and id(chosen) not in seen_nodes:
                candidate_blocks.append(chosen)
                seen_nodes.add(id(chosen))
        blocks = candidate_blocks

    for block in blocks:
        rec = parse_paper_block(block, year, volume, source_index_url)
        if rec is None:
            continue
        if rec["external_id"] in seen:
            continue
        records.append(rec)
        seen.add(rec["external_id"])

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
            pmlr_volume INTEGER,
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
        con.execute("DELETE FROM papers WHERE venue=? AND year=?", ("ICML", year))
        for rec in records:
            con.execute(
                """INSERT INTO papers
                (paper_uid, venue, year, external_id, title, track, pmlr_volume, source_url,
                 source_index_url, raw_author_text, fetched_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (rec["paper_uid"], "ICML", year, rec["external_id"], rec["title"], rec["track"],
                 rec["pmlr_volume"], rec["source_url"], rec["source_index_url"],
                 rec["raw_author_text"], fetched_at),
            )
            con.executemany(
                "INSERT INTO paper_authors (paper_uid, author_position, author_name, author_key) VALUES (?, ?, ?, ?)",
                [(rec["paper_uid"], pos, name, person_key(name)) for pos, name in enumerate(rec["authors"], 1)],
            )


def print_summary(con: sqlite3.Connection) -> None:
    print("\nDatabase summary:")
    for year, volume, papers, authors in con.execute(
        """SELECT p.year, MAX(p.pmlr_volume), COUNT(DISTINCT p.paper_uid), COUNT(a.author_name)
           FROM papers p JOIN paper_authors a USING (paper_uid)
           WHERE p.venue='ICML' GROUP BY p.year ORDER BY p.year"""
    ):
        print(f"  {year} / PMLR v{volume}: {papers} papers; {authors} paper-author rows")
    n = con.execute(
        "SELECT COUNT(DISTINCT author_key) FROM paper_authors a JOIN papers p USING (paper_uid) WHERE p.venue='ICML'"
    ).fetchone()[0]
    print(f"Distinct conservative author keys: {n}")
    print("Note: author keys are NOT entity-resolved people.")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", nargs="+", type=int, default=[2022, 2023, 2024, 2025])
    ap.add_argument("--db", default="icml_papers.sqlite")
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
            volume, url = index_url(year)
            print(f"Fetching ICML {year} / PMLR v{volume}: {url}")
            records = parse_index(fetch(session, url), year, volume, url)
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
