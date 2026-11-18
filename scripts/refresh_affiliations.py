#!/usr/bin/env python3
"""Refresh automatic affiliations from all latest-year DOI publications per researcher."""
from __future__ import annotations

import argparse
from contextlib import closing
import csv
import hashlib
import json
import random
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from automatic_affiliations import MetadataCache, OUTPUT_FILENAME, POLICY_VERSION, collect_evidence, evidence_refreshed_at, publication_dois
from build_site_data import DB_ER, PUBLICATIONS_CSV, DEFAULT_DATA_DIR, artifact_fingerprint, load_db_people


def metadata_cache_fingerprint(path: Path) -> dict | None:
    """Hash logical committed responses, including rows in SQLite's WAL.

    File bytes change after harmless SQLite maintenance and can omit committed
    WAL pages. Reading ordered response rows captures the evidence actually
    available to an offline build without creating a missing supporting cache.
    """
    if not path.exists():
        return None
    digest = hashlib.sha256(b"affiliation-metadata-responses-v1\n")
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
        have = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='responses'").fetchone()
        if have:
            for row in conn.execute("SELECT url,fetched_at,status,payload FROM responses ORDER BY url"):
                digest.update(json.dumps(row, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
                digest.update(b"\n")
    return {"version": 1, "sha256": digest.hexdigest()}


def latest_papers(path: Path, pids: set[str]) -> dict[str, list[dict]]:
    result, years = {}, {}
    with path.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            pid = row.get("dblp_pid")
            if pid not in pids:
                continue
            try:
                year = int(row.get("year") or 0)
            except ValueError:
                continue
            if year < years.get(pid, 0):
                continue
            if year > years.get(pid, 0):
                years[pid], result[pid] = year, []
            result.setdefault(pid, []).append({"dblpKey": row["dblp_key"], "year": year, "title": row["title"],
                "url": row.get("url", ""), "localAuthorName": row.get("author_signature", ""),
                **({"personVersionDblpKeys": json.loads(row["person_version_dblp_keys"])}
                   if row.get("person_version_dblp_keys", "").strip() else {}),
                "resolvedVersions": json.loads(row.get("publication_versions_json") or "[]")})
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--refresh", action="store_true", help="Re-fetch publisher metadata even when cached")
    parser.add_argument("--sample", type=int, default=0, help="Diagnostic sample only; does not replace the full evidence file")
    parser.add_argument("--seed", type=int, default=20261018)
    args = parser.parse_args()
    if args.offline and args.refresh:
        parser.error("--refresh cannot be used with --offline")
    root = args.data_dir.resolve()
    inputs = {"entityResolution": artifact_fingerprint(root / DB_ER), "publications": artifact_fingerprint(root / PUBLICATIONS_CSV)}
    cache_path = root / "affiliation_metadata.sqlite"
    snapshot_path = root / OUTPUT_FILENAME
    if args.offline and not args.sample and snapshot_path.exists():
        previous = json.loads(snapshot_path.read_text(encoding="utf-8"))
        if previous.get("schemaVersion") == POLICY_VERSION and previous.get("inputs") == inputs:
            cache_fingerprint = metadata_cache_fingerprint(cache_path)
            if cache_fingerprint is None or previous.get("metadataCache") == cache_fingerprint:
                print("Offline affiliations: reusing the snapshot for unchanged inputs and available metadata.", flush=True)
                return 0
    conn = sqlite3.connect(root / DB_ER)
    try:
        db, _ = load_db_people(conn)
    finally:
        conn.close()
    pids = sorted(db)
    if args.sample:
        pids = random.Random(args.seed).sample(pids, min(args.sample, len(pids)))
    papers = latest_papers(root / PUBLICATIONS_CSV, set(pids))
    client = MetadataCache(cache_path, offline=args.offline, refresh=args.refresh)
    try:
        client.prefetch([doi for group in papers.values() for p in group for doi in publication_dois(p)])
        people = {}
        for index, pid in enumerate(pids):
            item = db[pid]
            person = {"name": sorted(item["names"])[0], "aliases": {"db": sorted(item["aliases"] | item["names"])}, "orcids": sorted(item["orcids"])}
            people[pid] = collect_evidence(person, papers.get(pid, []), client)
            if (index + 1) % 100 == 0:
                print(f"Affiliation authors checked: {index + 1}/{len(pids)}", flush=True)
        payload = {"schemaVersion": POLICY_VERSION, "generatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "inputs": inputs,
            "metadataCache": metadata_cache_fingerprint(cache_path),
            "people": people}
        payload["refreshedAt"] = evidence_refreshed_at(payload)
        target = root / OUTPUT_FILENAME
        if args.sample:
            target = root / "reports" / f"automatic_affiliation_sample_{args.seed}.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(target)
        print(f"Affiliations: {sum(bool(p['candidates']) for p in people.values())}/{len(pids)} researchers have publisher evidence; {client.requests} requests, {client.errors} failed requests. Wrote {target}", flush=True)
    finally:
        client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
