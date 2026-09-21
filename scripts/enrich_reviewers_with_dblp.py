#!/usr/bin/env python3
"""
enrich_reviewers_with_dblp.py

Offline / reproducible DBLP enrichment for the DB-reviewer datasets.

Why this version exists
-----------------------
The live DBLP author-search API may rate-limit or challenge automated clients.
For a research workflow with ~1,500 names, DBLP itself recommends using a local
dataset snapshot. This script therefore uses the official September 2026
monthly DBLP XML snapshot and performs matching locally.

What it does
------------
1. Finds reviewer SQLite databases in data/*.sqlite (table: reviewer_records).
2. Keeps all legitimate service observations.
3. Removes only exact technical duplicates.
4. Flags obviously malformed scraped "names" instead of sending/matching them.
5. Builds provisional normalized-name groups.
6. Downloads the official DBLP 2026-09-01 XML snapshot + DTD if missing.
7. Streams the compressed XML directly (no multi-GB decompression required).
8. Reads DBLP person/homepage records only.
9. Matches reviewer names to DBLP identities using:
      - exact normalized aliases
      - same first + last name while ignoring middle names
      - initial + last name only when the scraped first name is an initial
10. Stores all candidates, DBLP PID, aliases, affiliations, and person-record
    ORCIDs.
11. Auto-resolves only a UNIQUE exact-alias match.
12. Exports human-readable CSVs.

Default outputs
---------------
data/entity_resolution.sqlite
data/dblp_name_resolution.csv
data/dblp_candidates.csv
data/invalid_reviewer_names.csv

Snapshot files
--------------
data/dblp_snapshot/dblp-2026-09-01.xml.gz
data/dblp_snapshot/dblp-2023-06-28.dtd

Dependencies
------------
    pip install requests lxml

Run
---
    python scripts/enrich_reviewers_with_dblp.py --data-dir .\\data

Test a subset of reviewer names (the XML still has to be scanned once):
    python scripts/enrich_reviewers_with_dblp.py --data-dir .\\data --max-names 25

Force rebuilding the local DBLP candidate index:
    python scripts/enrich_reviewers_with_dblp.py --data-dir .\\data --reindex

Skip automatic download and require local snapshot files:
    python scripts/enrich_reviewers_with_dblp.py --data-dir .\\data --no-download
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
from pathlib import Path
from typing import Any, Iterable

import requests
from lxml import etree

# ---------------------------------------------------------------------------
# Reproducible DBLP snapshot
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

REVIEWER_TABLE = "reviewer_records"
OUTPUT_DB = "entity_resolution.sqlite"
RESOLUTION_CSV = "dblp_name_resolution.csv"
CANDIDATES_CSV = "dblp_candidates.csv"
INVALID_NAMES_CSV = "invalid_reviewer_names.csv"

SPACE_RE = re.compile(r"\s+")
NON_ALNUM_RE = re.compile(r"[^a-z0-9 ]+")
DBLP_MAGIC_SUFFIX_RE = re.compile(r"\s+\d{4}$")
ORCID_RE = re.compile(r"(?:https?://orcid\.org/)?(\d{4}-\d{4}-\d{4}-[\dXx]{4})")

AFFIL_STOPWORDS = {
    "the",
    "of",
    "and",
    "at",
    "for",
    "in",
    "on",
    "university",
    "universita",
    "universitat",
    "universite",
    "college",
    "school",
    "department",
    "dept",
    "institute",
    "laboratory",
    "lab",
    "labs",
    "research",
    "center",
    "centre",
    "inc",
    "ltd",
    "llc",
    "group",
    "corporation",
    "corp",
}

BAD_NAME_PHRASES = (
    "after careful consideration",
    "program committee",
    "program chairs",
    "review committee",
    "reviewers",
    "associate editors",
    "area chairs",
    "important dates",
    "submission",
    "accepted papers",
    "call for papers",
    "we are",
    "we thank",
    "please ",
)

BAD_NAME_SUBSTRINGS = (
    "http://",
    "https://",
    "www.",
    "@",
)


# ---------------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------------


def now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    return SPACE_RE.sub(" ", str(value).strip())


def normalize_name(value: Any) -> str:
    s = clean_text(value)
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = s.lower().replace("-", " ")
    s = NON_ALNUM_RE.sub(" ", s)
    return SPACE_RE.sub(" ", s).strip()


def dblp_base_name(value: Any) -> str:
    # DBLP may append a magic four-digit disambiguation suffix to a name.
    s = clean_text(value)
    s = DBLP_MAGIC_SUFFIX_RE.sub("", s)
    return normalize_name(s)


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


def affiliation_tokens(value: str) -> set[str]:
    return {
        tok
        for tok in normalize_name(value).split()
        if len(tok) >= 2 and tok not in AFFIL_STOPWORDS
    }


def max_affiliation_similarity(
    left: Iterable[str],
    right: Iterable[str],
) -> float:
    best = 0.0
    for a in left:
        aa = affiliation_tokens(a)
        if not aa:
            continue
        for b in right:
            bb = affiliation_tokens(b)
            if not bb:
                continue
            union = aa | bb
            if union:
                best = max(best, len(aa & bb) / len(union))
    return best


def md5_file(path: Path, chunk_size: int = 8 * 1024 * 1024) -> str:
    h = hashlib.md5()
    with path.open("rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def target_hash(keys: Iterable[str]) -> str:
    h = hashlib.sha256()
    for key in sorted(keys):
        h.update(key.encode("utf-8"))
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


def pick(row: dict[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        if key in row and row[key] is not None:
            return row[key]
    return default


# ---------------------------------------------------------------------------
# Reviewer-name quality checks
# ---------------------------------------------------------------------------


def validate_person_name(name: str) -> tuple[bool, str]:
    """
    Deliberately conservative scraper-quality check.

    We do NOT try to decide whether a real person exists here. We only flag
    strings that are clearly not a single clean person name.
    """
    raw = clean_text(name)
    low = raw.lower()
    key = normalize_name(raw)
    toks = key.split()

    if not raw:
        return False, "empty"

    if len(raw) > 120:
        return False, "too_long"

    if any(x in low for x in BAD_NAME_SUBSTRINGS):
        return False, "contains_url_or_email"

    if any(low.startswith(x) or x in low for x in BAD_NAME_PHRASES):
        return False, "looks_like_page_text"

    # In these scraper sources parentheses are overwhelmingly affiliation text,
    # and multiple people sometimes get concatenated into one field.
    if "(" in raw or ")" in raw:
        return False, "contains_parenthetical_affiliation_or_concatenation"

    if ";" in raw or "|" in raw:
        return False, "contains_list_separator"

    alpha_tokens = [t for t in toks if any(ch.isalpha() for ch in t)]

    if len(alpha_tokens) < 2:
        return False, "fewer_than_two_name_tokens"

    if len(alpha_tokens) > 8:
        return False, "too_many_tokens_for_single_name"

    # Long prose-like strings frequently leak from committee pages.
    if len(raw) > 70 and len(alpha_tokens) >= 6:
        return False, "looks_like_prose_or_concatenated_people"

    return True, ""


# ---------------------------------------------------------------------------
# SQLite staging schema
# ---------------------------------------------------------------------------


def create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        PRAGMA journal_mode=WAL;
        PRAGMA foreign_keys=ON;

        CREATE TABLE IF NOT EXISTS reviewer_observations (
            observation_id INTEGER PRIMARY KEY,
            technical_hash TEXT NOT NULL UNIQUE,
            source_db TEXT NOT NULL,
            source_rowid INTEGER,
            venue TEXT,
            year INTEGER,
            role TEXT,
            review_round TEXT,
            review_cycle TEXT,
            track TEXT,
            name TEXT NOT NULL,
            name_key TEXT NOT NULL,
            name_is_valid INTEGER NOT NULL,
            name_validation_reason TEXT,
            affiliation TEXT,
            source_url TEXT,
            raw_text TEXT,
            source_payload_json TEXT NOT NULL,
            ingested_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_obs_name_key
            ON reviewer_observations(name_key);

        CREATE TABLE IF NOT EXISTS technical_duplicate_log (
            duplicate_id INTEGER PRIMARY KEY,
            technical_hash TEXT NOT NULL,
            source_db TEXT NOT NULL,
            source_rowid INTEGER,
            logged_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS name_groups (
            name_key TEXT PRIMARY KEY,
            representative_name TEXT NOT NULL,
            observation_count INTEGER NOT NULL,
            min_year INTEGER,
            max_year INTEGER,
            venues_json TEXT NOT NULL,
            roles_json TEXT NOT NULL,
            affiliations_json TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS dblp_people (
            dblp_pid TEXT PRIMARY KEY,
            canonical_name TEXT,
            aliases_json TEXT NOT NULL,
            affiliations_json TEXT NOT NULL,
            verified_orcids_json TEXT NOT NULL,
            source_key TEXT,
            snapshot_id TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS dblp_candidates (
            name_key TEXT NOT NULL,
            dblp_pid TEXT NOT NULL,
            match_type TEXT NOT NULL,
            matched_alias TEXT NOT NULL,
            affiliation_similarity REAL NOT NULL DEFAULT 0,
            PRIMARY KEY (name_key, dblp_pid, match_type, matched_alias)
        );

        CREATE INDEX IF NOT EXISTS idx_dblp_candidates_name
            ON dblp_candidates(name_key);

        CREATE TABLE IF NOT EXISTS name_resolution (
            name_key TEXT PRIMARY KEY,
            representative_name TEXT NOT NULL,
            status TEXT NOT NULL,
            resolved_dblp_pid TEXT,
            resolved_dblp_name TEXT,
            best_orcid TEXT,
            best_orcid_source TEXT,
            exact_candidate_count INTEGER NOT NULL,
            variant_candidate_count INTEGER NOT NULL,
            all_candidate_count INTEGER NOT NULL,
            notes TEXT,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS local_index_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        """)
    conn.commit()


def technical_hash(rec: dict[str, Any]) -> str:
    stable = {
        "source_db": rec["source_db"],
        "venue": rec["venue"],
        "year": rec["year"],
        "role": rec["role"],
        "review_round": rec["review_round"],
        "review_cycle": rec["review_cycle"],
        "track": rec["track"],
        "name": rec["name"],
        "affiliation": rec["affiliation"],
        "source_url": rec["source_url"],
    }
    payload = json.dumps(stable, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def discover_reviewer_dbs(data_dir: Path, output_db: Path) -> list[Path]:
    paths: list[Path] = []
    for path in sorted(data_dir.glob("*.sqlite")):
        if path.resolve() == output_db.resolve():
            continue
        try:
            c = sqlite3.connect(path)
            try:
                if not table_exists(c, REVIEWER_TABLE):
                    continue
                cols = set(table_columns(c, REVIEWER_TABLE))
                if "name" not in cols:
                    continue
                if "year" not in cols and "conference_year" not in cols:
                    continue
                paths.append(path)
            finally:
                c.close()
        except sqlite3.Error:
            pass
    return paths


def ingest_reviewers(
    conn: sqlite3.Connection,
    db_paths: list[Path],
) -> None:
    conn.execute("DELETE FROM technical_duplicate_log")
    conn.execute("DELETE FROM reviewer_observations")
    conn.execute("DELETE FROM name_groups")
    conn.commit()

    for db_path in db_paths:
        src = sqlite3.connect(db_path)
        src.row_factory = sqlite3.Row
        kept = 0
        skipped = 0

        try:
            for row in src.execute(
                f'SELECT rowid AS _rowid, * FROM "{REVIEWER_TABLE}"'
            ):
                raw = dict(row)

                year = pick(raw, "year", "conference_year")
                try:
                    year = int(year) if year is not None else None
                except (TypeError, ValueError):
                    year = None

                name = clean_text(pick(raw, "name", default=""))
                if not name:
                    continue

                valid, invalid_reason = validate_person_name(name)

                rec = {
                    "source_db": db_path.name,
                    "source_rowid": raw["_rowid"],
                    "venue": clean_text(
                        pick(raw, "venue", default=db_path.stem)
                    ),
                    "year": year,
                    "role": clean_text(pick(raw, "role", default="")),
                    "review_round": clean_text(
                        pick(raw, "review_round", default="")
                    ),
                    "review_cycle": clean_text(
                        pick(raw, "review_cycle", default="")
                    ),
                    "track": clean_text(pick(raw, "track", default="")),
                    "name": name,
                    "name_key": normalize_name(name),
                    "name_is_valid": int(valid),
                    "name_validation_reason": invalid_reason,
                    "affiliation": clean_text(
                        pick(raw, "affiliation", default="")
                    ),
                    "source_url": clean_text(
                        pick(raw, "source_url", default="")
                    ),
                    "raw_text": clean_text(pick(raw, "raw_text", default="")),
                    "source_payload_json": json.dumps(
                        raw, ensure_ascii=False, sort_keys=True, default=str
                    ),
                    "ingested_at": now_iso(),
                }
                rec["technical_hash"] = technical_hash(rec)

                try:
                    conn.execute(
                        """
                        INSERT INTO reviewer_observations (
                            technical_hash, source_db, source_rowid, venue, year,
                            role, review_round, review_cycle, track, name, name_key,
                            name_is_valid, name_validation_reason, affiliation,
                            source_url, raw_text, source_payload_json, ingested_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            rec["technical_hash"],
                            rec["source_db"],
                            rec["source_rowid"],
                            rec["venue"],
                            rec["year"],
                            rec["role"],
                            rec["review_round"],
                            rec["review_cycle"],
                            rec["track"],
                            rec["name"],
                            rec["name_key"],
                            rec["name_is_valid"],
                            rec["name_validation_reason"],
                            rec["affiliation"],
                            rec["source_url"],
                            rec["raw_text"],
                            rec["source_payload_json"],
                            rec["ingested_at"],
                        ),
                    )
                    kept += 1
                except sqlite3.IntegrityError:
                    skipped += 1
                    conn.execute(
                        """
                        INSERT INTO technical_duplicate_log (
                            technical_hash, source_db, source_rowid, logged_at
                        ) VALUES (?, ?, ?, ?)
                        """,
                        (
                            rec["technical_hash"],
                            rec["source_db"],
                            rec["source_rowid"],
                            now_iso(),
                        ),
                    )

            conn.commit()
            print(
                f"Loaded {kept:5d} observations from {db_path.name}"
                + (
                    f" ({skipped} exact technical duplicates skipped)"
                    if skipped
                    else ""
                )
            )
        finally:
            src.close()

    rebuild_name_groups(conn)


def rebuild_name_groups(conn: sqlite3.Connection) -> None:
    groups: dict[str, dict[str, Any]] = {}

    for row in conn.execute("""
        SELECT name_key, name, venue, role, affiliation, year
        FROM reviewer_observations
        WHERE name_is_valid=1
        ORDER BY observation_id
        """):
        key, name, venue, role, affiliation, year = row
        g = groups.setdefault(
            key,
            {
                "names": [],
                "venues": set(),
                "roles": set(),
                "affiliations": set(),
                "years": [],
                "count": 0,
            },
        )
        g["count"] += 1
        g["names"].append(name)
        if venue:
            g["venues"].add(venue)
        if role:
            g["roles"].add(role)
        if affiliation:
            g["affiliations"].add(affiliation)
        if year is not None:
            g["years"].append(int(year))

    conn.execute("DELETE FROM name_groups")
    for key, g in groups.items():
        conn.execute(
            """
            INSERT INTO name_groups (
                name_key, representative_name, observation_count,
                min_year, max_year, venues_json, roles_json,
                affiliations_json, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                key,
                g["names"][0],
                g["count"],
                min(g["years"]) if g["years"] else None,
                max(g["years"]) if g["years"] else None,
                json.dumps(sorted(g["venues"]), ensure_ascii=False),
                json.dumps(sorted(g["roles"]), ensure_ascii=False),
                json.dumps(sorted(g["affiliations"]), ensure_ascii=False),
                now_iso(),
            ),
        )
    conn.commit()


# ---------------------------------------------------------------------------
# Snapshot download
# ---------------------------------------------------------------------------


def download_file(
    url: str,
    dest: Path,
    expected_md5: str,
    label: str,
) -> None:
    if dest.exists():
        print(f"Checking existing {label}: {dest.name}")
        actual = md5_file(dest)
        if actual.lower() == expected_md5.lower():
            print(f"  MD5 OK: {actual}")
            return
        print(
            f"  Existing file has wrong MD5 ({actual}); downloading a fresh copy."
        )
        dest.unlink()

    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + ".part")

    headers = {
        "User-Agent": "db-ml-entity-resolution-research/1.0",
        "Accept": "*/*",
    }

    existing = part.stat().st_size if part.exists() else 0
    if existing:
        headers["Range"] = f"bytes={existing}-"
        print(f"Resuming {label} download at {existing / (1024**2):.1f} MiB...")
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
        if existing > 0 and not append:
            existing = 0

        total_header = r.headers.get("Content-Length")
        total = int(total_header) + existing if total_header else None

        mode = "ab" if append else "wb"
        downloaded = existing
        last_print = time.monotonic()

        with part.open(mode) as f:
            for chunk in r.iter_content(chunk_size=4 * 1024 * 1024):
                if not chunk:
                    continue
                f.write(chunk)
                downloaded += len(chunk)

                if time.monotonic() - last_print >= 5:
                    if total:
                        pct = 100.0 * downloaded / total
                        print(
                            f"  {downloaded / (1024**2):,.1f} MiB / "
                            f"{total / (1024**2):,.1f} MiB ({pct:.1f}%)"
                        )
                    else:
                        print(f"  {downloaded / (1024**2):,.1f} MiB")
                    last_print = time.monotonic()

    part.replace(dest)

    print(f"Verifying {label} MD5...")
    actual = md5_file(dest)
    if actual.lower() != expected_md5.lower():
        raise RuntimeError(
            f"{label} MD5 mismatch. Expected {expected_md5}, got {actual}."
        )
    print(f"  MD5 OK: {actual}")


# ---------------------------------------------------------------------------
# Streaming DBLP XML
# ---------------------------------------------------------------------------


class LocalDTDResolver(etree.Resolver):
    def __init__(self, dtd_path: Path):
        super().__init__()
        self.dtd_path = dtd_path

    def resolve(self, url, pubid, context):
        if str(url).lower().endswith(".dtd"):
            return self.resolve_filename(str(self.dtd_path), context)
        return None


def extract_orcid(text: str) -> str | None:
    m = ORCID_RE.search(text or "")
    return m.group(1).upper() if m else None


def person_record_from_www(elem: etree._Element) -> dict[str, Any] | None:
    key = clean_text(elem.attrib.get("key", ""))
    if not key.startswith("homepages/"):
        return None

    pid = key[len("homepages/") :]

    aliases: list[str] = []
    affiliations: list[str] = []
    orcids: set[str] = set()
    title = ""
    crossref = ""

    for child in elem:
        tag = etree.QName(child).localname
        text = clean_text("".join(child.itertext()))

        if tag == "author" and text:
            aliases.append(text)
        elif tag == "title":
            title = text
        elif (
            tag == "note" and child.attrib.get("type") == "affiliation" and text
        ):
            affiliations.append(text)
        elif tag == "url" and text:
            oc = extract_orcid(text)
            if oc:
                orcids.add(oc)
        elif tag == "crossref" and text:
            crossref = text

    # Crossref-only homepage remnants are merged/redirect records.
    if crossref and not aliases:
        return None

    if not aliases:
        return None

    return {
        "pid": pid,
        "source_key": key,
        "canonical_name": aliases[0],
        "aliases": sorted(set(aliases), key=str.casefold),
        "affiliations": sorted(set(affiliations), key=str.casefold),
        "verified_orcids": sorted(orcids),
        "title": title,
        "crossref": crossref,
    }


def build_target_maps(
    rows: list[tuple[str, str]],
) -> tuple[
    dict[str, set[str]],
    dict[str, set[str]],
    dict[str, set[str]],
]:
    exact: dict[str, set[str]] = defaultdict(set)
    full_first_last: dict[str, set[str]] = defaultdict(set)
    initial_first_last: dict[str, set[str]] = defaultdict(set)

    for name_key, _representative in rows:
        exact[name_key].add(name_key)

        toks = name_key.split()
        if len(toks) < 2:
            continue

        fl = first_last_key(name_key)

        # Only use middle-name-insensitive matching when the scraped first name
        # is a full token, e.g. Aaron J Elmore -> Aaron Elmore.
        if len(toks[0]) > 1:
            full_first_last[fl].add(name_key)
        else:
            initial_first_last[initial_last_key(name_key)].add(name_key)

    return exact, full_first_last, initial_first_last


def candidate_matches_for_person(
    person: dict[str, Any],
    exact_targets: dict[str, set[str]],
    full_first_last_targets: dict[str, set[str]],
    initial_last_targets: dict[str, set[str]],
) -> dict[tuple[str, str], str]:
    """
    Return:
        {(target_name_key, matched_alias): match_type}

    Match priority:
        exact_alias > first_last_variant > initial_last_variant
    """
    matches: dict[tuple[str, str], str] = {}

    for alias in person["aliases"]:
        alias_key = dblp_base_name(alias)
        if not alias_key:
            continue

        # Exact alias/base-name match.
        for target in exact_targets.get(alias_key, ()):
            matches[(target, alias)] = "exact_alias"

        toks = alias_key.split()
        if len(toks) < 2:
            continue

        fl = first_last_key(alias_key)
        for target in full_first_last_targets.get(fl, ()):
            k = (target, alias)
            if matches.get(k) != "exact_alias":
                matches[k] = "first_last_variant"

        il = initial_last_key(alias_key)
        for target in initial_last_targets.get(il, ()):
            k = (target, alias)
            if matches.get(k) != "exact_alias":
                matches[k] = "initial_last_variant"

    return matches


def get_name_affiliations(conn: sqlite3.Connection) -> dict[str, list[str]]:
    result = {}
    for key, value in conn.execute(
        "SELECT name_key, affiliations_json FROM name_groups"
    ):
        result[key] = json.loads(value or "[]")
    return result


def clear_local_index(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM dblp_candidates")
    conn.execute("DELETE FROM dblp_people")
    conn.execute("DELETE FROM name_resolution")
    conn.execute("DELETE FROM local_index_meta")
    conn.commit()


def meta_get(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute(
        "SELECT value FROM local_index_meta WHERE key=?",
        (key,),
    ).fetchone()
    return row[0] if row else None


def meta_set(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        """
        INSERT INTO local_index_meta(key, value)
        VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value
        """,
        (key, value),
    )


def scan_snapshot(
    conn: sqlite3.Connection,
    snapshot_gz: Path,
    dtd_path: Path,
    name_rows: list[tuple[str, str]],
) -> None:
    exact_targets, fl_targets, il_targets = build_target_maps(name_rows)
    db_affiliations = get_name_affiliations(conn)

    # IMPORTANT: listen for *all* end events, not only <www>.  lxml still
    # builds non-matching elements even when XMLPullParser(tag="www") is used.
    # If those publication records are never cleared, the complete DBLP XML
    # tree accumulates in RAM and eventually raises MemoryError.  We therefore
    # clear every completed top-level DBLP record, while only inspecting <www>
    # records for person metadata.
    parser = etree.XMLPullParser(
        events=("end",),
        load_dtd=True,
        resolve_entities=True,
        no_network=True,
        huge_tree=True,
        recover=False,
    )
    parser.resolvers.add(LocalDTDResolver(dtd_path))

    matched_pids: set[str] = set()
    matched_rows = 0
    top_level_records = 0
    homepage_records = 0
    last_report = time.monotonic()

    print(
        f"Streaming {snapshot_gz.name}; all top-level XML records are cleared "
        "as they are read, and only matching DBLP people are stored."
    )

    with gzip.open(snapshot_gz, "rb") as f:
        while True:
            # A small feed chunk keeps transient memory bounded.  The parser
            # itself is streaming; this does not change the result.
            chunk = f.read(1 * 1024 * 1024)
            if not chunk:
                break

            parser.feed(chunk)

            for _event, elem in parser.read_events():
                parent = elem.getparent()

                # Child elements (author/title/etc.) must remain intact until
                # their top-level publication/person element has completed.
                # A top-level DBLP record has <dblp> as its parent, whose own
                # parent is None.
                if parent is None or parent.getparent() is not None:
                    continue

                top_level_records += 1
                tag = etree.QName(elem).localname

                if tag == "www":
                    homepage_records += 1
                    person = person_record_from_www(elem)

                    if person:
                        matches = candidate_matches_for_person(
                            person,
                            exact_targets,
                            fl_targets,
                            il_targets,
                        )

                        if matches:
                            pid = person["pid"]
                            matched_pids.add(pid)

                            conn.execute(
                                """
                                INSERT OR REPLACE INTO dblp_people (
                                    dblp_pid, canonical_name, aliases_json,
                                    affiliations_json, verified_orcids_json,
                                    source_key, snapshot_id
                                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                                """,
                                (
                                    pid,
                                    person["canonical_name"],
                                    json.dumps(
                                        person["aliases"], ensure_ascii=False
                                    ),
                                    json.dumps(
                                        person["affiliations"],
                                        ensure_ascii=False,
                                    ),
                                    json.dumps(
                                        person["verified_orcids"],
                                        ensure_ascii=False,
                                    ),
                                    person["source_key"],
                                    SNAPSHOT_ID,
                                ),
                            )

                            for (
                                target_key,
                                alias,
                            ), match_type in matches.items():
                                aff_sim = max_affiliation_similarity(
                                    db_affiliations.get(target_key, []),
                                    person["affiliations"],
                                )

                                conn.execute(
                                    """
                                    INSERT OR REPLACE INTO dblp_candidates (
                                        name_key, dblp_pid, match_type,
                                        matched_alias, affiliation_similarity
                                    ) VALUES (?, ?, ?, ?, ?)
                                    """,
                                    (
                                        target_key,
                                        pid,
                                        match_type,
                                        alias,
                                        aff_sim,
                                    ),
                                )
                                matched_rows += 1

                # Free every completed top-level record (articles, proceedings,
                # www/person entries, etc.) so memory usage stays bounded.
                elem.clear()
                while elem.getprevious() is not None:
                    del parent[0]

            if time.monotonic() - last_report >= 10:
                print(
                    f"  scanned {top_level_records:,} total DBLP records; "
                    f"{homepage_records:,} www/person records; "
                    f"{len(matched_pids):,} candidate PIDs retained"
                )
                conn.commit()
                last_report = time.monotonic()

    parser.close()
    conn.commit()

    print(
        f"Finished DBLP scan: {top_level_records:,} total records; "
        f"{homepage_records:,} www/person records; "
        f"{len(matched_pids):,} candidate PIDs retained."
    )


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


def resolve_names(
    conn: sqlite3.Connection,
    name_rows: list[tuple[str, str]],
) -> None:
    conn.execute("DELETE FROM name_resolution")

    for name_key, representative in name_rows:
        rows = conn.execute(
            """
            SELECT
                c.dblp_pid,
                c.match_type,
                c.matched_alias,
                c.affiliation_similarity,
                p.canonical_name,
                p.verified_orcids_json
            FROM dblp_candidates c
            JOIN dblp_people p ON p.dblp_pid=c.dblp_pid
            WHERE c.name_key=?
            ORDER BY
                CASE c.match_type
                    WHEN 'exact_alias' THEN 0
                    WHEN 'first_last_variant' THEN 1
                    ELSE 2
                END,
                c.affiliation_similarity DESC,
                c.dblp_pid
            """,
            (name_key,),
        ).fetchall()

        by_pid: dict[str, dict[str, Any]] = {}
        for pid, match_type, alias, aff_sim, canonical, orcids_json in rows:
            x = by_pid.setdefault(
                pid,
                {
                    "types": set(),
                    "aliases": set(),
                    "aff_sim": 0.0,
                    "canonical": canonical,
                    "orcids": json.loads(orcids_json or "[]"),
                },
            )
            x["types"].add(match_type)
            x["aliases"].add(alias)
            x["aff_sim"] = max(x["aff_sim"], float(aff_sim or 0))

        exact_pids = [
            pid for pid, x in by_pid.items() if "exact_alias" in x["types"]
        ]
        variant_pids = [
            pid for pid, x in by_pid.items() if "exact_alias" not in x["types"]
        ]

        resolved_pid = None
        resolved_name = None
        best_orcid = None
        best_orcid_source = None
        notes = ""

        if len(exact_pids) == 1:
            resolved_pid = exact_pids[0]
            x = by_pid[resolved_pid]
            resolved_name = x["canonical"]
            status = "resolved_unique_exact"

            if len(x["orcids"]) == 1:
                best_orcid = x["orcids"][0]
                best_orcid_source = "dblp_person_record"
            elif len(x["orcids"]) > 1:
                notes = (
                    "Unique exact DBLP PID, but multiple person-record ORCIDs "
                    "are present; inspect manually."
                )

        elif len(exact_pids) > 1:
            status = "ambiguous_exact_name"
            notes = (
                f"{len(exact_pids)} DBLP person identities have an exact "
                "normalized alias match. Do not merge by name alone."
            )

        elif len(variant_pids) == 1:
            status = "candidate_unique_variant"
            notes = (
                "One DBLP candidate matches after ignoring middle-name detail "
                "or via an initial. Kept as candidate; not auto-resolved."
            )

        elif len(variant_pids) > 1:
            status = "ambiguous_variant"
            notes = (
                f"{len(variant_pids)} DBLP candidates match by a relaxed "
                "name rule. Additional evidence is required."
            )

        else:
            status = "unresolved_no_local_dblp_candidate"
            notes = "No local DBLP person alias matched the current rules."

        conn.execute(
            """
            INSERT INTO name_resolution (
                name_key, representative_name, status,
                resolved_dblp_pid, resolved_dblp_name,
                best_orcid, best_orcid_source,
                exact_candidate_count, variant_candidate_count,
                all_candidate_count, notes, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                name_key,
                representative,
                status,
                resolved_pid,
                resolved_name,
                best_orcid,
                best_orcid_source,
                len(exact_pids),
                len(variant_pids),
                len(by_pid),
                notes,
                now_iso(),
            ),
        )

    conn.commit()


# ---------------------------------------------------------------------------
# CSV exports
# ---------------------------------------------------------------------------


def export_invalid_names(conn: sqlite3.Connection, path: Path) -> None:
    rows = conn.execute("""
        SELECT
            source_db, venue, year, role, name,
            name_validation_reason, affiliation, source_url, raw_text
        FROM reviewer_observations
        WHERE name_is_valid=0
        ORDER BY source_db, year, name
        """).fetchall()

    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(
            [
                "source_db",
                "venue",
                "year",
                "role",
                "name",
                "reason",
                "affiliation",
                "source_url",
                "raw_text",
            ]
        )
        w.writerows(rows)


def export_resolution(conn: sqlite3.Connection, path: Path) -> None:
    rows = conn.execute("""
        SELECT
            r.name_key,
            r.representative_name,
            r.status,
            r.resolved_dblp_pid,
            r.resolved_dblp_name,
            r.best_orcid,
            r.best_orcid_source,
            r.exact_candidate_count,
            r.variant_candidate_count,
            r.all_candidate_count,
            g.observation_count,
            g.min_year,
            g.max_year,
            g.venues_json,
            g.roles_json,
            g.affiliations_json,
            r.notes
        FROM name_resolution r
        JOIN name_groups g USING(name_key)
        ORDER BY
            CASE r.status
                WHEN 'resolved_unique_exact' THEN 0
                WHEN 'ambiguous_exact_name' THEN 1
                WHEN 'candidate_unique_variant' THEN 2
                WHEN 'ambiguous_variant' THEN 3
                ELSE 4
            END,
            r.representative_name COLLATE NOCASE
        """).fetchall()

    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(
            [
                "name_key",
                "representative_name",
                "status",
                "resolved_dblp_pid",
                "resolved_dblp_name",
                "best_orcid",
                "best_orcid_source",
                "exact_candidate_count",
                "variant_candidate_count",
                "all_candidate_count",
                "reviewer_observation_count",
                "min_year",
                "max_year",
                "venues",
                "roles",
                "affiliations",
                "notes",
            ]
        )

        for row in rows:
            row = list(row)
            for idx in (13, 14, 15):
                try:
                    row[idx] = " | ".join(json.loads(row[idx] or "[]"))
                except (TypeError, json.JSONDecodeError):
                    pass
            w.writerow(row)


def export_candidates(conn: sqlite3.Connection, path: Path) -> None:
    rows = conn.execute("""
        SELECT
            c.name_key,
            g.representative_name,
            c.dblp_pid,
            p.canonical_name,
            c.match_type,
            c.matched_alias,
            c.affiliation_similarity,
            p.aliases_json,
            p.affiliations_json,
            p.verified_orcids_json
        FROM dblp_candidates c
        JOIN name_groups g USING(name_key)
        JOIN dblp_people p ON p.dblp_pid=c.dblp_pid
        ORDER BY
            c.name_key,
            CASE c.match_type
                WHEN 'exact_alias' THEN 0
                WHEN 'first_last_variant' THEN 1
                ELSE 2
            END,
            c.affiliation_similarity DESC,
            c.dblp_pid
        """).fetchall()

    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(
            [
                "name_key",
                "db_representative_name",
                "dblp_pid",
                "dblp_canonical_name",
                "match_type",
                "matched_alias",
                "affiliation_similarity",
                "dblp_aliases",
                "dblp_affiliations",
                "verified_orcids",
            ]
        )

        for row in rows:
            row = list(row)
            for idx in (7, 8, 9):
                try:
                    row[idx] = " | ".join(json.loads(row[idx] or "[]"))
                except (TypeError, json.JSONDecodeError):
                    pass
            w.writerow(row)


def print_summary(conn: sqlite3.Connection) -> None:
    obs = conn.execute("SELECT COUNT(*) FROM reviewer_observations").fetchone()[
        0
    ]
    invalid = conn.execute(
        "SELECT COUNT(*) FROM reviewer_observations WHERE name_is_valid=0"
    ).fetchone()[0]
    names = conn.execute("SELECT COUNT(*) FROM name_groups").fetchone()[0]
    dupes = conn.execute(
        "SELECT COUNT(*) FROM technical_duplicate_log"
    ).fetchone()[0]

    print("\nLocal staging summary")
    print("---------------------")
    print(f"Reviewer observations kept:         {obs}")
    print(f"Exact technical duplicates skipped: {dupes}")
    print(f"Malformed-name observations flagged:{invalid:>8}")
    print(f"Valid provisional name groups:      {names}")

    if table_exists(conn, "name_resolution"):
        statuses = conn.execute("""
            SELECT status, COUNT(*)
            FROM name_resolution
            GROUP BY status
            ORDER BY COUNT(*) DESC, status
            """).fetchall()
        if statuses:
            print("\nDBLP local resolution status")
            print("----------------------------")
            for status, count in statuses:
                print(f"{status:40s} {count:6d}")

            orcids = conn.execute("""
                SELECT COUNT(*)
                FROM name_resolution
                WHERE best_orcid IS NOT NULL
                """).fetchone()[0]
            print(f"\nUnique exact matches with usable ORCID: {orcids}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(
        description=(
            "Resolve reviewer names against the official local DBLP "
            "September 2026 monthly snapshot."
        )
    )
    ap.add_argument(
        "--data-dir",
        type=Path,
        default=DEFAULT_DATA_DIR,
        help=f"Directory containing reviewer SQLite files (default: {DEFAULT_DATA_DIR})",
    )
    ap.add_argument(
        "--max-names",
        type=int,
        default=None,
        help="Limit reviewer name groups for a test run",
    )
    ap.add_argument(
        "--no-download",
        action="store_true",
        help="Do not download missing DBLP snapshot/DTD files",
    )
    ap.add_argument(
        "--reindex",
        action="store_true",
        help="Force rescanning the local DBLP snapshot",
    )
    args = ap.parse_args()

    data_dir = args.data_dir.resolve()
    data_dir.mkdir(parents=True, exist_ok=True)

    output_db = data_dir / OUTPUT_DB
    snapshot_dir = data_dir / "dblp_snapshot"
    snapshot_gz = snapshot_dir / SNAPSHOT_FILENAME
    dtd_path = snapshot_dir / DTD_FILENAME

    reviewer_dbs = discover_reviewer_dbs(data_dir, output_db)
    if not reviewer_dbs:
        print(
            f"ERROR: no reviewer SQLite databases with table "
            f"'{REVIEWER_TABLE}' found in {data_dir}",
            file=sys.stderr,
        )
        return 2

    print("Reviewer databases discovered:")
    for path in reviewer_dbs:
        print(f"  {path.name}")

    conn = sqlite3.connect(output_db)

    try:
        create_schema(conn)

        print(
            "\n[1/4] Ingesting reviewer observations, exact-deduplicating, "
            "and checking scraper-name quality..."
        )
        ingest_reviewers(conn, reviewer_dbs)

        invalid_path = data_dir / INVALID_NAMES_CSV
        export_invalid_names(conn, invalid_path)
        print_summary(conn)

        rows = conn.execute("""
            SELECT name_key, representative_name
            FROM name_groups
            ORDER BY name_key
            """).fetchall()

        if args.max_names is not None:
            rows = rows[: max(0, args.max_names)]

        keys = [r[0] for r in rows]
        t_hash = target_hash(keys)

        print(
            f"\n[2/4] Preparing DBLP monthly snapshot {SNAPSHOT_ID} "
            f"for {len(rows)} reviewer name groups..."
        )

        if args.no_download:
            if not snapshot_gz.exists() or not dtd_path.exists():
                print(
                    "ERROR: --no-download was specified, but the snapshot or "
                    "DTD is missing.",
                    file=sys.stderr,
                )
                print(f"Expected: {snapshot_gz}", file=sys.stderr)
                print(f"Expected: {dtd_path}", file=sys.stderr)
                return 2
        else:
            download_file(
                DTD_URL,
                dtd_path,
                DTD_MD5,
                "DBLP DTD",
            )
            download_file(
                SNAPSHOT_URL,
                snapshot_gz,
                SNAPSHOT_MD5,
                "DBLP monthly XML snapshot",
            )

        # Even when downloads are disabled, verify the files before parsing.
        if md5_file(dtd_path).lower() != DTD_MD5.lower():
            raise RuntimeError(
                "DBLP DTD MD5 does not match the published checksum."
            )

        current_snapshot_md5 = md5_file(snapshot_gz)
        if current_snapshot_md5.lower() != SNAPSHOT_MD5.lower():
            raise RuntimeError(
                "DBLP XML snapshot MD5 does not match the published checksum."
            )

        cache_ok = (
            not args.reindex
            and meta_get(conn, "index_complete") == "1"
            and meta_get(conn, "snapshot_id") == SNAPSHOT_ID
            and meta_get(conn, "snapshot_md5") == SNAPSHOT_MD5
            and meta_get(conn, "target_hash") == t_hash
        )

        if cache_ok:
            print(
                "\n[3/4] Reusing completed local DBLP candidate index "
                "for this exact reviewer-name set."
            )
        else:
            print(
                "\n[3/4] Streaming DBLP snapshot and building local "
                "candidate index..."
            )
            clear_local_index(conn)
            scan_snapshot(
                conn,
                snapshot_gz,
                dtd_path,
                rows,
            )
            meta_set(conn, "snapshot_id", SNAPSHOT_ID)
            meta_set(conn, "snapshot_md5", SNAPSHOT_MD5)
            meta_set(conn, "target_hash", t_hash)
            meta_set(conn, "index_complete", "1")
            conn.commit()

        print(
            "\n[4/4] Resolving conservative exact matches and exporting CSVs..."
        )
        resolve_names(conn, rows)

        resolution_path = data_dir / RESOLUTION_CSV
        candidates_path = data_dir / CANDIDATES_CSV

        export_resolution(conn, resolution_path)
        export_candidates(conn, candidates_path)
        export_invalid_names(conn, invalid_path)

        print_summary(conn)

        print("\nOutputs")
        print("-------")
        print(f"SQLite:                 {output_db}")
        print(f"Resolution summary:     {resolution_path}")
        print(f"All DBLP candidates:    {candidates_path}")
        print(f"Malformed scraped names:{invalid_path}")
        print(f"DBLP snapshot:          {snapshot_gz}")
        print(f"DBLP DTD:               {dtd_path}")

        print(
            "\nOnly 'resolved_unique_exact' is automatically assigned a DBLP PID. "
            "Variant and ambiguous matches are retained for later entity resolution."
        )
        return 0

    except KeyboardInterrupt:
        print(
            "\nInterrupted. Existing downloaded files/partial download are preserved."
        )
        return 130
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
