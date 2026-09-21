#!/usr/bin/env python3
"""
enrich_ml_with_dblp.py

Offline DBLP enrichment for the NeurIPS / ICML / ICLR paper databases created
by the companion scrapers in this project.

The script reuses the same official DBLP monthly XML snapshot used by
`enrich_reviewers_with_dblp.py` and does NOT query the live DBLP API.

Workflow
--------
1. Read all local ML papers/authors from SQLite databases in data/.
2. Stream the DBLP snapshot and match publications by:
       venue + year + normalized exact title
3. For uniquely matched papers, align local authors with the DBLP author
   signatures in publication order.
4. Record publication-level ORCIDs from DBLP author elements when available.
5. Stream the DBLP snapshot a second time and map only the author signatures
   seen on matched ML papers to persistent DBLP person PIDs.
6. Store person-record aliases, affiliations, and ORCIDs.
7. If data/entity_resolution.sqlite exists, export high-confidence DB<->ML
   overlaps where the resolved DB reviewer and ML author have the same DBLP PID.

Why match papers first?
-----------------------
A name such as "Wei Wang" can refer to many people.  On a matched publication,
DBLP uses a disambiguated author signature (possibly with its "magic" four-digit
suffix).  DBLP documents that the exact case-sensitive author signature refers
to one author entity, and the corresponding aliases are collected in the
person/homepage record.  This gives us a much stronger identity signal than
searching ML authors by name alone.

Default inputs
--------------
data/neurips_papers.sqlite
data/icml_papers.sqlite
data/iclr_papers.sqlite
data/dblp_snapshot/dblp-2026-09-01.xml.gz
data/dblp_snapshot/dblp-2023-06-28.dtd

Default outputs
---------------
data/ml_entity_resolution.sqlite
data/ml_dblp_paper_resolution.csv
data/ml_dblp_author_occurrences.csv
data/ml_dblp_people.csv
data/db_ml_dblp_pid_matches.csv        (when reviewer enrichment is available)

Dependencies
------------
    pip install requests lxml

Run
---
    python scripts/enrich_ml_with_dblp.py --data-dir .\\data

Force rescanning the DBLP snapshot:
    python scripts/enrich_ml_with_dblp.py --data-dir .\\data --reindex

The first pass scans all DBLP records for matching ML publications.  The second
pass scans person/homepage records for only the DBLP author signatures found on
resolved ML papers.  Both passes stream and clear top-level XML records, so RAM
usage stays bounded.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
import re
import sqlite3
import sys
import time
import unicodedata
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Iterable

import requests
from lxml import etree


# ---------------------------------------------------------------------------
# Reproducible DBLP snapshot - same release as reviewer enrichment
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA_DIR = REPO_ROOT / "data"

SNAPSHOT_ID = "dblp-2026-09-01"
SNAPSHOT_FILENAME = "dblp-2026-09-01.xml.gz"
SNAPSHOT_URL = (
    "https://drops.dagstuhl.de/storage/artifacts/dblp/xml/2026/"
    "dblp-2026-09-01.xml.gz"
)
SNAPSHOT_MD5 = "41abb87b4e8b8a2e3c470efd6528ad88"

DTD_FILENAME = "dblp-2023-06-28.dtd"
DTD_URL = (
    "https://drops.dagstuhl.de/storage/artifacts/dblp/xml/2023/"
    "dblp-2023-06-28.dtd"
)
DTD_MD5 = "96e4283e71886dec2173718957293648"

OUTPUT_DB = "ml_entity_resolution.sqlite"
PAPER_CSV = "ml_dblp_paper_resolution.csv"
AUTHOR_CSV = "ml_dblp_author_occurrences.csv"
PEOPLE_CSV = "ml_dblp_people.csv"
OVERLAP_CSV = "db_ml_dblp_pid_matches.csv"

TARGET_VENUES = {"NeurIPS", "ICML", "ICLR"}

SPACE_RE = re.compile(r"\s+")
NON_ALNUM_RE = re.compile(r"[^a-z0-9 ]+")
DBLP_MAGIC_SUFFIX_RE = re.compile(r"\s+\d{4}$")
ORCID_RE = re.compile(
    r"(?:https?://orcid\.org/)?(\d{4}-\d{4}-\d{4}-[\dXx]{4})"
)


# ---------------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------------


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    return SPACE_RE.sub(" ", str(value).strip())


def fold_ascii(value: Any) -> str:
    s = clean_text(value)
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return s


def normalize_name(value: Any) -> str:
    s = fold_ascii(value).casefold()
    s = s.replace("-", " ")
    s = NON_ALNUM_RE.sub(" ", s)
    return SPACE_RE.sub(" ", s).strip()


def dblp_author_base_key(value: Any) -> str:
    # Strip DBLP's homonym-disambiguation suffix for human-name comparison only.
    s = clean_text(value)
    s = DBLP_MAGIC_SUFFIX_RE.sub("", s)
    return normalize_name(s)


def normalize_title(value: Any) -> str:
    """Strong-but-practical title key for paper matching.

    Punctuation, capitalization, accents, HTML layout, and Unicode dash/quote
    differences are ignored.  We deliberately do not remove words or perform
    fuzzy title matching automatically.
    """
    s = fold_ascii(value).casefold()
    s = (
        s.replace("&", " and ")
        .replace("−", "-")
        .replace("–", "-")
        .replace("—", "-")
        .replace("’", "'")
        .replace("‘", "'")
        .replace("“", '"')
        .replace("”", '"')
    )
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return SPACE_RE.sub(" ", s).strip()


def first_last_key(name_key: str) -> str:
    toks = name_key.split()
    if len(toks) < 2:
        return ""
    return f"{toks[0]} {toks[-1]}"


def initial_last_key(name_key: str) -> str:
    toks = name_key.split()
    if len(toks) < 2 or not toks[0]:
        return ""
    return f"{toks[0][0]} {toks[-1]}"


def author_name_score(local_name: str, dblp_name: str) -> tuple[float, str]:
    """Return a conservative name-compatibility score and reason."""
    a = normalize_name(local_name)
    b = dblp_author_base_key(dblp_name)
    if not a or not b:
        return 0.0, "empty"
    if a == b:
        return 1.0, "exact_normalized"

    at = a.split()
    bt = b.split()
    if len(at) >= 2 and len(bt) >= 2:
        if first_last_key(a) == first_last_key(b):
            return 0.92, "same_first_last"
        if initial_last_key(a) == initial_last_key(b):
            return 0.84, "same_initial_last"

    ratio = SequenceMatcher(None, a, b).ratio()
    if ratio >= 0.94:
        return 0.80, "high_string_similarity"
    if ratio >= 0.88:
        return 0.70, "moderate_string_similarity"
    return 0.0, "incompatible"


def extract_orcid(value: str) -> str | None:
    m = ORCID_RE.search(value or "")
    return m.group(1).upper() if m else None


def md5_file(path: Path, chunk_size: int = 8 * 1024 * 1024) -> str:
    h = hashlib.md5()
    with path.open("rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def source_fingerprint(conn: sqlite3.Connection) -> str:
    h = hashlib.sha256()
    for row in conn.execute(
        """
        SELECT source_db, paper_uid, venue, year, title, local_author_count
        FROM ml_papers
        ORDER BY source_db, paper_uid
        """
    ):
        h.update("\t".join("" if v is None else str(v) for v in row).encode("utf-8"))
        h.update(b"\n")
    for row in conn.execute(
        """
        SELECT source_db, paper_uid, author_position, author_name
        FROM ml_authors
        ORDER BY source_db, paper_uid, author_position
        """
    ):
        h.update("\t".join("" if v is None else str(v) for v in row).encode("utf-8"))
        h.update(b"\n")
    return h.hexdigest()


def table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return bool(
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (name,),
        ).fetchone()
    )


def table_columns(conn: sqlite3.Connection, name: str) -> list[str]:
    return [r[1] for r in conn.execute(f'PRAGMA table_info("{name}")')]


# ---------------------------------------------------------------------------
# Local ML data ingestion
# ---------------------------------------------------------------------------


def discover_ml_databases(data_dir: Path, output_db: Path) -> list[Path]:
    result: list[Path] = []
    for path in sorted(data_dir.glob("*.sqlite")):
        if path.resolve() == output_db.resolve():
            continue
        try:
            c = sqlite3.connect(path)
            try:
                if not table_exists(c, "papers") or not table_exists(c, "paper_authors"):
                    continue
                pcols = set(table_columns(c, "papers"))
                acols = set(table_columns(c, "paper_authors"))
                needed_p = {"paper_uid", "venue", "year", "title"}
                needed_a = {"paper_uid", "author_position", "author_name"}
                if not needed_p <= pcols or not needed_a <= acols:
                    continue
                venues = {
                    r[0]
                    for r in c.execute("SELECT DISTINCT venue FROM papers")
                    if r[0]
                }
                if venues & TARGET_VENUES:
                    result.append(path)
            finally:
                c.close()
        except sqlite3.Error:
            continue
    return result


def create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        PRAGMA journal_mode=WAL;

        CREATE TABLE IF NOT EXISTS ml_papers (
            source_db TEXT NOT NULL,
            paper_uid TEXT NOT NULL,
            venue TEXT NOT NULL,
            year INTEGER NOT NULL,
            external_id TEXT,
            title TEXT NOT NULL,
            title_key TEXT NOT NULL,
            track TEXT,
            source_url TEXT,
            local_author_count INTEGER NOT NULL,
            PRIMARY KEY (source_db, paper_uid)
        );
        CREATE INDEX IF NOT EXISTS idx_ml_papers_lookup
            ON ml_papers(venue, year, title_key);

        CREATE TABLE IF NOT EXISTS ml_authors (
            source_db TEXT NOT NULL,
            paper_uid TEXT NOT NULL,
            author_position INTEGER NOT NULL,
            author_name TEXT NOT NULL,
            author_key TEXT NOT NULL,
            PRIMARY KEY (source_db, paper_uid, author_position)
        );
        CREATE INDEX IF NOT EXISTS idx_ml_authors_key ON ml_authors(author_key);

        CREATE TABLE IF NOT EXISTS dblp_publications (
            dblp_key TEXT PRIMARY KEY,
            record_type TEXT NOT NULL,
            venue TEXT NOT NULL,
            year INTEGER NOT NULL,
            title TEXT NOT NULL,
            title_key TEXT NOT NULL,
            ee_json TEXT NOT NULL,
            snapshot_id TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS dblp_publication_authors (
            dblp_key TEXT NOT NULL,
            author_position INTEGER NOT NULL,
            dblp_author_name TEXT NOT NULL,
            dblp_author_key TEXT NOT NULL,
            publication_orcid TEXT,
            PRIMARY KEY (dblp_key, author_position)
        );
        CREATE INDEX IF NOT EXISTS idx_dblp_pub_auth_name
            ON dblp_publication_authors(dblp_author_name);

        CREATE TABLE IF NOT EXISTS paper_candidates (
            source_db TEXT NOT NULL,
            paper_uid TEXT NOT NULL,
            dblp_key TEXT NOT NULL,
            PRIMARY KEY (source_db, paper_uid, dblp_key)
        );

        CREATE TABLE IF NOT EXISTS paper_resolution (
            source_db TEXT NOT NULL,
            paper_uid TEXT NOT NULL,
            status TEXT NOT NULL,
            resolved_dblp_key TEXT,
            candidate_count INTEGER NOT NULL,
            local_author_count INTEGER NOT NULL,
            dblp_author_count INTEGER,
            aligned_author_count INTEGER NOT NULL DEFAULT 0,
            author_alignment_score REAL,
            notes TEXT,
            PRIMARY KEY (source_db, paper_uid)
        );

        CREATE TABLE IF NOT EXISTS author_occurrences (
            source_db TEXT NOT NULL,
            paper_uid TEXT NOT NULL,
            venue TEXT NOT NULL,
            year INTEGER NOT NULL,
            paper_title TEXT NOT NULL,
            local_author_position INTEGER NOT NULL,
            local_author_name TEXT NOT NULL,
            local_author_key TEXT NOT NULL,
            dblp_key TEXT,
            dblp_author_position INTEGER,
            dblp_author_name TEXT,
            dblp_author_key TEXT,
            author_name_score REAL,
            author_name_match_type TEXT,
            publication_orcid TEXT,
            dblp_pid TEXT,
            dblp_canonical_name TEXT,
            person_orcids_json TEXT NOT NULL DEFAULT '[]',
            person_affiliations_json TEXT NOT NULL DEFAULT '[]',
            best_orcid TEXT,
            best_orcid_source TEXT,
            resolution_status TEXT NOT NULL,
            PRIMARY KEY (source_db, paper_uid, local_author_position)
        );
        CREATE INDEX IF NOT EXISTS idx_author_occurrences_pid
            ON author_occurrences(dblp_pid);
        CREATE INDEX IF NOT EXISTS idx_author_occurrences_orcid
            ON author_occurrences(best_orcid);

        CREATE TABLE IF NOT EXISTS dblp_people (
            dblp_pid TEXT PRIMARY KEY,
            canonical_name TEXT NOT NULL,
            aliases_json TEXT NOT NULL,
            affiliations_json TEXT NOT NULL,
            person_orcids_json TEXT NOT NULL,
            source_key TEXT NOT NULL,
            snapshot_id TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS dblp_alias_pid (
            dblp_author_name TEXT NOT NULL,
            dblp_pid TEXT NOT NULL,
            PRIMARY KEY (dblp_author_name, dblp_pid)
        );

        CREATE TABLE IF NOT EXISTS local_index_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        """
    )
    conn.commit()


def ingest_ml_databases(
    conn: sqlite3.Connection,
    db_paths: list[Path],
    venues: set[str],
    years: set[int] | None,
    max_papers: int | None,
) -> None:
    conn.execute("DELETE FROM ml_authors")
    conn.execute("DELETE FROM ml_papers")
    conn.commit()

    remaining = max_papers

    for path in db_paths:
        src = sqlite3.connect(path)
        src.row_factory = sqlite3.Row
        try:
            pcols = set(table_columns(src, "papers"))
            sql = "SELECT * FROM papers ORDER BY year, paper_uid"
            paper_rows = src.execute(sql)
            loaded_papers = 0
            loaded_authors = 0

            for prow in paper_rows:
                p = dict(prow)
                venue = clean_text(p.get("venue"))
                if venue not in venues:
                    continue
                try:
                    year = int(p.get("year"))
                except (TypeError, ValueError):
                    continue
                if years is not None and year not in years:
                    continue
                if remaining is not None and remaining <= 0:
                    break

                paper_uid = str(p.get("paper_uid"))
                authors = src.execute(
                    """
                    SELECT author_position, author_name
                    FROM paper_authors
                    WHERE paper_uid=?
                    ORDER BY author_position
                    """,
                    (paper_uid,),
                ).fetchall()

                title = clean_text(p.get("title"))
                conn.execute(
                    """
                    INSERT INTO ml_papers (
                        source_db, paper_uid, venue, year, external_id,
                        title, title_key, track, source_url, local_author_count
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        path.name,
                        paper_uid,
                        venue,
                        year,
                        clean_text(p.get("external_id")) if "external_id" in pcols else "",
                        title,
                        normalize_title(title),
                        clean_text(p.get("track")) if "track" in pcols else "",
                        clean_text(p.get("source_url")) if "source_url" in pcols else "",
                        len(authors),
                    ),
                )
                for pos, name in authors:
                    name = clean_text(name)
                    conn.execute(
                        """
                        INSERT INTO ml_authors (
                            source_db, paper_uid, author_position,
                            author_name, author_key
                        ) VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            path.name,
                            paper_uid,
                            int(pos),
                            name,
                            normalize_name(name),
                        ),
                    )
                    loaded_authors += 1

                loaded_papers += 1
                if remaining is not None:
                    remaining -= 1

            conn.commit()
            print(
                f"Loaded {loaded_papers:6d} papers / {loaded_authors:7d} authors "
                f"from {path.name}"
            )
        finally:
            src.close()

        if remaining is not None and remaining <= 0:
            break


# ---------------------------------------------------------------------------
# Snapshot handling
# ---------------------------------------------------------------------------


def download_file(url: str, dest: Path, expected_md5: str, label: str) -> None:
    if dest.exists():
        print(f"Checking existing {label}: {dest.name}")
        actual = md5_file(dest)
        if actual.lower() == expected_md5.lower():
            print(f"  MD5 OK: {actual}")
            return
        print(f"  MD5 mismatch ({actual}); downloading a fresh copy.")
        dest.unlink()

    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + ".part")
    existing = part.stat().st_size if part.exists() else 0
    headers = {
        "User-Agent": "db-ml-entity-resolution-research/1.0",
        "Accept": "*/*",
    }
    if existing:
        headers["Range"] = f"bytes={existing}-"
        print(f"Resuming {label} at {existing / (1024**2):.1f} MiB...")
    else:
        print(f"Downloading {label}...")

    with requests.get(
        url,
        headers=headers,
        stream=True,
        timeout=(30, 120),
        allow_redirects=True,
    ) as r:
        r.raise_for_status()
        append = existing > 0 and r.status_code == 206
        if existing and not append:
            existing = 0
        total_header = r.headers.get("Content-Length")
        total = int(total_header) + existing if total_header else None
        mode = "ab" if append else "wb"
        downloaded = existing
        last = time.monotonic()
        with part.open(mode) as f:
            for chunk in r.iter_content(chunk_size=4 * 1024 * 1024):
                if not chunk:
                    continue
                f.write(chunk)
                downloaded += len(chunk)
                if time.monotonic() - last >= 5:
                    if total:
                        print(
                            f"  {downloaded/(1024**2):,.1f} / "
                            f"{total/(1024**2):,.1f} MiB "
                            f"({100*downloaded/total:.1f}%)"
                        )
                    else:
                        print(f"  {downloaded/(1024**2):,.1f} MiB")
                    last = time.monotonic()

    part.replace(dest)
    actual = md5_file(dest)
    if actual.lower() != expected_md5.lower():
        raise RuntimeError(
            f"{label} MD5 mismatch: expected {expected_md5}, got {actual}"
        )
    print(f"  MD5 OK: {actual}")


class LocalDTDResolver(etree.Resolver):
    def __init__(self, dtd_path: Path):
        super().__init__()
        self.dtd_path = dtd_path

    def resolve(self, url, pubid, context):
        if str(url).lower().endswith(".dtd"):
            return self.resolve_filename(str(self.dtd_path), context)
        return None


def venue_from_dblp_key(key: str) -> str | None:
    low = key.casefold()
    if low.startswith("conf/icml/"):
        return "ICML"
    if low.startswith("conf/iclr/"):
        return "ICLR"
    if low.startswith("conf/nips/") or low.startswith("conf/neurips/"):
        return "NeurIPS"
    return None


def publication_record(elem: etree._Element) -> dict[str, Any] | None:
    key = clean_text(elem.attrib.get("key", ""))
    venue = venue_from_dblp_key(key)
    if venue is None:
        return None

    title = ""
    year: int | None = None
    authors: list[tuple[str, str | None]] = []
    ees: list[str] = []

    for child in elem:
        tag = etree.QName(child).localname
        text = clean_text("".join(child.itertext()))
        if tag == "title":
            title = text
        elif tag == "year":
            try:
                year = int(text)
            except (TypeError, ValueError):
                year = None
        elif tag == "author" and text:
            authors.append((text, extract_orcid(child.attrib.get("orcid", ""))))
        elif tag == "ee" and text:
            ees.append(text)

    if not key or not title or year is None or not authors:
        return None

    return {
        "dblp_key": key,
        "record_type": etree.QName(elem).localname,
        "venue": venue,
        "year": year,
        "title": title,
        "title_key": normalize_title(title),
        "authors": authors,
        "ees": ees,
    }


def build_paper_target_map(
    conn: sqlite3.Connection,
) -> dict[tuple[str, int, str], list[tuple[str, str]]]:
    result: dict[tuple[str, int, str], list[tuple[str, str]]] = defaultdict(list)
    for source_db, paper_uid, venue, year, title_key in conn.execute(
        "SELECT source_db, paper_uid, venue, year, title_key FROM ml_papers"
    ):
        result[(venue, int(year), title_key)].append((source_db, paper_uid))
    return result


def clear_dblp_index(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        DELETE FROM paper_candidates;
        DELETE FROM dblp_publication_authors;
        DELETE FROM dblp_publications;
        DELETE FROM paper_resolution;
        DELETE FROM author_occurrences;
        DELETE FROM dblp_alias_pid;
        DELETE FROM dblp_people;
        """
    )
    conn.commit()


def scan_publications(
    conn: sqlite3.Connection,
    snapshot_gz: Path,
    dtd_path: Path,
) -> None:
    targets = build_paper_target_map(conn)

    parser = etree.XMLPullParser(
        events=("end",),
        load_dtd=True,
        resolve_entities=True,
        no_network=True,
        huge_tree=True,
        recover=False,
    )
    parser.resolvers.add(LocalDTDResolver(dtd_path))

    top_level = 0
    relevant_venue_records = 0
    matched_dblp_records = 0
    candidate_links = 0
    last_report = time.monotonic()

    print(
        f"Streaming {snapshot_gz.name} for NeurIPS/ICML/ICLR publication matches..."
    )

    with gzip.open(snapshot_gz, "rb") as f:
        while True:
            chunk = f.read(1 * 1024 * 1024)
            if not chunk:
                break
            parser.feed(chunk)

            for _event, elem in parser.read_events():
                parent = elem.getparent()
                if parent is None or parent.getparent() is not None:
                    continue

                top_level += 1
                tag = etree.QName(elem).localname
                key = clean_text(elem.attrib.get("key", ""))
                venue = venue_from_dblp_key(key)

                if venue is not None and tag in {"inproceedings", "article"}:
                    relevant_venue_records += 1
                    rec = publication_record(elem)
                    if rec:
                        target_key = (rec["venue"], rec["year"], rec["title_key"])
                        local_targets = targets.get(target_key, [])
                        if local_targets:
                            matched_dblp_records += 1
                            conn.execute(
                                """
                                INSERT OR REPLACE INTO dblp_publications (
                                    dblp_key, record_type, venue, year, title,
                                    title_key, ee_json, snapshot_id
                                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                                """,
                                (
                                    rec["dblp_key"], rec["record_type"], rec["venue"],
                                    rec["year"], rec["title"], rec["title_key"],
                                    json.dumps(rec["ees"], ensure_ascii=False), SNAPSHOT_ID,
                                ),
                            )
                            conn.execute(
                                "DELETE FROM dblp_publication_authors WHERE dblp_key=?",
                                (rec["dblp_key"],),
                            )
                            for pos, (author_name, publication_orcid) in enumerate(
                                rec["authors"], start=1
                            ):
                                conn.execute(
                                    """
                                    INSERT INTO dblp_publication_authors (
                                        dblp_key, author_position, dblp_author_name,
                                        dblp_author_key, publication_orcid
                                    ) VALUES (?, ?, ?, ?, ?)
                                    """,
                                    (
                                        rec["dblp_key"], pos, author_name,
                                        dblp_author_base_key(author_name),
                                        publication_orcid,
                                    ),
                                )

                            for source_db, paper_uid in local_targets:
                                conn.execute(
                                    """
                                    INSERT OR IGNORE INTO paper_candidates (
                                        source_db, paper_uid, dblp_key
                                    ) VALUES (?, ?, ?)
                                    """,
                                    (source_db, paper_uid, rec["dblp_key"]),
                                )
                                candidate_links += 1

                # Critical for bounded RAM usage: clear every top-level record.
                elem.clear()
                while elem.getprevious() is not None:
                    del parent[0]

            if time.monotonic() - last_report >= 10:
                conn.commit()
                print(
                    f"  scanned {top_level:,} total records; "
                    f"{relevant_venue_records:,} target-venue records; "
                    f"{matched_dblp_records:,} DBLP records matched"
                )
                last_report = time.monotonic()

    parser.close()
    conn.commit()
    print(
        f"Finished publication scan: {top_level:,} total records; "
        f"{relevant_venue_records:,} target-venue records; "
        f"{matched_dblp_records:,} matching DBLP publications; "
        f"{candidate_links:,} paper-candidate links."
    )


# ---------------------------------------------------------------------------
# Author-list alignment
# ---------------------------------------------------------------------------


@dataclass
class AlignmentPair:
    local_index: int
    dblp_index: int
    score: float
    match_type: str


def align_authors(
    local_names: list[str],
    dblp_names: list[str],
) -> list[AlignmentPair]:
    """Order-aware dynamic-programming alignment of two author lists.

    Incompatible names receive a negative pairing reward, so insertion/deletion
    gaps are preferred when an author is genuinely missing from one source.
    """
    n, m = len(local_names), len(dblp_names)
    gap_penalty = -0.55

    dp = [[0.0] * (m + 1) for _ in range(n + 1)]
    back: list[list[tuple[str, float, str] | None]] = [
        [None] * (m + 1) for _ in range(n + 1)
    ]

    for i in range(1, n + 1):
        dp[i][0] = i * gap_penalty
        back[i][0] = ("up", 0.0, "gap")
    for j in range(1, m + 1):
        dp[0][j] = j * gap_penalty
        back[0][j] = ("left", 0.0, "gap")

    for i in range(1, n + 1):
        for j in range(1, m + 1):
            score, kind = author_name_score(local_names[i - 1], dblp_names[j - 1])
            pair_reward = (2.0 * score - 1.0) if score > 0 else -1.0
            opts = [
                (dp[i - 1][j - 1] + pair_reward, "diag", score, kind),
                (dp[i - 1][j] + gap_penalty, "up", 0.0, "gap"),
                (dp[i][j - 1] + gap_penalty, "left", 0.0, "gap"),
            ]
            best = max(opts, key=lambda x: x[0])
            dp[i][j] = best[0]
            back[i][j] = (best[1], best[2], best[3])

    pairs: list[AlignmentPair] = []
    i, j = n, m
    while i > 0 or j > 0:
        step = back[i][j]
        if step is None:
            break
        direction, score, kind = step
        if direction == "diag":
            if score >= 0.65:
                pairs.append(
                    AlignmentPair(
                        local_index=i - 1,
                        dblp_index=j - 1,
                        score=score,
                        match_type=kind,
                    )
                )
            i -= 1
            j -= 1
        elif direction == "up":
            i -= 1
        else:
            j -= 1

    pairs.reverse()
    return pairs


def resolve_papers_and_align_authors(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM paper_resolution")
    conn.execute("DELETE FROM author_occurrences")

    # Baseline row for every local author, even if the paper never matches DBLP.
    conn.execute(
        """
        INSERT INTO author_occurrences (
            source_db, paper_uid, venue, year, paper_title,
            local_author_position, local_author_name, local_author_key,
            resolution_status
        )
        SELECT
            a.source_db, a.paper_uid, p.venue, p.year, p.title,
            a.author_position, a.author_name, a.author_key,
            'paper_unresolved'
        FROM ml_authors a
        JOIN ml_papers p
          ON p.source_db=a.source_db AND p.paper_uid=a.paper_uid
        """
    )

    papers = conn.execute(
        """
        SELECT source_db, paper_uid, venue, year, title, local_author_count
        FROM ml_papers
        ORDER BY source_db, paper_uid
        """
    ).fetchall()

    for source_db, paper_uid, venue, year, title, local_count in papers:
        candidates = [
            r[0]
            for r in conn.execute(
                """
                SELECT dblp_key FROM paper_candidates
                WHERE source_db=? AND paper_uid=?
                ORDER BY dblp_key
                """,
                (source_db, paper_uid),
            )
        ]

        if not candidates:
            conn.execute(
                """
                INSERT INTO paper_resolution (
                    source_db, paper_uid, status, resolved_dblp_key,
                    candidate_count, local_author_count, dblp_author_count,
                    aligned_author_count, author_alignment_score, notes
                ) VALUES (?, ?, ?, NULL, 0, ?, NULL, 0, NULL, ?)
                """,
                (
                    source_db, paper_uid,
                    "unmatched_title", local_count,
                    "No exact normalized title+venue+year match in DBLP snapshot.",
                ),
            )
            continue

        if len(candidates) > 1:
            conn.execute(
                """
                INSERT INTO paper_resolution (
                    source_db, paper_uid, status, resolved_dblp_key,
                    candidate_count, local_author_count, dblp_author_count,
                    aligned_author_count, author_alignment_score, notes
                ) VALUES (?, ?, ?, NULL, ?, ?, NULL, 0, NULL, ?)
                """,
                (
                    source_db, paper_uid,
                    "ambiguous_title", len(candidates), local_count,
                    "Multiple DBLP publications share this normalized title, venue, and year.",
                ),
            )
            continue

        dblp_key = candidates[0]
        local_authors = conn.execute(
            """
            SELECT author_position, author_name
            FROM ml_authors
            WHERE source_db=? AND paper_uid=?
            ORDER BY author_position
            """,
            (source_db, paper_uid),
        ).fetchall()
        dblp_authors = conn.execute(
            """
            SELECT author_position, dblp_author_name, publication_orcid
            FROM dblp_publication_authors
            WHERE dblp_key=?
            ORDER BY author_position
            """,
            (dblp_key,),
        ).fetchall()

        pairs = align_authors(
            [x[1] for x in local_authors],
            [x[1] for x in dblp_authors],
        )
        coverage = len(pairs) / max(1, len(local_authors))
        avg_score = (
            sum(p.score for p in pairs) / len(pairs)
            if pairs else 0.0
        )

        if len(local_authors) == len(dblp_authors) and coverage == 1.0 and avg_score >= 0.98:
            status = "resolved_exact_title_authors"
            notes = "Unique title match; all author positions match exactly after normalization."
        elif coverage >= 0.90 and avg_score >= 0.80:
            status = "resolved_title_author_variants"
            notes = "Unique title match with strong order-aware author agreement."
        else:
            status = "resolved_title_low_author_agreement"
            notes = (
                "Unique exact title+venue+year match, but author lists differ enough "
                "to require caution for unmatched author positions."
            )

        conn.execute(
            """
            INSERT INTO paper_resolution (
                source_db, paper_uid, status, resolved_dblp_key,
                candidate_count, local_author_count, dblp_author_count,
                aligned_author_count, author_alignment_score, notes
            ) VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?, ?)
            """,
            (
                source_db, paper_uid, status, dblp_key,
                len(local_authors), len(dblp_authors), len(pairs), avg_score, notes,
            ),
        )

        for pair in pairs:
            local_pos, local_name = local_authors[pair.local_index]
            dblp_pos, dblp_name, publication_orcid = dblp_authors[pair.dblp_index]
            conn.execute(
                """
                UPDATE author_occurrences
                SET dblp_key=?,
                    dblp_author_position=?,
                    dblp_author_name=?,
                    dblp_author_key=?,
                    author_name_score=?,
                    author_name_match_type=?,
                    publication_orcid=?,
                    resolution_status='dblp_signature_aligned'
                WHERE source_db=? AND paper_uid=? AND local_author_position=?
                """,
                (
                    dblp_key,
                    int(dblp_pos),
                    dblp_name,
                    dblp_author_base_key(dblp_name),
                    pair.score,
                    pair.match_type,
                    publication_orcid,
                    source_db,
                    paper_uid,
                    int(local_pos),
                ),
            )

    conn.commit()


# ---------------------------------------------------------------------------
# DBLP person/PID pass
# ---------------------------------------------------------------------------


def person_record_from_www(elem: etree._Element) -> dict[str, Any] | None:
    key = clean_text(elem.attrib.get("key", ""))
    if not key.startswith("homepages/"):
        return None

    aliases: list[str] = []
    affiliations: list[str] = []
    orcids: set[str] = set()
    crossref = ""

    for child in elem:
        tag = etree.QName(child).localname
        text = clean_text("".join(child.itertext()))
        if tag == "author" and text:
            aliases.append(text)
        elif tag == "note" and child.attrib.get("type") == "affiliation" and text:
            affiliations.append(text)
        elif tag == "url" and text:
            oc = extract_orcid(text)
            if oc:
                orcids.add(oc)
        elif tag == "crossref" and text:
            crossref = text

    if crossref and not aliases:
        return None
    if not aliases:
        return None

    return {
        "pid": key[len("homepages/"):],
        "source_key": key,
        "canonical_name": aliases[0],
        "aliases": sorted(set(aliases), key=str.casefold),
        "affiliations": sorted(set(affiliations), key=str.casefold),
        "orcids": sorted(orcids),
    }


def scan_people_for_author_signatures(
    conn: sqlite3.Connection,
    snapshot_gz: Path,
    dtd_path: Path,
) -> None:
    target_signatures = {
        r[0]
        for r in conn.execute(
            """
            SELECT DISTINCT dblp_author_name
            FROM author_occurrences
            WHERE dblp_author_name IS NOT NULL
            """
        )
    }

    conn.execute("DELETE FROM dblp_alias_pid")
    conn.execute("DELETE FROM dblp_people")
    conn.commit()

    print(
        f"Second DBLP pass: resolving {len(target_signatures):,} distinct "
        "publication author signatures to persistent person PIDs..."
    )

    parser = etree.XMLPullParser(
        events=("end",),
        load_dtd=True,
        resolve_entities=True,
        no_network=True,
        huge_tree=True,
        recover=False,
    )
    parser.resolvers.add(LocalDTDResolver(dtd_path))

    top_level = 0
    homepage_records = 0
    matched_people: set[str] = set()
    matched_aliases: set[str] = set()
    last_report = time.monotonic()

    with gzip.open(snapshot_gz, "rb") as f:
        while True:
            chunk = f.read(1 * 1024 * 1024)
            if not chunk:
                break
            parser.feed(chunk)

            for _event, elem in parser.read_events():
                parent = elem.getparent()
                if parent is None or parent.getparent() is not None:
                    continue

                top_level += 1
                tag = etree.QName(elem).localname
                if tag == "www":
                    homepage_records += 1
                    person = person_record_from_www(elem)
                    if person:
                        matching_aliases = [
                            a for a in person["aliases"] if a in target_signatures
                        ]
                        if matching_aliases:
                            matched_people.add(person["pid"])
                            conn.execute(
                                """
                                INSERT OR REPLACE INTO dblp_people (
                                    dblp_pid, canonical_name, aliases_json,
                                    affiliations_json, person_orcids_json,
                                    source_key, snapshot_id
                                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                                """,
                                (
                                    person["pid"],
                                    person["canonical_name"],
                                    json.dumps(person["aliases"], ensure_ascii=False),
                                    json.dumps(person["affiliations"], ensure_ascii=False),
                                    json.dumps(person["orcids"], ensure_ascii=False),
                                    person["source_key"],
                                    SNAPSHOT_ID,
                                ),
                            )
                            for alias in matching_aliases:
                                matched_aliases.add(alias)
                                conn.execute(
                                    """
                                    INSERT OR IGNORE INTO dblp_alias_pid (
                                        dblp_author_name, dblp_pid
                                    ) VALUES (?, ?)
                                    """,
                                    (alias, person["pid"]),
                                )

                elem.clear()
                while elem.getprevious() is not None:
                    del parent[0]

            if time.monotonic() - last_report >= 10:
                conn.commit()
                print(
                    f"  scanned {top_level:,} total records; "
                    f"{homepage_records:,} person records; "
                    f"{len(matched_aliases):,}/{len(target_signatures):,} "
                    "target signatures resolved"
                )
                last_report = time.monotonic()

    parser.close()
    conn.commit()
    print(
        f"Finished person scan: {len(matched_people):,} PIDs retained; "
        f"{len(matched_aliases):,}/{len(target_signatures):,} target signatures resolved."
    )


def apply_person_resolution(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        """
        SELECT source_db, paper_uid, local_author_position,
               dblp_author_name, publication_orcid
        FROM author_occurrences
        WHERE dblp_author_name IS NOT NULL
        """
    ).fetchall()

    for source_db, paper_uid, local_pos, dblp_name, publication_orcid in rows:
        pids = [
            r[0]
            for r in conn.execute(
                "SELECT dblp_pid FROM dblp_alias_pid WHERE dblp_author_name=?",
                (dblp_name,),
            )
        ]

        if len(pids) == 1:
            pid = pids[0]
            person = conn.execute(
                """
                SELECT canonical_name, affiliations_json, person_orcids_json
                FROM dblp_people WHERE dblp_pid=?
                """,
                (pid,),
            ).fetchone()
            if not person:
                continue
            canonical, aff_json, orcid_json = person
            person_orcids = json.loads(orcid_json or "[]")

            best_orcid = None
            best_source = None
            if len(person_orcids) == 1:
                best_orcid = person_orcids[0]
                best_source = "dblp_person_record"
            elif publication_orcid:
                best_orcid = publication_orcid
                best_source = "dblp_publication_author"

            conn.execute(
                """
                UPDATE author_occurrences
                SET dblp_pid=?,
                    dblp_canonical_name=?,
                    person_orcids_json=?,
                    person_affiliations_json=?,
                    best_orcid=?,
                    best_orcid_source=?,
                    resolution_status='resolved_dblp_pid'
                WHERE source_db=? AND paper_uid=? AND local_author_position=?
                """,
                (
                    pid, canonical, orcid_json, aff_json,
                    best_orcid, best_source,
                    source_db, paper_uid, local_pos,
                ),
            )
        elif len(pids) > 1:
            conn.execute(
                """
                UPDATE author_occurrences
                SET resolution_status='ambiguous_dblp_signature_pid'
                WHERE source_db=? AND paper_uid=? AND local_author_position=?
                """,
                (source_db, paper_uid, local_pos),
            )
        else:
            best_orcid = publication_orcid if publication_orcid else None
            best_source = "dblp_publication_author" if publication_orcid else None
            conn.execute(
                """
                UPDATE author_occurrences
                SET best_orcid=?, best_orcid_source=?,
                    resolution_status='dblp_signature_without_person_pid'
                WHERE source_db=? AND paper_uid=? AND local_author_position=?
                """,
                (
                    best_orcid, best_source,
                    source_db, paper_uid, local_pos,
                ),
            )

    conn.commit()


# ---------------------------------------------------------------------------
# Optional reviewer overlap by persistent DBLP PID
# ---------------------------------------------------------------------------


def export_db_reviewer_pid_overlap(
    conn: sqlite3.Connection,
    reviewer_db: Path,
    output_csv: Path,
) -> int:
    if not reviewer_db.exists():
        return 0

    review = sqlite3.connect(reviewer_db)
    try:
        if not table_exists(review, "name_resolution"):
            return 0
        reviewer_rows = review.execute(
            """
            SELECT name_key, representative_name, status,
                   resolved_dblp_pid, best_orcid
            FROM name_resolution
            WHERE resolved_dblp_pid IS NOT NULL
            """
        ).fetchall()
        by_pid: dict[str, list[tuple[Any, ...]]] = defaultdict(list)
        for row in reviewer_rows:
            by_pid[row[3]].append(row)
    finally:
        review.close()

    rows_out: list[list[Any]] = []
    for row in conn.execute(
        """
        SELECT dblp_pid, venue, year, paper_title,
               source_db, paper_uid,
               local_author_position, local_author_name,
               dblp_author_name, best_orcid
        FROM author_occurrences
        WHERE dblp_pid IS NOT NULL
        ORDER BY dblp_pid, venue, year, paper_uid, local_author_position
        """
    ):
        pid = row[0]
        for reviewer in by_pid.get(pid, []):
            rows_out.append(
                [
                    pid,
                    reviewer[1],
                    reviewer[2],
                    reviewer[4],
                    row[1], row[2], row[3], row[4], row[5],
                    row[6], row[7], row[8], row[9],
                ]
            )

    with output_csv.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(
            [
                "dblp_pid",
                "db_reviewer_name",
                "db_resolution_status",
                "db_best_orcid",
                "ml_venue",
                "ml_year",
                "paper_title",
                "ml_source_db",
                "paper_uid",
                "author_position",
                "ml_author_name",
                "dblp_author_signature",
                "ml_best_orcid",
            ]
        )
        w.writerows(rows_out)
    return len(rows_out)


# ---------------------------------------------------------------------------
# CSV exports and summaries
# ---------------------------------------------------------------------------


def export_papers(conn: sqlite3.Connection, path: Path) -> None:
    rows = conn.execute(
        """
        SELECT
            p.source_db, p.paper_uid, p.venue, p.year, p.title,
            r.status, r.resolved_dblp_key, r.candidate_count,
            r.local_author_count, r.dblp_author_count,
            r.aligned_author_count, r.author_alignment_score,
            r.notes
        FROM ml_papers p
        JOIN paper_resolution r
          ON r.source_db=p.source_db AND r.paper_uid=p.paper_uid
        ORDER BY p.venue, p.year, p.paper_uid
        """
    ).fetchall()
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(
            [
                "source_db", "paper_uid", "venue", "year", "title",
                "status", "resolved_dblp_key", "candidate_count",
                "local_author_count", "dblp_author_count",
                "aligned_author_count", "author_alignment_score", "notes",
            ]
        )
        w.writerows(rows)


def export_authors(conn: sqlite3.Connection, path: Path) -> None:
    rows = conn.execute(
        """
        SELECT
            source_db, paper_uid, venue, year, paper_title,
            local_author_position, local_author_name, local_author_key,
            dblp_key, dblp_author_position, dblp_author_name,
            author_name_score, author_name_match_type,
            publication_orcid, dblp_pid, dblp_canonical_name,
            person_orcids_json, person_affiliations_json,
            best_orcid, best_orcid_source, resolution_status
        FROM author_occurrences
        ORDER BY venue, year, paper_uid, local_author_position
        """
    ).fetchall()
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(
            [
                "source_db", "paper_uid", "venue", "year", "paper_title",
                "local_author_position", "local_author_name", "local_author_key",
                "dblp_key", "dblp_author_position", "dblp_author_signature",
                "author_name_score", "author_name_match_type",
                "publication_orcid", "dblp_pid", "dblp_canonical_name",
                "person_orcids", "person_affiliations",
                "best_orcid", "best_orcid_source", "resolution_status",
            ]
        )
        for row in rows:
            row = list(row)
            for idx in (16, 17):
                try:
                    row[idx] = " | ".join(json.loads(row[idx] or "[]"))
                except (TypeError, json.JSONDecodeError):
                    pass
            w.writerow(row)


def export_people(conn: sqlite3.Connection, path: Path) -> None:
    rows = conn.execute(
        """
        SELECT
            p.dblp_pid,
            p.canonical_name,
            p.aliases_json,
            p.affiliations_json,
            p.person_orcids_json,
            COUNT(DISTINCT o.source_db || ':' || o.paper_uid) AS ml_paper_count,
            COUNT(*) AS ml_author_occurrence_count,
            MIN(o.year) AS first_ml_year,
            MAX(o.year) AS last_ml_year,
            GROUP_CONCAT(DISTINCT o.venue) AS ml_venues
        FROM dblp_people p
        JOIN author_occurrences o ON o.dblp_pid=p.dblp_pid
        GROUP BY p.dblp_pid, p.canonical_name,
                 p.aliases_json, p.affiliations_json, p.person_orcids_json
        ORDER BY p.canonical_name COLLATE NOCASE
        """
    ).fetchall()

    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(
            [
                "dblp_pid", "canonical_name", "aliases", "affiliations",
                "person_orcids", "ml_paper_count", "ml_author_occurrence_count",
                "first_ml_year", "last_ml_year", "ml_venues",
            ]
        )
        for row in rows:
            row = list(row)
            for idx in (2, 3, 4):
                try:
                    row[idx] = " | ".join(json.loads(row[idx] or "[]"))
                except (TypeError, json.JSONDecodeError):
                    pass
            w.writerow(row)


def print_summary(conn: sqlite3.Connection) -> None:
    paper_n = conn.execute("SELECT COUNT(*) FROM ml_papers").fetchone()[0]
    author_n = conn.execute("SELECT COUNT(*) FROM ml_authors").fetchone()[0]
    print("\nLocal ML staging summary")
    print("------------------------")
    print(f"Papers:             {paper_n:8d}")
    print(f"Paper-author rows:  {author_n:8d}")

    statuses = conn.execute(
        """
        SELECT status, COUNT(*)
        FROM paper_resolution
        GROUP BY status
        ORDER BY COUNT(*) DESC, status
        """
    ).fetchall()
    if statuses:
        print("\nPaper resolution status")
        print("-----------------------")
        for status, count in statuses:
            print(f"{status:40s} {count:8d}")

    author_statuses = conn.execute(
        """
        SELECT resolution_status, COUNT(*)
        FROM author_occurrences
        GROUP BY resolution_status
        ORDER BY COUNT(*) DESC, resolution_status
        """
    ).fetchall()
    if author_statuses:
        print("\nAuthor occurrence resolution")
        print("----------------------------")
        for status, count in author_statuses:
            print(f"{status:40s} {count:8d}")

        distinct_pids = conn.execute(
            "SELECT COUNT(DISTINCT dblp_pid) FROM author_occurrences WHERE dblp_pid IS NOT NULL"
        ).fetchone()[0]
        with_orcid = conn.execute(
            "SELECT COUNT(*) FROM author_occurrences WHERE best_orcid IS NOT NULL"
        ).fetchone()[0]
        people_orcid = conn.execute(
            "SELECT COUNT(DISTINCT dblp_pid) FROM author_occurrences WHERE dblp_pid IS NOT NULL AND best_orcid IS NOT NULL"
        ).fetchone()[0]
        print(f"\nDistinct resolved DBLP PIDs:       {distinct_pids}")
        print(f"Author occurrences with ORCID:     {with_orcid}")
        print(f"Distinct resolved PIDs with ORCID: {people_orcid}")


def meta_get(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute(
        "SELECT value FROM local_index_meta WHERE key=?", (key,)
    ).fetchone()
    return row[0] if row else None


def meta_set(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        """
        INSERT INTO local_index_meta(key, value) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value
        """,
        (key, value),
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    p.add_argument(
        "--venues",
        nargs="+",
        choices=sorted(TARGET_VENUES),
        default=sorted(TARGET_VENUES),
        help="ML venues to enrich (default: all three)",
    )
    p.add_argument(
        "--years",
        nargs="+",
        type=int,
        default=None,
        help="Optional year filter",
    )
    p.add_argument(
        "--max-papers",
        type=int,
        default=None,
        help="Optional small test subset; the full DBLP snapshot is still scanned",
    )
    p.add_argument("--reindex", action="store_true")
    p.add_argument(
        "--no-download",
        action="store_true",
        help="Require the DBLP snapshot/DTD to already exist",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    data_dir = args.data_dir.resolve()
    data_dir.mkdir(parents=True, exist_ok=True)

    output_db = data_dir / OUTPUT_DB
    snapshot_dir = data_dir / "dblp_snapshot"
    snapshot_gz = snapshot_dir / SNAPSHOT_FILENAME
    dtd_path = snapshot_dir / DTD_FILENAME

    db_paths = discover_ml_databases(data_dir, output_db)
    if not db_paths:
        print("ERROR: no compatible ML paper SQLite databases found.", file=sys.stderr)
        return 2

    print("ML paper databases discovered:")
    for path in db_paths:
        print(f"  {path.name}")

    conn = sqlite3.connect(output_db)
    try:
        create_schema(conn)

        print("\n[1/6] Loading local ML papers and authors...")
        ingest_ml_databases(
            conn,
            db_paths,
            venues=set(args.venues),
            years=set(args.years) if args.years else None,
            max_papers=args.max_papers,
        )
        print_summary(conn)

        if args.no_download:
            if not snapshot_gz.exists() or not dtd_path.exists():
                print("ERROR: DBLP snapshot/DTD missing and --no-download set.", file=sys.stderr)
                return 2
        else:
            print(f"\n[2/6] Preparing DBLP snapshot {SNAPSHOT_ID}...")
            download_file(DTD_URL, dtd_path, DTD_MD5, "DBLP DTD")
            download_file(SNAPSHOT_URL, snapshot_gz, SNAPSHOT_MD5, "DBLP XML snapshot")

        if md5_file(dtd_path).lower() != DTD_MD5.lower():
            raise RuntimeError("DBLP DTD checksum mismatch")
        current_snapshot_md5 = md5_file(snapshot_gz)
        if current_snapshot_md5.lower() != SNAPSHOT_MD5.lower():
            raise RuntimeError("DBLP snapshot checksum mismatch")

        src_hash = source_fingerprint(conn)
        cache_ok = (
            not args.reindex
            and meta_get(conn, "index_complete") == "1"
            and meta_get(conn, "snapshot_id") == SNAPSHOT_ID
            and meta_get(conn, "snapshot_md5") == SNAPSHOT_MD5
            and meta_get(conn, "source_hash") == src_hash
        )

        if cache_ok:
            print("\n[3/6] Reusing completed DBLP publication/person index for this exact ML dataset.")
        else:
            print("\n[3/6] Matching ML papers to DBLP publications...")
            clear_dblp_index(conn)
            scan_publications(conn, snapshot_gz, dtd_path)

            print("\n[4/6] Resolving papers and aligning their author lists...")
            resolve_papers_and_align_authors(conn)

            print("\n[5/6] Resolving DBLP author signatures to persistent person PIDs...")
            scan_people_for_author_signatures(conn, snapshot_gz, dtd_path)
            apply_person_resolution(conn)

            meta_set(conn, "snapshot_id", SNAPSHOT_ID)
            meta_set(conn, "snapshot_md5", SNAPSHOT_MD5)
            meta_set(conn, "source_hash", src_hash)
            meta_set(conn, "index_complete", "1")
            conn.commit()

        # If cache was valid, resolution rows already exist.  Exports are always
        # regenerated so CSVs cannot become stale or be accidentally deleted.
        print("\n[6/6] Exporting enrichment files...")
        paper_csv = data_dir / PAPER_CSV
        author_csv = data_dir / AUTHOR_CSV
        people_csv = data_dir / PEOPLE_CSV
        overlap_csv = data_dir / OVERLAP_CSV

        export_papers(conn, paper_csv)
        export_authors(conn, author_csv)
        export_people(conn, people_csv)

        overlap_count = export_db_reviewer_pid_overlap(
            conn,
            data_dir / "entity_resolution.sqlite",
            overlap_csv,
        )

        print_summary(conn)
        print("\nOutputs")
        print("-------")
        print(f"SQLite enrichment DB:     {output_db}")
        print(f"Paper resolution CSV:     {paper_csv}")
        print(f"Author occurrences CSV:   {author_csv}")
        print(f"Unique DBLP people CSV:   {people_csv}")
        if (data_dir / "entity_resolution.sqlite").exists():
            print(f"DB<->ML PID overlap CSV:  {overlap_csv} ({overlap_count:,} paper-author rows)")

        print(
            "\nInterpretation: title matching is exact after normalization. "
            "A DBLP PID is assigned only after a matched publication author "
            "signature is found in a DBLP person record."
        )
        return 0

    except KeyboardInterrupt:
        print("\nInterrupted. The DBLP snapshot is preserved; rerun the same command.")
        return 130
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
