#!/usr/bin/env python3
"""Refresh current/former DBLP person affiliations without changing identity resolution.

This reads the already-pinned local DBLP snapshot, finds only resolved DBLP PIDs,
and stores DBLP's current/former affiliation distinction in dblp_people.
"""

from __future__ import annotations

import argparse
import gzip
import json
import sqlite3
import time
from pathlib import Path

from lxml import etree

try:
    from .enrich_reviewers_with_dblp import (
        DEFAULT_DATA_DIR,
        DTD_FILENAME,
        LocalDTDResolver,
        SNAPSHOT_FILENAME,
        person_record_from_www,
    )
except ImportError:
    from enrich_reviewers_with_dblp import (
        DEFAULT_DATA_DIR,
        DTD_FILENAME,
        LocalDTDResolver,
        SNAPSHOT_FILENAME,
        person_record_from_www,
    )


def ensure_columns(conn: sqlite3.Connection) -> None:
    columns = {row[1] for row in conn.execute("PRAGMA table_info(dblp_people)")}
    if "current_affiliations_json" not in columns:
        conn.execute(
            "ALTER TABLE dblp_people ADD COLUMN current_affiliations_json TEXT NOT NULL DEFAULT '[]'"
        )
    if "former_affiliations_json" not in columns:
        conn.execute(
            "ALTER TABLE dblp_people ADD COLUMN former_affiliations_json TEXT NOT NULL DEFAULT '[]'"
        )
    conn.commit()


def refresh(entity_db: Path, snapshot_gz: Path, dtd_path: Path) -> tuple[int, int]:
    conn = sqlite3.connect(entity_db)
    try:
        ensure_columns(conn)
        targets = {
            str(row[0]).strip()
            for row in conn.execute(
                """
                SELECT DISTINCT resolved_dblp_pid
                FROM name_resolution
                WHERE resolved_dblp_pid IS NOT NULL
                  AND TRIM(resolved_dblp_pid) <> ''
                """
            )
            if row[0]
        }
        if not targets:
            raise RuntimeError("No resolved DBLP PIDs found in entity_resolution.sqlite")

        parser = etree.XMLPullParser(
            events=("end",),
            load_dtd=True,
            resolve_entities=True,
            no_network=True,
            huge_tree=True,
            recover=False,
        )
        parser.resolvers.add(LocalDTDResolver(dtd_path))

        found: set[str] = set()
        records = 0
        last_report = time.monotonic()
        done = False

        with gzip.open(snapshot_gz, "rb") as fh:
            while not done:
                chunk = fh.read(1024 * 1024)
                if not chunk:
                    break
                parser.feed(chunk)
                for _event, elem in parser.read_events():
                    parent = elem.getparent()
                    if parent is None or parent.getparent() is not None:
                        continue

                    records += 1
                    if etree.QName(elem).localname == "www":
                        person = person_record_from_www(elem)
                        if person and person["pid"] in targets:
                            pid = person["pid"]
                            conn.execute(
                                """
                                UPDATE dblp_people
                                SET current_affiliations_json=?, former_affiliations_json=?
                                WHERE dblp_pid=?
                                """,
                                (
                                    json.dumps(person["current_affiliations"], ensure_ascii=False),
                                    json.dumps(person["former_affiliations"], ensure_ascii=False),
                                    pid,
                                ),
                            )
                            found.add(pid)
                            if found == targets:
                                done = True

                    elem.clear()
                    while elem.getprevious() is not None:
                        del parent[0]

                    if done:
                        break

                if time.monotonic() - last_report >= 10:
                    print(
                        f"  scanned {records:,} DBLP records; "
                        f"refreshed {len(found):,}/{len(targets):,} resolved people"
                    )
                    conn.commit()
                    last_report = time.monotonic()

        if not done:
            parser.close()
        conn.commit()
        return len(found), len(targets)
    finally:
        conn.close()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    args = ap.parse_args()

    data_dir = args.data_dir.resolve()
    entity_db = data_dir / "entity_resolution.sqlite"
    snapshot_gz = data_dir / "dblp_snapshot" / SNAPSHOT_FILENAME
    dtd_path = data_dir / "dblp_snapshot" / DTD_FILENAME

    missing = [path for path in (entity_db, snapshot_gz, dtd_path) if not path.exists()]
    if missing:
        for path in missing:
            print(f"ERROR: missing {path}")
        return 2

    found, total = refresh(entity_db, snapshot_gz, dtd_path)
    print(f"Current/former DBLP affiliations refreshed: {found}/{total} resolved people")
    if found < total:
        print(f"WARNING: {total - found} resolved people had no matching person record in the snapshot")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
