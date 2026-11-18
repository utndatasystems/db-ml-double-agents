#!/usr/bin/env python3
"""Build the publication corpus for every resolved DB reviewer directly from DBLP.

This is the only publication-ingestion step used by the website.

Inputs
------
- data/entity_resolution.sqlite
- data/dblp_snapshot/<pinned DBLP XML snapshot + DTD>

Output
------
- data/dblp_publications_for_db_people.csv

Identity rule
-------------
The scan starts only from DB reviewers/editors that already have a resolved DBLP
PID. Publication author signatures are staged only when they exactly match one
of that person's DBLP aliases (or a verified ORCID/direct PID matches). During
the same XML pass, DBLP person records are used to confirm that an exact author
signature/ORCID is globally owned by that one PID. Ambiguous signatures are
skipped. No fuzzy publication-author matching is performed.

Publication scope
-----------------
- main: an exact main-conference paper at NeurIPS/NIPS, ICML, or ICLR.
- side: every other paper-like DBLP record by the resolved person.

"Side" is intentionally broad context. It is not evidence that a publication is
an ML paper and it never counts toward the core crossover definition or matrix.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
import sqlite3
import sys
import time
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from lxml import etree

try:
    from .enrich_reviewers_with_dblp import (
        DEFAULT_DATA_DIR,
        DTD_FILENAME,
        DTD_MD5,
        DTD_URL,
        LocalDTDResolver,
        SNAPSHOT_FILENAME,
        SNAPSHOT_ID,
        SNAPSHOT_MD5,
        SNAPSHOT_URL,
        TOP_LEVEL_RECORD_TYPES,
        XML_READ_CHUNK_SIZE,
        clean_text,
        download_file,
        extract_orcid,
        verified_md5,
    )
except ImportError:  # Running as ``python scripts/scan_dblp_publications.py``
    from enrich_reviewers_with_dblp import (
        DEFAULT_DATA_DIR,
        DTD_FILENAME,
        DTD_MD5,
        DTD_URL,
        LocalDTDResolver,
        SNAPSHOT_FILENAME,
        SNAPSHOT_ID,
        SNAPSHOT_MD5,
        SNAPSHOT_URL,
        TOP_LEVEL_RECORD_TYPES,
        XML_READ_CHUNK_SIZE,
        clean_text,
        download_file,
        extract_orcid,
        verified_md5,
    )

OUTPUT_CSV = "dblp_publications_for_db_people.csv"
MAIN_TOTALS_CSV = "dblp_main_ml_venue_year_totals.csv"
SCAN_CACHE_VERSION = "7"
REVIEWED_AUTHORS_CSV = "reviewed_publication_authors.csv"
PAPER_RECORD_TYPES = {"article", "inproceedings", "incollection"}
MAIN_STREAMS = {
    "conf/nips": "NeurIPS",
    "conf/neurips": "NeurIPS",
    "conf/icml": "ICML",
    "conf/iclr": "ICLR",
}
MAIN_BOOKTITLES = {
    "nips": "NeurIPS",
    "neurips": "NeurIPS",
    "icml": "ICML",
    # The three main-conference proceedings cycles of ICML 2013.
    "icml (1)": "ICML",
    "icml (2)": "ICML",
    "icml (3)": "ICML",
    "iclr": "ICLR",
    # DBLP labels accepted ICLR conference-track poster papers this way in
    # historical editions (notably 2018/2019). Poster is a presentation format,
    # not a workshop/satellite classification.
    "iclr (poster)": "ICLR",
}
MAIN_KEY_PREFIXES = tuple(f"{stream}/" for stream in MAIN_STREAMS)


@dataclass(frozen=True)
class TargetPerson:
    pid: str
    canonical_name: str
    aliases: tuple[str, ...]
    orcids: tuple[str, ...]
    # Exact record-level exceptions; never aliases for an entire short-name PID.
    reviewed_publications: tuple[tuple[str, str, str], ...] = ()


def _stable_strings(values: Iterable[str]) -> list[str]:
    return sorted(set(values), key=lambda value: (value.casefold(), value))


def target_fingerprint(targets: list[TargetPerson]) -> str:
    h = hashlib.sha256()
    for person in sorted(targets, key=lambda p: p.pid):
        payload = {
            "pid": person.pid,
            "canonical_name": person.canonical_name,
            # Fingerprints must not depend on tuple/set iteration order. In
            # particular, str.casefold alone is not a total ordering when two
            # aliases differ only by case, so canonicalize again here.
            "aliases": _stable_strings(person.aliases),
            "orcids": _stable_strings(person.orcids),
            "reviewed_publications": sorted(person.reviewed_publications),
        }
        h.update(
            json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
        )
        h.update(b"\n")
    return h.hexdigest()


def scan_cache_path(output: Path) -> Path:
    return output.with_suffix(output.suffix + ".scan.json")


def file_sha256(path: Path, *, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def scan_cache_payload(
    snapshot_gz: Path,
    targets: list[TargetPerson],
) -> dict[str, Any]:
    stat = snapshot_gz.stat()
    return {
        "cache_version": SCAN_CACHE_VERSION,
        "snapshot_id": SNAPSHOT_ID,
        "snapshot_md5": SNAPSHOT_MD5.lower(),
        "snapshot_size": stat.st_size,
        # Do not key the expensive publication scan on filesystem mtimes. The
        # pinned snapshot is verified independently; touching/copying the same
        # bytes must not force a 13M-record XML scan.
        "target_fingerprint": target_fingerprint(targets),
    }


def scan_cache_is_current(
    output: Path,
    main_totals_output: Path,
    snapshot_gz: Path,
    targets: list[TargetPerson],
) -> bool:
    if not output.exists() or not main_totals_output.exists():
        return False
    cache_path = scan_cache_path(output)
    if not cache_path.exists():
        return False
    try:
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    expected = scan_cache_payload(snapshot_gz, targets)
    if not all(cached.get(key) == value for key, value in expected.items()):
        return False

    output_stat = output.stat()
    totals_stat = main_totals_output.stat()
    if not (
        cached.get("output_size") == output_stat.st_size
        and cached.get("main_totals_output_size") == totals_stat.st_size
    ):
        return False

    # Fast path for the common case. Existing v3 cache files without hashes
    # remain valid as long as their output mtimes are unchanged.
    mtimes_match = (
        cached.get("output_mtime_ns") == output_stat.st_mtime_ns
        and cached.get("main_totals_output_mtime_ns") == totals_stat.st_mtime_ns
    )
    if mtimes_match:
        # Upgrade legacy v3 sidecars in place so future harmless mtime changes
        # can be verified by content without requiring another full scan.
        if not cached.get("output_sha256") or not cached.get("main_totals_output_sha256"):
            cached["output_sha256"] = file_sha256(output)
            cached["main_totals_output_sha256"] = file_sha256(main_totals_output)
            tmp = cache_path.with_suffix(cache_path.suffix + ".tmp")
            tmp.write_text(
                json.dumps(cached, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            tmp.replace(cache_path)
        return True

    # A checkout, copy, archive extraction, or other harmless operation can
    # change mtimes without changing content. New cache files carry hashes so
    # those cases stay cheap instead of rescanning all of DBLP.
    output_sha256 = cached.get("output_sha256")
    totals_sha256 = cached.get("main_totals_output_sha256")
    if not output_sha256 or not totals_sha256:
        return False
    return (
        output_sha256 == file_sha256(output)
        and totals_sha256 == file_sha256(main_totals_output)
    )


def write_scan_cache(
    output: Path,
    main_totals_output: Path,
    snapshot_gz: Path,
    targets: list[TargetPerson],
    *,
    row_count: int,
    stats: dict[str, int],
) -> None:
    payload = scan_cache_payload(snapshot_gz, targets)
    output_stat = output.stat()
    totals_stat = main_totals_output.stat()
    payload["output_size"] = output_stat.st_size
    payload["output_mtime_ns"] = output_stat.st_mtime_ns
    payload["output_sha256"] = file_sha256(output)
    payload["main_totals_output_size"] = totals_stat.st_size
    payload["main_totals_output_mtime_ns"] = totals_stat.st_mtime_ns
    payload["main_totals_output_sha256"] = file_sha256(main_totals_output)
    payload["row_count"] = row_count
    payload["stats"] = stats
    path = scan_cache_path(output)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    tmp.replace(path)


def json_list(value: Any) -> list[str]:
    import json

    if value in (None, ""):
        return []
    try:
        parsed = json.loads(str(value))
    except (TypeError, json.JSONDecodeError):
        return []
    if not isinstance(parsed, list):
        return []
    return [clean_text(x) for x in parsed if clean_text(x)]


def load_targets(entity_db: Path) -> list[TargetPerson]:
    conn = sqlite3.connect(entity_db)
    try:
        have = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        missing = {"name_resolution", "dblp_people"} - have
        if missing:
            raise RuntimeError(
                "entity_resolution.sqlite is missing table(s): "
                + ", ".join(sorted(missing))
            )

        resolved = {
            clean_text(row[0])
            for row in conn.execute(
                """
                SELECT DISTINCT resolved_dblp_pid
                FROM name_resolution
                WHERE resolved_dblp_pid IS NOT NULL
                  AND TRIM(resolved_dblp_pid) <> ''
                """
            )
            if clean_text(row[0])
        }

        targets: list[TargetPerson] = []
        reviewed = load_reviewed_authors(entity_db.parent / REVIEWED_AUTHORS_CSV, resolved)
        if "dblp_nonperson_candidates" in have:
            nonpeople = {r[0] for r in conn.execute("SELECT DISTINCT dblp_pid FROM dblp_nonperson_candidates")}
            if resolved & nonpeople:
                raise RuntimeError("A resolved PID is a DBLP disambiguation page; rebuild identity resolution first")
        stored: set[str] = set()
        for pid, canonical, aliases_json, orcids_json in conn.execute(
            """
            SELECT dblp_pid, canonical_name, aliases_json, verified_orcids_json
            FROM dblp_people
            """
        ):
            pid = clean_text(pid)
            if not pid:
                continue
            stored.add(pid)
            if pid not in resolved:
                continue
            aliases = set(json_list(aliases_json))
            if clean_text(canonical):
                aliases.add(clean_text(canonical))
            orcids = {x.upper() for x in json_list(orcids_json)}
            targets.append(
                TargetPerson(
                    pid=pid,
                    canonical_name=clean_text(canonical) or pid,
                    aliases=tuple(_stable_strings(aliases)),
                    orcids=tuple(sorted(orcids)),
                    reviewed_publications=tuple(sorted(reviewed.get(pid, []))),
                )
            )

        missing_people = resolved - stored
        if missing_people:
            print(
                "WARNING: resolved PIDs without a stored DBLP person record: "
                f"{len(missing_people)}. They cannot be publication-scanned."
            )
        return sorted(targets, key=lambda p: p.pid)
    finally:
        conn.close()


def load_reviewed_authors(path: Path, resolved: set[str]) -> dict[str, list[tuple[str, str, str]]]:
    """Load evidence-backed author assignments limited to individual DBLP keys."""
    result: dict[str, list[tuple[str, str, str]]] = defaultdict(list)
    if not path.exists():
        return result
    seen: dict[tuple[str, str], str] = {}
    with path.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            key, source, target, signature = (row[k].strip() for k in ("dblp_key", "source_pid", "target_pid", "author_signature"))
            date.fromisoformat(row["verified_on"])
            if not all((key, source, target, signature, row["verified_on"], row["reason"])) or not row["source_url"].startswith("https://"):
                raise ValueError("Incomplete reviewed publication author: " + key)
            identity = (key, signature)
            if identity in seen:
                raise ValueError("Duplicate or conflicting reviewed publication author: " + key)
            seen[identity] = target
            if target in resolved:
                result[target].append((key, source, signature))
    return result


def reviewed_authors_in_publication(elem, assignments, by_pid=None):
    matches = []
    authors = [c for c in elem if c.tag == "author"]
    for target, source, signature in assignments:
        matching = [(i, c) for i, c in enumerate(authors, 1) if clean_text(c.text) == signature]
        if len(matching) != 1:
            continue
        position, author = matching[0]
        # An explicit contradictory DBLP PID takes precedence over an old review.
        if author.get("pid") and author.get("pid") not in (source, target):
            continue
        orcid = clean_text(extract_orcid(author.get("orcid", "")) or "").upper()
        if orcid and by_pid is not None and orcid not in by_pid[target].orcids:
            continue
        matches.append(dict(pid=target, basis="reviewed_publication_author", signature=signature,
                            orcid=orcid, position=position))
    return matches


def build_target_maps(
    targets: list[TargetPerson],
) -> tuple[dict[str, set[str]], dict[str, set[str]], dict[str, TargetPerson]]:
    aliases: dict[str, set[str]] = defaultdict(set)
    orcids: dict[str, set[str]] = defaultdict(set)
    by_pid = {person.pid: person for person in targets}
    for person in targets:
        for alias in person.aliases:
            aliases[alias].add(person.pid)
        for orcid in person.orcids:
            orcids[orcid.upper()].add(person.pid)
    return aliases, orcids, by_pid


def normalize_stream(value: str) -> str:
    value = clean_text(value).casefold().strip("/")
    if value.startswith("streams/"):
        value = value[len("streams/") :]
    return value


def normalize_booktitle(value: str) -> str:
    return clean_text(value).casefold().replace(".", "").strip()


def main_venue_for_record(
    *, key: str, booktitle: str, streams: list[str]
) -> str | None:
    """Return the exact target main venue, otherwise None.

    DBLP stream keys are preferred. A conservative booktitle fallback is kept
    for older snapshot records without structured stream data. Generic
    conf/nips-style prefixes alone are not enough because DBLP can associate
    satellite workshops with a parent conference namespace.
    """
    book = normalize_booktitle(booktitle)
    if "workshop" in book:
        return None

    stream_values = {normalize_stream(value) for value in streams if clean_text(value)}
    exact_stream_venues = {
        MAIN_STREAMS[value] for value in stream_values if value in MAIN_STREAMS
    }
    if len(exact_stream_venues) == 1:
        venue = next(iter(exact_stream_venues))
        # If DBLP also provides a concise booktitle, require it to agree when
        # it is one of the four canonical acronyms. Unknown/long booktitles do
        # not override the structured stream relation.
        if book in MAIN_BOOKTITLES and MAIN_BOOKTITLES[book] != venue:
            return None
        return venue

    fallback = MAIN_BOOKTITLES.get(book)
    low_key = key.casefold()
    if fallback == "NeurIPS" and (
        low_key.startswith("conf/nips/") or low_key.startswith("conf/neurips/")
    ):
        return fallback
    if fallback == "ICML" and low_key.startswith("conf/icml/"):
        return fallback
    if fallback == "ICLR" and low_key.startswith("conf/iclr/"):
        return fallback
    return None


def main_venue_year_for_record(elem: etree._Element) -> tuple[str, int] | None:
    """Return (main venue, year) for a target main-conference record.

    This is intentionally cheap for the full DBLP scan: records outside the
    NeurIPS/NIPS, ICML, and ICLR namespaces are rejected from the key alone.
    Candidate records then use the same strict stream/booktitle logic as the
    researcher publication classifier, so workshop records are not counted in
    the conference-size denominator.
    """
    if elem.tag != "inproceedings":
        return None

    key = str(elem.attrib.get("key", "")).strip()
    low_key = key.casefold()
    if not any(low_key.startswith(prefix) for prefix in MAIN_KEY_PREFIXES):
        return None

    booktitle = ""
    year: int | None = None
    streams: list[str] = []
    for child in elem:
        tag = child.tag
        if tag not in {"booktitle", "year", "stream"}:
            continue
        text = clean_text("".join(child.itertext()))
        if tag == "booktitle":
            booktitle = text
        elif tag == "year":
            try:
                year = int(text)
            except (TypeError, ValueError):
                year = None
        elif tag == "stream" and text:
            streams.append(text)

    if year is None:
        return None
    venue = main_venue_for_record(key=key, booktitle=booktitle, streams=streams)
    return (venue, year) if venue else None


def target_authors_in_publication(
    elem: etree._Element,
    *,
    target_pids: set[str],
    target_aliases: dict[str, set[str]],
    target_orcids: dict[str, set[str]],
) -> tuple[list[dict[str, Any]], int]:
    """Return target-author matches plus the total author count.

    The hot path deliberately avoids parsing titles, venues, URLs and other
    publication metadata until at least one target author is present.  A DBLP
    author PID is authoritative: when it exists and is not one of our targets,
    we can reject that author without normalizing their name at all.
    """
    matches: list[dict[str, Any]] = []
    author_count = 0

    for child in elem:
        if child.tag != "author":
            continue

        author_count += 1
        author_pid = str(child.attrib.get("pid", "")).strip()
        if author_pid:
            if author_pid not in target_pids:
                continue
            signature = clean_text(child.text)
            author_orcid = clean_text(
                extract_orcid(child.attrib.get("orcid", "")) or ""
            ).upper()
            matches.append(
                {
                    "pid": author_pid,
                    "basis": "dblp_author_pid",
                    "signature": signature,
                    "orcid": author_orcid,
                    "position": author_count,
                }
            )
            continue

        author_orcid = clean_text(
            extract_orcid(child.attrib.get("orcid", "")) or ""
        ).upper()
        if author_orcid:
            candidate = unique_pid(target_orcids.get(author_orcid))
            if candidate:
                matches.append(
                    {
                        "pid": candidate,
                        "basis": "verified_orcid",
                        "signature": clean_text(child.text),
                        "orcid": author_orcid,
                        "position": author_count,
                    }
                )
                continue

        signature = clean_text(child.text)
        if signature:
            candidate = unique_pid(target_aliases.get(signature))
            if candidate:
                matches.append(
                    {
                        "pid": candidate,
                        "basis": "exact_dblp_author_signature",
                        "signature": signature,
                        "orcid": author_orcid,
                        "position": author_count,
                    }
                )

    return matches, author_count


def publication_fields(elem: etree._Element) -> dict[str, Any] | None:
    key = str(elem.attrib.get("key", "")).strip()
    if not key:
        return None

    title = ""
    booktitle = ""
    journal = ""
    year: int | None = None
    authors: list[str] = []
    ees: list[str] = []
    streams: list[str] = []
    rels: list[dict[str, str]] = []

    for child in elem:
        tag = child.tag
        text = clean_text("".join(child.itertext()))
        if tag == "author":
            if text:
                authors.append(text)
            continue
        if tag == "title":
            title = text
        elif tag == "booktitle":
            booktitle = text
        elif tag == "journal":
            journal = text
        elif tag == "year":
            try:
                year = int(text)
            except (TypeError, ValueError):
                year = None
        elif tag == "ee" and text:
            ees.append(text)
        elif tag == "stream" and text:
            streams.append(text)
        elif tag == "rel":
            relation: dict[str, str] = {}
            if text:
                relation["text"] = text
                relation["target"] = text
            for attr in ("uri", "type", "label", "sort"):
                value = clean_text(child.attrib.get(attr, ""))
                if value:
                    relation[attr] = value
            if "target" not in relation and relation.get("uri"):
                relation["target"] = relation["uri"]
            if relation:
                rels.append(relation)

    if not title or year is None:
        return None

    venue = main_venue_for_record(key=key, booktitle=booktitle, streams=streams)
    scope = "main" if venue else "side"
    venue_label = venue or booktitle or journal or "Other DBLP publication"
    return {
        "dblp_key": key,
        "title": title,
        "year": year,
        "booktitle": booktitle,
        "journal": journal,
        "streams": streams,
        "authors": authors,
        "ees": ees,
        "rels": rels,
        "url": ees[0] if ees else f"https://dblp.org/rec/{key}",
        "scope": scope,
        "venue": venue or "",
        "venue_label": venue_label,
        "record_type": str(elem.tag),
        "publtype": str(elem.attrib.get("publtype", "")).strip(),
    }


def unique_pid(values: set[str] | None) -> str | None:
    if values and len(values) == 1:
        return next(iter(values))
    return None


def interested_person_identifiers(
    elem: etree._Element,
    *,
    alias_interest: set[str],
    orcid_interest: set[str],
) -> tuple[str, set[str], set[str]] | None:
    """Read identifiers needed for global ambiguity checks.

    Disambiguation pages remain ambiguity blockers here, never person targets.
    Dropping them could falsely attach an unassigned author signature to a
    same-named person. Target identities are validated in load_targets().
    """
    key = str(elem.attrib.get("key", "")).strip()
    if not key.startswith("homepages/"):
        return None

    pid = key[len("homepages/") :]
    aliases: set[str] = set()
    orcids: set[str] = set()
    for child in elem:
        tag = child.tag
        if tag == "author":
            value = clean_text(child.text)
            if value in alias_interest:
                aliases.add(value)
        elif tag == "url":
            value = extract_orcid(clean_text(child.text))
            if value and value.upper() in orcid_interest:
                orcids.add(value.upper())
    return pid, aliases, orcids


def scan(
    snapshot_gz: Path,
    dtd_path: Path,
    targets: list[TargetPerson],
) -> tuple[list[dict[str, Any]], dict[str, int], dict[tuple[str, int], int]]:
    target_aliases, target_orcids, by_pid = build_target_maps(targets)
    target_pids = set(by_pid)
    reviewed_by_key = defaultdict(list)
    for person in targets:
        for key, source_pid, signature in person.reviewed_publications:
            reviewed_by_key[key].append((person.pid, source_pid, signature))
    alias_interest = set(target_aliases)
    orcid_interest = set(target_orcids)

    # Global ownership is collected from all DBLP person records later in the
    # same XML pass. This prevents an exact-but-homonymous author signature
    # from being accepted merely because only one such PID happened to be in
    # our local candidate index.
    global_alias_owners: dict[str, set[str]] = defaultdict(set)
    global_orcid_owners: dict[str, set[str]] = defaultdict(set)

    staged: dict[tuple[str, str], dict[str, Any]] = {}
    stats = defaultdict(int)
    main_venue_year_totals: dict[tuple[str, int], int] = defaultdict(int)

    parser = etree.XMLPullParser(
        events=("end",),
        tag=TOP_LEVEL_RECORD_TYPES,
        load_dtd=True,
        resolve_entities=True,
        no_network=True,
        huge_tree=True,
        recover=False,
    )
    parser.resolvers.add(LocalDTDResolver(dtd_path))

    print(
        f"Scanning {snapshot_gz.name} for all paper-like DBLP publications "
        f"by {len(targets)} resolved DB people..."
    )
    last_report = time.monotonic()

    with gzip.open(snapshot_gz, "rb") as fh:
        while True:
            chunk = fh.read(XML_READ_CHUNK_SIZE)
            if not chunk:
                break
            parser.feed(chunk)
            for _event, elem in parser.read_events():
                parent = elem.getparent()
                if parent is None or parent.getparent() is not None:
                    continue

                stats["records"] += 1
                tag = elem.tag

                if tag in PAPER_RECORD_TYPES:
                    main_venue_year = main_venue_year_for_record(elem)
                    if main_venue_year:
                        main_venue_year_totals[main_venue_year] += 1

                    matches, author_count = target_authors_in_publication(
                        elem,
                        target_pids=target_pids,
                        target_aliases=target_aliases,
                        target_orcids=target_orcids,
                    )
                    assignments = reviewed_by_key.get(elem.get("key", ""))
                    if assignments:
                        reviewed = reviewed_authors_in_publication(elem, assignments, by_pid)
                        replaced = {(source, match["position"])
                                    for match in reviewed for target, source, signature in assignments
                                    if target == match["pid"] and signature == match["signature"]}
                        # A validated correction replaces this source authorship,
                        # not a different coauthor elsewhere in the same paper.
                        matches = [m for m in matches if (m["pid"], m["position"]) not in replaced]
                        matches.extend(reviewed)
                    if matches:
                        rec = publication_fields(elem)
                    else:
                        rec = None
                    if rec:
                        for match in matches:
                            pid = match["pid"]
                            basis = match["basis"]
                            signature = match["signature"]
                            author_orcid = match["orcid"]
                            person = by_pid[pid]
                            identity = (pid, rec["dblp_key"])
                            row = {
                                "dblp_pid": pid,
                                "canonical_name": person.canonical_name,
                                "scope": rec["scope"],
                                "venue": rec["venue"],
                                "venue_label": rec["venue_label"],
                                "year": rec["year"],
                                "title": rec["title"],
                                "record_type": rec["record_type"],
                                "publtype": rec["publtype"],
                                "booktitle": rec["booktitle"],
                                "journal": rec["journal"],
                                "streams": ";".join(rec["streams"]),
                                "authors_json": json.dumps(rec["authors"], ensure_ascii=False),
                                "ees_json": json.dumps(rec["ees"], ensure_ascii=False),
                                "rels_json": json.dumps(rec["rels"], ensure_ascii=False, sort_keys=True),
                                "dblp_key": rec["dblp_key"],
                                "url": rec["url"],
                                "author_signature": signature,
                                "author_orcid": author_orcid,
                                "author_position": match["position"],
                                "author_count": author_count,
                                "match_basis": basis,
                                "snapshot_id": SNAPSHOT_ID,
                            }
                            rank = {
                                "reviewed_publication_author": 4,
                                "dblp_author_pid": 3,
                                "verified_orcid": 2,
                                "exact_dblp_author_signature": 1,
                            }
                            old = staged.get(identity)
                            if old is None or rank[basis] > rank.get(old["match_basis"], 0):
                                staged[identity] = row

                elif tag == "www":
                    interested = interested_person_identifiers(
                        elem,
                        alias_interest=alias_interest,
                        orcid_interest=orcid_interest,
                    )
                    if interested:
                        pid, aliases, orcids = interested
                        for alias in aliases:
                            global_alias_owners[alias].add(pid)
                        for orcid in orcids:
                            global_orcid_owners[orcid].add(pid)

                elem.clear()
                while elem.getprevious() is not None:
                    del parent[0]

                if time.monotonic() - last_report >= 10:
                    print(
                        f"  scanned {stats['records']:,} records; "
                        f"{len(staged):,} candidate person/publication links staged"
                    )
                    last_report = time.monotonic()

    parser.close()

    accepted: list[dict[str, Any]] = []
    for row in staged.values():
        pid = row["dblp_pid"]
        basis = row["match_basis"]
        valid = False
        if basis in {"dblp_author_pid", "reviewed_publication_author"}:
            valid = True
        elif basis == "verified_orcid":
            valid = global_orcid_owners.get(row["author_orcid"]) == {pid}
            if not valid:
                stats["skipped_orcid_ambiguity"] += 1
        elif basis == "exact_dblp_author_signature":
            valid = global_alias_owners.get(row["author_signature"]) == {pid}
            if not valid:
                stats["skipped_alias_ambiguity"] += 1
        if valid:
            accepted.append(row)

    accepted.sort(
        key=lambda row: (
            row["dblp_pid"],
            -int(row["year"]),
            0 if row["scope"] == "main" else 1,
            row["venue_label"].casefold(),
            row["title"].casefold(),
        )
    )
    stats["accepted"] = len(accepted)
    stats["main"] = sum(1 for row in accepted if row["scope"] == "main")
    stats["side"] = sum(1 for row in accepted if row["scope"] == "side")
    return accepted, dict(stats), dict(main_venue_year_totals)


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fields = [
        "dblp_pid",
        "canonical_name",
        "scope",
        "venue",
        "venue_label",
        "year",
        "title",
        "record_type",
        "publtype",
        "booktitle",
        "journal",
        "streams",
        "authors_json",
        "ees_json",
        "rels_json",
        "dblp_key",
        "url",
        "author_signature",
        "author_orcid",
        "author_position",
        "author_count",
        "match_basis",
        "snapshot_id",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def write_main_venue_year_totals(
    path: Path,
    totals: dict[tuple[str, int], int],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=["venue", "year", "paper_count", "snapshot_id"])
        writer.writeheader()
        for (venue, year), count in sorted(
            totals.items(), key=lambda item: (item[0][1], item[0][0])
        ):
            writer.writerow(
                {
                    "venue": venue,
                    "year": year,
                    "paper_count": count,
                    "snapshot_id": SNAPSHOT_ID,
                }
            )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    ap.add_argument("--entity-db", type=Path, default=None)
    ap.add_argument("--output", type=Path, default=None)
    ap.add_argument(
        "--no-download",
        action="store_true",
        help="Fail instead of downloading a missing pinned DBLP snapshot/DTD.",
    )
    ap.add_argument(
        "--force-scan",
        action="store_true",
        help="Force rescanning DBLP even when the publication CSV cache is current.",
    )
    ap.add_argument(
        "--verify-snapshot",
        action="store_true",
        help=(
            "Force a full MD5 reread of the local DBLP snapshot/DTD even when "
            "an unchanged file was verified successfully before."
        ),
    )
    args = ap.parse_args()

    data_dir = args.data_dir.resolve()
    entity_db = (args.entity_db or (data_dir / "entity_resolution.sqlite")).resolve()
    output = (args.output or (data_dir / OUTPUT_CSV)).resolve()
    main_totals_output = (data_dir / MAIN_TOTALS_CSV).resolve()
    snapshot_dir = data_dir / "dblp_snapshot"
    snapshot_gz = snapshot_dir / SNAPSHOT_FILENAME
    dtd_path = snapshot_dir / DTD_FILENAME

    if not entity_db.exists():
        print(f"ERROR: missing resolved reviewer DB: {entity_db}", file=sys.stderr)
        return 2

    if args.no_download:
        missing = [path for path in (snapshot_gz, dtd_path) if not path.exists()]
        if missing:
            print(
                "ERROR: missing local DBLP snapshot file(s): "
                + ", ".join(str(path) for path in missing),
                file=sys.stderr,
            )
            return 2
    else:
        download_file(
            SNAPSHOT_URL,
            snapshot_gz,
            SNAPSHOT_MD5,
            "DBLP monthly XML snapshot",
            force_verify=args.verify_snapshot,
        )
        download_file(
            DTD_URL,
            dtd_path,
            DTD_MD5,
            "DBLP DTD",
            force_verify=args.verify_snapshot,
        )

    snapshot_actual, snapshot_reused = verified_md5(
        snapshot_gz,
        SNAPSHOT_MD5,
        force=args.verify_snapshot and args.no_download,
    )
    if snapshot_actual.lower() != SNAPSHOT_MD5.lower():
        print("ERROR: DBLP snapshot MD5 does not match the pinned checksum.", file=sys.stderr)
        return 2
    dtd_actual, _dtd_reused = verified_md5(
        dtd_path,
        DTD_MD5,
        force=args.verify_snapshot and args.no_download,
    )
    if dtd_actual.lower() != DTD_MD5.lower():
        print("ERROR: DBLP DTD MD5 does not match the pinned checksum.", file=sys.stderr)
        return 2
    if snapshot_reused:
        print("DBLP snapshot is unchanged since the previous successful MD5 verification.")

    targets = load_targets(entity_db)
    if not targets:
        print("ERROR: no resolved DBLP people found in entity_resolution.sqlite", file=sys.stderr)
        return 2

    if not args.force_scan and scan_cache_is_current(
        output, main_totals_output, snapshot_gz, targets
    ):
        cache_path = scan_cache_path(output)
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
        print("\nDBLP publication scan")
        print("---------------------")
        print("Publication CSV is already current for this snapshot and target set.")
        print(f"Resolved DB people:                {len(targets)}")
        print(f"Cached person/paper links:         {cached.get('row_count', '?')}")
        print("Skipped full DBLP XML scan.")
        print(f"\nCurrent output: {output}")
        print(f"Conference-size totals: {main_totals_output}")
        return 0

    rows, stats, main_venue_year_totals = scan(snapshot_gz, dtd_path, targets)
    write_csv(output, rows)
    write_main_venue_year_totals(main_totals_output, main_venue_year_totals)
    write_scan_cache(
        output,
        main_totals_output,
        snapshot_gz,
        targets,
        row_count=len(rows),
        stats=stats,
    )

    main_people = {row["dblp_pid"] for row in rows if row["scope"] == "main"}
    any_people = {row["dblp_pid"] for row in rows}
    main_venues: dict[str, set[str]] = defaultdict(set)
    for row in rows:
        if row["scope"] == "main":
            main_venues[row["venue"]].add(row["dblp_pid"])

    print("\nDBLP publication scan")
    print("---------------------")
    print(f"Resolved DB people scanned:        {len(targets)}")
    print(f"People with any DBLP paper:        {len(any_people)}")
    print(f"People with main ML paper:         {len(main_people)}")
    print(f"Accepted person/paper links:       {stats.get('accepted', 0)}")
    print(f"  main NeurIPS/ICML/ICLR links:    {stats.get('main', 0)}")
    print(f"  side-paper links:                {stats.get('side', 0)}")
    print(f"Skipped ambiguous exact aliases:   {stats.get('skipped_alias_ambiguity', 0)}")
    print(f"Skipped ambiguous verified ORCIDs: {stats.get('skipped_orcid_ambiguity', 0)}")
    for venue in ("NeurIPS", "ICML", "ICLR"):
        count = sum(1 for row in rows if row["scope"] == "main" and row["venue"] == venue)
        conference_total = sum(
            papers
            for (candidate_venue, _year), papers in main_venue_year_totals.items()
            if candidate_venue == venue
        )
        print(
            f"  {venue}: {len(main_venues[venue])} people / {count} paper links / "
            f"{conference_total} total conference papers"
        )
    print(f"\nWrote: {output}")
    print(f"Wrote: {main_totals_output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
