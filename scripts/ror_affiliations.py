#!/usr/bin/env python3
"""Pinned ROR snapshot support for conservative affiliation entity resolution.

This module deliberately has a narrow purpose: map affiliation strings used by
DB service rosters and DBLP person records to canonical ROR organization IDs.
It does not rewrite display affiliations and it does not call the live ROR API.

Matching is intentionally deterministic and conservative:
- exact normalized ROR names, labels, aliases, and acronyms only;
- exact affiliation chunks / parenthetical acronyms only (no fuzzy matching);
- aliases that point at more than one ROR organization are ignored;
- very short one-token aliases are accepted only when ROR marks them as an
  acronym (e.g. MIT, TUM), and two-character acronyms are rejected.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import unicodedata
import zipfile
from dataclasses import dataclass
from io import TextIOWrapper
from pathlib import Path
from typing import Any, Iterable

import requests

try:
    from .snapshot_sources import load_snapshot_sources
except ImportError:
    from snapshot_sources import load_snapshot_sources

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA_DIR = REPO_ROOT / "data"
DEFAULT_ROR_DIRNAME = "ror_snapshot"

# One selection is shared by download, matching, affiliation and website stages.
_ROR_SOURCE = load_snapshot_sources()["ror"]
ROR_RELEASE = _ROR_SOURCE["release"]
ROR_ZENODO_RECORD = _ROR_SOURCE["record"]
ROR_ZIP_FILENAME = _ROR_SOURCE["filename"]
ROR_ZIP_URL = _ROR_SOURCE["url"]
ROR_ZIP_MD5 = _ROR_SOURCE["md5"]
ROR_INDEX_FILENAME = "ror_affiliation_index.sqlite"
ROR_INDEX_SCHEMA_VERSION = "2"

SPACE_RE = re.compile(r"\s+")
NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")
PAREN_RE = re.compile(r"\(([^()]*)\)")
CHUNK_SPLIT_RE = re.compile(r"\s*(?:,|;|\||\s/\s)\s*")


@dataclass(frozen=True)
class RorMatch:
    ror_id: str
    display_name: str
    source_affiliation: str
    matched_surface: str
    matched_ror_name: str
    matched_name_type: str


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    return SPACE_RE.sub(" ", str(value).strip())


def normalize_org_name(value: Any) -> str:
    """Normalize organization text for exact alias lookup, not fuzzy matching."""
    text = clean_text(value)
    if not text:
        return ""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.lower().replace("&", " and ")
    text = NON_ALNUM_RE.sub(" ", text)
    return SPACE_RE.sub(" ", text).strip()


def md5_file(path: Path, chunk_size: int = 8 * 1024 * 1024) -> str:
    digest = hashlib.md5()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def download_ror_snapshot(dest: Path, *, force: bool = False) -> Path:
    """Download and checksum-verify the pinned ROR data dump."""
    dest = dest.resolve()
    dest.parent.mkdir(parents=True, exist_ok=True)

    if dest.exists() and not force:
        actual = md5_file(dest).lower()
        if actual == ROR_ZIP_MD5:
            print(f"ROR snapshot already present and verified: {dest}")
            return dest
        print(f"Existing ROR snapshot has wrong MD5 ({actual}); replacing it.")
        dest.unlink()

    part = dest.with_suffix(dest.suffix + ".part")
    if part.exists():
        part.unlink()

    print(f"Downloading pinned ROR snapshot {ROR_RELEASE}...")
    headers = {
        "User-Agent": "db-ml-entity-resolution-research/1.0",
        "Accept": "application/zip,application/octet-stream,*/*",
    }
    with requests.get(
        ROR_ZIP_URL,
        headers=headers,
        stream=True,
        timeout=(30, 120),
        allow_redirects=True,
    ) as response:
        response.raise_for_status()
        with part.open("wb") as handle:
            for chunk in response.iter_content(chunk_size=4 * 1024 * 1024):
                if chunk:
                    handle.write(chunk)

    part.replace(dest)
    actual = md5_file(dest).lower()
    if actual != ROR_ZIP_MD5:
        dest.unlink(missing_ok=True)
        raise RuntimeError(
            "ROR snapshot MD5 mismatch: "
            f"expected {ROR_ZIP_MD5}, got {actual}."
        )

    print(f"ROR snapshot MD5 OK: {actual}")
    return dest


def _json_member_name(archive: zipfile.ZipFile) -> str:
    candidates = [
        name
        for name in archive.namelist()
        if not name.endswith("/") and name.lower().endswith("-ror-data.json")
    ]
    if len(candidates) != 1:
        raise RuntimeError(
            "Expected exactly one '*-ror-data.json' file in the ROR archive; "
            f"found {len(candidates)}: {candidates}"
        )
    return candidates[0]


def _display_name(record: dict[str, Any]) -> str:
    for entry in record.get("names") or []:
        if "ror_display" in (entry.get("types") or []):
            return clean_text(entry.get("value"))
    for entry in record.get("names") or []:
        value = clean_text(entry.get("value"))
        if value:
            return value
    return clean_text(record.get("id"))


def build_ror_index_from_records(
    records: Iterable[dict[str, Any]],
    index_path: Path,
    *,
    snapshot_filename: str = ROR_ZIP_FILENAME,
    snapshot_md5: str = ROR_ZIP_MD5,
    release: str = ROR_RELEASE,
) -> dict[str, int]:
    """Build the compact local index from already-parsed ROR records.

    Exposed separately so the matcher can be unit-tested with tiny synthetic ROR
    records without downloading or embedding the real snapshot in the test suite.
    """
    index_path = index_path.resolve()
    index_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = index_path.with_suffix(index_path.suffix + ".tmp")
    tmp_path.unlink(missing_ok=True)

    conn = sqlite3.connect(tmp_path)
    org_count = 0
    name_count = 0
    try:
        conn.executescript(
            """
            PRAGMA journal_mode=OFF;
            PRAGMA synchronous=OFF;

            CREATE TABLE ror_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE ror_organizations (
                ror_id TEXT PRIMARY KEY,
                display_name TEXT NOT NULL,
                status TEXT NOT NULL
            );

            CREATE TABLE ror_names (
                normalized_name TEXT NOT NULL,
                ror_id TEXT NOT NULL,
                name_value TEXT NOT NULL,
                name_type TEXT NOT NULL,
                PRIMARY KEY (normalized_name, ror_id, name_value, name_type),
                FOREIGN KEY (ror_id) REFERENCES ror_organizations(ror_id)
            );
            """
        )

        for record in records:
            if not isinstance(record, dict):
                continue
            ror_id = clean_text(record.get("id"))
            status = clean_text(record.get("status")) or "unknown"
            if not ror_id or status == "withdrawn":
                continue

            display_name = _display_name(record)
            conn.execute(
                "INSERT OR REPLACE INTO ror_organizations (ror_id, display_name, status) VALUES (?, ?, ?)",
                (ror_id, display_name, status),
            )
            org_count += 1

            for entry in record.get("names") or []:
                if not isinstance(entry, dict):
                    continue
                name_value = clean_text(entry.get("value"))
                normalized = normalize_org_name(name_value)
                if not normalized:
                    continue
                name_types = entry.get("types") or ["unknown"]
                for name_type in name_types:
                    name_type = clean_text(name_type) or "unknown"
                    before = conn.total_changes
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO ror_names (
                            normalized_name, ror_id, name_value, name_type
                        ) VALUES (?, ?, ?, ?)
                        """,
                        (normalized, ror_id, name_value, name_type),
                    )
                    if conn.total_changes > before:
                        name_count += 1


        conn.executescript(
            """
            CREATE INDEX idx_ror_names_normalized
                ON ror_names(normalized_name);
            CREATE INDEX idx_ror_names_ror_id
                ON ror_names(ror_id);
            """
        )
        conn.executemany(
            "INSERT INTO ror_meta (key, value) VALUES (?, ?)",
            [
                ("ror_release", release),
                ("ror_index_schema_version", ROR_INDEX_SCHEMA_VERSION),
                ("ror_zenodo_record", ROR_ZENODO_RECORD),
                ("ror_snapshot_filename", snapshot_filename),
                ("ror_snapshot_md5", snapshot_md5),
            ],
        )
        conn.commit()
    finally:
        conn.close()

    tmp_path.replace(index_path)
    return {"organizations": org_count, "name_rows": name_count}


def build_ror_index(snapshot_zip: Path, index_path: Path) -> dict[str, int]:
    """Build a compact local exact-name index from the pinned ROR JSON dump."""
    snapshot_zip = snapshot_zip.resolve()
    if not snapshot_zip.exists():
        raise FileNotFoundError(snapshot_zip)

    actual_md5 = md5_file(snapshot_zip).lower()
    if actual_md5 != ROR_ZIP_MD5:
        raise RuntimeError(
            f"Refusing to index unverified ROR snapshot {snapshot_zip}; "
            f"expected MD5 {ROR_ZIP_MD5}, got {actual_md5}."
        )

    with zipfile.ZipFile(snapshot_zip) as archive:
        member = _json_member_name(archive)
        with archive.open(member, "r") as raw:
            payload = json.load(TextIOWrapper(raw, encoding="utf-8"))

    if not isinstance(payload, list):
        raise RuntimeError("ROR JSON dump root is not a list of organization records.")

    return build_ror_index_from_records(
        payload,
        index_path,
        snapshot_filename=snapshot_zip.name,
        snapshot_md5=actual_md5,
        release=ROR_RELEASE,
    )


def ror_index_is_current(index_path: Path) -> bool:
    if not index_path.exists():
        return False
    try:
        conn = sqlite3.connect(index_path)
        try:
            rows = dict(conn.execute("SELECT key, value FROM ror_meta").fetchall())
        finally:
            conn.close()
    except sqlite3.Error:
        return False
    return (
        rows.get("ror_release") == ROR_RELEASE
        and rows.get("ror_snapshot_md5") == ROR_ZIP_MD5
        and rows.get("ror_index_schema_version") == ROR_INDEX_SCHEMA_VERSION
    )


def ensure_ror_index(
    ror_dir: Path,
    *,
    download: bool = False,
    force_download: bool = False,
    force_rebuild: bool = False,
) -> tuple[Path, Path]:
    """Ensure the pinned archive and derived index exist.

    Entity-resolution callers normally pass ``download=False`` so the matching
    stage is fully offline. The dedicated downloader passes ``download=True``.
    """
    ror_dir = ror_dir.resolve()
    snapshot = ror_dir / ROR_ZIP_FILENAME
    index_path = ror_dir / ROR_INDEX_FILENAME

    if download:
        download_ror_snapshot(snapshot, force=force_download)
    elif not snapshot.exists():
        raise FileNotFoundError(
            f"Pinned ROR snapshot not found: {snapshot}\n"
            "Run scripts/download_ror_snapshot.py first."
        )

    if force_rebuild or not ror_index_is_current(index_path):
        stats = build_ror_index(snapshot, index_path)
        print(
            f"Built ROR affiliation index: {stats['organizations']:,} organizations, "
            f"{stats['name_rows']:,} name/type rows."
        )
    return snapshot, index_path


def affiliation_surfaces(value: str) -> list[tuple[str, str]]:
    """Return conservative exact-lookup surfaces from an affiliation string."""
    raw = clean_text(value)
    if not raw:
        return []

    candidates: list[tuple[str, str]] = []

    def add(surface: str, method: str) -> None:
        surface = clean_text(surface).strip(" ,;|-/")
        normalized = normalize_org_name(surface)
        if not normalized:
            return
        item = (surface, method)
        if item not in candidates:
            candidates.append(item)

    add(raw, "whole")

    # Parenthetical text can be part of the organization identity rather than
    # disposable metadata (for example, a distinct campus such as
    # "Hong Kong University of Science and Technology (Guangzhou)").  Never
    # delete parentheticals to manufacture a match to a parent organization.
    # Explicit parenthetical acronyms are still looked up independently below.

    for content in PAREN_RE.findall(raw):
        add(content, "parenthetical")

    # Organization names in the service/DBLP sources are overwhelmingly at the
    # start of an affiliation. Limiting exact chunk lookup to the first two
    # chunks avoids accidentally treating trailing city/country abbreviations
    # as organization acronyms. The whole string and all parenthetical acronyms
    # are still considered above.
    for index, chunk in enumerate(CHUNK_SPLIT_RE.split(raw)):
        if index > 1:
            break
        if not clean_text(chunk):
            continue
        method = "first_chunk" if index == 0 else "second_chunk"
        add(chunk, method)
        for content in PAREN_RE.findall(chunk):
            add(content, "parenthetical")

    return candidates


def _surface_is_safe(surface: str, name_types: set[str]) -> bool:
    normalized = normalize_org_name(surface)
    if not normalized:
        return False
    tokens = normalized.split()
    compact_len = len(normalized.replace(" ", ""))
    if len(tokens) == 1 and compact_len <= 2:
        return False
    if len(tokens) == 1 and compact_len <= 4:
        # Short singleton forms are safe only when ROR explicitly identifies
        # them as acronyms; this permits MIT/TUM/CWI but rejects arbitrary short
        # words that happen to equal an organization name.
        return "acronym" in name_types
    return True


class RorAffiliationIndex:
    def __init__(self, index_path: Path):
        self.index_path = index_path.resolve()
        if not ror_index_is_current(self.index_path):
            raise RuntimeError(
                f"ROR affiliation index is missing/stale: {self.index_path}. "
                "Run scripts/download_ror_snapshot.py."
            )
        self.conn = sqlite3.connect(self.index_path)
        self.conn.row_factory = sqlite3.Row

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "RorAffiliationIndex":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def match_affiliation(self, affiliation: str) -> dict[str, RorMatch]:
        """Return all unambiguous exact ROR matches found in the affiliation."""
        matches: dict[str, RorMatch] = {}
        for surface, method in affiliation_surfaces(affiliation):
            normalized = normalize_org_name(surface)
            rows = self.conn.execute(
                """
                SELECT n.ror_id, n.name_value, n.name_type, o.display_name
                FROM ror_names n
                JOIN ror_organizations o USING(ror_id)
                WHERE n.normalized_name=?
                ORDER BY n.ror_id, n.name_type, n.name_value
                """,
                (normalized,),
            ).fetchall()
            if not rows:
                continue

            ror_ids = {row["ror_id"] for row in rows}
            # A shared acronym/alias is not identity evidence if ROR itself says
            # it can refer to multiple organizations.
            if len(ror_ids) != 1:
                continue

            name_types = {row["name_type"] for row in rows}
            if not _surface_is_safe(surface, name_types):
                continue

            row = rows[0]
            ror_id = row["ror_id"]
            existing = matches.get(ror_id)
            candidate = RorMatch(
                ror_id=ror_id,
                display_name=row["display_name"],
                source_affiliation=clean_text(affiliation),
                matched_surface=clean_text(surface),
                matched_ror_name=row["name_value"],
                matched_name_type=(
                    "acronym" if "acronym" in name_types else row["name_type"]
                ),
            )
            if existing is None or len(candidate.matched_surface) > len(existing.matched_surface):
                matches[ror_id] = candidate
        return matches

    def match_affiliations(self, affiliations: Iterable[str]) -> dict[str, RorMatch]:
        matches: dict[str, RorMatch] = {}
        for affiliation in affiliations:
            for ror_id, match in self.match_affiliation(affiliation).items():
                existing = matches.get(ror_id)
                if existing is None or len(match.matched_surface) > len(existing.matched_surface):
                    matches[ror_id] = match
        return matches


class IdentityRorAffiliationIndex(RorAffiliationIndex):
    """Exact organization evidence with campus detail preserved for identity matching.

    Display-affiliation reconciliation continues to use RorAffiliationIndex.
    A university system or a parent campus cannot establish a person's identity.
    """

    def __init__(self, index_path: Path):
        super().__init__(index_path)
        self._identity_cache: dict[str, dict[str, RorMatch]] = {}

    def match_affiliation(self, affiliation: str) -> dict[str, RorMatch]:
        if affiliation in self._identity_cache:
            return dict(self._identity_cache[affiliation])
        matches = dict(super().match_affiliation(affiliation))
        parts = [part.strip() for part in affiliation.split(",") if part.strip()]
        for length in range(2, min(len(parts), 4) + 1):
            prefix = ", ".join(parts[:length])
            for ror_id, candidate in super().match_affiliation(prefix).items():
                if normalize_org_name(candidate.matched_surface) == normalize_org_name(prefix):
                    matches[ror_id] = candidate

        normalized = normalize_org_name(affiliation)
        retained = {}
        for ror_id, candidate in matches.items():
            display = normalize_org_name(candidate.display_name)
            if display.endswith(" system"):
                continue
            if (display == "hong kong university of science and technology" and
                    any(label in normalized for label in ("guangzhou", "guang zhou", "hkust gz", "technology gz"))):
                continue
            if display == "chinese university of hong kong" and "shenzhen" in normalized:
                continue
            surface = normalize_org_name(candidate.matched_surface)
            if any(other_id != ror_id and "university" in normalize_org_name(other.display_name)
                   and normalize_org_name(other.matched_surface).startswith(surface + " ")
                   for other_id, other in matches.items()):
                continue
            retained[ror_id] = candidate
        self._identity_cache[affiliation] = retained
        return dict(retained)
