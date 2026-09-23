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
import sqlite3
import sys
import time
from collections import defaultdict
from dataclasses import dataclass
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
        clean_text,
        download_file,
        extract_orcid,
        person_record_from_www,
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
        clean_text,
        download_file,
        extract_orcid,
        person_record_from_www,
    )

OUTPUT_CSV = "dblp_publications_for_db_people.csv"
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
    "iclr": "ICLR",
}


@dataclass(frozen=True)
class TargetPerson:
    pid: str
    canonical_name: str
    aliases: tuple[str, ...]
    orcids: tuple[str, ...]


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
                    aliases=tuple(sorted(aliases, key=str.casefold)),
                    orcids=tuple(sorted(orcids)),
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


def publication_fields(elem: etree._Element) -> dict[str, Any] | None:
    key = clean_text(elem.attrib.get("key", ""))
    if not key:
        return None

    title = ""
    booktitle = ""
    journal = ""
    year: int | None = None
    ees: list[str] = []
    streams: list[str] = []
    authors: list[dict[str, Any]] = []

    for child in elem:
        tag = etree.QName(child).localname
        text = clean_text("".join(child.itertext()))
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
        elif tag == "author" and text:
            authors.append(
                {
                    "signature": text,
                    "orcid": extract_orcid(child.attrib.get("orcid", "")),
                    "pid": clean_text(child.attrib.get("pid", "")),
                }
            )

    if not title or year is None or not authors:
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
        "url": ees[0] if ees else f"https://dblp.org/rec/{key}",
        "authors": authors,
        "scope": scope,
        "venue": venue or "",
        "venue_label": venue_label,
        "record_type": etree.QName(elem).localname,
        "publtype": clean_text(elem.attrib.get("publtype", "")),
    }


def unique_pid(values: set[str] | None) -> str | None:
    if values and len(values) == 1:
        return next(iter(values))
    return None


def scan(snapshot_gz: Path, dtd_path: Path, targets: list[TargetPerson]) -> tuple[list[dict[str, Any]], dict[str, int]]:
    target_aliases, target_orcids, by_pid = build_target_maps(targets)
    target_pids = set(by_pid)
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

    parser = etree.XMLPullParser(
        events=("end",),
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
            chunk = fh.read(1 * 1024 * 1024)
            if not chunk:
                break
            parser.feed(chunk)
            for _event, elem in parser.read_events():
                parent = elem.getparent()
                if parent is None or parent.getparent() is not None:
                    continue

                stats["records"] += 1
                tag = etree.QName(elem).localname

                if tag in PAPER_RECORD_TYPES:
                    rec = publication_fields(elem)
                    if rec:
                        for position, author in enumerate(rec["authors"], start=1):
                            pid: str | None = None
                            basis = ""

                            author_pid = clean_text(author.get("pid"))
                            if author_pid and author_pid in target_pids:
                                pid = author_pid
                                basis = "dblp_author_pid"

                            author_orcid = clean_text(author.get("orcid")).upper()
                            if pid is None and author_orcid:
                                candidate = unique_pid(target_orcids.get(author_orcid))
                                if candidate:
                                    pid = candidate
                                    basis = "verified_orcid"

                            signature = clean_text(author.get("signature"))
                            if pid is None and signature:
                                candidate = unique_pid(target_aliases.get(signature))
                                if candidate:
                                    pid = candidate
                                    basis = "exact_dblp_author_signature"

                            if pid is None:
                                continue

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
                                "dblp_key": rec["dblp_key"],
                                "url": rec["url"],
                                "author_signature": signature,
                                "author_orcid": author_orcid,
                                "author_position": position,
                                "match_basis": basis,
                                "snapshot_id": SNAPSHOT_ID,
                            }
                            rank = {
                                "dblp_author_pid": 3,
                                "verified_orcid": 2,
                                "exact_dblp_author_signature": 1,
                            }
                            old = staged.get(identity)
                            if old is None or rank[basis] > rank.get(old["match_basis"], 0):
                                staged[identity] = row

                elif tag == "www":
                    person = person_record_from_www(elem)
                    if person:
                        pid = clean_text(person["pid"])
                        for alias in person["aliases"]:
                            if alias in alias_interest:
                                global_alias_owners[alias].add(pid)
                        for orcid in person["verified_orcids"]:
                            value = orcid.upper()
                            if value in orcid_interest:
                                global_orcid_owners[value].add(pid)

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
        if basis == "dblp_author_pid":
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
    return accepted, dict(stats)


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
        "dblp_key",
        "url",
        "author_signature",
        "author_orcid",
        "author_position",
        "match_basis",
        "snapshot_id",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


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
    args = ap.parse_args()

    data_dir = args.data_dir.resolve()
    entity_db = (args.entity_db or (data_dir / "entity_resolution.sqlite")).resolve()
    output = (args.output or (data_dir / OUTPUT_CSV)).resolve()
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
        download_file(SNAPSHOT_URL, snapshot_gz, SNAPSHOT_MD5, "DBLP monthly XML snapshot")
        download_file(DTD_URL, dtd_path, DTD_MD5, "DBLP DTD")

    targets = load_targets(entity_db)
    if not targets:
        print("ERROR: no resolved DBLP people found in entity_resolution.sqlite", file=sys.stderr)
        return 2

    rows, stats = scan(snapshot_gz, dtd_path, targets)
    write_csv(output, rows)

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
        print(f"  {venue}: {len(main_venues[venue])} people / {count} paper links")
    print(f"\nWrote: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
